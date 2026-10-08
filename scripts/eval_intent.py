"""意图验收2:意图识别真实闸门(九类判对率 + JSON 合法 + 怪问题落"其他" + 多轮漂移)。

原版(2026-09 前)的四个缺陷,本版逐个修掉:
  1. 样本写死在脚本里,只有 18 条(九类各 2 条),其中 6 条与 INTENT_CLASSIFY_SYSTEM 的
     few-shot 逐字重合 —— 测的是"模型记没记住 prompt 里的例子"。现读 tests/data/eval_intent.jsonl
     (188 条独立集,避开 few-shot 原句);
  2. 只看计数,没有逐类 P/R/F1、没有混淆矩阵 —— 看不出弱在哪一类。现全给;
  3. 多轮漂移那条只 print,pased 不加、退出码不受影响 —— "测过"是给人眼看的。现真断言;
  4. **最严重**:上游调用失败时 app/core/intent.py 的 except 兜底返回
     {"intent":"其他","confidence":0.0},而"其他"是合法标签、0.0 是合法 confidence,
     旧校验(九类之一 + [0,1] 的 float)全部通过 —— 于是"期望其他"的样本在解析失败时
     被判为答对,而**整段上游挂掉会算出一个接近 0 的准确率却不报错**。
     实测:2026-09-22 上游 402(余额不足)时 188/188 条走兜底,旧逻辑毫无反应。
     现把(其他+0.0)一律判为**调用失败**:重试 → 仍失败则剔除并退出码非 0;
     前 10 次全失败直接熔断退出 2。

用法:
  python -m scripts.eval_intent            # 验收子集(边界样例 + "其他"类 + 多轮漂移)
  python -m scripts.eval_intent --full     # 全量 188 条
"""
import argparse
import asyncio
import json
import pathlib
import statistics
import sys
from collections import Counter, defaultdict

from app.core.intent import INTENTS, classify

ROOT = pathlib.Path(__file__).resolve().parent.parent
EVALSET = ROOT / "tests/data/eval_intent.jsonl"
OUT_TXT = ROOT / "data/intent/reports/intent_acceptance.txt"

CONCURRENCY = 5
RETRIES = 3
FATAL_STATUS = (401, 402, 403)          # 余额/鉴权:重试无用

# 验收阈值(实测值见 data/intent/reports/intent_eval.txt,阈值留了余量)
MIN_ACC_MAIN = 0.85                     # 无歧义子集(=主指标):两次独立运行都是 161/170 = 94.7%
MIN_OTHER_RECALL = 0.80                 # 怪问题必须落"其他":实测 0.850 / 0.900
MIN_DRIFT_ACC = 0.875                   # 多轮漂移 ≥7/8。**不能要求 100%**:
#   实测跨运行范围是 8/8 → 7/8 → 8/8(见 .diag_drift.py:8 条各跑 3 次全部稳定正确)。
#   漂移/边界这类小样本难题会整体漂 ±1 条,拿 100% 当闸门等于让闸门间歇性误报 ——
#   那比没有闸门更糟,因为人会开始忽略它。≥7/8 能挡住真回归(≤6/8)又不假报。
# 边界样例故意做难(歧义/否定/省略),只统计不设闸 —— 拿它去卡阈值等于逼模型猜。
#   证据:边界样例跨运行 13/18 (72.2%) → 14/18 (77.8%),而主指标两次都是 94.7%。

_LINES: list[str] = []


def log(msg=""):
    print(msg, flush=True)
    _LINES.append(msg)


class FatalUpstream(RuntimeError):
    pass


class _Gate:
    """前 N 次全失败 → 上游不可用,立即熔断(兜底签名让调用层看不到异常,必须自己数)。"""

    def __init__(self, n: int):
        self.n, self.done, self.failed = n, 0, 0

    def record(self, ok: bool):
        self.done += 1
        self.failed += 0 if ok else 1
        if self.done >= self.n and self.failed == self.done:
            raise FatalUpstream(f"前 {self.done} 次调用全部失败,判定上游不可用,立即熔断")


def is_degraded(r: dict) -> bool:
    """兜底签名。confidence==0.0 只由 intent.classify 的 except 分支产生。"""
    return r.get("intent") == "其他" and float(r.get("confidence") or 0.0) == 0.0


def _status_of(exc: BaseException):
    st = getattr(exc, "status_code", None)
    if st is None:
        st = getattr(getattr(exc, "response", None), "status_code", None)
    return int(st) if isinstance(st, int) else None


async def classify_all(items, sem):
    """逐条分类。失败不伪装成标签:got=None / err=True,由调用方丢弃。"""
    gate = _Gate(min(10, len(items)))

    async def one(it):
        last = None
        for attempt in range(RETRIES):
            async with sem:
                try:
                    r = await classify(it["query"], it.get("history", ""))
                    if is_degraded(r):
                        raise RuntimeError("degraded: classify 走了 except 兜底(其他+0.0)")
                    gate.record(True)
                    return {**it, "got": r.get("intent"), "conf": r.get("confidence"), "err": False}
                except FatalUpstream:
                    raise
                except Exception as e:  # noqa: BLE001
                    if _status_of(e) in FATAL_STATUS:
                        raise FatalUpstream(f"上游 HTTP {_status_of(e)}({type(e).__name__})") from e
                    last = e
            await asyncio.sleep(1.5 * (attempt + 1))
        gate.record(False)
        return {**it, "got": None, "conf": None, "err": True,
                "error": type(last).__name__ if last else "unknown"}
    return await asyncio.gather(*(one(i) for i in items))


def prf(cm, labels):
    out = {}
    for lb in labels:
        tp = cm[lb][lb]
        fp = sum(cm[o][lb] for o in labels if o != lb)
        fn = sum(cm[lb][p] for p in labels if p != lb)
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        out[lb] = {"p": p, "r": r, "f1": 2 * p * r / (p + r) if p + r else 0.0, "n": tp + fn}
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="跑全部 188 条(默认只跑验收子集)")
    args = ap.parse_args()

    items = [json.loads(ln) for ln in EVALSET.open(encoding="utf-8") if ln.strip()]
    if not args.full:
        items = [i for i in items
                 if i.get("ambiguous") or i["intent"] == "其他" or "drift" in i.get("tags", [])]

    log("=" * 84)
    log("意图验收2:意图识别(九类 + JSON + 怪问题落其他 + 多轮漂移)")
    log("=" * 84)
    log(f"评估集 {EVALSET.relative_to(ROOT)} · 本次 {len(items)} 条"
        f"{'(全量)' if args.full else '(验收子集;--full 跑全量)'} · 上游 {_upstream_label()}")
    log("")

    sem = asyncio.Semaphore(CONCURRENCY)
    res = await classify_all(items, sem)
    ok_res = [r for r in res if not r["err"]]
    errs = [r for r in res if r["err"]]

    cm = defaultdict(lambda: defaultdict(int))
    for r in ok_res:
        cm[r["intent"]][r["got"]] += 1
    pf = prf(cm, list(INTENTS))

    def acc(pred) -> tuple[float, int, int]:
        s = [r for r in ok_res if pred(r)]
        hit = sum(1 for r in s if r["got"] == r["intent"])
        return (hit / len(s) if s else 0.0), hit, len(s)

    a_all, hit_all, n_all = acc(lambda r: True)
    a_main, hit_main, n_main = acc(lambda r: not r.get("ambiguous"))
    a_amb, hit_amb, n_amb = acc(lambda r: r.get("ambiguous"))
    a_drift, hit_drift, n_drift = acc(lambda r: "drift" in r.get("tags", []))
    other_r = pf["其他"]["r"]
    macro_f1 = statistics.mean(v["f1"] for v in pf.values())

    log("-" * 84)
    log("结果")
    log("-" * 84)
    log(f"  无歧义子集(主指标) {hit_main}/{n_main} = {a_main:.1%}")
    log(f"  整体(含边界)   {hit_all}/{n_all} = {a_all:.1%}")
    log(f"  边界样例      {hit_amb}/{n_amb} = {a_amb:.1%}   (故意做难,单列不设闸)")
    log(f"  多轮漂移      {hit_drift}/{n_drift} = {a_drift:.1%}")
    log(f"  macro-F1      {macro_f1:.4f}")
    log(f"  怪问题召回(其他) {other_r:.3f}")
    log("")
    log(f"  {'意图':<10}{'P':>8}{'R':>8}{'F1':>8}{'support':>9}")
    for lb in INTENTS:
        v = pf[lb]
        log(f"  {lb:<10}{v['p']:>8.3f}{v['r']:>8.3f}{v['f1']:>8.3f}{v['n']:>9}")
    log("")
    wrong = [(lb, k, v) for lb in INTENTS for k, v in cm[lb].items() if v and k != lb]
    if wrong:
        log("  主要混淆(期望→预测):")
        for lb, k, v in sorted(wrong, key=lambda t: -t[2])[:6]:
            log(f"    {lb} → {k} ×{v}")
    else:
        log("  无混淆")

    # ---------- 断言 ----------
    checks: list[tuple[str, bool, str]] = []
    checks.append(("全部调用成功(无兜底/无异常)",
                   not errs, f"失败 {len(errs)} 条" + (f": {[e['id'] for e in errs[:5]]}" if errs else "")))
    checks.append(("confidence 均为 [0,1] 的 float",
                   all(isinstance(r["conf"], float) and 0.0 <= r["conf"] <= 1.0 for r in ok_res),
                   "越界 " + str([r["id"] for r in ok_res
                                  if not (isinstance(r["conf"], float) and 0.0 <= r["conf"] <= 1.0)])))
    checks.append(("预测值均为九类之一",
                   all(r["got"] in INTENTS for r in ok_res),
                   "越界 " + str([r["id"] for r in ok_res if r["got"] not in INTENTS])))
    checks.append((f"无歧义子集准确率 ≥ {MIN_ACC_MAIN:.2f}", a_main >= MIN_ACC_MAIN,
                   f"实测 {a_main:.1%}(边界样例 {a_amb:.1%} 不计入闸门)"))
    checks.append((f"怪问题落'其他'召回 ≥ {MIN_OTHER_RECALL:.2f}",
                   other_r >= MIN_OTHER_RECALL, f"实测 {other_r:.3f}"))
    if n_drift:
        checks.append((f"多轮漂移 ≥ {MIN_DRIFT_ACC:.1%}", a_drift >= MIN_DRIFT_ACC,
                       f"实测 {hit_drift}/{n_drift}(不设 100%:该子集跨运行会漂 ±1 条)"))

    log("")
    log("-" * 84)
    log("验收")
    log("-" * 84)
    failed = 0
    for name, ok, detail in checks:
        log(f"  {'✅' if ok else '❌'} {name} —— {detail}")
        failed += not ok
    log("")
    log(f"  结论:{'全部通过' if not failed else f'{failed} 项不通过'}"
        f"  (阈值见 scripts/eval_intent.py 顶部常量)")

    OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
    OUT_TXT.write_text("\n".join(_LINES) + "\n", encoding="utf-8")
    log(f"  报告:{OUT_TXT.relative_to(ROOT)}")
    return 1 if failed else 0


def _upstream_label() -> str:
    try:
        from app.config import settings
        base = (settings.chat_base_url or "").split("//")[-1].split("/")[0]
        return f"{settings.intent_model or settings.chat_model} @ {base}"
    except Exception:  # noqa: BLE001
        return "(配置未读取)"


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except FatalUpstream as e:
        print("!" * 84, flush=True)
        print(f"验收中止:上游不可恢复故障 —— {e}", flush=True)
        print("本次结果不可用:失败的调用会被兜底成'其他',不区分就会伪装成误判。", flush=True)
        print("最常见原因:API 余额不足(402)或密钥失效(401)。", flush=True)
        print("!" * 84, flush=True)
        sys.exit(2)
