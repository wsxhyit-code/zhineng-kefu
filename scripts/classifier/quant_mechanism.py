"""量化不一致的「机制」分类 + F1 差异的统计显著性。

前一步把不一致分成「边界抖动/自信翻转」用的是「离阈值多远」,但那个判据漏了一种机制:
apply_threshold 有「全不过线取最高分兜底」。当两侧都没有任何标签过阈值时,预测标签是
argmax 给的 —— 一点点概率扰动就能把 argmax 换掉,输出整个换一个类,而这跟「离阈值多远」
毫无关系(两侧都离阈值很远)。所以要把机制分对:

  阈值穿越  某个标签在两侧分别落在阈值两边(最常见、最无害)
  兜底切换  两侧都没有标签过线,是 argmax 换了类(隐蔽,且和阈值距离无关)
  其他      以上都不是(理论上不该出现,出现就是有 bug)

另外:F1 差值必须和噪声比。149 条测试集上 micro-F1 的标准误约 sqrt(p(1-p)/n),
不做这一步就会把「噪声」讲成「精度提升」。
"""
import json
import math
import pathlib

import numpy as np
import onnxruntime as ort
from sklearn.metrics import precision_recall_fscore_support
from tokenizers import Tokenizer

from app.core.taxonomy import LABEL2ID, NUM_CLASSES, SEVERITY, TOPIC_NAMES
from scripts.classifier.inference_lib import apply_threshold

DIR = pathlib.Path("data/classifier/onnx")
TEST = pathlib.Path("data/classifier/dataset/test.jsonl")
FP32 = DIR / "model.onnx"
VARIANTS = [("int8-matmul", DIR / "model.int8.matmul.onnx"),
            ("int8-matmul-gather", DIR / "model.int8.gather.onnx")]
BATCH = 32


def _sigmoid(x):
    return 1 / (1 + np.exp(-x))


def _probs(sess, tok, texts):
    out = []
    for i in range(0, len(texts), BATCH):
        encs = tok.encode_batch(texts[i:i + BATCH])
        feed = {"input_ids": np.array([e.ids for e in encs], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in encs], dtype=np.int64),
                "token_type_ids": np.array([e.type_ids for e in encs], dtype=np.int64)}
        (lg,) = sess.run(["logits"], feed)
        out.append(_sigmoid(lg))
    return np.concatenate(out)


def _classify(p32_row, p8_row, y32_row, y8_row, thr):
    """给一条不一致定机制。"""
    # 两侧是否触发了兜底(整行没有任何标签过线)
    fb32 = not (p32_row >= thr).any()
    fb8 = not (p8_row >= thr).any()
    if fb32 and fb8:
        return "兜底切换"
    kinds = set()
    for j in range(NUM_CLASSES):
        if y32_row[j] == y8_row[j]:
            continue
        a, b = p32_row[j] >= thr, p8_row[j] >= thr
        if a != b:
            kinds.add("阈值穿越")
        else:
            kinds.add("其他")
    return "+".join(sorted(kinds)) if kinds else "其他"


def main() -> int:
    tok = Tokenizer.from_file(str(DIR / "tokenizer.json"))
    tok.enable_truncation(max_length=128)
    tok.enable_padding(pad_id=0, pad_token="[PAD]")
    thr = json.loads((DIR / "threshold.json").read_text(encoding="utf-8"))["threshold"]

    rows = [json.loads(l) for l in TEST.read_text(encoding="utf-8").splitlines() if l.strip()]
    texts = [r["text"] for r in rows]
    gold = np.zeros((len(texts), NUM_CLASSES), dtype=int)
    for i, r in enumerate(rows):
        for n in r["labels"]:
            gold[i][LABEL2ID[n]] = 1
    n = len(texts)

    p32 = _probs(ort.InferenceSession(str(FP32), providers=["CPUExecutionProvider"]), tok, texts)
    y32 = apply_threshold(p32, thr)
    mi32 = precision_recall_fscore_support(gold, y32, average="micro", zero_division=0)[2]
    # 标准误:多标签 micro-F1 近似按单标签的 p(1-p)/n 估,量级够用
    se = math.sqrt(mi32 * (1 - mi32) / n)
    print(f"测试集 n={n}  阈值 {thr}")
    print(f"fp32 micro-F1 = {mi32:.4f}   (噪声量级 ±1SE ≈ ±{se:.4f}，"
          f"2SE ≈ ±{2 * se:.4f})\n")

    for name, path in VARIANTS:
        p8 = _probs(ort.InferenceSession(str(path), providers=["CPUExecutionProvider"]), tok, texts)
        y8 = apply_threshold(p8, thr)
        mi = precision_recall_fscore_support(gold, y8, average="micro", zero_division=0)[2]
        ma = precision_recall_fscore_support(gold, y8, average="macro", zero_division=0)[2]
        d = mi - mi32
        verdict = "噪声内" if abs(d) <= 2 * se else "超出噪声"
        print(f"=== {name} ===")
        print(f"  micro-F1 {mi:.4f} (Δ{d:+.4f})  macro-F1 {ma:.4f}   → Δ 相对噪声:{verdict}")

        bad = [i for i in range(n) if (y8[i] != y32[i]).any()]
        tally, strict = {}, 0
        print(f"  {len(bad)} 条不一致,机制分类:")
        for i in bad:
            kind = _classify(p32[i], p8[i], y32[i], y8[i], thr)
            tally[kind] = tally.get(kind, 0) + 1
            diffs = {TOPIC_NAMES[j] for j in range(NUM_CLASSES) if y8[i][j] != y32[i][j]}
            strict += sum(1 for j in range(NUM_CLASSES)
                          if y8[i][j] != y32[i][j] and SEVERITY[TOPIC_NAMES[j]] == "严")
            top32, top8 = p32[i].max(), p8[i].max()
            print(f"   · [{kind}] {texts[i][:30]}…")
            print(f"       差异={sorted(diffs)}  fp32最高分={top32:.3f} int8最高分={top8:.3f}"
                  f"  过线数 fp32={int((p32[i] >= thr).sum())} int8={int((p8[i] >= thr).sum())}")
        print(f"  机制汇总:{tally}")
        print(f"  牵涉「严」档类目 {strict} 次\n")

    print("判读:")
    print("  · Δmicro 落在 ±2SE 内 → 只能说「无可察觉的差异」,不能说「提升」也不能说「下降」")
    print("  · 机制以「阈值穿越」为主 → 常规量化副作用,可接受")
    print("  · 出现「兜底切换」→ 是 apply_threshold 的空标签兜底把微小扰动放大成整类替换")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
