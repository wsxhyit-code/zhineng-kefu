"""从 Langfuse API 侧读回最近的 trace,打印成树。

用途:
  1. 验证"接上了没有"——界面之外的第二条独立证据
  2. 当练习脚手架:改 fields / limit / filter 就能练各种查询

跑法(需 Langfuse 栈在跑 + .env 三键齐全):
  .venv\\Scripts\\python.exe .verify_trace.py
"""
import sys

from app.core.observability import get_langfuse


def _fmt_dur(obs) -> str:
    st, et = getattr(obs, "start_time", None), getattr(obs, "end_time", None)
    if not (st and et):
        return "  (未结束)"
    return f"{(et - st).total_seconds():7.2f}s"


def _fmt_usage(obs) -> str:
    u = getattr(obs, "usage_details", None) or {}
    if not u:
        return ""
    keys = ("input", "output", "total", "cache_read")
    parts = [f"{k}={u[k]}" for k in keys if k in u]
    other = [f"{k}={v}" for k, v in u.items() if k not in keys]
    return "  tokens[" + " ".join(parts + other) + "]"


def _text(v, n=70) -> str:
    if v is None:
        return "—"
    s = v if isinstance(v, str) else repr(v)
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + "…"


def main() -> int:
    client = get_langfuse()
    if client is None:
        print("Langfuse 未配置(.env 三键不全),无法查询")
        return 1

    traces = client.api.trace.list(limit=3)
    rows = traces.data or []
    print(f"取回 {len(rows)} 条 trace\n" + "=" * 78)
    if not rows:
        print("一条都没有 —— 检查:① 有没有真的聊过 ② attach_observability 是否挂上")
        return 1

    for t in rows:
        print(f"\ntrace id    {t.id}")
        print(f"  name      {getattr(t, 'name', None)}")
        print(f"  session   {getattr(t, 'session_id', None)}      ← langfuse_session_id")
        print(f"  user      {getattr(t, 'user_id', None)}")
        print(f"  tags      {getattr(t, 'tags', None)}")
        print(f"  metadata  {getattr(t, 'metadata', None)}")

    newest = rows[0]
    print("\n" + "=" * 78)
    print(f"最新一条的观测树(trace_id={newest.id})")
    print("=" * 78)

    # 走 legacy/observations_v1 而不是 observation.get_many:
    # v2 observations 端点在本机自部署(v3 write mode)上直接 404
    # ("only available in a Langfuse v4 write mode"),和 cost_by_intent.py 里
    # 记的 metrics_v1 是同一类限制 —— 这正是"自部署版本决定了哪些 API 可用"。
    # limit 上限 100(传 200 会被 400 拒:"Too big: expected number to be <=100")
    obs = client.api.legacy.observations_v1.get_many(trace_id=newest.id, limit=100)
    items = obs.data or []
    by_parent: dict = {}
    for o in items:
        by_parent.setdefault(getattr(o, "parent_observation_id", None), []).append(o)

    def walk(parent_id, depth):
        for o in sorted(by_parent.get(parent_id, []), key=lambda x: getattr(x, "start_time", None) or 0):
            otype = str(getattr(o, "type", "?")).split(".")[-1]
            print(f"{'  ' * depth}├─ {otype:11s} {str(getattr(o, 'name', '?')):24s}"
                  f" {_fmt_dur(o)}{_fmt_usage(o)}")
            walk(getattr(o, "id", None), depth + 1)

    walk(None, 0)
    print(f"\n共 {len(items)} 个观测")

    total = sum((getattr(o, "usage_details", None) or {}).get("total", 0) or 0 for o in items)
    cached = sum((getattr(o, "usage_details", None) or {}).get("cache_read", 0) or 0 for o in items)
    print(f"该轮 token 合计 total={total}  cache_read={cached}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
