"""实验D:意图识别评估重建。

要解决的问题(全部是 scripts/eval_intent.py 的实测缺陷):
  1. 评估集只有 18 条(九类各 2 条),且其中 6 条与 INTENT_CLASSIFY_SYSTEM 的 few-shot
     **逐字或近乎逐字重合** —— 等于在测"模型记没记住 prompt 里的例子";
  2. 只有计数,没有逐类 P/R/F1、没有混淆矩阵、没有置信区间;
  3. 多轮漂移那条**只 print 不进 passed、不影响退出码** —— 所谓"测了"是打出来给人眼看;
  4. 上游解析失败会返回 {"intent":"其他","confidence":0.0},而"其他"是合法标签,
     旧脚本的校验只查"是九类之一 + confidence 是 [0,1] 的 float",**0.0 完全合法** ——
     于是"期望其他"的两条里,解析失败会被算作答对。

本脚本做四件事:
  ① 在新建的 188 条独立评估集上跑九类评估:整体准确率 / 无歧义子集 / 边界子集 / 漂转子集,
     逐类 P/R/F1 + macro-F1 + 9×9 混淆矩阵;
  ② 把"调用失败"与"模型判了其他"分开:confidence==0.0 只由 app/core/intent.py 的
     `except Exception` 兜底产生,所以(其他+0.0)一律按**调用失败**重试/剔除,不进指标;
  ③ 跑旧的 18 条做对照,并拆出"未被污染的 12 条",量化污染带来的虚高;
  ④ **重放一致性**:对子集重复调用多次,统计逐条一致率与两两一致率
     (旧脚本自己承认"glm 非确定性,抖动如实重跑记录",但从没量化过)。

中止与退出码(2026-09-22 两次实测教训):
  · 401/402/403 → 立即中止,退出码 2,报告打横幅,数字不可用;
  · 前 10 次调用全失败 → 立即熔断(因为 classify 会把 402 吞成"其他+0.0",
    调用层看不到异常,不熔断就会跑完 188 条并写出"准确率 10.6%");
  · 有个别条目重试 3 次仍失败 → 剔除该条目,退出码 1(报告不完整,需重跑)。

用法:python .exp_intent.py
"""
import asyncio
import json
import pathlib
import statistics
import sys
from collections import Counter, defaultdict

from app.core.intent import INTENTS, classify

ROOT = pathlib.Path(__file__).resolve().parent
EVALSET = ROOT / "tests/data/eval_intent.jsonl"
OUT_TXT = ROOT / "data/intent/reports/intent_eval.txt"

CONCURRENCY = 5
REPLAY_N = 30          # 重放子集大小
REPLAY_TIMES = 3       # 每条重复调用次数

# 旧评估集(来自 scripts/eval_intent.py:11-21)+ 污染标记:
# 与 INTENT_CLASSIFY_SYSTEM 的 few-shot 逐字相同(verbatim)或近乎相同(near)
OLD_SAMPLES = [
    ("订单1001的快递到哪了", "物流", None),
    ("我买的东西发货了吗", "物流", None),
    ("订单2002现在什么状态", "订单", None),
    ("我上周下的单多少钱来着", "订单", None),
    ("这款猫粮多少钱一包", "商品咨询", None),
    ("退货运费一般谁承担", "商品咨询", "near"),          # few-shot: 退货运费谁出
    ("我要退货", "退款退货", None),
    ("这个订单还能申请退款吗", "退款退货", None),
    ("我的猫爬架坏了能保修吗", "售后", "verbatim"),        # few-shot 原句
    ("换新进度到哪了", "售后", None),
    ("你们这什么破服务,我要投诉", "投诉", "near"),         # few-shot: 你们这什么破服务
    ("太差了给我个说法", "投诉", None),
    ("帮我建个工单", "人工", "verbatim"),                 # few-shot 原句
    ("猫砂盆漏电了,帮我建个工单跟进", "人工", "verbatim"),   # few-shot 原句
    ("你好呀", "闲聊", None),
    ("今天天气不错", "闲聊", None),
    ("帮我写一段 Python 代码", "其他", "near"),            # few-shot: 帮我写首诗
    ("阿斯顿发发", "其他", None),
]

_LINES: list[str] = []
_ERRS: list[str] = []      # 上游失败累计:非空则整次运行判为"不完整",退出码非 0


def log(msg=""):
    print(msg, flush=True)
    _LINES.append(msg)


def is_degraded(r: dict) -> bool:
    """降级签名:classify 里解析失败/越界都返回 其他 + 0.0。"""
    return r.get("intent") == "其他" and float(r.get("confidence") or 0.0) == 0.0


def prf(cm: dict, labels: list[str]) -> dict:
    """从混淆矩阵算逐类 P/R/F1。cm[期望][预测] = 计数。"""
    out = {}
    for lb in labels:
        tp = cm[lb][lb]
        fp = sum(cm[o][lb] for o in labels if o != lb)
        fn = sum(cm[lb][p] for p in labels if p != lb)
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        f = 2 * p * r / (p + r) if p + r else 0.0
        out[lb] = {"p": p, "r": r, "f1": f, "support": tp + fn}
    return out


# 401/402/403 是余额与鉴权问题,重试无用:继续跑只会把"故障"记录成"模型判错/模型抖动"
FATAL_STATUS = (401, 402, 403)


class FatalUpstream(RuntimeError):
    """上游不可恢复故障。抛出即中止,绝不让失败调用进入指标。"""


class _Gate:
    """前 N 次调用全失败 → 判定上游不可用,立即熔断。

    2026-09-22 二次踩坑:402 时 intent.classify 内部 except 把异常吞成(其他+0.0),
    调用层**看不到任何异常**,于是脚本老老实实跑完 188 条并写出"准确率 10.6%"。
    """

    def __init__(self, n: int):
        self.n = n
        self.done = 0
        self.failed = 0

    def record(self, ok: bool):
        self.done += 1
        if not ok:
            self.failed += 1
        if self.done >= self.n and self.failed == self.done:
            raise FatalUpstream(
                f"前 {self.done} 次调用全部失败(降级兜底或抛异常),判定上游不可用,立即熔断")


def _status_of(exc: BaseException):
    """从 openai/httpx 异常里挖 HTTP 状态码。"""
    st = getattr(exc, "status_code", None)
    if st is None:
        st = getattr(getattr(exc, "response", None), "status_code", None)
    return int(st) if isinstance(st, int) else None


async def run_set(items, sem, tag: str, retries: int = 3):
    """跑一批样本,返回逐条结果。

    失败**不伪装成标签**:穷尽重试后 got=None / err=True,由调用方显式丢弃。
    失败判据有两条,缺一不可:
      ① classify 抛异常;
      ② 返回(其他, 0.0) —— app/core/intent.py 里唯一产生 confidence==0.0 的路径是
         `except Exception: return {"intent":"其他","confidence":0.0}`,即调用没成功。
         旧版只看 ①,于是 402 时 188/188 条"降级"被当成 188 条真实"其他"预测,
         一路算出 macro-F1 0.0214 写进报告。
    """
    gate = _Gate(min(10, len(items)))

    async def one(it):
        last = None
        for attempt in range(retries):
            async with sem:
                try:
                    r = await classify(it["query"], it.get("history", ""))
                    if is_degraded(r):
                        raise RuntimeError("degraded: classify 走了解析失败兜底(其他+0.0)")
                    gate.record(True)
                    return {**it, "got": r.get("intent"), "conf": r.get("confidence"),
                            "degraded": False, "err": False}
                except FatalUpstream:
                    raise
                except Exception as e:  # noqa: BLE001
                    st = _status_of(e)
                    if st in FATAL_STATUS:
                        raise FatalUpstream(f"上游 HTTP {st}({type(e).__name__})") from e
                    last = e
            await asyncio.sleep(1.5 * (attempt + 1))
        gate.record(False)
        _ERRS.append(f"{tag}:{it['id']}")
        return {**it, "got": None, "conf": None, "degraded": False, "err": True,
                "error": type(last).__name__ if last else "unknown"}
    log(f"  跑 {tag}:{len(items)} 条 ...")
    return await asyncio.gather(*(one(i) for i in items))


async def main():
    log("=" * 84)
    log("实验D:意图识别评估重建")
    log("=" * 84)

    items = [json.loads(ln) for ln in open(EVALSET, encoding="utf-8")]
    per = Counter(i["intent"] for i in items)
    log(f"新评估集:{len(items)} 条 · 九类分布 " +
        " ".join(f"{k}={v}" for k, v in sorted(per.items(), key=lambda kv: -kv[1])))
    log(f"  其中:无歧义 {sum(1 for i in items if not i.get('ambiguous'))} 条 · "
        f"边界样例 {sum(1 for i in items if i.get('ambiguous'))} 条 · "
        f"多轮漂移 {sum(1 for i in items if 'drift' in i.get('tags', []))} 条")

    sem = asyncio.Semaphore(CONCURRENCY)
    res = await run_set(items, sem, "新评估集")

    # ---------- 指标(上游失败条目不进任何指标) ----------
    all_ids = [i["id"] for i in items]
    err_ids = {r["id"] for r in res if r["err"]}
    res_ok = [r for r in res if not r["err"]]
    by_id = {r["id"]: r for r in res}
    cm = defaultdict(lambda: defaultdict(int))
    for r in res_ok:
        cm[r["intent"]][r["got"]] += 1
    pf = prf(cm, list(INTENTS))

    def acc(subset):
        s = [r for r in res_ok if r["id"] in subset]
        return (sum(1 for r in s if r["got"] == r["intent"]) / len(s), len(s)) if s else (0.0, 0)

    hard = {i["id"] for i in items if not i.get("ambiguous")}
    amb = {i["id"] for i in items if i.get("ambiguous")}
    drift = {i["id"] for i in items if "drift" in i.get("tags", [])}

    log("\n" + "-" * 84)
    log("① 整体指标")
    log("-" * 84)
    for label, subset in (("全部", set(all_ids)), ("无歧义子集(主指标)", hard),
                          ("边界样例(单列)", amb), ("多轮漂移", drift)):
        a, n = acc(subset)
        log(f"  {label:<22}{sum(1 for r in res_ok if r['id'] in subset and r['got']==r['intent'])}/"
            f"{n} = {a:>7.1%}")
    macro_f1 = statistics.mean(v["f1"] for v in pf.values())
    log(f"  {'macro-F1(九类平均)':<22}{macro_f1:>7.4f}")
    deg_e = sum(1 for r in res if r["err"] and str(r.get("error", "")).startswith("degraded"))
    log(f"  {'模型真的答其他+0.0':<22}{sum(1 for r in res_ok if r['degraded']):>7} 条"
        "  ← 该签名只由调用失败产生,已按失败处理")
    if err_ids:
        log(f"  {'上游失败(不进指标)':<22}{len(err_ids):>7} 条  ← 重试 3 次仍失败,整条丢弃")
        log(f"    其中 classify 兜底降级 {deg_e} 条 · 抛异常 {len(err_ids) - deg_e} 条")

    log("\n" + "-" * 84)
    log("② 逐类 P/R/F1")
    log("-" * 84)
    log(f"  {'意图':<10}{'P':>8}{'R':>8}{'F1':>8}{'support':>9}")
    for lb in INTENTS:
        v = pf[lb]
        log(f"  {lb:<10}{v['p']:>8.3f}{v['r']:>8.3f}{v['f1']:>8.3f}{v['support']:>9}")

    log("\n" + "-" * 84)
    log("③ 混淆矩阵(行=期望,列=预测;只显示非零格)")
    log("-" * 84)
    for lb in INTENTS:
        hits = {k: v for k, v in cm[lb].items() if v}
        if hits:
            wrong = {k: v for k, v in hits.items() if k != lb}
            log(f"  {lb:<10}正确 {cm[lb][lb]:>2}" +
                ("  误判为: " + ", ".join(f"{k}×{v}" for k, v in wrong.items()) if wrong else ""))

    # ---------- 旧 18 条对照 ----------
    log("\n" + "-" * 84)
    log("④ 旧 18 条评估集对照(量化 few-shot 污染的虚高)")
    log("-" * 84)
    old_items = [{"id": f"OLD{i:02d}", "query": q, "intent": e, "pollution": p, "tags": []}
                 for i, (q, e, p) in enumerate(OLD_SAMPLES, 1)]
    old_res = [o for o in await run_set(old_items, sem, "旧评估集") if not o["err"]]
    n_old = len(old_res)
    n_clean = sum(1 for o in old_res if o["pollution"] is None)
    clean_hit = sum(1 for o in old_res if o["pollution"] is None and o["got"] == o["intent"])
    all_hit = sum(1 for o in old_res if o["got"] == o["intent"])
    n_pol = n_old - n_clean
    pol_hit = all_hit - clean_hit
    log(f"  旧集有效 {n_old} 条:{all_hit}/{n_old} = {all_hit/n_old:.1%}")
    if n_clean:
        log(f"  其中**未污染**的 {n_clean} 条:{clean_hit}/{n_clean} = {clean_hit/n_clean:.1%}")
    if n_pol:
        log(f"  被 few-shot 污染的 {n_pol} 条:{pol_hit}/{n_pol} = {pol_hit/n_pol:.1%}")
        gap = pol_hit / n_pol - clean_hit / n_clean if n_clean else 0.0
        log(f"  → 污染组与干净组差 {gap:+.1%}")
        if gap > 0.02:
            log("     结论:污染组明显更高,旧集确实存在'背过答案'带来的虚高。")
        elif gap < -0.02:
            log("     结论:污染组反而更低 —— 与'背过答案'假设相反,虚高不成立。")
        else:
            log("     结论:两组无差异 → **污染假设被实测否定**:与 few-shot 重合的句子本身就在")
            log("     模型能力范围内,旧集接近满分不是'背答案'背出来的。旧集真正的问题是"
                "样本量小 + 难度缺失")
            log("     (18 条、每类仅 2 条、无边界/否定/省略样例),而非答案泄漏 —— 换 188 条独立集后"
                f"同一模型为 {acc(hard)[0]:.1%}。")

    # ---------- 重放一致性 ----------
    log("\n" + "-" * 84)
    log(f"⑤ 重放一致性(抽 {REPLAY_N} 条 × 各 {REPLAY_TIMES} 次)")
    log("-" * 84)
    subset = [i for i in items if not i.get("ambiguous")][:REPLAY_N]
    passes = [[by_id[it["id"]] for it in subset]]      # 第 1 次已在上面的 res 里
    for t in range(REPLAY_TIMES - 1):
        passes.append(await run_set(subset, sem, f"重放第 {t+2} 次"))
    for pi, p in enumerate(passes):
        bad = sum(1 for r in p if r["err"])
        if bad:
            log(f"  ⚠ 第 {pi+1} 次重放有 {bad}/{len(subset)} 条重试后仍失败,这些标签已剔除"
                "(它们既不算'一致'也不算'抖动')")
    # 关键:只保留成功的标签。失败调用**不算"一次不同的回答"**,否则故障会被读成模型抖动。
    # 同时不因个别失败就把整条判为不可用 —— 那会连带丢掉本来有效的几次重放。
    runs = [[p[k]["got"] for p in passes if not p[k]["err"]] for k in range(len(subset))]
    usable = [k for k in range(len(subset)) if len(runs[k]) >= 2]   # 至少 2 次才能比
    avg_rep = statistics.mean(len(r) for r in runs) if runs else 0.0

    n_cmp = len(usable)
    unanimous = sum(1 for k in usable if len(set(runs[k])) == 1)
    stable_correct = sum(1 for k in usable
                         if len(set(runs[k])) == 1 and runs[k][0] == subset[k]["intent"])
    pairs_tot = pairs_agree = 0
    for k in usable:
        r = runs[k]
        for i in range(len(r)):
            for j in range(i + 1, len(r)):
                pairs_tot += 1
                pairs_agree += r[i] == r[j]
    dropped = sum(REPLAY_TIMES - len(r) for r in runs)
    if not n_cmp:
        log(f"  可比较条目 0 条:重放全部落在调用层失败上,本次一致性指标**不可用**"
            f"(这不是模型抖动,是调用没成功;丢弃标签 {dropped} 个)")
    else:
        log(f"  可比较条目:{n_cmp}/{len(subset)}"
            + ("" if n_cmp == len(subset) else "  ← 其余有效重放不足 2 次")
            + f" · 平均有效重放 {avg_rep:.2f}/{REPLAY_TIMES} 次"
            + (f" · 因失败丢弃标签 {dropped} 个" if dropped else ""))
        log(f"  {REPLAY_TIMES} 次完全一致:{unanimous}/{n_cmp} = {unanimous/n_cmp:.1%}")
        log(f"  其中'稳定且正确':{stable_correct}/{n_cmp} = {stable_correct/n_cmp:.1%}")
        log(f"  两两一致率:{pairs_agree}/{pairs_tot} = {pairs_agree/pairs_tot:.1%}")
    unstable = [(subset[k]["query"], runs[k]) for k in usable if len(set(runs[k])) > 1]
    if unstable:
        log("  抖动样例:")
        for q, r in unstable[:6]:
            log(f"    {q!r} → {r}")
    elif n_cmp:
        log("  没有抖动样例:同一句话多次调用结果稳定(可复现,不是随机判)")
    log("  读法:一致率高 = 该节点可复现,报告里的准确率不是运气;不稳定条目要单独看"
        "是否都落在同一对易混类上。")

    if _ERRS:
        log("\n" + "!" * 84)
        log(f"注意:本次运行有 {len(_ERRS)} 条调用重试 3 次仍失败,已从所有指标中剔除。")
        log("这些条目**不是**模型判错,也不是模型抖动;指标是在更小样本上算的,需重跑补齐。")
        log("!" * 84)

    OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
    OUT_TXT.write_text("\n".join(_LINES) + "\n", encoding="utf-8")
    log(f"\n报告已落 {OUT_TXT.relative_to(ROOT)}")
    return 1 if _ERRS else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()) or 0)
    except FatalUpstream as e:
        log("")
        log("!" * 84)
        log(f"实验中止:上游不可恢复故障 —— {e}")
        log("本次数字**不可用**:调用没成功,失败一旦被记成'其他'就会伪装成误判或抖动。")
        log("最常见原因:API 余额不足(402)或密钥失效(401)。充值/换 key 后重跑。")
        log("!" * 84)
        # 中止的运行写到单独文件:绝不能拿一份废掉的运行去覆盖已有的有效报告
        abort_path = OUT_TXT.with_name(OUT_TXT.stem + ".ABORTED.txt")
        abort_path.parent.mkdir(parents=True, exist_ok=True)
        abort_path.write_text("\n".join(_LINES) + "\n", encoding="utf-8")
        print(f"中止记录已落到 {abort_path.relative_to(ROOT)}(未触碰 {OUT_TXT.name})", flush=True)
        sys.exit(2)
