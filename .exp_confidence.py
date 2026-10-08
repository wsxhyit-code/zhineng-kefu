"""置信度闸的两个实验:四信号消融 + 阈值稳定性。

背景:随附的 confidence_calibration.json 是在 RAG 评估集的**早期 80 题版本**(60 可答 + 20 应拒)
上跑的,而当前评估集是 300 题(A/B/C/E/D 各 60)。阈值 0.26 因此需要一个有出处的重算,
而"四信号权重 0.5/0.2/0.2/0.1"从来没有消融证据。

本脚本只读评估集与检索结果,不改任何现有代码;rerank_score 与逐题证据缓存到 .exp_cache/,
所以分析可以反复迭代而不重复烧上游额度。

用法:
  python .exp_confidence.py retrieve   # 只做检索并落缓存(耗时,需 Milvus + 嵌入 + 重排)
  python .exp_confidence.py analyze    # 只用缓存做全部分析
  python .exp_confidence.py all        # 两步都做
"""
import asyncio
import json
import pathlib
import random
import statistics
import sys
from collections import Counter, defaultdict

from app.core import retrieval
from app.core.confidence import (VALID_COUNT_CAP, VALID_SCORE_FLOOR, W_KEY,
                                 W_MARGIN, W_TOP1, W_VALID)
from app.kb.documents import KEY_TERMS

ROOT = pathlib.Path(__file__).resolve().parent
CACHE = ROOT / ".exp_cache" / "conf_hits.json"
OUT_TXT = ROOT / "data/flywheel/reports/confidence_ablation.txt"
EVALSET = ROOT / "tests/data/eval_rag.jsonl"

CONCURRENCY = 8
PROD_ANSWERABLE = ("A_policy", "B_model", "C_colloquial")   # 与 calibrate_confidence.py 同口径
ALL_ANSWERABLE = ("A_policy", "B_model", "C_colloquial", "E_multi")  # 含跨文档桶
ABSENT = "D_absent"
WEIGHTS = {"top1": W_TOP1, "valid": W_VALID, "margin": W_MARGIN, "key": W_KEY}

_LINES: list[str] = []


def log(msg=""):
    print(msg, flush=True)
    _LINES.append(msg)


# ---------------- 信号与打分(与 app/core/confidence.py 同语义,只是权重可换) ----------------
def signals_of(hits):
    if not hits:
        return {"top1": 0.0, "valid": 0.0, "margin": 0.0, "key": 0.0}
    scores = [float(h.get("rerank_score", 0.0)) for h in hits]
    top1 = scores[0]
    margin = top1 - scores[1] if len(scores) > 1 else top1
    valid = sum(1 for s in scores if s >= VALID_SCORE_FLOOR)
    key = any(any(t in f"{h.get('question','')}{h.get('answer','')}" for t in KEY_TERMS)
              for h in hits[:3])
    return {"top1": top1, "valid": float(valid), "margin": margin, "key": 1.0 if key else 0.0}


def clip(x):
    return max(0.0, min(1.0, x))


def score_with(sig, weights, enabled=None):
    """按给定权重算分;enabled 里的信号才参与,其余剔除后**重新归一**(消融用)。"""
    enabled = enabled or set(weights)
    norm = {k: weights[k] for k in enabled}
    total = sum(norm.values()) or 1.0
    s = 0.0
    if "top1" in norm:
        s += norm["top1"] / total * clip(sig["top1"])
    if "valid" in norm:
        s += norm["valid"] / total * min(sig["valid"], VALID_COUNT_CAP) / VALID_COUNT_CAP
    if "margin" in norm:
        s += norm["margin"] / total * clip(sig["margin"])
    if "key" in norm:
        s += norm["key"] / total * sig["key"]
    return round(clip(s), 4)


# ---------------- 阈值扫描 ----------------
def youden(an, ab, t):
    tpr = sum(1 for c in an if c >= t) / len(an) if an else 0.0
    fpr = sum(1 for c in ab if c >= t) / len(ab) if ab else 0.0
    return tpr, fpr, tpr - fpr


def best_threshold(an, ab, lo=0.05, hi=0.95):
    """严格大于取最早的最优点——与 calibrate_confidence.py 的 :75 同口径。"""
    bt, bj, btr, bfr = None, -9e9, 0.0, 0.0
    i = 5
    while i <= 95:
        t = i / 100
        tpr, fpr, j = youden(an, ab, t)
        if j > bj:
            bt, bj, btr, bfr = t, j, tpr, fpr
        i += 1
    return {"t": bt, "j": round(bj, 4), "tpr": round(btr, 4), "fpr": round(bfr, 4)}


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def dist(xs):
    if not xs:
        return "n=0"
    return (f"n={len(xs)} min={pct(xs,0):.3f} p25={pct(xs,0.25):.3f} "
            f"p50={pct(xs,0.5):.3f} p75={pct(xs,0.75):.3f} max={pct(xs,1.0):.3f}")


# ---------------- 阶段 1:检索并缓存 ----------------
async def do_retrieve():
    samples = [json.loads(ln) for ln in open(EVALSET, encoding="utf-8")]
    sem = asyncio.Semaphore(CONCURRENCY)
    done = {"n": 0}
    total = len(samples)

    async def one(s):
        async with sem:
            try:
                hits = await retrieval.search_knowledge(s["query"], strategy="hybrid_rerank")
            except Exception as e:  # noqa: BLE001
                log(f"  检索失败 {s['id']}: {type(e).__name__}")
                hits = []
            done["n"] += 1
            if done["n"] % 25 == 0:
                log(f"  已检索 {done['n']}/{total}")
            return s["id"], [{"rerank_score": float(h.get("rerank_score", 0.0)),
                              "question": h.get("question", ""),
                              "answer": h.get("answer", ""),
                              "section_path": h.get("section_path", "")} for h in hits]

    log(f"=== 阶段1 检索:{total} 题 × hybrid_rerank(并发 {CONCURRENCY})===")
    results = await asyncio.gather(*(one(s) for s in samples))
    cache = dict(results)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    log(f"  缓存已落 {CACHE.relative_to(ROOT)}({len(cache)} 题)")
    return cache


def load_cache():
    if not CACHE.exists():
        raise SystemExit(f"没有缓存 {CACHE},先跑 retrieve")
    return json.loads(CACHE.read_text(encoding="utf-8"))


# ---------------- 阶段 2:分析 ----------------
def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("retrieve", "all"):
        asyncio.run(do_retrieve())
        if sys.argv[1] == "retrieve":
            return
    if not (len(sys.argv) > 1 and sys.argv[1] in ("analyze", "all")):
        raise SystemExit(__doc__)

    samples = [json.loads(ln) for ln in open(EVALSET, encoding="utf-8")]
    cache = load_cache()
    per_bucket = defaultdict(list)
    sig_by_id = {}
    for s in samples:
        sig_by_id[s["id"]] = signals_of(cache.get(s["id"], []))
        per_bucket[s["bucket"]].append(s["id"])

    # ---------- 段0:评估集与缓存概况 ----------
    log("\n" + "=" * 78)
    log("置信度闸实验:四信号消融 + 阈值稳定性")
    log("=" * 78)
    log(f"评估集 {len(samples)} 题 · 缓存命中 {sum(1 for s in samples if s['id'] in cache)} 题")
    log("桶分布:" + "  ".join(f"{b}={len(v)}" for b, v in sorted(per_bucket.items())))

    def scores(ids, weights=None, enabled=None):
        return [score_with(sig_by_id[i], weights or WEIGHTS, enabled) for i in ids]

    abc = [i for b in PROD_ANSWERABLE for i in per_bucket[b]]
    alle = abc + list(per_bucket["E_multi"])
    absent = list(per_bucket[ABSENT])

    # ---------- 段1:重算基线(当前 300 题集) ----------
    log("\n" + "-" * 78)
    log("段1 基线重算:同一套权重,跑在当前 300 题集上")
    log("-" * 78)
    for label, ans in (("可答=A/B/C(生产校准口径)", abc), ("可答=A/B/C/E(全部计分桶)", alle)):
        a, b = scores(ans), scores(absent)
        log(f"\n  【{label}】")
        log(f"    可答分布 {dist(a)}")
        log(f"    应拒分布 {dist(b)}")
        r = best_threshold(a, b)
        log(f"    最优阈值 t={r['t']}  YoudenJ={r['j']}  可答通过率={r['tpr']}  应拒放行率={r['fpr']}")
        in_use = 0.26
        tpr, fpr, j = youden(a, b, in_use)
        log(f"    沿用线上阈值 {in_use}:通过率={tpr:.4f} 放行率={fpr:.4f} J={j:.4f}"
            f"  → 误拦 {1-tpr:.1%} / 误放 {fpr:.1%}")

    # ---------- 段2:考古验证——旧 80 题集能否复现 0.26/J=0.85 ----------
    log("\n" + "-" * 78)
    log("段2 考古验证:随附报告(60 可答 + 20 应拒,阈值 0.26,J=0.85)能否复现")
    log("-" * 78)
    for k in (20, 60):
        sub_an = [i for b in PROD_ANSWERABLE for i in per_bucket[b][:k]]
        sub_ab = per_bucket[ABSENT][:k]
        a, b = scores(sub_an), scores(sub_ab)
        r = best_threshold(a, b)
        log(f"  取每桶前 {k} 条 → 可答 {len(sub_an)} / 应拒 {len(sub_ab)}:"
            f" 最优 t={r['t']} J={r['j']} (通过 {r['tpr']} / 放行 {r['fpr']})"
            f"   [报告值: t=0.26 J=0.85 通过 0.85 放行 0.0]")

    # ---------- 段3:四信号消融 ----------
    log("\n" + "-" * 78)
    log("段3 四信号消融(剔除某信号后重新归一,跑在 A/B/C + D 上)")
    log("-" * 78)
    a_all, b_all = scores(abc), scores(absent)
    base = best_threshold(a_all, b_all)
    log(f"  {'配置':<28}{'最优t':>7}{'YoudenJ':>9}{'通过率':>9}{'放行率':>9}{'ΔJ':>9}")
    log(f"  {'全部四信号(现状)':<28}{base['t']:>7}{base['j']:>9.4f}{base['tpr']:>9.4f}{base['fpr']:>9.4f}{0.0:>9.4f}")
    for drop in ("top1", "valid", "margin", "key"):
        enabled = set(WEIGHTS) - {drop}
        a, b = scores(abc, enabled=enabled), scores(absent, enabled=enabled)
        r = best_threshold(a, b)
        log(f"  {'剔除 '+drop:<28}{r['t']:>7}{r['j']:>9.4f}{r['tpr']:>9.4f}{r['fpr']:>9.4f}"
            f"{r['j']-base['j']:>+9.4f}")
    log("\n  单信号各自的判别力(只用该信号):")
    for only in ("top1", "valid", "margin", "key"):
        a, b = scores(abc, enabled={only}), scores(absent, enabled={only})
        r = best_threshold(a, b)
        log(f"  {'只用 '+only:<28}{r['t']:>7}{r['j']:>9.4f}{r['tpr']:>9.4f}{r['fpr']:>9.4f}")
    log(f"\n  key_clause_hit 是否恒真:可答桶命中率="
        f"{sum(sig_by_id[i]['key'] for i in abc)/len(abc):.3f}"
        f"  应拒桶命中率={sum(sig_by_id[i]['key'] for i in absent)/len(absent):.3f}")

    # ---------- 段4:阈值稳定性(5 折 + bootstrap) ----------
    log("\n" + "-" * 78)
    log("段4 阈值稳定性")
    log("-" * 78)
    fold_n = 5
    rng = random.Random(42)
    bucket_of = {s["id"]: s["bucket"] for s in samples}
    # 按桶分层分折
    thin = defaultdict(list)
    for i in abc:
        thin[bucket_of[i]].append(i)
    tfold = defaultdict(list)
    for b, ids in thin.items():
        ids = list(ids)
        rng.shuffle(ids)
        for k, i in enumerate(ids):
            tfold[k % fold_n].append(i)
    ab_ids = list(absent)
    rng.shuffle(ab_ids)
    afold = defaultdict(list)
    for k, i in enumerate(ab_ids):
        afold[k % fold_n].append(i)

    log("  ① 5 折交叉验证(4 折选阈值 → 留出折上报指标)")
    log(f"  {'折':<5}{'选出的t':>9}{'选阈集J':>10}{'留出折通过率':>13}{'留出折放行率':>13}")
    oof_j, sel_ts = [], []
    for k in range(fold_n):
        tr_an = [i for f in range(fold_n) if f != k for i in tfold[f]]
        tr_ab = [i for f in range(fold_n) if f != k for i in afold[f]]
        te_an, te_ab = tfold[k], afold[k]
        r = best_threshold(scores(tr_an), scores(tr_ab))
        tpr, fpr, j = youden(scores(te_an), scores(te_ab), r["t"])
        sel_ts.append(r["t"])
        oof_j.append(j)
        log(f"  {k+1:<5}{r['t']:>9}{r['j']:>10.4f}{tpr:>13.4f}{fpr:>13.4f}")
    log(f"  → 5 折选出的阈值:{sel_ts}  区间 [{min(sel_ts)}, {max(sel_ts)}]")
    log(f"  → 全量选阈 J={base['j']:.4f} vs 留出折平均 J={statistics.mean(oof_j):.4f}"
        f"  乐观偏差={base['j']-statistics.mean(oof_j):+.4f}")

    log("\n  ② Bootstrap(B=1000,两桶各自有放回重采样)")
    B = 1000
    tsa, tpa, fpa = [], [], []
    for _ in range(B):
        sa = [random.choice(scores(abc)) for _ in abc]
        sb = [random.choice(scores(absent)) for _ in absent]
        r = best_threshold(sa, sb)
        tsa.append(r["t"])
        tpr, fpr, _ = youden(sa, sb, 0.26)
        tpa.append(tpr)
        fpa.append(fpr)
    log(f"    阈值的 95% 区间: [{pct(tsa,0.025):.2f}, {pct(tsa,0.975):.2f}]  (点估计 {base['t']})")
    log(f"    在 t=0.26 处:可答通过率 95% 区间 [{pct(tpa,0.025):.4f}, {pct(tpa,0.975):.4f}]")
    log(f"                 应拒放行率 95% 区间 [{pct(fpa,0.025):.4f}, {pct(fpa,0.975):.4f}]")
    log(f"    零放行是否稳定:{sum(1 for x in fpa if x == 0.0)/B:.1%} 的 bootstrap 样本在 t=0.26 上零放行")

    log("\n  ③ 应拒桶规模的直接影响(确定性)")
    for k in (20, 40, 60):
        sub_ab = absent[:k]
        a, b = scores(abc), scores(sub_ab)
        in_use_t = best_threshold(a, b)["t"]
        tpr, fpr, _ = youden(a, b, 0.26)
        log(f"    应拒桶取前 {k:>2} 条:max={max(b):.3f}  0.26 处放行率={fpr:.4f}"
            f"  该子集最优 t={in_use_t}")

    # ---------- 段5:E_multi 纳入的影响 ----------
    log("\n" + "-" * 78)
    log("段5 跨文档桶(E_multi)纳入可答桶的影响——校准口径 vs 线上口径")
    log("-" * 78)
    e_ids = list(per_bucket["E_multi"])
    log(f"  E_multi 的置信度分布 {dist(scores(e_ids))}")
    log(f"  A/B/C 的置信度分布  {dist(scores(abc))}")
    tpr_e, fpr_e, _ = youden(scores(e_ids), scores(absent), 0.26)
    log(f"  在 t=0.26 处:E_multi 通过率={tpr_e:.4f}(即 {1-tpr_e:.1%} 被误拦)"
        f"  而 A/B/C 通过率={youden(scores(abc), scores(absent), 0.26)[0]:.4f}")
    log(f"  → 把 E_multi 计入可答桶后,整体误拦率从 "
        f"{1-youden(scores(abc), scores(absent), 0.26)[0]:.1%} 变成 "
        f"{1-youden(scores(alle), scores(absent), 0.26)[0]:.1%}")

    # ---------- 段6:权重粗网格(回答"为什么是 0.5/0.2/0.2/0.1") ----------
    log("\n" + "-" * 78)
    log("段6 权重粗网格搜索(全量选阈,看当前权重离最优有多远)")
    log("-" * 78)
    best = None
    grid = []
    for w1 in (0.3, 0.4, 0.5, 0.6, 0.7):
        for w2 in (0.1, 0.2, 0.3):
            for w3 in (0.1, 0.2, 0.3):
                for w4 in (0.0, 0.1, 0.2):
                    tot = w1 + w2 + w3 + w4
                    if abs(tot - 1.0) > 1e-9:
                        continue
                    w = {"top1": w1, "valid": w2, "margin": w3, "key": w4}
                    a, b = scores(abc, weights=w), scores(absent, weights=w)
                    r = best_threshold(a, b)
                    grid.append((r["j"], w1, w2, w3, w4, r["t"]))
                    if best is None or r["j"] > best[0]:
                        best = (r["j"], w1, w2, w3, w4, r["t"])
    grid.sort(reverse=True)
    log(f"  共评估 {len(grid)} 组权重")
    log(f"  {'top1':>6}{'valid':>7}{'margin':>8}{'key':>6}{'阈值':>7}{'YoudenJ':>10}")
    for j, w1, w2, w3, w4, t in grid[:8]:
        log(f"  {w1:>6}{w2:>7}{w3:>8}{w4:>6}{t:>7}{j:>10.4f}")
    cur = [g for g in grid if (g[1], g[2], g[3], g[4]) == (0.5, 0.2, 0.2, 0.1)]
    log(f"\n  现状权重 0.5/0.2/0.2/0.1:J={cur[0][0]:.4f} 阈值={cur[0][5]}"
        f"  排名 {grid.index(cur[0])+1}/{len(grid)}")
    log(f"  最优权重 {best[1]}/{best[2]}/{best[3]}/{best[4]}:J={best[0]:.4f}"
        f"  比现状高 {best[0]-cur[0][0]:+.4f}")
    log("  注意:这是在同一集上选权重的 in-sample 结果,高出的部分含过拟合成分。")

    OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
    OUT_TXT.write_text("\n".join(_LINES) + "\n", encoding="utf-8")
    log(f"\n报告已落 {OUT_TXT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
