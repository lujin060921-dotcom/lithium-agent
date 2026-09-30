"""CLI 入口：跑一次完整的产业链影响分析。

默认按框架要求做**涨跌双向**分析（同一次运行内并列覆盖涨价与下跌两个方向）。

用法示例
  # 默认：碳酸锂 ±15%（情景 A-1 / A-2），自动带上 data/reports 下的财报 PDF，调真实模型
  .venv\\Scripts\\python.exe app.py

  # 指定幅度 / 基准日期 / PDF / 走离线确定性报告
  .venv\\Scripts\\python.exe app.py --delta 0.5 --as-of 2025-12-31 --pdf data\\reports\\xxx.pdf --no-llm

  # 只看单一方向（调试用）
  .venv\\Scripts\\python.exe app.py --delta 0.15 --no-bidirectional

  # 一次跑完传导链配置里的全部情景分级（A-1 / A-2 / B-1 / B-2）
  .venv\\Scripts\\python.exe app.py --scenarios

  # 只做模型连通性自检（阶段 0 的"跑出一次 hello"）
  .venv\\Scripts\\python.exe app.py --check-model
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent.orchestrator import AnalysisRequest, Orchestrator
from config import DATA_DIR, PROJECT_ROOT, settings
from tools.retriever import DEFAULT_PRICE_NAME, Retriever


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lithium-agent",
        description="产业链影响分析智能体 · 碳酸锂价格波动（默认涨跌双向）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--delta",
        type=float,
        default=0.15,
        help="单边变动幅度，如 0.15 表示 ±15%%（默认取情景 A-1/A-2 的中点）",
    )
    p.add_argument(
        "--no-bidirectional",
        action="store_true",
        help="只跑单方向（调试用）；默认正向与反向并列各跑一遍",
    )
    p.add_argument("--scenarios", action="store_true", help="跑完传导链配置中的全部情景分级（不含压力情景 S-1）")
    p.add_argument(
        "--stress",
        action="store_true",
        help="独立压力情景 S-1（历史极端下跌 -80%%，情景价约 2.68 万元/吨），仅压实 F-010 现金成本线击穿分支，"
        "单独触发、不进默认批量",
    )
    p.add_argument("--as-of", default=None, help="基准日期 YYYY-MM-DD（默认取最新一条报价）")
    p.add_argument("--series", default=DEFAULT_PRICE_NAME, help="价格品种名称")
    p.add_argument("--pdf", action="append", default=[], help="财报 PDF 路径，可重复传入")
    p.add_argument("--no-llm", action="store_true", help="不调用模型，输出离线确定性报告")
    p.add_argument("--json", action="store_true", help="以 JSON 输出结构化结果")
    p.add_argument("--quiet", action="store_true", help="不打印过程日志")
    p.add_argument("--check-model", action="store_true", help="仅做模型连通性自检")
    return p


def auto_pdfs() -> list[str]:
    """未显式指定 PDF 时，自动使用 data/reports 下的财报文件，保证 Tool 1 参与全流程。"""
    reports_dir = DATA_DIR / "reports"
    if not reports_dir.exists():
        return []
    return [str(p) for p in sorted(reports_dir.glob("*.pdf"))]


def run_check_model() -> int:
    from agent.llm_client import LLMClient
    from config import LOG_DIR

    print("模型配置：", json.dumps(settings.describe(), ensure_ascii=False))
    if settings.offline:
        print("[!] 未配置 DEEPSEEK_API_KEY，当前为离线模式；请在项目根目录 .env 中填写后重试。")
        return 1
    client = LLMClient(settings, LOG_DIR / "cache")
    resp = client.check()
    print("调用结果：", json.dumps(resp.to_dict(), ensure_ascii=False))
    if resp.status == "ok":
        print("模型回复：", resp.content.strip())
        print("[OK] 连通性自检通过")
        return 0
    print("[FAIL] 连通性自检失败：", resp.error)
    return 1


def run_once(
    args, delta: float, orchestrator: Orchestrator, index: int = 0, total: int = 1,
    stress: bool = False, note: str = "",
) -> dict:
    request = AnalysisRequest(
        price_series=args.series,
        delta_pct=delta,
        as_of=args.as_of,
        pdf_paths=args.pdf or auto_pdfs(),
        use_llm=not args.no_llm,
        bidirectional=not args.no_bidirectional,
        note=note,
        stress=stress,
    )
    if not args.quiet:
        title = f"情景 {index}/{total}" if total > 1 else ("压力情景 S-1" if stress else "分析")
        cover = f"涨跌双向±{abs(delta):.0%}" if request.bidirectional else f"{delta:+.2%}"
        if stress:
            cover = f"压力情景 S-1 {delta:+.0%}（历史极端，触发 F-010 击穿）"
        print(f"\n{'=' * 72}\n▶ {title}：{args.series} {cover}\n{'=' * 72}")
    result = orchestrator.run(request, quiet=args.quiet)

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"\n[状态] {result.status}")
        if result.report_path:
            print(f"[报告] {result.report_path}")
        print(f"[日志] {result.log_path}")
        if result.warnings:
            print("[告警]")
            for w in result.warnings:
                print(f"  - {w}")
        if result.report and not args.quiet:
            print("\n" + result.report)
    return {"status": result.status, "report_path": result.report_path, "delta": delta}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.check_model:
        return run_check_model()

    if not args.quiet:
        print("模型配置：", json.dumps(settings.describe(), ensure_ascii=False))
        print("项目目录：", PROJECT_ROOT)

    orchestrator = Orchestrator()
    try:
        if args.stress:
            # 独立压力情景 S-1：-80%，只跑下跌方向；单独触发，绝不并入默认四情景批量
            args.no_bidirectional = True
            chain = Retriever(DATA_DIR).get_chain_config()
            s1 = next((s for s in chain["scenarios"] if s.get("id") == "S-1"), None)
            if s1 is None:
                print("[FAIL] 传导链配置中未定义压力情景 S-1（--stress 无法触发）")
                return 1
            r = run_once(
                args, float(s1["delta_pct"]), orchestrator,
                note=s1.get("note"), stress=True,
            )
            if not args.quiet:
                print(f"\n  S-1 {s1['name']} {s1['delta_pct']:+.0%} → {r['status']}"
                      f"（{s1.get('label')}；仅供压实 F-010 分支，不作行情预测）")
            return 0 if r["status"] == "ok" else 1

        if args.scenarios:
            # A-1/A-2/B-1/B-2 本身就是「涨/跌 × 温和/剧烈」的四个方向性情景，
            # 逐条跑时不再叠加双向，否则 A-1 与 A-2 会互相重复。压力情景 S-1 不走本批量。
            args.no_bidirectional = True
            chain = Retriever(DATA_DIR).get_chain_config()
            scenarios = [s for s in chain["scenarios"] if not s.get("stress_only")]
            results = [
                run_once(args, float(s["delta_pct"]), orchestrator, i, len(scenarios))
                for i, s in enumerate(scenarios, 1)
            ]
            if not args.quiet:
                print(f"\n{'=' * 72}\n情景分级汇总\n{'=' * 72}")
                for s, r in zip(scenarios, results):
                    print(
                        f"  {s['id']:<4} {s['name']:<8} {s['delta_pct']:+.0%}"
                        f"  [{s.get('class', '')}]  →  {r['status']}"
                    )
            return 0 if all(r["status"] == "ok" for r in results) else 1

        r = run_once(args, args.delta, orchestrator)
        return 0 if r["status"] == "ok" else 1
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())