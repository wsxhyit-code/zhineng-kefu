"""Fetch container images on the HOST and emit docker-load tars.

The Docker daemon's registry path is unusably slow/broken on this machine, so we
download here (host HTTPS works fine) and only hand the finished tar to Docker.

Usage: python .fetch_images.py [name ...]
"""

import concurrent.futures as cf
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import time

import httpx

ACCEPT = ", ".join([
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
])

# key -> (registry, repo, tag, local target ref)
# Go straight to the official registries through the host proxy: the domestic
# mirrors throttle their blob CDN (a 40 MiB layer took 22 minutes and then
# truncated). Set IMG_REGISTRY_MIRROR=1 to use the mirror hosts instead.
_MIRROR = bool(os.environ.get("IMG_REGISTRY_MIRROR"))
_HUB = "docker.m.daocloud.io" if _MIRROR else "registry-1.docker.io"
IMAGES = {
    "langfuse":   (_HUB, "langfuse/langfuse", "3", "langfuse/langfuse:3"),
    "worker":     (_HUB, "langfuse/langfuse-worker", "3", "langfuse/langfuse-worker:3"),
    "clickhouse": (_HUB, "clickhouse/clickhouse-server", "latest", "clickhouse/clickhouse-server:latest"),
    "minio":      ("cgr.dev", "chainguard/minio", "latest", "cgr.dev/chainguard/minio:latest"),
    "redis":      (_HUB, "library/redis", "7", "redis:7"),
    "postgres":   (_HUB, "library/postgres", "17", "postgres:17"),
}

OUT = ".imgcache"
TMP = os.path.join(OUT, "_tmp")
# The machine's system proxy is the only route to the official registries here
# (docker.io is DNS-poisoned when dialled directly). Override with IMG_PROXY=none.
_PROXY = os.environ.get("IMG_PROXY", "http://127.0.0.1:7890")
CLIENT = httpx.Client(timeout=httpx.Timeout(90.0, connect=20.0), follow_redirects=True,
                      trust_env=False,
                      proxy=None if _PROXY in ("", "none") else _PROXY,
                      headers={"User-Agent": "img-fetch/1.0"})

# registry tokens are short-lived (minutes) while these downloads take hours, so
# remember each repo's challenge and mint a fresh token whenever a blob 401s.
AUTH: dict[tuple[str, str], tuple[str, str]] = {}


def refresh_token(registry: str, repo: str) -> str | None:
    realm, service = AUTH[(registry, repo)]
    r = CLIENT.get(realm, params={"service": service, "scope": f"repository:{repo}:pull"})
    r.raise_for_status()
    d = r.json()
    return d.get("token") or d.get("access_token")


def token_from_challenge(resp: httpx.Response, registry: str, repo: str) -> str | None:
    head = resp.headers.get("WWW-Authenticate", "")
    if "bearer" not in head.lower():
        return None
    params = dict(re.findall(r'(\w+)="([^"]*)"', head))
    realm, service = params.get("realm"), params.get("service")
    if not realm:
        return None
    AUTH[(registry, repo)] = (realm, service or "")
    return refresh_token(registry, repo)


def fetch_manifest(registry: str, repo: str, ref: str) -> tuple[dict, str | None]:
    url = f"https://{registry}/v2/{repo}/manifests/{ref}"
    r = CLIENT.get(url, headers={"Accept": ACCEPT})
    token = None
    if r.status_code == 401:
        token = token_from_challenge(r, registry, repo)
        r = CLIENT.get(url, headers={"Accept": ACCEPT, "Authorization": f"Bearer {token}"})
    r.raise_for_status()
    doc = r.json()
    if doc.get("manifests"):                      # multi-arch index -> pick linux/amd64
        for m in doc["manifests"]:
            p = m.get("platform", {})
            if p.get("os") == "linux" and p.get("architecture") == "amd64":
                return fetch_manifest(registry, repo, m["digest"])[0], token
        raise SystemExit(f"{repo}: no linux/amd64 manifest")
    return doc, token


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def download_blob(registry: str, repo: str, digest: str, dest: str, token: str | None) -> str:
    """Download one blob, resumable.

    This link drops long transfers (SSL EOF) and registry tokens expire while a
    single blob is still coming down, so each attempt picks up where the last one
    stopped via a Range request: progress only ever moves forward.
    """
    url = f"https://{registry}/v2/{repo}/blobs/{digest}"
    expected = None
    last = None
    for _ in range(40):
        have = os.path.getsize(dest) if os.path.exists(dest) else 0
        if have and os.path.exists(dest + ".size"):
            with open(dest + ".size") as fh:
                expected = int(fh.read().strip())
            if have >= expected:
                break
        headers = {"Authorization": f"Bearer {token}"}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            with CLIENT.stream("GET", url, headers=headers) as r:
                if r.status_code == 401 and (registry, repo) in AUTH:
                    token = refresh_token(registry, repo)
                    continue
                if r.status_code == 416:            # asked past the end: already complete
                    break
                r.raise_for_status()
                if expected is None:
                    cl = r.headers.get("Content-Length")
                    if cl:
                        expected = have + int(cl)
                        with open(dest + ".size", "w") as fh:
                            fh.write(str(expected))
                mode = "ab" if (have and r.status_code == 206) else "wb"
                if mode == "wb":
                    have = 0
                with open(dest, mode) as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        fh.write(chunk)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2)
            continue
        if expected and os.path.getsize(dest) < expected:
            time.sleep(1)
            continue
        if _sha256_file(dest) == digest:
            if os.path.exists(dest + ".size"):
                os.remove(dest + ".size")
            return dest
        last = RuntimeError("digest mismatch")
        os.remove(dest)
        if os.path.exists(dest + ".size"):
            os.remove(dest + ".size")
        expected = None
    if os.path.exists(dest) and _sha256_file(dest) == digest:
        return dest
    got = os.path.getsize(dest) if os.path.exists(dest) else 0
    raise SystemExit(f"blob failed {digest[:19]} ({got}/{expected or '?'} bytes): {last}")


def build_tar(name: str) -> str:
    registry, repo, tag, target = IMAGES[name]
    t0 = time.time()
    print(f"[{name}] manifest {registry}/{repo}:{tag}", flush=True)
    manifest, token = fetch_manifest(registry, repo, tag)
    cfg_digest = manifest["config"]["digest"]
    layers = manifest["layers"]
    print(f"[{name}] {len(layers)} layers", flush=True)

    workdir = os.path.join(TMP, name)
    # resume: keep whatever already finished (these pulls can run for hours and a
    # token expiry or a network blip must not throw away an hour of downloading)
    os.makedirs(workdir, exist_ok=True)

    download_blob(registry, repo, cfg_digest, os.path.join(workdir, "config.json"), token)

    def one_layer(pair):
        i, layer = pair
        digest = layer["digest"]
        diff_hex = digest.split(":")[1]
        layer_dir = os.path.join(workdir, diff_hex)
        layer_tar = os.path.join(layer_dir, "layer.tar")
        if os.path.exists(layer_tar) and os.path.getsize(layer_tar) > 0:
            print(f"[{name}]   layer {i+1}/{len(layers)} {digest[:19]} already done, skip", flush=True)
            return f"{diff_hex}/layer.tar"
        blob = os.path.join(workdir, f"blob{i}.gz")
        download_blob(registry, repo, digest, blob, token)
        os.makedirs(layer_dir, exist_ok=True)
        with open(blob, "rb") as src:
            magic = src.read(2)
            src.seek(0)
            if magic == b"\x1f\x8b":
                with gzip.open(src, "rb") as gz, open(layer_tar, "wb") as out:
                    shutil.copyfileobj(gz, out, 1 << 20)
            else:
                shutil.copyfileobj(src, out, 1 << 20)
        with open(os.path.join(layer_dir, "VERSION"), "w") as fh:
            fh.write("1.0")
        os.remove(blob)
        size = os.path.getsize(layer_tar) / 1048576
        print(f"[{name}]   layer {i+1}/{len(layers)} {digest[:19]} -> {size:.1f} MiB", flush=True)
        return f"{diff_hex}/layer.tar"

    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        rel_layers = list(pool.map(one_layer, enumerate(layers)))

    with open(os.path.join(workdir, "manifest.json"), "w") as fh:
        json.dump([{"Config": "config.json", "RepoTags": [target], "Layers": rel_layers}], fh)

    tar_path = os.path.join(OUT, f"{name}.tar")
    print(f"[{name}] packing {tar_path}", flush=True)
    with tarfile.open(tar_path, "w") as tar:
        tar.add(os.path.join(workdir, "manifest.json"), arcname="manifest.json")
        tar.add(os.path.join(workdir, "config.json"), arcname="config.json")
        for rel in rel_layers:
            tar.add(os.path.join(workdir, rel), arcname=rel)
            tar.add(os.path.join(workdir, os.path.dirname(rel), "VERSION"),
                    arcname=os.path.join(os.path.dirname(rel), "VERSION"))
    shutil.rmtree(workdir, ignore_errors=True)
    print(f"[{name}] DONE {tar_path} {os.path.getsize(tar_path)/1048576:.0f} MiB in {time.time()-t0:.0f}s", flush=True)
    return tar_path


def main() -> None:
    names = sys.argv[1:] or list(IMAGES)
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(TMP, exist_ok=True)
    t0 = time.time()
    for name in names:
        if name not in IMAGES:
            raise SystemExit(f"unknown image key: {name}")
        try:
            build_tar(name)
        except SystemExit as e:
            print(f"[{name}] FAILED: {e}", flush=True)
        except Exception as e:  # noqa: BLE001 one flaky manifest must not kill the rest
            print(f"[{name}] FAILED: {type(e).__name__} {str(e)[:120]}", flush=True)
    print(f"ALL DONE in {time.time()-t0:.0f}s", flush=True)
    for name in names:
        p = os.path.join(OUT, f"{name}.tar")
        if os.path.exists(p):
            print(f"  {name}.tar {os.path.getsize(p)/1048576:.0f} MiB", flush=True)


if __name__ == "__main__":
    main()
