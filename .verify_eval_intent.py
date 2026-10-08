"""桩测 scripts/eval_intent.py 的闸门逻辑(不调上游,不花钱)。

验四件事:
  A 全对          → 退出 0,检查项全绿
  B 全降级(402 签名) → 熔断,退出 2,且不产出验收报告
  C 个别调用失败    → 那些条目被剔除(不进指标),第 1 项检查不通过,退出 1
  D 全预测同一类    → 准确率崩,退出 1(确认指标真的在算,而不是恒真)
"""
import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from scripts import eval_intent as E   # noqa: E402

# 桩测的产物一律写 .exp_cache/:假数据绝不能落在 data/intent/reports/ 里冒充真结果。
# (放工作区内是为了让 OUT_TXT.relative_to(ROOT) 仍然成立,不要用系统临时目录)
E.OUT_TXT = E.ROOT / ".exp_cache/stub_intent_acceptance.txt"

items = [json.loads(ln) for ln in E.EVALSET.open(encoding="utf-8") if ln.strip()]
subset = [i for i in items
          if i.get("ambiguous") or i["intent"] == "其他" or "drift" in i.get("tags", [])]


def run_case(name, fn, expect_code):
    E._LINES.clear()
    E.classify = fn
    sys.argv = ["eval_intent"]           # 不传 --full,走验收子集
    try:
        code = asyncio.run(E.main())
    except E.FatalUpstream:
        code = 2
    ok = code == expect_code
    print(f"{'✅' if ok else '❌'} {name}:退出码 {code}(期望 {expect_code})")
    return ok


async def perfect(q, h=""):
    e = {it["query"]: it["intent"] for it in subset}
    return {"intent": e.get(q, "其他"), "confidence": 0.9}


async def degraded(q, h=""):
    return {"intent": "其他", "confidence": 0.0}


_fail_ids = {subset[0]["id"], subset[1]["id"], subset[2]["id"]}
_map = {it["query"]: it["id"] for it in subset}


async def few_fail(q, h=""):
    if _map.get(q) in _fail_ids:
        raise RuntimeError("模拟上游 5xx")
    return {"intent": {it["query"]: it["intent"] for it in subset}.get(q, "其他"), "confidence": 0.9}


async def all_one_class(q, h=""):
    return {"intent": "物流", "confidence": 0.9}


print("=" * 84)
print("桩测:scripts/eval_intent.py 闸门逻辑(验收子集 %d 条)" % len(subset))
print("=" * 84)
results = [
    run_case("A 全对", perfect, 0),
    run_case("B 全降级(402 签名)", degraded, 2),
    run_case("C 前 3 条调用失败", few_fail, 1),
    run_case("D 全预测物流", all_one_class, 1),
]
print("=" * 84)
print("结论:" + ("四场景全部符合预期,闸门有效" if all(results) else "有场景不符合预期"))
sys.exit(0 if all(results) else 1)
