"""分类器 ONNX 导出:torch 模型 → onnx(变长 batch/seq),导出后跑测试集校验与 torch 预测完全一致。
注意:torch 2.9+ 的 torch.onnx.export 默认 dynamo=True,导 HF 模型此处显式 dynamo=False
走 TorchScript 导出器 + dynamic_axes 稳定路线(Context7 查证)。运行:make classifier-export。
落 reports/export_report.json 给验收页读(/acceptance):校验条数、不一致条数、产物大小。"""
import argparse
import datetime as dt
import json
import pathlib
import shutil

import numpy as np
import onnxruntime as ort
import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

# 默认值 = 分类器原流程;蒸馏学生走 --model-dir/--out-dir 复用同一套导出+校验逻辑,
# 免得两处导出实现分叉
MODEL_DIR = pathlib.Path("data/classifier/model")
OUT = pathlib.Path("data/classifier/onnx")
TEST = pathlib.Path("data/classifier/dataset/test.jsonl")
REPORTS = pathlib.Path("data/classifier/reports")
REPORT_NAME = "export_report.json"


def _write_report(checked: int, mismatch: int) -> None:
    """一致性校验的结论落盘;失败也写,验收页要能看见「上次导出没过」而不是一片空白。"""
    onnx_path = OUT / "model.onnx"
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / REPORT_NAME).write_text(json.dumps({
        "ran_at": dt.datetime.now().isoformat(timespec="seconds"),
        "checked": checked, "mismatch": mismatch, "passed": mismatch == 0,
        "onnx_path": str(onnx_path),
        "onnx_bytes": onnx_path.stat().st_size if onnx_path.exists() else 0,
        "opset": 17,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


class LogitsWrapper(torch.nn.Module):
    """HF 模型输出是 ModelOutput dict,导出前包一层只回 logits 张量。"""

    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, input_ids, attention_mask, token_type_ids):
        return self.model(input_ids=input_ids, attention_mask=attention_mask,
                          token_type_ids=token_type_ids).logits


def main() -> None:
    global MODEL_DIR, OUT, REPORTS, REPORT_NAME
    ap = argparse.ArgumentParser(description="导出 ONNX 并与 torch 预测逐条校验")
    ap.add_argument("--model-dir", default=str(MODEL_DIR), help="要导出的 HF 模型目录")
    ap.add_argument("--out-dir", default=str(OUT), help="ONNX 产物目录")
    ap.add_argument("--reports-dir", default=str(REPORTS), help="报告目录")
    ap.add_argument("--report-name", default=REPORT_NAME)
    a = ap.parse_args()
    MODEL_DIR, OUT, REPORTS = pathlib.Path(a.model_dir), pathlib.Path(a.out_dir), pathlib.Path(a.reports_dir)
    REPORT_NAME = a.report_name
    OUT.mkdir(parents=True, exist_ok=True)
    tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)
    model = AutoModelForSequenceClassification.from_pretrained(MODEL_DIR)
    model.eval()
    wrapper = LogitsWrapper(model)
    sample = tokenizer(["买大了想退", "快递到哪了"], padding=True, return_tensors="pt")
    dyn = {0: "batch", 1: "seq"}
    torch.onnx.export(
        wrapper,
        (sample["input_ids"], sample["attention_mask"], sample["token_type_ids"]),
        str(OUT / "model.onnx"),
        input_names=["input_ids", "attention_mask", "token_type_ids"],
        output_names=["logits"],
        dynamic_axes={"input_ids": dyn, "attention_mask": dyn,
                      "token_type_ids": dyn, "logits": {0: "batch"}},
        dynamo=False,
        opset_version=17,
    )
    tokenizer.save_pretrained(OUT)          # 带出 tokenizer.json 给轻运行时用
    shutil.copy(MODEL_DIR / "threshold.json", OUT / "threshold.json")

    # 一致性校验:测试集全量,ONNX 与 torch 的过线标签必须完全一致。
    # 参照模型必须重新加载:tracing 会把 transformers v5 masking_utils 的分支
    # 打成常量、污染进程内已有模型(实测被污染的参照 logits 差到 1.88,而文件本身没问题)
    ref = AutoModelForSequenceClassification.from_pretrained(MODEL_DIR)
    ref.eval()
    threshold = json.loads((OUT / "threshold.json").read_text())["threshold"]
    texts = [json.loads(l)["text"]
             for l in TEST.read_text(encoding="utf-8").splitlines() if l.strip()]
    sess = ort.InferenceSession(str(OUT / "model.onnx"),
                                providers=["CPUExecutionProvider"])
    mismatch = 0
    with torch.no_grad():
        for i in range(0, len(texts), 32):
            enc = tokenizer(texts[i:i + 32], truncation=True, padding=True,
                            max_length=128, return_tensors="pt")
            t_logits = ref(**enc).logits.numpy()
            (o_logits,) = sess.run(["logits"], {k: v.numpy() for k, v in enc.items()})
            assert np.allclose(t_logits, o_logits, atol=1e-3), "logits 超容差"
            t_pred = (1 / (1 + np.exp(-t_logits)) >= threshold)
            o_pred = (1 / (1 + np.exp(-o_logits)) >= threshold)
            mismatch += int((t_pred != o_pred).any(axis=1).sum())
    _write_report(len(texts), mismatch)
    if mismatch:
        raise SystemExit(f"ONNX 与 torch 预测不一致 {mismatch} 条,导出失败")
    print(f"ONNX 导出并校验通过({len(texts)} 条预测完全一致):{OUT}/model.onnx")


if __name__ == "__main__":
    main()
