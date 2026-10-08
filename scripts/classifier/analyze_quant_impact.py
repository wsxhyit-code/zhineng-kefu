"""量化影响分析:那几条标签不一致,到底是「无害的边界抖动」还是「自信误判」?

previous 一步(quantize_onnx.py)只给出「不一致 N 条」这个数,但那个数本身不能下结论:
多标签分类里,把 0.38 抖成 0.42 和把 0.95 抖成 0.05,是完全不同性质的事。要分开看:

  1. F1 掉了多少(与 evaluate.py 同一口径的 micro/macro)
  2. 每条不一致「差在哪个类目」——类目的 severity(严/中/宽)决定影响大小
  3. 差异标签的概率离阈值有多远:<=0.05 记「边界抖动」,否则记「自信翻转」

前一步的产物直接复用,不重新量化。
跑法:PYTHONPATH=. .venv\\Scripts\\python.exe scripts/classifier/analyze_quant_impact.py
"""
import argparse
import json
import pathlib

import numpy as np
import onnxruntime as ort
from sklearn.metrics import precision_recall_fscore_support
from tokenizers import Tokenizer

from app.core.taxonomy import LABEL2ID, NUM_CLASSES, SEVERITY, TOPIC_NAMES
from scripts.classifier.inference_lib import apply_threshold

DIR = pathlib.Path("data/classifier/onnx")
TEST = pathlib.Path("data/classifier/dataset/test.jsonl")
BATCH = 32
BORDERLINE = 0.05      # 差异标签两侧概率都在阈值 ±0.05 内 → 算边界抖动


def _variants(dir_: pathlib.Path):
    """产物名由 quantize_onnx.py 固定写死,这里跟着它走。"""
    return [("int8-matmul", dir_ / "model.int8.matmul.onnx"),
            ("int8-matmul-gather", dir_ / "model.int8.gather.onnx")]


def _sigmoid(x):
    return 1 / (1 + np.exp(-x))


def _predict(sess, tok, texts, threshold):
    probs = []
    for i in range(0, len(texts), BATCH):
        encs = tok.encode_batch(texts[i:i + BATCH])
        feed = {"input_ids": np.array([e.ids for e in encs], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in encs], dtype=np.int64),
                "token_type_ids": np.array([e.type_ids for e in encs], dtype=np.int64)}
        (lg,) = sess.run(["logits"], feed)
        probs.append(_sigmoid(lg))
    probs = np.concatenate(probs)
    return probs, apply_threshold(probs, threshold)


def _gold_matrix(labels: list[list[str]]) -> np.ndarray:
    m = np.zeros((len(labels), NUM_CLASSES), dtype=int)
    for i, names in enumerate(labels):
        for n in names:
            m[i][LABEL2ID[n]] = 1
    return m


def main() -> int:
    global DIR
    ap = argparse.ArgumentParser(description="量化影响分析:不一致条数的性质与严重度")
    ap.add_argument("--onnx-dir", default=str(DIR), help="含 model.onnx 与 int8 产物的目录")
    DIR = pathlib.Path(ap.parse_args().onnx_dir)
    fp32, variants = DIR / "model.onnx", _variants(DIR)
    if not fp32.exists():
        raise SystemExit(f"缺 {fp32}")
    tok = Tokenizer.from_file(str(DIR / "tokenizer.json"))
    tok.enable_truncation(max_length=128)
    tok.enable_padding(pad_id=0, pad_token="[PAD]")
    threshold = json.loads((DIR / "threshold.json").read_text(encoding="utf-8"))["threshold"]

    rows = [json.loads(l) for l in TEST.read_text(encoding="utf-8").splitlines() if l.strip()]
    texts = [r["text"] for r in rows]
    gold = _gold_matrix([r["labels"] for r in rows])
    print(f"测试集 {len(texts)} 条,{NUM_CLASSES} 类,阈值 {threshold}\n")

    base = ort.InferenceSession(str(fp32), providers=["CPUExecutionProvider"])
    p32, y32 = _predict(base, tok, texts, threshold)

    def f1s(y):
        mi = precision_recall_fscore_support(gold, y, average="micro", zero_division=0)
        ma = precision_recall_fscore_support(gold, y, average="macro", zero_division=0)
        return mi[2], ma[2]

    mi32, ma32 = f1s(y32)
    print(f"{'变体':22s}{'micro-F1':>10s}{'Δmicro':>9s}{'macro-F1':>10s}{'Δmacro':>9s}")
    print(f"{'fp32':22s}{mi32:10.4f}{'—':>9s}{ma32:10.4f}{'—':>9s}")

    for name, path in variants:
        if not path.exists():
            print(f"{name:22s} 缺文件,先跑 quantize_onnx.py")
            continue
        sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        probs, preds = _predict(sess, tok, texts, threshold)
        mi, ma = f1s(preds)
        print(f"{name:22s}{mi:10.4f}{mi - mi32:+9.4f}{ma:10.4f}{ma - ma32:+9.4f}")

        bad = [i for i in range(len(texts)) if (preds[i] != y32[i]).any()]
        borderline = confident = 0
        strict_hits = 0
        print(f"\n  {len(bad)} 条不一致,逐条看差异标签:")
        for i in bad:
            diffs = [j for j in range(NUM_CLASSES) if preds[i][j] != y32[i][j]]
            print(f"   · {texts[i][:38]}…")
            print(f"     真实={[TOPIC_NAMES[j] for j in range(NUM_CLASSES) if gold[i][j]]}"
                  f"  fp32={[TOPIC_NAMES[j] for j in range(NUM_CLASSES) if y32[i][j]]}"
                  f"  {name}={[TOPIC_NAMES[j] for j in range(NUM_CLASSES) if preds[i][j]]}")
            for j in diffs:
                near = min(abs(p32[i][j] - threshold), abs(probs[i][j] - threshold)) <= BORDERLINE
                borderline += int(near)
                confident += int(not near)
                sev = SEVERITY[TOPIC_NAMES[j]]
                strict_hits += int(sev == "严")
                print(f"     {TOPIC_NAMES[j]:8s} 严重度={sev}  "
                      f"fp32={p32[i][j]:.4f} → int8={probs[i][j]:.4f}  "
                      f"{'边界抖动' if near else '★自信翻转★'}")
        total = borderline + confident
        print(f"\n  差异标签 {total} 个:边界抖动 {borderline} / 自信翻转 {confident};"
              f"牵涉「严」档类目 {strict_hits} 次")

    print("\n判读:边界抖动(两侧都在阈值 ±0.05 内)属可接受;自信翻转才是真问题。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
