"""等上游恢复(充值到账),每 15 秒探一次,通了就退出 0。

为什么单独写:build_corpus.py 是"连续 5 批失败就放弃残货"的实现,余额不足时它会正常退出 0
并留下一份不均衡的语料(见 REPRO.md §4)。所以必须先确认上游真的可用,再启动长任务。

用法:python .wait_upstream.py
"""
import sys
import time

import httpx

from app.config import settings

MAX_WAIT_MIN = 60
INTERVAL = 15


def probe() -> tuple[int, str]:
    with httpx.Client(timeout=20.0) as c:
        r = c.post(settings.chat_base_url.rstrip("/") + "/chat/completions",
                   headers={"Authorization": f"Bearer {settings.chat_api_key}"},
                   json={"model": settings.chat_model, "max_tokens": 5,
                         "messages": [{"role": "user", "content": "hi"}]})
        return r.status_code, r.text[:120]


def main() -> int:
    rounds = MAX_WAIT_MIN * 60 // INTERVAL
    for i in range(rounds):
        elapsed = i * INTERVAL
        try:
            code, body = probe()
        except Exception as e:  # noqa: BLE001
            code, body = -1, f"{type(e).__name__}"
        if code == 200:
            print(f"UPSTREAM_OK 在第 {elapsed} 秒恢复", flush=True)
            return 0
        print(f"  [{elapsed:>4}s] HTTP {code}  {body}", flush=True)
        time.sleep(INTERVAL)
    print(f"UPSTREAM_TIMEOUT {MAX_WAIT_MIN} 分钟仍未恢复", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
