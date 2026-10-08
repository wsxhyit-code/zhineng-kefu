# 来源:自主改造(UI 重新着色)
"""UI 重新着色:把「喵喵优选」的暖橙像素风换成「爪印优选」的青碧像素风。

只动「识别层」,不动「结构层」——这样画风一致但一眼看出不是同一个项目:
  换:品牌名 / 标题 / favicon 图形 / 整套色板 / 背景纹理(网格→圆点)/ 主色变量名
  不换:布局、栅格、硬偏移阴影技法、粗描边、等宽字体、直角、交互与所有 JS 逻辑

做法是**字面替换**(十六进制与 rgb 两种写法都覆盖),不做结构重构,风险最低。
跑完会报告「未映射的残留色值」——有残留说明色板盘漏了,要补。

可回滚:原文件先备份到 .exp_cache/ui_original/。
跑法:.venv\\Scripts\\python.exe scripts/retint_ui.py [--dry-run]
"""
import argparse
import pathlib
import re
import shutil

STATIC = pathlib.Path("app/static")
BACKUP = pathlib.Path(".exp_cache/ui_original")

# ---- 品牌 ----
OLD_BRAND, NEW_BRAND = "喵喵优选", "爪印优选"

# ---- 色板:主色系(要换) ----
HEX_MAP = {
    "2b2632": "1d2b28",   # ink 墨黑 → 墨绿黑
    "ff9f57": "3fbf9f",   # 主色 橙 → 青碧
    "ff7a59": "2b9fd8",   # 辅助 珊瑚 → 湖蓝
    "fff6e6": "f2f7f4",   # cream
    "fffaf0": "fbfdfc",   # paper
    "9b8f86": "7d918c",   # muted
    "f7d9a8": "cbe6dd",   # 页面底色
    "6d6478": "5c6f6a",   # 次级 muted
    "4c4456": "39473f",   # 更深的 muted
    "b7a6ff": "7fd4c4",   # 强调(原紫)
    "ffedd6": "dff2ec",   # 主色浅底(hover)
    "e6dcc9": "d4e4de",
    "f3ead9": "e2efeb",
    "efe6d4": "dcebe6",
    "e8dcc8": "d4e6e0",
    "d8cbc0": "c2d6d0",
    "6b5b48": "3f5a52",
    "ffd7a8": "a8e0d0",
    "ffe0b3": "c4ece0",
    "ff8b6d": "4fb8e0",
    "ff6a45": "1f8fc4",
    "ff8095": "2b9fd8",   # favicon 里那个鼻头色
    "ffe3e6": "dff0f5",
    # ---- 语义色(保留色相,只微调,不然红/绿会失去含义) ----
    "d6455a": "c9455c",   # 错误红
    "5fcf6a": "4fbf6a",   # 在线绿
    "2f8f3c": "2f8f5f",   # 成功绿
    "ffe9ec": "fbe8ec",
    "ffd4dc": "f7d6dc",
    "8e1f31": "8e2434",
    "e8fae9": "e6f6ec",
    "ffe0e0": "fbe4e4",
    "ffd66e": "ffd166",
    "fff0d6": "fdf3dd",
    "7fc4e8": "6fb8e0",
}
KEEP = {"fff", "eee"}

# rgb 写法:rgb(43,38,50) 就是 #2b2632
RGB_MAP = {"43,38,50": "29,43,40"}

# ---- 变量改名:去掉猫味的 --fur / --coral ----
VAR_RENAME = {"--fur": "--paw", "--coral": "--accent"}

# ---- 新 favicon:像素爪印,沿用它自己的「硬偏移投影」语言 ----
NEW_ICON = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' "
    "shape-rendering='crispEdges'>"
    "<rect x='2' y='4' width='3' height='4' fill='%231d2b28'/>"
    "<rect x='7' y='2' width='3' height='4' fill='%231d2b28'/>"
    "<rect x='12' y='4' width='3' height='4' fill='%231d2b28'/>"
    "<rect x='4' y='9' width='9' height='6' fill='%231d2b28'/>"
    "<rect x='1' y='3' width='3' height='4' fill='%233fbf9f'/>"
    "<rect x='6' y='1' width='3' height='4' fill='%233fbf9f'/>"
    "<rect x='11' y='3' width='3' height='4' fill='%233fbf9f'/>"
    "<rect x='3' y='8' width='9' height='6' fill='%233fbf9f'/>"
    "</svg>"
)

# 背景纹理:24px 网格线 → 24px 圆点(同技法,不同观感)
GRID_RE = re.compile(
    r"linear-gradient\(\s*rgba\(43,\s*38,\s*50,\s*0?\.05\)\s*2px\s*,\s*transparent\s*2px\s*\)"
    r"\s*,\s*"
    r"linear-gradient\(\s*90deg\s*,\s*rgba\(43,\s*38,\s*50,\s*0?\.05\)\s*2px\s*,\s*transparent\s*2px\s*\)"
)
DOTS = "radial-gradient(rgba(29,43,40,.16) 2px, transparent 2px)"


def retint(text: str) -> tuple[str, dict]:
    n = {}

    # 1) favicon 整块换掉(图形要变,不只是换色)
    text, k = re.subn(r'<link rel="icon" href="data:image/svg\+xml,[^"]*"\s*/>',
                      f'<link rel="icon" href="data:image/svg+xml,{NEW_ICON}" />', text)
    n["favicon"] = k

    # 2) 品牌名
    text, k = re.subn(re.escape(OLD_BRAND), NEW_BRAND, text)
    n["brand"] = k

    # 3) 背景纹理:先换,免得下面的色值替换把匹配模式打断
    text, k = GRID_RE.subn(DOTS, text)
    n["pattern"] = k

    # 4) 变量改名
    for old, new in VAR_RENAME.items():
        text, k = re.subn(re.escape(old) + r"\b", new, text)
        n[f"var{old}"] = k

    # 5) 色值:#xxxxxx 与 %23xxxxxx 两种写法都覆盖
    for old, new in HEX_MAP.items():
        for pre in ("#", "%23", "#".upper(), "%23".upper()):
            pat = re.compile(re.escape(pre + old), re.IGNORECASE)
            text, k = pat.subn(pre.lower() + new, text)
            n["hex"] = n.get("hex", 0) + k

    # 6) rgb/rgba 写法。连接符必须是 `,\s*` —— 只写 `\s*` 匹配不到 `43, 38, 50` 里的逗号,
    #    会静默一处不改(实测 dry-run 靠 rgb 计数为 0 才发现)。
    for old, new in RGB_MAP.items():
        pat = re.compile(r",\s*".join(re.escape(p) for p in old.split(",")))
        text, k = pat.subn(new, text)
        n["rgb"] = n.get("rgb", 0) + k

    return text, n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    files = sorted(list(STATIC.glob("*.html")) + list(STATIC.glob("*.css")))
    if not a.dry_run:
        BACKUP.mkdir(parents=True, exist_ok=True)
        for f in files:
            dst = BACKUP / f.name
            if not dst.exists():          # 只备份第一次,别把改过的当原件覆盖掉
                shutil.copy2(f, dst)
        print(f"原文件已备份到 {BACKUP}({len(files)} 个)\n")

    total = {}
    outs = {}
    print(f"{'文件':<26}{'favicon':>8}{'品牌':>6}{'纹理':>6}{'变量':>6}{'色值':>7}{'rgb':>6}")
    print("-" * 68)
    for f in files:
        src = f.read_text(encoding="utf-8")
        out, n = retint(src)
        outs[f.name] = out
        conv = {k: v for k, v in n.items() if k.startswith("var")}
        row = (n.get("favicon", 0), n.get("brand", 0), n.get("pattern", 0),
               sum(conv.values()), n.get("hex", 0), n.get("rgb", 0))
        print(f"{f.name:<26}{row[0]:>8}{row[1]:>6}{row[2]:>6}{row[3]:>6}{row[4]:>7}{row[5]:>6}")
        for k, v in n.items():
            total[k] = total.get(k, 0) + v
        if out != src and not a.dry_run:
            f.write_text(out, encoding="utf-8")

    print("-" * 68)
    print("合计:", {k: v for k, v in total.items()})

    # 残留检查:必须查【转换后】的文本。查磁盘原文件只会报出改造前的旧色,报告完全无效
    # (实测 dry-run 踩过这个坑)。
    allowed = set(HEX_MAP.values()) | KEEP
    print("\n=== 残留色值检查(基于转换结果) ===")
    leftovers = {}
    for name, t in outs.items():
        found = set()
        for m in re.findall(r"#[0-9a-fA-F]{3,6}\b", t):
            if m.lower().lstrip("#") not in allowed:
                found.add(m.lower())
        for m in re.findall(r"%23[0-9a-fA-F]{6}", t):
            if m.lower()[3:] not in allowed:
                found.add(m.lower())
        if found:
            leftovers[name] = sorted(found)
    if leftovers:
        for name, cols in leftovers.items():
            print(f"  ⚠ {name}: {cols}")
    else:
        print("  ✅ 无残留,全部色值都落在新色板内")

    # rgb()/rgba() 形式也要查 —— 只查十六进制会漏掉整类写法
    triples = {}
    for name, t in outs.items():
        for m in re.findall(r"rgba?\(([^)]*)\)", t):
            nums = tuple(x.strip() for x in m.split(",")[:3])
            if len(nums) == 3 and all(x.isdigit() for x in nums):
                triples.setdefault(",".join(nums), set()).add(name)
    print("\n=== rgb()/rgba() 三通道汇总 ===")
    for k, fl in sorted(triples.items()):
        flag = "  ← 新 ink" if k == "29,43,40" else ("  ← 旧 ink 残留!" if k == "43,38,50" else "")
        print(f"  rgb({k}) ×{len(fl)} 文件{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
