"""上下文关键信息保留率 A/B 实验。

要解决的问题:「长对话关键信息保留率提升 50%」这个说法在仓库里**找不到任何出处**
(全仓 grep 保留率/关键信息/50% 只命中 CSS),而 上下文的现有验收只有 3 个样例、且有断言缺口。
本脚本用**真实的 上下文代码路径**把"保留率"测出来。

三组对照(单变量):
  A 现状   = 滚动摘要 + 锚点滑窗(8 轮 / 3000 token)        ← 生产路径
  B 基线   = 纯滑窗(同一个窗口,但不注入摘要)                ← 摘要层的净贡献
  C 上限   = 全量历史(不摘要、不切窗)                       ← 理论上界

两个指标:
  指标1(确定性·不调模型):关键事实是否出现在拼装后的上下文字符串里
  指标2(端到端):把拼装好的上下文喂给模型,问一个必须用到早期事实的问题,看答案里有没有

设计:12 段合成长对话 × 30 轮(60 条消息),每段在**第 3~6 轮**(早期、必然滑出 8 轮窗口)
埋 5 个关键事实 → 共 60 个事实槽位,分辨率 1.7%。

只读真实实现(app/core/memory.py、app/core/summarizer.py),不修改任何现有代码。
用法:python .exp_context.py
"""
import asyncio
import pathlib
import sys
from dataclasses import dataclass

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.config import settings
from app.core import summarizer
from app.core.llm import get_chat_model
from app.core.memory import build_window, summary_system
from app.graph.nodes import AGENT_SYSTEM

ROOT = pathlib.Path(__file__).resolve().parent
OUT_TXT = ROOT / "data/flywheel/reports/context_retention.txt"

N_DIALOGUES = 12
TURNS = 30                     # 30 轮 = 60 条消息
CONCURRENCY = 4

_LINES: list[str] = []


def log(msg=""):
    print(msg, flush=True)
    _LINES.append(msg)


def norm(s: str) -> str:
    """去空白与常见分隔符,兼容 '138 0013 8000' / '138-0013-8000' 这类写法。"""
    return "".join(ch for ch in (s or "") if ch not in " \t\n-—、,，.。")


@dataclass
class Row:
    """模拟 repository.list_dialog_messages 的行(id/role/content)。"""
    id: int
    role: str
    content: str


# ---------------- 合成对话 ----------------
ADDRESSES = [{"text": "深圳市南山区科技园", "check": ["深圳"]},
             {"text": "杭州市西湖区文三路", "check": ["杭州"]},
             {"text": "成都市武侯区天府大道", "check": ["成都"]},
             {"text": "上海市浦东新区张江", "check": ["上海"]}]
COURIERS = [{"text": "顺丰", "check": ["顺丰"]},
            {"text": "京东物流", "check": ["京东"]},
            {"text": "中通", "check": ["中通"]},
            {"text": "圆通", "check": ["圆通"]}]
# 事实用"埋入原文 + 判据关键词"两段式:长句模型会改写,逐字比对会误判成"没保留"
SPECIALS = [{"text": "我对猫毛过敏,别给我推荐长毛猫", "check": ["过敏"]},
            {"text": "我家有小孩,别推荐带小零件的", "check": ["小孩"]},
            {"text": "我需要发票抬头开公司名", "check": ["发票", "公司"]},
            {"text": "我住六楼没电梯,大件要送货上门", "check": ["六楼"]}]

FACT_LABELS = ["订单号", "手机号", "特别诉求", "收货地址", "快递偏好"]
QUESTIONS = {
    "订单号": "我们最开始聊的那个订单号是多少?",
    "手机号": "我留给你们的手机号是多少?",
    "特别诉求": "我之前特别提过的那个要求是什么?",
    "收货地址": "我留的收货地址是哪里?",
    "快递偏好": "我说过希望用哪家快递?",
}


def build_dialogue(d: int) -> tuple[list[Row], dict[str, dict]]:
    """造一段 30 轮对话;第 3~6 轮埋 5 个事实。返回 {标签: {"text": 埋入原文, "check": 判据关键词}}"""
    sp = SPECIALS[d % len(SPECIALS)]
    ad = ADDRESSES[d % len(ADDRESSES)]
    co = COURIERS[d % len(COURIERS)]
    order_no, phone = str(1000 + d), f"1380013{8000 + d}"
    facts = {
        "订单号": {"text": order_no, "check": [order_no]},
        "手机号": {"text": phone, "check": [phone]},
        "特别诉求": sp,
        "收货地址": ad,
        "快递偏好": co,
    }
    rows: list[Row] = []
    mid = 1

    def add(role: str, content: str):
        nonlocal mid
        rows.append(Row(id=mid, role=role, content=content))
        mid += 1

    # 第 1~2 轮:普通寒暄
    add("user", "你好,我想咨询一下猫砂盆")
    add("assistant", "您好~我是喵喵优选的智能客服小喵,猫砂盆我们有几款,您想看哪方面?")
    add("user", "先随便看看")
    add("assistant", "好的,有需要随时问我~")
    # 第 3~6 轮:埋事实
    t_ = {k: v["text"] for k, v in facts.items()}
    add("user", f"我的订单号是 {t_['订单号']},帮我记一下")
    add("assistant", f"好的,已记录订单号 {t_['订单号']}。")
    add("user", f"我的手机号是 {t_['手机号']},有事打这个")
    add("assistant", f"好的,手机号 {t_['手机号']} 已记下。")
    add("user", f"另外补充一下:{t_['特别诉求']}")
    add("assistant", f"明白,我记下了:{t_['特别诉求']}。")
    add("user", f"收货地址是{t_['收货地址']}")
    add("assistant", f"好的,收货地址{t_['收货地址']} 已记录。")
    add("user", f"对了,我希望用{t_['快递偏好']}发货")
    add("assistant", f"好的,已备注优先用{t_['快递偏好']}。")
    # 第 7~29 轮:与事实无关的闲聊,把早期信息推出 8 轮窗口
    topics = ["猫砂盆的容量", "猫粮的保质期", "猫抓板的材质", "猫窝的尺寸", "猫爬架的高度",
              "自动喂食器", "逗猫棒", "猫包", "饮水机", "驱虫药", "猫薄荷", "玩具球",
              "猫砂的种类", "除臭剂", "指甲剪", "梳毛刷", "猫窝垫子", "项圈", "零食",
              "益生菌", "化毛膏", "猫草"]
    t = 0
    while len(rows) < (TURNS - 1) * 2:
        topic = topics[t % len(topics)]
        add("user", f"{topic}怎么样?有什么推荐吗?")
        add("assistant", f"{topic}我们有两款,价格分别是 59 和 129,都是现货。")
        t += 1
    # 末轮:提问
    add("user", "麻烦你把我们之前聊到的几个信息跟我核对一下:订单号、手机号、我提过的特别要求、收货地址,还有我说想用哪家快递。")
    return rows, facts


def to_lc(rows: list[Row]) -> list:
    """转成 LangChain 消息,HumanMessage.id 用 'db-{id}' 锚点约定。"""
    out = []
    for r in rows[:-1]:      # 末轮提问单独处理
        if r.role == "user":
            out.append(HumanMessage(r.content, id=f"db-{r.id}"))
        else:
            out.append(AIMessage(r.content, id=f"db-{r.id}"))
    return out


# ---------------- 三个 arm 的上下文拼装 ----------------
def assemble(arm: str, rows: list[Row], lc_msgs: list, summary: str,
             boundary: int | None, question: str):
    """按 arm 拼装上下文。
    A = 摘要 system 消息 + 锚点滑窗;  B = 只要滑窗(同窗口,去掉摘要);
    C = 全量历史(不摘要不切窗)。"""
    if arm == "C_全量历史":
        base = list(lc_msgs)
    else:
        base = build_window(lc_msgs, boundary or 0, settings.context_window_max_tokens)
    out = []
    if arm == "A_摘要+滑窗":
        sm = summary_system(summary)
        if sm is not None:
            out.append(sm)
    out.append(SystemMessage(AGENT_SYSTEM))
    out.extend(base)
    out.append(HumanMessage(question))
    return out


def context_text(msgs) -> str:
    parts = []
    for m in msgs:
        c = m.content if isinstance(m.content, str) else str(m.content)
        parts.append(c)
    return "\n".join(parts)


async def one_dialogue(d: int, sem: asyncio.Semaphore, model):
    rows, facts = build_dialogue(d)
    question = rows[-1].content
    lc_msgs = to_lc(rows)

    # 真实边界逻辑 + 真实摘要
    boundary = summarizer.compute_boundary(rows, settings.context_window_turns)
    if boundary is None:
        return {"d": d, "error": "boundary=None"}
    seg = [r for r in rows if 0 < r.id <= boundary]
    dialog = "\n".join(f"{'用户' if r.role == 'user' else '客服'}:{r.content}" for r in seg if r.content)
    async with sem:
        try:
            summary = await summarizer.summarize_dialog("", dialog)
        except Exception as e:  # noqa: BLE001
            return {"d": d, "error": f"摘要失败 {type(e).__name__}"}

    res = {"d": d, "summary": summary, "boundary": boundary, "facts": facts, "arms": {}}
    for arm in ("A_摘要+滑窗", "B_纯滑窗", "C_全量历史"):
        msgs = assemble(arm, rows, lc_msgs, summary, boundary, question)
        ctx = context_text(msgs)
        # 指标1:确定性——事实判据是否在上下文字面里(关键词全中才算)
        m1 = {k: all(tok in norm(ctx) for tok in v["check"]) for k, v in facts.items()}
        # 指标2:端到端——问模型
        async with sem:
            try:
                resp = await model.ainvoke(msgs)
                ans = resp.content if isinstance(resp.content, str) else str(resp.content)
            except Exception as e:  # noqa: BLE001
                ans = f"__ERROR__ {type(e).__name__}"
        m2 = {k: all(tok in norm(ans) for tok in v["check"]) for k, v in facts.items()}
        res["arms"][arm] = {"m1": m1, "m2": m2, "ctx_len": len(ctx), "answer": ans[:400]}
    return res


async def main():
    log("=" * 78)
    log("上下文关键信息保留率 A/B 实验")
    log("=" * 78)
    log(f"设计:{N_DIALOGUES} 段 × {TURNS} 轮,每段在第 3~6 轮埋 5 个关键事实")
    log(f"      → 共 {N_DIALOGUES*5} 个事实槽位(分辨率 {100/(N_DIALOGUES*5):.1f}%)")
    log(f"      上下文配置:摘要触发={settings.summary_trigger_messages} 条 · "
        f"滑窗={settings.context_window_turns} 轮 · token 上限={settings.context_window_max_tokens}")
    log(f"臂: A=摘要+滑窗(生产)  B=纯滑窗(基线)  C=全量历史(上限)")

    sem = asyncio.Semaphore(CONCURRENCY)
    model = get_chat_model()
    log(f"\n开始跑({N_DIALOGUES} 轮摘要是真实 LLM 调用)...")
    tasks = [one_dialogue(d, sem, model) for d in range(1, N_DIALOGUES + 1)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    ok = [r for r in results if isinstance(r, dict) and "error" not in r]
    bad = [r for r in results if not (isinstance(r, dict) and "error" not in r)]
    for b in bad:
        log(f"  跳过: {b if not isinstance(b, dict) else b.get('error')}")

    arms = ("A_摘要+滑窗", "B_纯滑窗", "C_全量历史")
    log("\n" + "-" * 78)
    log("结果:每个 arm 下 60 个事实槽位的保留情况")
    log("-" * 78)
    log(f"  {'臂':<16}{'指标1 上下文可寻址':>22}{'指标2 答案命中':>18}")
    agg = {a: {"m1": 0, "m2": 0, "n": 0} for a in arms}
    per_fact = {a: {k: [0, 0] for k in FACT_LABELS} for a in arms}
    for r in ok:
        for a in arms:
            for k in r["facts"]:
                agg[a]["m1"] += 1 if r["arms"][a]["m1"][k] else 0
                agg[a]["m2"] += 1 if r["arms"][a]["m2"][k] else 0
                agg[a]["n"] += 1
                per_fact[a][k][0] += 1 if r["arms"][a]["m2"][k] else 0
                per_fact[a][k][1] += 1
    for a in arms:
        n = agg[a]["n"] or 1
        log(f"  {a:<16}{agg[a]['m1']}/{n} = {agg[a]['m1']/n:>6.1%}{'':>8}"
            f"{agg[a]['m2']}/{n} = {agg[a]['m2']/n:>6.1%}")

    log("\n  按事实类型(指标2 答案命中率):")
    log(f"  {'事实':<12}" + "".join(f"{a.split('_')[0]:>14}" for a in arms))
    for k in FACT_LABELS:
        row = f"  {k:<12}"
        for a in arms:
            hit, tot = per_fact[a][k]
            row += f"{(f'{hit}/{tot} = {hit/tot:.0%}' if tot else '—'):>14}"
        log(row)

    a_m2 = agg["A_摘要+滑窗"]["m2"] / max(agg["A_摘要+滑窗"]["n"], 1)
    b_m2 = agg["B_纯滑窗"]["m2"] / max(agg["B_纯滑窗"]["n"], 1)
    c_m2 = agg["C_全量历史"]["m2"] / max(agg["C_全量历史"]["n"], 1)
    log("\n" + "-" * 78)
    log("结论")
    log("-" * 78)
    log(f"  基线(纯滑窗)        = {b_m2:.1%}")
    log(f"  现状(摘要+滑窗)     = {a_m2:.1%}   相对基线 {a_m2-b_m2:+.1%}(绝对) / "
        f"{(a_m2/b_m2-1)*100 if b_m2 else float('nan'):+.0f}%(相对)")
    log(f"  上限(全量历史)      = {c_m2:.1%}   现状距上限还差 {c_m2-a_m2:+.1%}")
    log(f"\n  → 这就是「保留率」的可复现数字:摘要层把早期事实的可达率从 "
        f"{b_m2:.0%} 提到 {a_m2:.0%}(+{a_m2-b_m2:.0%} 绝对)。")

    log("\n  样例(第 1 段对话):")
    if ok:
        r0 = ok[0]
        log(f"    摘要: {r0['summary'][:150]}")
        for a in arms:
            log(f"    [{a}] 上下文 {r0['arms'][a]['ctx_len']} 字 · 答案命中 "
                f"{sum(r0['arms'][a]['m2'].values())}/5")

    OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
    OUT_TXT.write_text("\n".join(_LINES) + "\n", encoding="utf-8")
    log(f"\n报告已落 {OUT_TXT.relative_to(ROOT)}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
