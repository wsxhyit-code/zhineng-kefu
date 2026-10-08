"""诊断:多轮漂移 8 条里到底哪条不稳/错,以及错因。

背景:全量跑(188 条)与验收闸门(44 条)两次独立运行都得到"多轮漂移 7/8",而首跑是 8/8。
两次独立复现同一条失败 ⇒ 更像**稳定的功能缺陷或题目本身有歧义**,不是模型随机抖动。
这直接决定闸门里"漂移必须 100%"这条断言是否成立。

做法:把 8 条漂移样例各跑 3 次,逐条打印期望/预测/历史,统计稳定性。
用法:python .diag_drift.py
"""
import asyncio
import json
import pathlib

from app.core.intent import classify

ROOT = pathlib.Path(__file__).resolve().parent
EVALSET = ROOT / "tests/data/eval_intent.jsonl"
TIMES = 3
CONCURRENCY = 4


async def main():
    items = [json.loads(ln) for ln in EVALSET.open(encoding="utf-8") if ln.strip()]
    drift = [i for i in items if "drift" in i.get("tags", [])]
    print("=" * 88)
    print(f"多轮漂移诊断:{len(drift)} 条 × {TIMES} 次")
    print("=" * 88)

    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(it):
        out = []
        for _ in range(TIMES):
            async with sem:
                r = await classify(it["query"], it.get("history", ""))
            out.append((r.get("intent"), r.get("confidence")))
        return it, out

    results = await asyncio.gather(*(one(i) for i in drift))

    for it, out in results:
        labels = [g for g, _ in out]
        stable = len(set(labels)) == 1
        correct = labels[0] == it["intent"]
        mark = "✅" if (stable and correct) else ("⚠️ " if not stable else "❌")
        print(f"\n{mark} {it['id']}  期望={it['intent']}  预测={labels}")
        print(f"    query  : {it['query']!r}")
        print(f"    历史    : {it.get('history', '(无)')!r}")
        if it.get("note"):
            print(f"    出题意图: {it['note']}")

    print("\n" + "=" * 88)
    bad = [(it, out) for it, out in results
           if len({g for g, _ in out}) > 1 or out[0][0] != it["intent"]]
    print(f"有问题的条目:{len(bad)}/{len(drift)}")
    for it, out in bad:
        print(f"  {it['id']} 期望={it['intent']} 预测={[g for g, _ in out]}")


if __name__ == "__main__":
    asyncio.run(main())
