"""Streamlit Web 入口：可视化演示用（决赛录屏）。

运行： .venv\\Scripts\\python.exe -m streamlit run streamlit_app.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent.orchestrator import AnalysisRequest, Orchestrator  # noqa: E402
from config import DATA_DIR, settings  # noqa: E402
from tools import calculator as calc  # noqa: E402
from tools.retriever import DEFAULT_PRICE_NAME, Retriever  # noqa: E402

st.set_page_config(page_title="产业链影响分析智能体", page_icon="🔗", layout="wide")

retriever = Retriever(DATA_DIR)
chain = retriever.get_chain_config()

st.title("产业链影响分析智能体")
st.caption(
    f"选题：碳酸锂价格波动的产业链影响分析 ｜ 传导链配置版本 {chain.get('version')} ｜ "
    f"输出：涨跌双向 × 四段结构（产业分析逻辑 / 证据充分性 / 量化合理性 / 理论应用边界）"
)
st.caption(f"核心红线：{chain.get('core_redline')}｜涨跌非对称：{(chain.get('asymmetry') or {}).get('rule')}")

# ---------------- 侧边栏：输入 ----------------
with st.sidebar:
    st.header("输入参数")
    series = st.selectbox("价格品种", retriever.list_price_names(), index=0)
    delta_pct = st.slider("原材料价格单边变动幅度（涨跌双向并列）", 0, 50, 15, step=1) / 100.0
    bidirectional = st.toggle("涨跌双向并列分析", value=True, help="框架 2.2：涨价转嫁系数与下跌红利释放系数为两套参数，必须分别测算")
    as_of = st.text_input("基准日期（留空取最新报价）", value="")
    api_key = st.text_input(
        "（可选）DeepSeek API Key",
        type="password",
        value="",
        help="留空 = 离线确定性模式（免 Key，全部数值由工具计算，可复现）。填写并勾选下方开关后，可调用模型生成更流畅的报告文字；若 Key 无效或调用失败，会自动降级为离线确定性报告，不会中断。",
    )
    use_llm = st.toggle(
        "调用大模型生成报告",
        value=False,
        disabled=not bool(api_key.strip()),
        help="默认离线。需先在上方填写有效 API Key 才能启用；未填 Key 时保持离线确定性输出（稳定、免 Key、可复现）。",
    )
    pdf_paths = [
        str(p) for p in sorted((DATA_DIR / "reports").glob("*.pdf"))
    ]
    use_pdf = st.checkbox("抽取财报 PDF 作为证据（Tool 1）", value=bool(pdf_paths), disabled=not pdf_paths)
    st.divider()
    st.json(settings.describe(), expanded=False)
    run = st.button("开始分析", type="primary", use_container_width=True)

    st.divider()
    with st.expander("公式库（Tool 3）"):
        st.dataframe(pd.DataFrame(calc.FORMULA_CATALOG), hide_index=True, use_container_width=True)

# ---------------- 主区 ----------------
if run:
    # 动态注入 API Key：默认离线（免 Key）；填写有效 Key 并勾选后才切 LLM 模式。
    # config.settings 为进程内单例，LLMClient/缓存在构造时引用同一对象，故须先更新再建 Orchestrator。
    key = (api_key or "").strip()
    settings.api_key = key
    settings.offline = not bool(key)
    request = AnalysisRequest(
        price_series=series,
        delta_pct=delta_pct,
        as_of=as_of.strip() or None,
        pdf_paths=pdf_paths if use_pdf else [],
        use_llm=use_llm,
        bidirectional=bidirectional,
    )
    with st.spinner("分析中：取数 → 计算 → 推理 → 生成报告 ..."):
        result = Orchestrator().run(request, quiet=True)

    if result.status != "ok":
        st.error("分析未完成：" + ("；".join(result.warnings) or "未知错误"))
        st.stop()

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("基准价", f"{result.price_base['price']:,.0f} {result.price_base['unit']}", result.price_base["date"])
    up_s, down_s = result.scenarios.get("up"), result.scenarios.get("down")
    if up_s:
        col2.metric(
            "涨价周期情景价",
            f"{up_s['new_price']:,.0f} {up_s['unit']}",
            f"{up_s['delta_pct']:+.1%}｜情景 {(up_s['scenario'] or {}).get('id')}",
        )
    if down_s:
        col3.metric(
            "下跌周期情景价",
            f"{down_s['new_price']:,.0f} {down_s['unit']}",
            f"{down_s['delta_pct']:+.1%}｜情景 {(down_s['scenario'] or {}).get('id')}",
            delta_color="inverse",
        )
    col4.metric("报告方式", result.llm.get("status", "-"))
    col5.metric("告警数", len(result.warnings))

    if result.warnings:
        with st.expander(f"⚠️ 告警 / 局限（{len(result.warnings)} 条）", expanded=True):
            for w in result.warnings:
                st.warning(w)

    tab_report, tab_table, tab_audit, tab_evidence, tab_log = st.tabs(
        ["四段结构报告", "涨跌对照与量化表", "计算审计", "证据与来源", "运行日志"]
    )

    with tab_report:
        st.markdown(result.report)
        st.download_button(
            "下载 Markdown 报告",
            data=result.report.encode("utf-8"),
            file_name=Path(result.report_path).name,
            mime="text/markdown",
        )

    with tab_table:
        st.subheader("涨跌对照表（③ 段口径：只出方向与强弱，绝对金额在下方明细）")
        side = []
        for s in result.segments:
            if s.get("status") != "ok":
                continue
            u = s["directions"].get("up") or {}
            d = s["directions"].get("down") or {}
            src = s.get("params_source") or {}
            side.append(
                {
                    "环节": s.get("node_name"),
                    "企业": s.get("company"),
                    "涨价周期": f"{u.get('label')}（{u.get('grade')}）",
                    "下跌周期": f"{d.get('label')}（{d.get('grade')}）",
                    "涨跌是否对称": "镜像" if abs(
                        (u.get("margin_shift_pct") or 0) + (d.get("margin_shift_pct") or 0)
                    ) < 1e-9 else "非对称",
                    "红利阻断": "是" if d.get("dividend_blocked") else ("否" if d.get("dividend_blocked") is not None else "—"),
                    "方向系数来源": "公开量化来源" if src.get("coefficient_public_source") else "模型假设（＊）",
                }
            )
        st.dataframe(pd.DataFrame(side), hide_index=True, use_container_width=True)
        st.caption(
            "「方向系数来源」与表格内 ＊ 标记联动：财务 / 规模参数有公开来源 ≠ 方向系数有公开来源。"
            "标为模型假设的环节，其涨跌结论只作定性推演，绝对金额不可对外引用。"
        )

        st.subheader("量化明细（含绝对金额，仅供审计，不得写入正文）")
        rows = []
        for s in result.segments:
            for key, label in (("up", "涨价周期"), ("down", "下跌周期")):
                d = s["directions"].get(key) or {}
                if d.get("calc_status") != "ok":
                    continue
                rows.append(
                    {
                        "环节": s.get("node_name"),
                        "企业": s.get("company"),
                        "代码": s.get("code"),
                        "方向": label,
                        "成本冲击": None if d.get("cost_impact_pct") is None else round(d["cost_impact_pct"] * 100, 3),
                        "售价传导": None if d.get("price_pass_pct") is None else round(d["price_pass_pct"] * 100, 3),
                        "毛利率前": None if d.get("margin_before") is None else round(d["margin_before"] * 100, 2),
                        "毛利率后": None if d.get("margin_after") is None else round(d["margin_after"] * 100, 2),
                        "毛利率变化(pct)": None if d.get("margin_shift_pct") is None else round(d["margin_shift_pct"] * 100, 3),
                        "基准营收(亿元)": None if d.get("base_revenue") is None else round(d["base_revenue"] / 1e8, 2),
                        "利润影响(亿元)": None if d.get("delta_profit") is None else round(d["delta_profit"] / 1e8, 2),
                        "取用系数": d.get("coefficient_field") or "定价机制联动（无人工系数）",
                    }
                )
        df = pd.DataFrame(rows)
        st.dataframe(df, hide_index=True, use_container_width=True)
        st.bar_chart(df.dropna(subset=["利润影响(亿元)"]), x="企业", y="利润影响(亿元)", color="方向")
        st.caption("百分比列单位：%；毛利率变化列单位：百分点（pct）。全部数值由 Tool 3 计算产出。")

    with tab_audit:
        audit = [
            {"环节/企业": s.get("company"), "方向": d.get("direction_label"), **c}
            for s in result.segments
            for d in s.get("directions", {}).values()
            for c in d.get("calculations", [])
        ]
        st.dataframe(pd.DataFrame(audit), hide_index=True, use_container_width=True)
        st.subheader("Tool 3 受限沙箱独立复算（交叉校验）")
        checks = []
        for s in result.segments:
            for d in s.get("directions", {}).values():
                for fid, check in (d.get("sandbox_checks") or {}).items():
                    checks.append(
                        {
                            "企业": s.get("company"),
                            "方向": d.get("direction_label"),
                            "校验公式": fid,
                            **check,
                        }
                    )
        st.dataframe(pd.DataFrame(checks), hide_index=True, use_container_width=True)
        all_ok = [
            check
            for s in result.segments
            for d in s.get("directions", {}).values()
            for check in (d.get("sandbox_checks") or {}).values()
        ]
        if all_ok and all(c.get("status") == "ok" for c in all_ok):
            st.success("全部关键数值经沙箱独立复算，与公式库结果一致。")

        st.subheader("强弱分级口径（F-011，相对口径）")
        st.json(chain.get("strength_grades"))
        st.caption("主导分档用相对口径 ρ=|Δm|/m₀（高 ρ≥0.20；中 0.05≤ρ<0.20；低 ρ<0.05）；基准毛利率非正时回退绝对口径；ρ 距阈值 ±0.5pct 带内标「临界」。")

    with tab_evidence:
        st.subheader("价格数据")
        st.json(result.price_base)
        st.subheader("企业参数来源")
        st.json([s.get("params_source") for s in result.segments])
        if result.pdf_evidence:
            st.subheader("财报 PDF 抽取证据（Tool 1）")
            for entry in result.pdf_evidence:
                st.markdown(f"**{entry['file']}**（{entry['engine']}，{entry['page_count']} 页）")
                st.json(entry["fields"])
                if entry["cross_checks"]:
                    st.dataframe(pd.DataFrame(entry["cross_checks"]), hide_index=True, use_container_width=True)

    with tab_log:
        log_file = Path(result.log_path)
        st.code(log_file.read_text(encoding="utf-8") if log_file.exists() else "日志文件缺失")
        st.download_button(
            "下载运行日志（JSONL）",
            data=log_file.read_bytes() if log_file.exists() else b"",
            file_name=log_file.name,
            mime="application/jsonl",
        )

    st.success(f"报告已落盘：{result.report_path}")
else:
    st.info("在左侧设置原材料价格变动幅度，然后点击「开始分析」。默认涨跌双向并列。")

    def _coefficient_source(company: str) -> str:
        try:
            params = retriever.get_params(company)
        except Exception:  # noqa: BLE001 - 界面降级：取不到参数不影响预览
            return "—"
        return "模型假设（＊）" if not params.get("coefficient_public_source") else "公开量化来源"

    st.subheader("已配置的传导链")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "环节": n["name"],
                    "企业": "、".join(n["companies"]),
                    "计算方法": n["method"],
                    "涨价方向角色": n.get("role_up"),
                    "下跌方向角色": n.get("role_down"),
                    "决定性变量": n.get("decisive_variable"),
                    "公式": "、".join(n["formulas"]),
                    "方向系数来源": "、".join(
                        f"{c}：{_coefficient_source(c)}" for c in n["companies"]
                    ),
                }
                for n in chain["nodes"]
            ]
        ),
        hide_index=True,
        use_container_width=True,
    )
    st.caption(
        "参数已替换为【2】交付的真实公开数据（S1~S43）。方向系数来源为「模型假设（＊）」的环节，"
        "表示券商研报中未检索到以此命名的正式量化测算，其涨跌结论只作定性推演。"
    )
    st.subheader("涨跌非对称（框架 2.2）")
    st.json(chain["asymmetry"])
    st.subheader("对照组")
    st.json(chain["control_group"])
    st.subheader("情景分级")
    st.dataframe(pd.DataFrame(chain["scenarios"]), hide_index=True, use_container_width=True)
    st.subheader("传导关系")
    st.json(chain["links"])