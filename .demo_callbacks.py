"""最小实验:证明 graph.with_config({"callbacks":[h]}) 为什么能让整张图被追踪。

不需要 LLM、不需要 Langfuse 服务 —— 因为回调机制是【LangChain 框架自己】的,
Langfuse 只是写了一个 handler 去接这个机制。看懂这个就懂 Langfuse 挂在哪了。
"""
from typing import TypedDict

from langchain_core.callbacks import BaseCallbackHandler
from langgraph.graph import END, START, StateGraph


class Spy(BaseCallbackHandler):
    """假 handler:只 print(真的 Langfuse handler 是"翻译成记录并 POST")。"""

    @staticmethod
    def _s(u):
        # 取【后】8 位:UUID1 是时间前缀,前 8 位在同一进程里全都一样,看不出谁是谁
        return str(u)[-8:] if u else "—— (无父)"

    def on_chain_start(self, serialized, inputs, *, run_id=None, parent_run_id=None, **kw):
        name = kw.get("name") or (serialized or {}).get("name") or "?"
        print(f"  [handler] START  {str(name):10s} run_id=…{self._s(run_id)}"
              f"  parent=…{self._s(parent_run_id)}")

    def on_chain_end(self, outputs, *, run_id=None, parent_run_id=None, **kw):
        print(f"  [handler] END    run_id=…{self._s(run_id)}  outputs={outputs}")


class S(TypedDict):
    x: int


def node_a(state):
    print("        (节点 a 在干活)")
    return {"x": state["x"] + 1}


def node_b(state):
    print("        (节点 b 在干活)")
    return {"x": state["x"] * 10}


g = StateGraph(S)
g.add_node("a", node_a)
g.add_node("b", node_b)
g.add_edge(START, "a")
g.add_edge("a", "b")
g.add_edge("b", END)
plain = g.compile()

print("【1】编译出的图,【没挂】任何回调:")
print(f"  结果 {plain.invoke({'x': 1})}")
print("  ↑ 节点正常跑完,但没有任何 START/END 输出 —— 没人在听\n")

print("【2】同一张图,只多一句 .with_config(...):")
traced = plain.with_config({"callbacks": [Spy()]})
print(f"  结果 {traced.invoke({'x': 1})}")
print("  ↑ handler 的 START/END 自动出现了\n")

print("【3】关键:node_a / node_b 的函数体一行都没改")
