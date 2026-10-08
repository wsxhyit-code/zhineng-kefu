# 来源:自主实验(分类器蒸馏扩展)
"""蒸馏结果重评测:把口径修对,再下结论。

为什么要单独重评(distill.py 里的评测有三个问题):
  1. 后处理用了裸 (probs >= t),而项目生产/评测一律走 inference_lib.apply_threshold,
     那个函数有「全不过线取最高分兜底」。口径不同 → 数字不可比。
     证据:同一个教师在测试集上,裸阈值算 0.8895,项目 evaluate.py 报 0.8924。
  2. 阈值网格 0.30~0.70,学生最优落在网格边界 0.30 —— 真最优可能在下界之外,
     拿边界值去测测试集属于阈值过拟合。
  3. 只报「各自最优阈值」下的数字会同时抬高两边。要另报同一阈值下的对照。

本脚本:教师与学生各跑一遍,统一用 apply_threshold,阈值网格扩到 0.05~0.80,
同时给出「各自最优」与「同一阈值」两组数字,并统计兜底触发率。
"""
import json
import pathlib

import numpy as np
import torch
from sklearn.metrics import precision_recall_fscore_support
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from app.core.taxonomy import LABEL2ID, NUM_CLASSES
from scripts.classifier.inference_lib import apply_threshold

TEACHER_DIR = pathlib.Path("data/classifier/model")
STUDENT_DIR = pathlib.Path("data/classifier/distill")
DATA = pathlib.Path("data/classifier/dataset")
REPORTS = pathlib.Path("data/classifier/reports")
GRID = np.round(np.arange(0.05, 0.81, 0.05), 2)


def load(path):
    return (AutoTokenizer.from_pretrained(path),
            AutoModelForSequenceClassification.from_pretrained(path).eval())


def encode(samples, tok):
    enc = tok([s["text"] for s in samples], truncation=True, padding="max_length", max_length=128)
    return [{"input_ids": enc["input_ids"][i], "attention_mask": enc["attention_mask"][i],
             "token_type_ids": enc["token_type_ids"][i]} for i in range(len(samples))]


def gold_of(samples):
    g = np.zeros((len(samples), NUM_CLASSES), dtype=int)
    for i, s in enumerate(samples):
        for lb in s["labels"]:
            g[i][LABEL2ID[lb]] = 1
    return g


@torch.no_grad()
def probs_of(model, ds):
    out = []
    for i in range(0, len(ds), 64):
        b = ds[i:i + 64]
        lg = model(input_ids=torch.tensor([x["input_ids"] for x in b]),
                   attention_mask=torch.tensor([x["attention_mask"] for x in b]),
                   token_type_ids=torch.tensor([x["token_type_ids"] for x in b])).logits
        out.append(torch.sigmoid(lg).numpy())
    return np.concatenate(out)


def f1(preds, gold):
    mi = precision_recall_fscore_support(gold, preds, average="micro", zero_division=0)
    ma = precision_recall_fscore_support(gold, preds, average="macro", zero_division=0)
    return float(mi[2]), float(ma[2])


def scan(probs, gold):
    """返回(最优阈值, 该阈值 micro-F1)。用生产同一套 apply_threshold。"""
    best_t, best_f1 = 0.5, -1.0
    for t in GRID:
        v, _ = f1(apply_threshold(probs, float(t)), gold)
        if v > best_f1:
            best_t, best_f1 = float(t), v
    return best_t, best_f1


def read_jsonl(p):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def main() -> int:
    val_rows, test_rows = read_jsonl(DATA / "val.jsonl"), read_jsonl(DATA / "test.jsonl")
    tok_t, teacher = load(TEACHER_DIR)
    tok_s, student = load(STUDENT_DIR)
    val_t, val_s = encode(val_rows, tok_t), encode(val_rows, tok_s)
    test_t, test_s = encode(test_rows, tok_t), encode(test_rows, tok_s)
    gv, gt = gold_of(val_rows), gold_of(test_rows)

    res = {}
    for name, model, vd, td in [("teacher", teacher, val_t, test_t),
                                ("student", student, val_s, test_s)]:
        pv, pt = probs_of(model, vd), probs_of(model, td)
        t_best, v_best = scan(pv, gv)
        res[name] = {"probs_val": pv, "probs_test": pt, "t_best": t_best, "val_best": v_best}
        print(f"[{name}] 验证集最优阈值 {t_best}(micro-F1 {v_best:.4f})")

    print("\n=== 测试集:各自在验证集选出的最优阈值(项目现行做法) ===")
    print(f"{'模型':10s}{'阈值':>7s}{'micro-F1':>11s}{'macro-F1':>11s}{'兜底触发':>10s}")
    test_mi = {}
    for name in ("teacher", "student"):
        r = res[name]
        preds = apply_threshold(r["probs_test"], r["t_best"])
        mi, ma = f1(preds, gt)
        test_mi[name] = mi
        no_pass = int((r["probs_test"] < r["t_best"]).all(axis=1).sum())
        print(f"{name:10s}{r['t_best']:7.2f}{mi:11.4f}{ma:11.4f}"
              f"{no_pass:7d}/{len(gt)}")
        r["test_mi"], r["test_ma"] = mi, ma

    print("\n=== 测试集:同一阈值下的对照(排除「各自挑最优」的抬高) ===")
    print(f"{'阈值':>7s}{'教师 micro':>13s}{'学生 micro':>13s}{'Δ':>9s}")
    for t in (0.3, 0.4, 0.5, 0.6):
        a, _ = f1(apply_threshold(res["teacher"]["probs_test"], t), gt)
        b, _ = f1(apply_threshold(res["student"]["probs_test"], t), gt)
        print(f"{t:7.2f}{a:13.4f}{b:13.4f}{b - a:+9.4f}")

    n = len(gt)
    mi_t, mi_s = test_mi["teacher"], test_mi["student"]
    se = (mi_t * (1 - mi_t) / n) ** 0.5
    print(f"\nn={n}  教师 {mi_t:.4f} vs 学生 {mi_s:.4f}  Δ={mi_s - mi_t:+.4f}")
    print(f"噪声 ±1SE={se:.4f}  ±2SE={2 * se:.4f}  → "
          f"{'噪声内' if abs(mi_s - mi_t) <= 2 * se else '超出噪声'}")

    # 兜底到底影响多大:同一阈值下带/不带兜底各算一遍
    print("\n=== 后处理兜底的影响(同一组预测,带/不带 apply_threshold) ===")
    for name in ("teacher", "student"):
        p = res[name]["probs_test"]
        t = res[name]["t_best"]
        with_fb, _ = f1(apply_threshold(p, t), gt)
        bare, _ = f1((p >= t).astype(int), gt)
        print(f"  {name:10s} 带兜底 {with_fb:.4f}  裸阈值 {bare:.4f}  Δ{with_fb - bare:+.4f}")

    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "distill_eval.json").write_text(json.dumps({
        "grid": [float(x) for x in GRID],
        "postprocess": "scripts.classifier.inference_lib.apply_threshold(生产同口径)",
        "teacher": {"threshold": res["teacher"]["t_best"],
                    "val_micro_f1": res["teacher"]["val_best"],
                    "test_micro_f1": res["teacher"]["test_mi"],
                    "test_macro_f1": res["teacher"]["test_ma"]},
        "student": {"threshold": res["student"]["t_best"],
                    "val_micro_f1": res["student"]["val_best"],
                    "test_micro_f1": res["student"]["test_mi"],
                    "test_macro_f1": res["student"]["test_ma"]},
        "delta_test_micro": round(mi_s - mi_t, 4),
        "noise_2se": round(2 * se, 4),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n落盘 {REPORTS / 'distill_eval.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
