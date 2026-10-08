"""桩测 .exp_intent.py 的重放一致性段(零 API 调用)。

为什么单独测这一段:2026-09-22 首跑时,上游 402 让第 3 次重放的 30 条全部走 classify 的
except 兜底(其他+0.0),脚本把它读成"3 次一致 0%、两两一致 33.3%",并打印成"模型抖动"。
修复后必须证明:故障标签会被剔除,而不是被当成"一次不同的回答"。

四个场景写成五组(第 3 组拆成"整批失败"与"部分失败"两种,退出码不同):
  A 全对且稳定        → 一致率 100%,无抖动样例
  B 某次重放**整批**降级 → 判上游不可用,熔断退出 2,**不产出正式报告**;
                          关键:绝不把故障算成"模型抖动 0%"
  B2 某次重放**部分**失败(3/6) → 不熔断:失败标签剔除、退回有效重放算一致率、退出 1
  C 真有抖动(2 条翻转) → 一致率下降、抖动样例列出(确认指标真的在算,不是恒真)
  D 上游全挂          → 熔断退出 2,写 ABORTED 文件,不覆盖 intent_eval.txt

桩测产物写 .exp_cache/,绝不落进 data/intent/reports/。
"""
import asyncio
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("exp_intent", ROOT / ".exp_intent.py")
E = importlib.util.module_from_spec(spec)
spec.loader.exec_module(E)

E.out_guard = True
E.OUT_TXT = ROOT / ".exp_cache/stub_intent_eval.txt"
E.REPLAY_N = 6
E.CONCURRENCY = 8

_items = [__import__("json").loads(ln) for ln in E.EVALSET.open(encoding="utf-8") if ln.strip()]
_truth = {it["query"]: it["intent"] for it in _items}


async def honest(q, h=""):
    return {"intent": _truth.get(q, "其他"), "confidence": 0.9}


async def degraded(q, h=""):
    return {"intent": "其他", "confidence": 0.0}


async def dead(q, h=""):
    raise RuntimeError("模拟上游不可用")


_orig_run_set = E.run_set
# base = 所有 tag 的基准桩(保证任何场景都不真连网络);by_tag = 只对某一轮替换
_CTX = {"base": None, "by_tag": {}}


async def hooked_run_set(items, sem, tag, retries=3):
    fn = _CTX["by_tag"].get(tag, _CTX["base"])
    assert fn is not None, f"tag {tag!r} 没有桩,会真的打网络!"
    saved = E.classify
    E.classify = fn
    try:
        return await _orig_run_set(items, sem, tag, retries)
    finally:
        E.classify = saved


E.run_set = hooked_run_set


def report_text():
    """优先读报告文件;若本轮熔断未落盘,退回累积的打印行(便于断言中止前的输出)。"""
    p = E.OUT_TXT
    return p.read_text(encoding="utf-8") if p.exists() else "\n".join(E._LINES)


def run_case(name, base, by_tag, expect_code, checks):
    _CTX["base"], _CTX["by_tag"] = base, by_tag
    E._LINES.clear()
    E._ERRS.clear()
    E.OUT_TXT.unlink(missing_ok=True)
    try:
        code = asyncio.run(E.main()) or 0
    except E.FatalUpstream:
        code = 2
    txt = report_text()
    wrote = E.OUT_TXT.exists()
    ok = code == expect_code
    detail = []
    for label, pred in checks:
        good = pred(txt, wrote)
        ok = ok and good
        detail.append(f"{'✓' if good else '✗'}{label}")
    print(f"{'✅' if ok else '❌'} {name}(退出码 {code},期望 {expect_code})")
    print(f"     {' · '.join(detail)}")
    return ok


print("=" * 84)
print(f"桩测:.exp_intent.py 重放一致性段(重放子集 {E.REPLAY_N} 条 × {E.REPLAY_TIMES} 次)")
print("=" * 84)

results = []

# A 全对且稳定
results.append(run_case(
    "A 全对且稳定", honest, {}, 0, [
        ("一致率100%", lambda t, w: f"完全一致:{E.REPLAY_N}/{E.REPLAY_N} = 100.0%" in t),
        ("无抖动样例", lambda t, w: "没有抖动样例" in t),
        ("平均有效重放 3.00/3", lambda t, w: "平均有效重放 3.00/3" in t),
    ]))

# B 第 3 次重放**整批**降级(重现 402 事故):整批全失败 ⇒ 熔断判上游不可用 ⇒ 退出 2,
#   且**绝不把故障算成"模型抖动"**。关键断言:① 主评估集已完成 ② 正式报告不落盘。
results.append(run_case(
    "B 第3次重放整批降级(重现402事故)", honest, {"重放第 3 次": degraded}, 2, [
        ("主评估集已完成(188 条真实桩结果)",
         lambda t, w: "全部                    188/188 =  100.0%" in t),
        ("进入 ⑤ 段", lambda t, w: "⑤ 重放一致性" in t),
        ("**熔断,不产出正式报告**", lambda t, w: not w),
        ("**绝不出现故障伪装成的 0%**",
         lambda t, w: "完全一致:0/" not in t and "= 0.0%" not in t),
    ]))

# B2 第 3 次重放**部分**失败(3/6):不熔断,失败标签剔除,退回有效重放算一致率,退出 1
_half_qs = [i["query"] for i in _items if not i.get("ambiguous")][:3]


async def half_degraded(q, h=""):
    if q in _half_qs:
        return {"intent": "其他", "confidence": 0.0}
    return {"intent": _truth.get(q, "其他"), "confidence": 0.9}


results.append(run_case(
    "B2 第3次重放部分失败(3/6)", honest, {"重放第 3 次": half_degraded}, 1, [
        ("如实警告 3/6 条失败", lambda t, w: "⚠ 第 3 次重放有 3/6 条重试后仍失败" in t),
        ("可比较条目仍 6/6", lambda t, w: "可比较条目:6/6" in t),
        ("平均有效重放 2.50/3", lambda t, w: "平均有效重放 2.50/3" in t),
        ("丢弃 3 个标签", lambda t, w: "因失败丢弃标签 3 个" in t),
        ("一致率 100%(基于有效重放)",
         lambda t, w: f"完全一致:{E.REPLAY_N}/{E.REPLAY_N} = 100.0%" in t),
        ("仍落盘正式报告(带提醒横幅)", lambda t, w: w and "条调用重试 3 次仍失败" in t),
    ]))

# C 真有抖动:第 2 次重放把 2 条的标签翻转
_flip_qs = [i["query"] for i in _items if not i.get("ambiguous")][:2]


async def jittery(q, h=""):
    if q in _flip_qs:
        return {"intent": "闲聊" if _truth.get(q) != "闲聊" else "物流", "confidence": 0.9}
    return {"intent": _truth.get(q, "其他"), "confidence": 0.9}


results.append(run_case(
    "C 真实抖动(2 条翻转)", honest, {"重放第 2 次": jittery}, 0, [
        ("一致条目降到 4/6", lambda t, w: f"完全一致:4/{E.REPLAY_N} = 66.7%" in t),
        ("两两一致率 14/18 = 77.8%", lambda t, w: "两两一致率:14/18 = 77.8%" in t),
        ("列出 2 条抖动样例", lambda t, w: "抖动样例:" in t and t.count("→ [") == 2),
    ]))

# D 上游全挂 → 熔断
results.append(run_case(
    "D 上游全挂(熔断)", dead, {}, 2, [
        ("未产出正式报告", lambda t, w: not w),
    ]))

print("=" * 84)
print("结论:" + ("五场景全部符合预期,重放段可安全用于真实重跑"
                if all(results) else "有场景不符合预期,别急着跑真实验"))
sys.exit(0 if all(results) else 1)
