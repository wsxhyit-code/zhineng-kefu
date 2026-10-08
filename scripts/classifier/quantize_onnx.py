"""分类器 ONNX 动态量化实验:fp32 → int8,量出「大小 / 行为一致 / 延迟」三个数。

为什么要做:390MB 的 fp32 模型无论走哪条部署路(PyInstaller 打包 / 浏览器 WASM /
原生二进制)都太笨重。量化是这些路共同的前置条件,而且「精度掉几个点」必须实测,
不能拍脑袋说「无损」。

对照三个变体:
  fp32              基线(现有 model.onnx)
  int8-matmul       只量化 MatMul —— 保守,embedding 保持 fp32
  int8-matmul-gather 连 Gather(词嵌入)也量化 —— 更小,但精度风险更高

验收纪律照 export_onnx.py:测试集全量逐条比对「过线标签」,不一致条数必须是 0。
后处理与线上完全共用(serve.py 那套:tokenizers + sigmoid + apply_threshold),
不然量出来的差异分不清是量化引起的还是预处理不一致引起的。

跑法:PYTHONPATH=. .venv\\Scripts\\python.exe scripts/classifier/quantize_onnx.py
落 reports/quant_report.json 给验收页读。
"""
import argparse
import datetime as dt
import json
import pathlib
import time

import numpy as np
import onnxruntime as ort
from onnxruntime.quantization import QuantType, quantize_dynamic
from tokenizers import Tokenizer

from scripts.classifier.inference_lib import apply_threshold

DIR = pathlib.Path("data/classifier/onnx")
TEST = pathlib.Path("data/classifier/dataset/test.jsonl")
REPORTS = pathlib.Path("data/classifier/reports")
REPORT_NAME = "quant_report.json"
FP32 = DIR / "model.onnx"

BATCH = 32
LATENCY_RUNS = 60
LATENCY_WARMUP = 5


def _load_texts() -> list[str]:
    return [json.loads(line)["text"]
            for line in TEST.read_text(encoding="utf-8").splitlines() if line.strip()]


def _make_tokenizer() -> Tokenizer:
    """与 serve.py 完全同一套预处理,否则口径就分叉了。"""
    tok = Tokenizer.from_file(str(DIR / "tokenizer.json"))
    tok.enable_truncation(max_length=128)
    tok.enable_padding(pad_id=0, pad_token="[PAD]")
    return tok


def _feed(encs) -> dict:
    return {
        "input_ids": np.array([e.ids for e in encs], dtype=np.int64),
        "attention_mask": np.array([e.attention_mask for e in encs], dtype=np.int64),
        "token_type_ids": np.array([e.type_ids for e in encs], dtype=np.int64),
    }


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-x))


def _logits_of(sess, feed: dict) -> np.ndarray:
    (logits,) = sess.run(["logits"], feed)
    return logits


def _latency_ms(sess, feed: dict) -> tuple[float, float]:
    for _ in range(LATENCY_WARMUP):
        sess.run(["logits"], feed)
    samples = []
    for _ in range(LATENCY_RUNS):
        t0 = time.perf_counter()
        sess.run(["logits"], feed)
        samples.append((time.perf_counter() - t0) * 1000)
    arr = np.array(samples)
    return float(np.percentile(arr, 50)), float(np.percentile(arr, 95))


def _quantize(op_types: list[str], out: pathlib.Path) -> None:
    quantize_dynamic(str(FP32), str(out), op_types_to_quantize=op_types,
                     per_channel=True, weight_type=QuantType.QInt8)


def main() -> int:
    global DIR, REPORTS, REPORT_NAME, FP32
    ap = argparse.ArgumentParser(description="ONNX 动态量化:大小/行为一致/延迟三个数")
    ap.add_argument("--onnx-dir", default=str(DIR), help="含 model.onnx 的目录")
    ap.add_argument("--reports-dir", default=str(REPORTS))
    ap.add_argument("--report-name", default=REPORT_NAME)
    a = ap.parse_args()
    DIR, REPORTS, REPORT_NAME = pathlib.Path(a.onnx_dir), pathlib.Path(a.reports_dir), a.report_name
    FP32 = DIR / "model.onnx"
    if not FP32.exists():
        raise SystemExit(f"缺 {FP32},先跑 scripts/classifier/export_onnx.py")
    REPORTS.mkdir(parents=True, exist_ok=True)
    tok = _make_tokenizer()
    texts = _load_texts()
    threshold = json.loads((DIR / "threshold.json").read_text(encoding="utf-8"))["threshold"]
    print(f"测试集 {len(texts)} 条,阈值 {threshold}\n")

    # ---- 基线:fp32 ----
    base = ort.InferenceSession(str(FP32), providers=["CPUExecutionProvider"])
    ref_logits, ref_preds = [], []
    for i in range(0, len(texts), BATCH):
        lg = _logits_of(base, _feed(tok.encode_batch(texts[i:i + BATCH])))
        ref_logits.append(lg)
        ref_preds.append(apply_threshold(_sigmoid(lg), threshold))
    ref_logits = np.concatenate(ref_logits)
    ref_preds = np.concatenate(ref_preds)

    variants = [("fp32", None, FP32),
                ("int8-matmul", ["MatMul"], DIR / "model.int8.matmul.onnx"),
                ("int8-matmul-gather", ["MatMul", "Gather"], DIR / "model.int8.gather.onnx")]

    rows = []
    for name, ops, path in variants:
        if ops is not None:
            print(f"量化中 {name} …")
            _quantize(ops, path)

        sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        logits, preds = [], []
        for i in range(0, len(texts), BATCH):
            lg = _logits_of(sess, _feed(tok.encode_batch(texts[i:i + BATCH])))
            logits.append(lg)
            preds.append(apply_threshold(_sigmoid(lg), threshold))
        logits = np.concatenate(logits)
        preds = np.concatenate(preds)

        # 逐条比对「过线标签」——和 export_onnx.py 同一把尺子:任一标签不同就算不一致
        mismatch = int((preds != ref_preds).any(axis=1).sum())
        row = {
            "variant": name,
            "op_types_to_quantize": ops or "—",
            "bytes": path.stat().st_size,
            "mb": round(path.stat().st_size / 1024 / 1024, 1),
            "logits_max_abs_diff": round(float(np.abs(logits - ref_logits).max()), 5),
            "probs_mean_abs_diff": round(float(np.abs(_sigmoid(logits) - _sigmoid(ref_logits)).mean()), 6),
            "label_mismatch": mismatch,
            "identical": mismatch == 0,
        }
        p50, p95 = _latency_ms(sess, _feed(tok.encode_batch([texts[0]])))
        row["latency_p50_ms"], row["latency_p95_ms"] = round(p50, 2), round(p95, 2)
        rows.append(row)
        print(f"  {name:20s} {row['mb']:7.1f} MB  lab_mismatch={mismatch:3d}  "
              f"p50={row['latency_p50_ms']:6.2f}ms  p95={row['latency_p95_ms']:6.2f}ms")

    (REPORTS / REPORT_NAME).write_text(json.dumps({
        "ran_at": dt.datetime.now().isoformat(timespec="seconds"),
        "test_size": len(texts), "threshold": threshold,
        "batch_for_consistency": BATCH, "latency_runs": LATENCY_RUNS,
        "latency_batch": 1,
        "variants": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 汇总 ===")
    print(f"{'变体':22s}{'大小(MB)':>10s}{'压缩比':>9s}{'标签不一致':>12s}{'p50(ms)':>10s}{'p95(ms)':>10s}")
    b0 = rows[0]["bytes"]
    for r in rows:
        print(f"{r['variant']:22s}{r['mb']:10.1f}{b0 / r['bytes']:8.2f}x"
              f"{r['label_mismatch']:12d}{r['latency_p50_ms']:10.2f}{r['latency_p95_ms']:10.2f}")
    print(f"\n落盘:{REPORTS / REPORT_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
