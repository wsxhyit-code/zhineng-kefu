"""手工下载 hfl/chinese-roberta-wwm-ext 到 data/classifier/base_model/。

为什么不用 huggingface_hub.snapshot_download:
  1. 它用 tempfile.mkdtemp 建 `tmpXXXX` 目录,本机沙箱对这种临时目录的写入返回
     PermissionError [Errno 13]，chmod/rmtree 也被拒 —— 下载到一半必挂;
  2. 它默认走 hf-xet,而 Xet 存储直连 cas-server.xethub.hf.co 会 401;
  3. 本机注册表里残留着系统代理(代理进程其实已不在),httpx trust_env=True 会握手超时。

所以这里用最小实现:自己的 httpx 客户端(trust_env=False) + Range 断点续传 + 多次重试,
直接写最终路径(`.part` 后缀,下完再改名),不碰 tempfile、不碰 symlink。

用法:python .fetch_base_model.py
"""
import pathlib
import time

import httpx

BASE = "https://hf-mirror.com/hfl/chinese-roberta-wwm-ext/resolve/main"
OUT = pathlib.Path(__file__).resolve().parent / "data/classifier/base_model"
FILES = [
    "config.json",
    "vocab.txt",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "pytorch_model.bin",
]
ATTEMPTS = 12
CHUNK = 1 << 20          # 1 MiB


def fetch(name: str) -> bool:
    dst = OUT / name
    part = OUT / (name + ".part")
    for attempt in range(1, ATTEMPTS + 1):
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with httpx.Client(timeout=httpx.Timeout(30.0, read=120.0), trust_env=False,
                              follow_redirects=True) as c:
                with c.stream("GET", f"{BASE}/{name}", headers=headers) as r:
                    if r.status_code == 416:            # 已经下完
                        break
                    if have and r.status_code != 206:   # 服务器不支持续传 → 重下
                        part.unlink(missing_ok=True)
                        have = 0
                    if r.status_code not in (200, 206):
                        raise RuntimeError(f"HTTP {r.status_code}")
                    total = int(r.headers.get("content-length", 0)) + have
                    mode = "ab" if have else "wb"
                    with part.open(mode) as f:
                        for chunk in r.iter_bytes(CHUNK):
                            f.write(chunk)
                            have += len(chunk)
                        f.flush()
            got = part.stat().st_size
            if total and got < total:
                raise RuntimeError(f"只拿到 {got}/{total}")
            part.replace(dst)
            print(f"  ✓ {name}  {got/1048576:.1f} MB", flush=True)
            return True
        except Exception as e:  # noqa: BLE001
            size = part.stat().st_size if part.exists() else 0
            print(f"  … {name} 第 {attempt}/{ATTEMPTS} 次失败({type(e).__name__}: {str(e)[:50]}),"
                  f"已存 {size/1048576:.1f} MB,退避后重试", flush=True)
            time.sleep(min(3 * attempt, 20))
    print(f"  ✗ {name} 重试 {ATTEMPTS} 次仍失败", flush=True)
    return False


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"目标目录 {OUT}", flush=True)
    ok = all([fetch(n) for n in FILES])
    print("=" * 60)
    total = sum(f.stat().st_size for f in OUT.iterdir() if f.suffix != ".part")
    print(f"结果: {'全部成功' if ok else '有失败'} · 合计 {total/1048576:.1f} MB", flush=True)
    for f in sorted(OUT.iterdir()):
        print(f"  {f.stat().st_size/1048576:>8.2f} MB  {f.name}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
