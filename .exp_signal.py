"""实验3:替换 key_clause_hit 的信号工程。

实验1 证明 key_clause_hit 与目标**负相关**(单独用 J=-0.38,应拒桶命中率 96.7% > 可答桶 58.3%),
剔除它 J 反而 +0.0056。本脚本回答下一个问题:**换成什么更好,而且换得站得住?**

判据三重(全部只用实验1留下的检索缓存,零额外 API 调用):
  ① AUC —— 单个信号把"可答"排在"应拒"前面的概率(0.5 = 无信息,<0.5 = 方向相反)
  ② 单独使用时的 Youden J(全量选阈)
  ③ **5 折留出评估** —— 在训练折上选(信号, 权重, 阈值),在留出折上算 J。
     任何"换掉更好"的结论都必须在这里也成立,否则就是 in-sample 幻觉。

候选信号(都来自检索结果,零额外模型调用):
  query_overlap  Top-3 证据覆盖了查询里多少字符 bigram(库外问题问的东西没被检到 → 覆盖率低)
  leader_gap     top1 − 其余分数的均值(领头有多突出)
  z_gap          (top1 − 均值) / 标准差(领头偏离整体多少倍标准差)
  spread         top1 − 末位分数(整段分数的落差)

用法:python .exp_signal.py
"""
import json
import pathlib
import random
import statistics
import sys
from collections import defaultdict

from app.core.confidence import VALID_COUNT_CAP, VALID_SCORE_FLOOR, W_KEY, W_MARGIN, W_TOP1, W_VALID
from app.kb.documents import KEY_TERMS

ROOT = pathlib.Path(__file__).resolve().parent
CACHE = ROOT / ".exp_cache" / "conf_hits.json"
EVALSET = ROOT / "tests/data/eval_rag.jsonl"
OUT_TXT = ROOT / "data/flywheel/reports/signal_replacement.txt"

PROD_ANSWERABLE = ("A_policy", "B_model", "C_colloquial")
ABSENT = "D_absent"
BASE_W = {"top1": W_TOP1, "valid": W_VALID, "margin": W_MARGIN, "key": W_KEY}
FOLD_N = 5
CANDIDATES = ("query_overlap", "leader_gap", "z_gap", "spread")

_LINES: list[str] = []


def log(msg=""):
    print(msg, flush=True)
    _LINES.append(msg)


def clip(x):
    return max(0.0, min(1.0, x))


def bigrams(s: str) -> set:
    s = "".join(ch for ch in (s or "") if ch.isalnum())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def signals_of(query: str, hits: list[dict]) -> dict:
    """原有四信号 + 四个候选信号,全部只依赖检索结果。"""
    if not hits:
        return {k: 0.0 for k in ("top1", "valid", "margin", "key",
                                 "query_overlap", "leader_gap", "z_gap", "spread")}
    scores = [float(h.get("rerank_score", 0.0)) for h in hits]
    top1 = scores[0]
    rest = scores[1:]
    margin = top1 - rest[0] if rest else top1
    valid = sum(1 for s in scores if s >= VALID_SCORE_FLOOR)
    key = any(any(t in f"{h.get('question','')}{h.get('answer','')}" for t in KEY_TERMS)
              for h in hits[:3])
    top3_text = "".join(f"{h.get('question','')}{h.get('answer','')}" for h in hits[:3])
    qb, eb = bigrams(query), bigrams(top3_text)
    overlap = len(qb & eb) / len(qb) if qb else 0.0
    leader_gap = top1 - (sum(rest) / len(rest) if rest else 0.0)
    sd = statistics.pstdev(scores) if len(scores) > 1 else 0.0
    z_gap = (top1 - statistics.fmean(scores)) / (sd + 1e-6)
    spread = top1 - scores[-1] if len(scores) > 1 else top1
    return {"top1": top1, "valid": float(valid), "margin": margin,
            "key": 1.0 if key else 0.0,
            "query_overlap": overlap, "leader_gap": leader_gap,
            "z_gap": z_gap, "spread": spread}


def norm_signal(name: str, v: float) -> float:
    """把候选信号压到 [0,1]:z_gap/leader_gap/spread 是开区间量,用固定尺度归一。"""
    if name == "z_gap":
        return clip(v / 4.0)          # 4 倍标准差封顶
    if name == "leader_gap":
        return clip(v)                # 分数本身在 0-1 量级
    if name == "spread":
        return clip(v)
    return clip(v)                    # query_overlap 已是 0-1


def score_with(sig: dict, weights: dict) -> float:
    total = sum(weights.values()) or 1.0
    s = 0.0
    for k, w in weights.items():
        if w <= 0:
            continue
        v = sig.get(k, 0.0)
        if k == "valid":
            v = min(v, VALID_COUNT_CAP) / VALID_COUNT_CAP
        elif k in CANDIDATES:
            v = norm_signal(k, v)
        else:
            v = clip(v)
        s += w / total * v
    return round(clip(s), 4)


def youden(an, ab, t):
    tpr = sum(1 for c in an if c >= t) / len(an) if an else 0.0
    fpr = sum(1 for c in ab if c >= t) / len(ab) if ab else 0.0
    return tpr, fpr, tpr - fpr


def best_threshold(an, ab):
    bt, bj = None, -9e9
    for i in range(5, 96):
        t = i / 100
        _, _, j = youden(an, ab, t)
        if j > bj:
            bt, bj = t, j
    return bt, bj


def auc(an, ab) -> float:
    """P(可答分 > 应拒分),含 0.5 权重的并列。"""
    if not an or not ab:
        return float("nan")
    allv = sorted([(v, 1) for v in an] + [(v, 0) for v in ab])
    ranks = {}
    i = 0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1][0] == allv[i][0]:
            j += 1
        avg = (i + j) / 2 + 1
        ranks[allv[i][0]] = avg
        i = j + 1
    r_pos = sum(ranks[v] for v in an)
    n1, n0 = len(an), len(ab)
    return (r_pos - n1 * (n1 + 1) / 2) / (n1 * n0)


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def main():
    samples = [json.loads(ln) for ln in open(EVALSET, encoding="utf-8")]
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    sig, bucket = {}, {}
    for s in samples:
        sig[s["id"]] = signals_of(s["query"], cache.get(s["id"], []))
        bucket[s["id"]] = s["bucket"]

    an_ids = [s["id"] for s in samples if s["bucket"] in PROD_ANSWERABLE]
    ab_ids = [s["id"] for s in samples if s["bucket"] == ABSENT]

    log("=" * 80)
    log("实验3:替换 key_clause_hit 的信号工程(零额外 API 调用,复用实验1的检索缓存)")
    log("=" * 80)
    log(f"可答(A/B/C) {len(an_ids)} 条 · 应拒(D) {len(ab_ids)} 条")

    # ---------- ① 每个信号的独立判别力 ----------
    log("\n" + "-" * 80)
    log("① 每个信号单独的判别力")
    log("-" * 80)
    log(f"  {'信号':<16}{'AUC':>9}{'单独最优t':>11}{'单独J':>9}   说明")
    notes = {
        "top1": "精排 Top1 分", "valid": "有效证据数(≥0.3,3 封顶)",
        "margin": "Top1−Top2", "key": "关键词表命中(现状,权重 0.1)",
        "query_overlap": "查询 bigram 被 Top3 覆盖比例(候选)",
        "leader_gap": "Top1 − 其余均值(候选)",
        "z_gap": "Top1 偏离均值几倍标准差(候选)",
        "spread": "Top1 − 末位(候选)",
    }
    solo = {}
    for name in ("top1", "valid", "margin", "key", *CANDIDATES):
        a = [norm_signal(name, sig[i][name]) for i in an_ids]
        b = [norm_signal(name, sig[i][name]) for i in ab_ids]
        t, j = best_threshold(a, b)
        solo[name] = {"auc": auc(a, b), "t": t, "j": j}
        flag = "  ← 反向!" if solo[name]["auc"] < 0.5 else ""
        log(f"  {name:<16}{solo[name]['auc']:>9.4f}{t:>11}{j:>9.4f}   {notes[name]}{flag}")

    # ---------- ② 全量:替换 key 的效果 ----------
    log("\n" + "-" * 80)
    log("② 全量替换效果(key 权重 0.1 → 候选信号权重 0.1)")
    log("-" * 80)
    def js(weights):
        a = [score_with(sig[i], weights) for i in an_ids]
        b = [score_with(sig[i], weights) for i in ab_ids]
        t, j = best_threshold(a, b)
        tpr, fpr, _ = youden(a, b, t)
        return {"t": t, "j": j, "tpr": tpr, "fpr": fpr}

    base = js(BASE_W)
    log(f"  {'配置':<32}{'最优t':>8}{'YoudenJ':>10}{'通过率':>9}{'放行率':>9}{'ΔJ':>9}")
    log(f"  {'现状(top1/valid/margin/key)':<32}{base['t']:>8}{base['j']:>10.4f}"
        f"{base['tpr']:>9.4f}{base['fpr']:>9.4f}{0.0:>+9.4f}")
    no_key = {k: v for k, v in BASE_W.items() if k != "key"}
    r = js(no_key)
    log(f"  {'去掉 key(三信号)':<32}{r['t']:>8}{r['j']:>10.4f}{r['tpr']:>9.4f}{r['fpr']:>9.4f}"
        f"{r['j']-base['j']:>+9.4f}")
    for c in CANDIDATES:
        w = {k: v for k, v in BASE_W.items() if k != "key"}
        w[c] = 0.1
        r = js(w)
        log(f"  {'key→'+c:<32}{r['t']:>8}{r['j']:>10.4f}{r['tpr']:>9.4f}{r['fpr']:>9.4f}"
            f"{r['j']-base['j']:>+9.4f}")

    # ---------- ③ 5 折留出评估(唯一能证明"换得对"的判据) ----------
    log("\n" + "-" * 80)
    log("③ 5 折留出评估:训练折选(权重,阈值) → 留出折算 J")
    log("-" * 80)
    rng = random.Random(42)
    thin = defaultdict(list)
    for i in an_ids:
        thin[bucket[i]].append(i)
    folds_an = defaultdict(list)
    for b, ids in thin.items():
        ids = list(ids)
        rng.shuffle(ids)
        for k, i in enumerate(ids):
            folds_an[k % FOLD_N].append(i)
    ab_sh = list(ab_ids)
    rng.shuffle(ab_sh)
    folds_ab = defaultdict(list)
    for k, i in enumerate(ab_sh):
        folds_ab[k % FOLD_N].append(i)

    def oof(config_fn, grid):
        """config_fn(w) -> 权重字典;grid 是候选 w 列表。返回 OOF J 均值与每折选中的 w/t。"""
        oof_js, picks = [], []
        for k in range(FOLD_N):
            tr_an = [i for f in range(FOLD_N) if f != k for i in folds_an[f]]
            tr_ab = [i for f in range(FOLD_N) if f != k for i in folds_ab[f]]
            best = None
            for w in grid:
                weights = config_fn(w)
                a = [score_with(sig[i], weights) for i in tr_an]
                b = [score_with(sig[i], weights) for i in tr_ab]
                t, j = best_threshold(a, b)
                if best is None or j > best[0]:
                    best = (j, w, t)
            _, w, t = best
            weights = config_fn(w)
            te_an = [score_with(sig[i], weights) for i in folds_an[k]]
            te_ab = [score_with(sig[i], weights) for i in folds_ab[k]]
            _, _, j = youden(te_an, te_ab, t)
            oof_js.append(j)
            picks.append((w, t))
        return statistics.mean(oof_js), picks

    # 现状配置:key 固定 0.1,只选阈值
    base_j, base_picks = oof(lambda w: BASE_W, [None])
    log(f"  现状(四信号,固定权重)            OOF J = {base_j:.4f}   每折(权重,阈值)={base_picks}")
    # 对等对比:去掉 key 也是**固定**配置(没有多出来的自由参数),只选阈值
    fixed3 = {k: v for k, v in BASE_W.items() if k != "key"}
    fixed3_j, fixed3_picks = oof(lambda w: fixed3, [None])
    log(f"  去掉 key(三信号,固定权重)        OOF J = {fixed3_j:.4f}   每折(权重,阈值)={fixed3_picks}"
        f"   (Δ vs 现状 {fixed3_j-base_j:+.4f})")

    # 每个候选:替换 key,并在训练折上选它的权重
    grid = (0.0, 0.05, 0.1, 0.15, 0.2, 0.3)
    results = {}
    for c in CANDIDATES:
        def cfg(w, c=c):
            d = {k: v for k, v in BASE_W.items() if k != "key"}
            if w > 0:
                d[c] = w
            return d
        mean_j, picks = oof(cfg, grid)
        results[c] = (mean_j, picks)
        flag = "  ← 优于现状" if mean_j > base_j else ""
        log(f"  key→{c:<28} OOF J = {mean_j:.4f}   (Δ vs 现状 {mean_j-base_j:+.4f})"
            f"  选中的权重={[p[0] for p in picks]}{flag}")

    log("\n" + "-" * 80)
    log("④ 结论")
    log("-" * 80)
    best_c = max(results.items(), key=lambda kv: kv[1][0])
    log(f"  对等对比(都只选阈值、权重固定):")
    log(f"    四信号(含 key)   OOF J = {base_j:.4f}")
    log(f"    三信号(去 key)   OOF J = {fixed3_j:.4f}   ({fixed3_j-base_j:+.4f})")
    log(f"  候选信号(需在训练折上另选权重,多一个自由参数):")
    for c, v in sorted(results.items(), key=lambda kv: -kv[1][0]):
        log(f"    key→{c:<16} OOF J = {v[0]:.4f}   ({v[0]-base_j:+.4f})  选中的权重={[p[0] for p in v[1]]}")
    log("")
    if fixed3_j > base_j + 1e-9:
        log(f"  → 建议:去掉 key_clause_hit(三信号)。它在留出折上也更好({fixed3_j:.4f} vs {base_j:.4f}),")
        log(f"     而且它在单信号 AUC 上只有 0.31(反向),两个判据一致。")
        log(f"     落地要做的:改 app/core/confidence.py 的权重常量 → **重新校准阈值**(旧 0.26 不再适用)")
        log(f"     → 重跑 rag_eval / 落新报告。")
    elif best_c[1][0] > base_j + 1e-9:
        log(f"  → 建议:把 key_clause_hit 换成 {best_c[0]}(留出折上 {best_c[1][0]:.4f} > {base_j:.4f})。")
    else:
        log(f"  → 留出评估下两种改法都没有稳定增益,诚实结论:**保持现状**。")
        log(f"     key_clause_hit 的 AUC 只有 0.31(统计上反向),但权重只 0.1,去掉它 / 换掉它")
        log(f"     在留出折上都没能稳定提升 J。这个负结果本身是有价值的结论:")
        log(f"     **四信号里有 1 个信号是噪声,但它的权重小到不影响系统表现。**")

    OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
    OUT_TXT.write_text("\n".join(_LINES) + "\n", encoding="utf-8")
    log(f"\n报告已落 {OUT_TXT.relative_to(ROOT)}")


if __name__ == "__main__":
    sys.exit(main() or 0)
