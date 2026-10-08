"""把本次复现的报告与仓库原有报告逐项对比,输出差异表。

基准: .exp_cache/分类器_reports_original/  (复现开始前备份的既有产物)
本次: data/classifier/reports/

逐项对比 5 份报告: golden / eval / export / threshold_scan / classify_run。
凡差异都打印出来,便于在 REPRO.md 里如实记录。

用法: python .compare_reports.py
"""
import json
import pathlib

BASE = pathlib.Path(".exp_cache/分类器_reports_original")
NOW = pathlib.Path("data/classifier/reports")


def load(d: pathlib.Path, name: str):
    p = d / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def row(label: str, old, new, note: str = "") -> None:
    same = "  =" if old == new else "  ≠"
    o = "—" if old is None else (f"{old:.4f}" if isinstance(old, float) else str(old))
    n = "—" if new is None else (f"{new:.4f}" if isinstance(new, float) else str(new))
    print(f"  {label:<34}{o:>16}{n:>16}{same}  {note}")


def section(title: str) -> None:
    print()
    print("=" * 92)
    print(title)
    print("=" * 92)
    print(f"  {'指标':<34}{'原报告':>16}{'本次':>16}")


def main() -> None:
    print("原报告目录:", BASE)
    print("本次目录  :", NOW)

    # ---- 黄金样例闸 ----
    o, n = load(BASE, "golden_report.json"), load(NOW, "golden_report.json")
    section("① 黄金样例闸 golden_report.json")
    if o and n:
        row("样例总数", o.get("total"), n.get("total"))
        row("集合全对", o.get("hits"), n.get("hits"))
        row("通过率", o.get("rate"), n.get("rate"))
        row("是否通过", o.get("passed"), n.get("passed"))
        row("错例数", len(o.get("failures", [])), len(n.get("failures", [])))
        print("\n  本次错例:")
        for f in n.get("failures", []):
            print(f"    {f['text']}  标准{f['gold']} → 预标{f['pred']}")

    # ---- 训练阈值扫描 ----
    o, n = load(BASE, "threshold_scan.json"), load(NOW, "threshold_scan.json")
    section("② 阈值扫描 threshold_scan.json")
    if o and n:
        row("验证集规模", o.get("val_size"), n.get("val_size"))
        row("最优阈值", o.get("best_threshold"), n.get("best_threshold"))
        row("最优 micro-F1", o.get("best_micro_f1"), n.get("best_micro_f1"))
        row("在用阈值", o.get("in_use_threshold"), n.get("in_use_threshold"))
        row("复算一致", o.get("consistent"), n.get("consistent"))
        print("\n  九候选线 micro-F1 对照(阈值: 原 / 本次):")
        om = {s["threshold"]: s["micro_f1"] for s in o.get("scan", [])}
        nm = {s["threshold"]: s["micro_f1"] for s in n.get("scan", [])}
        for t in sorted(set(om) | set(nm)):
            print(f"    {t:<6} {om.get(t, float('nan')):<10.4f} {nm.get(t, float('nan')):<10.4f}")

    # ---- 测试集评测 ----
    o, n = load(BASE, "eval_report.json"), load(NOW, "eval_report.json")
    section("③ 测试集评测 eval_report.json")
    if o and n:
        row("测试集条数", o.get("test_size"), n.get("test_size"))
        row("判定阈值", o.get("threshold"), n.get("threshold"))
        for scope in ("micro", "macro"):
            for m in ("p", "r", "f1"):
                row(f"{scope}_{m}", (o.get(scope) or {}).get(m), (n.get(scope) or {}).get(m))
        row("红线总判定通过", o.get("red_line_passed"), n.get("red_line_passed"))
        row("FP 总数 / FN 总数",
            f"{(o.get('total_fp'))}/{(o.get('total_fn'))}",
            f"{(n.get('total_fp'))}/{(n.get('total_fn'))}")

        print("\n  每类 F1 对照(只列差异 ≥0.03 的类):")
        of = {c["name"]: c for c in o.get("classes", [])}
        nf = {c["name"]: c for c in n.get("classes", [])}
        for name in sorted(set(of) | set(nf)):
            a = (of.get(name) or {}).get("f1")
            b = (nf.get(name) or {}).get("f1")
            if isinstance(a, float) and isinstance(b, float) and abs(a - b) >= 0.03:
                oa = "✅" if (of.get(name) or {}).get("passed") else "🔴"
                ob = "✅" if (nf.get(name) or {}).get("passed") else "🔴"
                row(f"  {name}", f"{a:.3f} {oa}", f"{b:.3f} {ob}", "← 差 ≥0.03")
        print("\n  红线达标类目数:")
        for sev in ("严", "中"):
            o_rl = (o.get("red_lines") or {}).get(sev) or {}
            n_rl = (n.get("red_lines") or {}).get(sev) or {}
            print(f"    {sev}档: 原 {o_rl}  本次 {n_rl}")

        print("\n  错例方向分布(漏打/多打/错位):")
        def kinds(d):
            k = {}
            for e in d.get("errors", []):
                k[e.get("kind")] = k.get(e.get("kind"), 0) + 1
            return f"{len(d.get('errors', []))} 条: " + " ".join(f"{a}={b}" for a, b in sorted(k.items()))
        print(f"    原报告: {kinds(o)}")
        print(f"    本次  : {kinds(n)}")
    else:
        print("  缺 json,改看 .md:")
        for d, tag in ((BASE, "原报告"), (NOW, "本次")):
            p = d / "eval_report.md"
            if p.exists():
                print(f"\n  --- {tag} ---")
                print("  " + "\n  ".join(p.read_text(encoding="utf-8").splitlines()[:6]))

    # ---- ONNX 导出 ----
    o, n = load(BASE, "export_report.json"), load(NOW, "export_report.json")
    section("④ ONNX 导出 export_report.json")
    if o and n:
        row("校验条数", o.get("checked"), n.get("checked"))
        row("不一致条数", o.get("mismatch"), n.get("mismatch"))
        row("是否通过", o.get("passed"), n.get("passed"))
        row("onnx 字节", o.get("onnx_bytes"), n.get("onnx_bytes"))
        row("opset", o.get("opset"), n.get("opset"))

    # ---- 旁路批处理 ----
    o, n = load(BASE, "classify_run.json"), load(NOW, "classify_run.json")
    section("⑤ 旁路批处理 classify_run.json")
    if o and n:
        row("status", o.get("status"), n.get("status"))
        row("pending", o.get("pending"), n.get("pending"))
        row("written", o.get("written"), n.get("written"))
    elif o:
        print("  本次还没生成(该步未跑)")

    print()
    print("=" * 92)
    print("说明: '=' 表示与原报告一致,'≠' 表示有差异 —— 差异需在 REPRO.md 里逐条给原因。")
    print("提醒: 语料是 LLM 非确定性生成的,本次 1485 条 vs 原报告 ~1700 条,")
    print("      所以测试集 149 条 vs 原 161 条 —— 逐类指标不可期望逐位相同。")


if __name__ == "__main__":
    main()
