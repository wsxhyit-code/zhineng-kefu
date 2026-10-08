# 来源:自主实验(分类器蒸馏扩展)
"""分类器知识蒸馏:把 12 层教师压成 6 层学生,多标签软标签蒸馏。

为什么做蒸馏而不是只换导出格式:教师全参微调已是既有内容,蒸馏是另一件事 ——
「训一个小模型去模仿大模型」,产物是真正更小的模型,而不是同一个模型的另一种序列化。

关键设计(都针对「多标签」这一前提):
  1. 学生初始化:取教师「隔层」[0,2,4,6,8,10] 而非前 6 层。隔层采样保留整条深度上的
     表征,业界(BERT→DistilBERT)用的就是这条;比只取底部 6 层收敛更快。
  2. 损失:多标签每类各自 sigmoid,所以【不能用】单标签的 softmax-KL 蒸馏。
     改为对教师 sigmoid 概率做 BCE 软标签,与硬标签 BCE 加权:
        L = α·BCE(student, hard) + (1-α)·BCE(student, teacher_prob)
  3. 教师软标签【预计算】一次缓存:教师冻结、数据固定,训练中每步重跑教师是纯浪费。
     缓存后每步只需学生前向+反向,约省 3 倍时间。

体积预期要诚实:词嵌入矩阵(21128×768=16.6M)不随层数变,它是地板。
  12 层 102.1M → 6 层 59.7M,只能到 1.7x,不是数量级。

跑法:PYTHONPATH=. .venv\\Scripts\\python.exe scripts/classifier/distill.py --epochs 1
"""
import argparse
import json
import pathlib
import time

import numpy as np
import torch
import torch.nn.functional as F
from sklearn.metrics import f1_score, precision_recall_fscore_support
from transformers import (AutoConfig, AutoModelForSequenceClassification,
                          AutoTokenizer, EarlyStoppingCallback, Trainer,
                          TrainerCallback, TrainingArguments, default_data_collator)

from app.core.taxonomy import ID2LABEL, LABEL2ID, NUM_CLASSES

TEACHER_DIR = pathlib.Path("data/classifier/model")
DATA = pathlib.Path("data/classifier/dataset")
OUT = pathlib.Path("data/classifier/distill")
REPORTS = pathlib.Path("data/classifier/reports")
CACHE = pathlib.Path("data/classifier/distill/_teacher_cache")


def load_jsonl(p: pathlib.Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def encode(samples: list[dict], tokenizer) -> list[dict]:
    enc = tokenizer([s["text"] for s in samples], truncation=True,
                    padding="max_length", max_length=128)
    items = []
    for i, s in enumerate(samples):
        vec = [0.0] * NUM_CLASSES
        for lb in s["labels"]:
            vec[LABEL2ID[lb]] = 1.0
        items.append({"input_ids": enc["input_ids"][i],
                      "attention_mask": enc["attention_mask"][i],
                      "token_type_ids": enc["token_type_ids"][i],
                      "labels": vec})
    return items


def build_student(teacher, src_layers: list[int]):
    """按 src_layers 从教师抽层,构造 6 层学生。embeddings/pooler/分类头同名直取。"""
    cfg = AutoConfig.from_pretrained(TEACHER_DIR)
    cfg.num_hidden_layers = len(src_layers)
    student = AutoModelForSequenceClassification.from_config(cfg)
    t_sd, s_sd = teacher.state_dict(), student.state_dict()
    new_sd = {}
    for k in s_sd:
        if k.startswith("roberta.encoder.layer."):
            idx = int(k.split(".")[3])
            src = k.replace(f"layer.{idx}.", f"layer.{src_layers[idx]}.")
        else:
            src = k
        new_sd[k] = t_sd[src].clone()
    student.load_state_dict(new_sd)
    return student


class DistillWrapper(torch.nn.Module):
    """把「硬标签 BCE + 软标签 BCE」塞进 forward,Trainer 就能直接用。"""

    def __init__(self, student, alpha: float):
        super().__init__()
        self.student = student
        self.alpha = alpha

    def forward(self, input_ids, attention_mask, token_type_ids,
                labels=None, soft_labels=None):
        logits = self.student(input_ids=input_ids, attention_mask=attention_mask,
                              token_type_ids=token_type_ids).logits
        loss = None
        if labels is not None:
            loss = F.binary_cross_entropy_with_logits(logits, labels)
            if soft_labels is not None:
                soft = F.binary_cross_entropy_with_logits(logits, soft_labels)
                loss = self.alpha * loss + (1 - self.alpha) * soft
        return {"loss": loss, "logits": logits}


class BestInMemory(TrainerCallback):
    """照 train.py 惯例:最优权重留内存,零磁盘 checkpoint。"""

    def __init__(self, model):
        self.model = model
        self.best_metric = -1.0
        self.best_state = None

    def on_evaluate(self, args, state, control, metrics=None, **kwargs):
        score = (metrics or {}).get("eval_micro_f1", -1.0)
        if score > self.best_metric:
            self.best_metric = score
            self.best_state = {k: v.detach().to("cpu", copy=True)
                               for k, v in self.model.state_dict().items()}


def compute_metrics(eval_pred):
    logits, labels = eval_pred
    preds = (1 / (1 + np.exp(-logits)) >= 0.5).astype(int)
    return {"micro_f1": f1_score(labels, preds, average="micro", zero_division=0),
            "macro_f1": f1_score(labels, preds, average="macro", zero_division=0)}


@torch.no_grad()
def teacher_probs(model, ds, tag: str) -> np.ndarray:
    """教师软标签:缓存到磁盘,重跑直接用。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{tag}.npy"
    if f.exists():
        print(f"  软标签命中缓存 {f}")
        return np.load(f)
    model.eval()
    out = []
    for i in range(0, len(ds), 64):
        batch = ds[i:i + 64]
        ids = torch.tensor([b["input_ids"] for b in batch])
        am = torch.tensor([b["attention_mask"] for b in batch])
        tt = torch.tensor([b["token_type_ids"] for b in batch])
        lg = model(input_ids=ids, attention_mask=am, token_type_ids=tt).logits
        out.append(torch.sigmoid(lg).numpy())
        print(f"  教师前向 {min(i + 64, len(ds))}/{len(ds)}", end="\r")
    p = np.concatenate(out).astype(np.float32)
    np.save(f, p)
    print(f"\n  软标签已缓存 {f}  shape={p.shape}")
    return p


def scan_threshold(probs: np.ndarray, gold: np.ndarray) -> tuple[float, float]:
    """与 train.py 完全同一套扫描口径(0.30~0.70 步进 0.05)。"""
    best_t, best_f1 = 0.5, -1.0
    for t in np.arange(0.30, 0.71, 0.05):
        f1 = f1_score(gold, (probs >= t).astype(int), average="micro", zero_division=0)
        if f1 > best_f1:
            best_t, best_f1 = round(float(t), 2), float(f1)
    return best_t, best_f1


def _logits_of(out):
    """兼容两种返回:HF 模型给 ModelOutput(有 .logits),我们的 DistillWrapper 给 dict。
    忘了这层会直接 AttributeError: 'dict' object has no attribute 'logits'(实测踩坑)。"""
    return out["logits"] if isinstance(out, dict) else out.logits


def evaluate(model, ds, gold, threshold, tag):
    model.eval()
    out = []
    with torch.no_grad():
        for i in range(0, len(ds), 64):
            batch = ds[i:i + 64]
            lg = _logits_of(model(
                input_ids=torch.tensor([b["input_ids"] for b in batch]),
                attention_mask=torch.tensor([b["attention_mask"] for b in batch]),
                token_type_ids=torch.tensor([b["token_type_ids"] for b in batch])))
            out.append(torch.sigmoid(lg).numpy())
    probs = np.concatenate(out)
    preds = (probs >= threshold).astype(int)
    mi = precision_recall_fscore_support(gold, preds, average="micro", zero_division=0)
    ma = precision_recall_fscore_support(gold, preds, average="macro", zero_division=0)
    print(f"  {tag:26s} micro-F1 {mi[2]:.4f} (P {mi[0]:.4f} R {mi[1]:.4f})  macro-F1 {ma[2]:.4f}")
    return float(mi[2]), float(ma[2]), probs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--alpha", type=float, default=0.5, help="硬标签权重,软标签权重 1-alpha")
    ap.add_argument("--lr", type=float, default=3e-5)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--layers", type=str, default="0,2,4,6,8,10", help="从教师抽哪几层")
    args = ap.parse_args()
    src_layers = [int(x) for x in args.layers.split(",")]

    tokenizer = AutoTokenizer.from_pretrained(TEACHER_DIR)
    teacher = AutoModelForSequenceClassification.from_pretrained(TEACHER_DIR)
    teacher.eval()
    for p in teacher.parameters():
        p.requires_grad_(False)

    train_ds = encode(load_jsonl(DATA / "train.jsonl"), tokenizer)
    val_ds = encode(load_jsonl(DATA / "val.jsonl"), tokenizer)
    test_ds = encode(load_jsonl(DATA / "test.jsonl"), tokenizer)
    gold_val = np.array([d["labels"] for d in val_ds])
    gold_test = np.array([d["labels"] for d in test_ds])
    print(f"数据 train={len(train_ds)} val={len(val_ds)} test={len(test_ds)}")

    # ---- 教师软标签(缓存)----
    print("\n[1/5] 预计算教师软标签")
    t0 = time.time()
    soft_train = teacher_probs(teacher, train_ds, "train")
    soft_val = teacher_probs(teacher, val_ds, "val")
    print(f"  用时 {time.time() - t0:.0f}s")
    train_ds = [{**d, "soft_labels": soft_train[i].tolist()} for i, d in enumerate(train_ds)]
    val_ds = [{**d, "soft_labels": soft_val[i].tolist()} for i, d in enumerate(val_ds)]

    # ---- 学生 ----
    print(f"\n[2/5] 构造学生(从教师抽层 {src_layers})")
    student = build_student(teacher, src_layers)
    n_all = sum(p.numel() for p in teacher.parameters())
    n_stu = sum(p.numel() for p in student.parameters())
    print(f"  教师 {n_all / 1e6:.1f}M 参数 → 学生 {n_stu / 1e6:.1f}M 参数"
          f"({n_stu / n_all * 100:.1f}%)")

    # ---- 蒸馏训练 ----
    print(f"\n[3/5] 蒸馏训练 epochs={args.epochs} alpha={args.alpha} lr={args.lr}")
    wrapped = DistillWrapper(student, args.alpha)
    targs = TrainingArguments(
        output_dir="data/classifier/distill/checkpoints",
        eval_strategy="epoch", save_strategy="no",
        learning_rate=args.lr,
        per_device_train_batch_size=args.batch,
        per_device_eval_batch_size=64,
        num_train_epochs=args.epochs,
        weight_decay=0.01,
        metric_for_best_model="micro_f1", greater_is_better=True,
        logging_steps=20, report_to="none",
        # 必须显式声明:Trainer 默认把 forward 里所有名字含 "label" 的参数都当标签列,
        # 我们的 forward 有 labels 和 soft_labels 两个 → 会被拼成 2 列,
        # 形状变 (2, N, 17),compute_metrics 收到 y_true 只有 2 个"样本"而报
        # "inconsistent numbers of samples: [2, 149]"(实测踩坑)。
        label_names=["labels"],
        disable_tqdm=True,          # 128 步 × 8 轮,进度条会把有用日志淹掉
    )
    best_cb = BestInMemory(wrapped)
    trainer = Trainer(model=wrapped, args=targs, train_dataset=train_ds, eval_dataset=val_ds,
                      processing_class=tokenizer, data_collator=default_data_collator,
                      compute_metrics=compute_metrics,
                      callbacks=[EarlyStoppingCallback(early_stopping_patience=2), best_cb])
    t0 = time.time()
    trainer.train()
    train_s = time.time() - t0
    if best_cb.best_state is not None:
        wrapped.load_state_dict(best_cb.best_state)
        print(f"  回填最优权重(验证集 micro-F1@0.5 {best_cb.best_metric:.4f})")
    print(f"  训练用时 {train_s:.0f}s")

    # 立刻落盘:训练完的权重只在内存里(BestInMemory + save_strategy=no),
    # 后面任何一步抛异常都会把几十分钟的训练全丢掉(实测踩过一次,42 分钟白跑)。
    OUT.mkdir(parents=True, exist_ok=True)
    student.save_pretrained(OUT)
    tokenizer.save_pretrained(OUT)
    print(f"  学生权重已落盘 {OUT}(先存盘再评测)")

    # ---- 阈值扫描(与 train.py 同口径)----
    print("\n[4/5] 验证集扫阈值 0.30~0.70")
    _, _, vp_stu = evaluate(wrapped, val_ds, gold_val, 0.5, "学生在验证集(阈值0.5)")
    _, _, vp_tea = evaluate(teacher, val_ds, gold_val, 0.5, "教师在验证集(阈值0.5)")
    t_stu, f_stu = scan_threshold(vp_stu, gold_val)
    t_tea, f_tea = scan_threshold(vp_tea, gold_val)
    print(f"  学生最优阈值 {t_stu}(验证 micro-F1 {f_stu:.4f})")
    print(f"  教师最优阈值 {t_tea}(验证 micro-F1 {f_tea:.4f})")

    # ---- 测试集对比(同一套代码,教师和学生各跑一遍)----
    print("\n[5/5] 测试集对比(阈值各取自己验证集最优)")
    mi_s, ma_s, _ = evaluate(wrapped, test_ds, gold_test, t_stu, f"学生(阈值{t_stu})")
    mi_t, ma_t, _ = evaluate(teacher, test_ds, gold_test, t_tea, f"教师(阈值{t_tea})")
    n = len(test_ds)
    se = (mi_t * (1 - mi_t) / n) ** 0.5
    print(f"\n  噪声量级 n={n} ±1SE={se:.4f} ±2SE={2 * se:.4f}")
    print(f"  Δmicro = {mi_s - mi_t:+.4f}  → "
          f"{'噪声内(无可察觉差异)' if abs(mi_s - mi_t) <= 2 * se else '超出噪声'}")
    print(f"  体积:教师 {n_all * 4 / 1024**2:.0f} MB → 学生 {n_stu * 4 / 1024**2:.0f} MB fp32")

    # ---- 落盘(权重已在训练后立刻存过,这里只补阈值与报告)----
    (OUT / "threshold.json").write_text(json.dumps(
        {"threshold": t_stu, "val_micro_f1": f_stu, "distilled": True,
         "alpha": args.alpha, "src_layers": src_layers, "teacher": str(TEACHER_DIR)}))
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "distill_report.json").write_text(json.dumps({
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "epochs": args.epochs, "alpha": args.alpha, "lr": args.lr,
        "src_layers": src_layers, "train_seconds": round(train_s, 1),
        "teacher_params": n_all, "student_params": n_stu,
        "teacher_test_micro_f1": round(mi_t, 4), "student_test_micro_f1": round(mi_s, 4),
        "teacher_test_macro_f1": round(ma_t, 4), "student_test_macro_f1": round(ma_s, 4),
        "teacher_threshold": t_tea, "student_threshold": t_stu,
        "noise_2se": round(2 * se, 4), "delta_micro": round(mi_s - mi_t, 4),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n学生模型已存 {OUT};报告 {REPORTS / 'distill_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
