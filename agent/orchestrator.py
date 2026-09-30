"""编排器：输入 → 解析 → 取数 → 双向计算 → 推理 → 四段结构报告。

工作流（每步均写 JSONL 日志）
1. 校验输入（变动幅度上下限），确定「涨价 / 下跌」双向幅度
2. Tool 2 取基准价格（含来源/抓取时间）
3. Tool 3 算双向情景价格（F-007）
4. 逐环节双向测算
   - 上游 upstream_elasticity：F-006（敏感度直接取锂资源自给率）；下跌方向追加 F-010 现金成本线击穿判定
   - 正极 pricing_formula：F-001 → F-000 → F-002 → F-004 → F-009 → F-005（涨跌由定价机制机械联动，不用人工系数）
   - 电池/整车 cost_push：F-002 → F-008 → F-004 → F-009 → F-005；传导系数按涨跌方向分别取用
   - 每个方向追加 F-011 强弱分级
5. Tool 1 抽取财报 PDF 字段，与参数表交叉校验
6. LLM 串联产业逻辑并生成四段报告；失败或离线时降级为确定性报告
7. 落盘报告 + 日志，返回结构化结果

三条红线
- 报告中的所有数值均来自 Tool 3 返回值，LLM 只做文字串联；
- ③ 段只出「方向 + 强弱」，绝对金额一律下沉到附录（框架六.5：不输出精准盈利预测）；
- 涨跌非对称：涨价转嫁系数与下跌红利释放系数为两套参数，按方向取用，禁止正负取反换算（框架 2.2）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.llm_client import LLMClient
from agent.logger import RunLogger
from config import DATA_DIR, LOG_DIR, PROMPTS_DIR, REPORTS_DIR, Settings
from tools import calculator as calc
from tools import pdf_parser
from tools.retriever import DEFAULT_PRICE_NAME, Retriever

CN_TZ = timezone(timedelta(hours=8))
MAX_ABS_DELTA = 5.0  # 硬校验上限
BOUNDARY_ABS_DELTA = 0.5  # 超出即提示线性传导失效

SECTION_HEADERS = [
    "## ① 产业分析逻辑",
    "## ② 证据充分性",
    "## ③ 量化合理性",
    "## ④ 理论应用边界",
]

# 双向：涨价周期 / 下跌周期，一次运行内并列覆盖（框架六.1）
DIRECTIONS = (("up", "涨价周期"), ("down", "下跌周期"))
SIGN = {"up": 1.0, "down": -1.0}


@dataclass
class AnalysisRequest:
    price_series: str = DEFAULT_PRICE_NAME
    delta_pct: float = 0.15
    as_of: str | None = None
    pdf_paths: list[str] = field(default_factory=list)
    use_llm: bool = True
    bidirectional: bool = True
    note: str = ""
    stress: bool = False  # 独立压力情景 S-1（-80%），仅由 app.py --stress 触发，不进默认批量


@dataclass
class AnalysisResult:
    run_id: str
    status: str
    request: dict
    price_base: dict = field(default_factory=dict)
    scenarios: dict = field(default_factory=dict)  # {"up": {...}, "down": {...}}
    segments: list[dict] = field(default_factory=list)
    pdf_evidence: list[dict] = field(default_factory=list)
    report: str = ""
    report_path: str = ""
    log_path: str = ""
    llm: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    formulas_used: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["report"] = self.report[:2000] + ("...(截断)" if len(self.report) > 2000 else "")
        return d


def _fmt_money(yuan: float) -> str:
    if abs(yuan) >= 1e8:
        return f"{yuan / 1e8:,.2f} 亿元"
    if abs(yuan) >= 1e4:
        return f"{yuan / 1e4:,.2f} 万元"
    return f"{yuan:,.2f} 元"


def _fmt_pct(value: float, digits: int = 2) -> str:
    return f"{value * 100:+.{digits}f}%"


def _fmt_ppt(value: float, digits: int = 2) -> str:
    """毛利率变化用「百分点」表述，避免与百分比混淆。"""
    return f"{value * 100:+.{digits}f} pct"


def _grade_thresholds(chain: dict) -> dict:
    """强弱分级阈值来自传导链配置（框架：强弱判断也必须由工具产出，不能由模型自定）。

    v6：主导分档用相对口径（ρ=|Δm|/m0）；高 ρ≥0.20、中 0.05≤ρ<0.20、低 ρ<0.05；
    另含临界判定带 critical_band（ρ 距阈值 ±0.5pct 带内标「临界」）。
    """
    raw = (chain.get("strength_grades") or {}).get("thresholds") or {}
    return {
        "high": float(raw.get("high", 0.20)),
        "medium": float(raw.get("medium", 0.05)),
        "critical_band": float(
            (chain.get("strength_grades") or {}).get(
                "critical_band", calc.DEFAULT_CRITICAL_BAND
            )
        ),
    }


def _direction_label(delta_profit: float | None) -> str:
    if delta_profit is None:
        return "未计算"
    if delta_profit > 0:
        return "受益"
    if delta_profit < 0:
        return "承压"
    return "中性"


def _scenario_of(chain: dict, delta: float) -> dict:
    """把本次幅度对回配置里的情景编号（A-1/A-2/B-1/B-2）。"""
    for s in chain.get("scenarios") or []:
        if abs(float(s.get("delta_pct", 0)) - delta) < 1e-9:
            return {"id": s.get("id"), "name": s.get("name"), "class": s.get("class")}
    return {"id": "自定义", "name": "自定义幅度", "class": "未在配置的情景分级内"}


class Orchestrator:
    def __init__(
        self,
        data_dir: Path = DATA_DIR,
        settings: Settings | None = None,
        report_dir: Path = REPORTS_DIR,
        log_dir: Path = LOG_DIR,
    ):
        self.settings = settings or Settings()
        self.retriever = Retriever(data_dir)
        self.report_dir = Path(report_dir)
        self.log_dir = Path(log_dir)
        # 允许调用方（如单元测试）指定独立目录，避免污染正式产物
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.llm = LLMClient(self.settings, self.log_dir / "cache")

    # ---------------- 主流程 ----------------

    def run(self, request: AnalysisRequest, quiet: bool = False) -> AnalysisResult:
        logger = RunLogger(self.log_dir, quiet=quiet)
        result = AnalysisResult(
            run_id=logger.run_id,
            status="running",
            request=asdict(request),
            log_path=str(logger.path),
        )
        try:
            self._run_pipeline(request, logger, result)
            result.status = "ok"
            logger.event("run", "完成", status="ok", warnings=len(result.warnings))
        except (ValueError, KeyError, FileNotFoundError) as exc:
            result.status = "failed"
            result.warnings.append(f"运行失败：{type(exc).__name__}: {exc}")
            logger.event("run", "失败", error=f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 —— 异常输入不崩溃：兜底为 failed 结果
            result.status = "failed"
            result.warnings.append(f"未预期错误：{type(exc).__name__}: {exc}")
            logger.event("run", "异常", error=f"{type(exc).__name__}: {exc}")
        return result

    def _run_pipeline(
        self, request: AnalysisRequest, logger: RunLogger, result: AnalysisResult
    ) -> None:
        delta = self._validate_delta(request.delta_pct)
        magnitude = abs(delta)
        directions = DIRECTIONS if request.bidirectional else (
            ("up", "涨价周期") if delta > 0 else ("down", "下跌周期"),
        )
        logger.event(
            "step1",
            "校验输入",
            delta_pct=delta,
            magnitude=magnitude,
            directions=[k for k, _ in directions],
            as_of=request.as_of,
        )
        if magnitude > BOUNDARY_ABS_DELTA:
            result.warnings.append(
                f"变动幅度 ±{magnitude:.0%} 已超出 ±50% 适用边界（情景 B 剧烈波动），"
                "线性传导假设可能失效：需求大幅萎缩、上游大规模减产保价、技术替代等非线性因素将主导"
            )

        chain = self.retriever.get_chain_config()
        logger.event("step1", "读取传导链配置", topic=chain.get("topic"), version=chain.get("version"))

        # --- 步骤 2：取基准价格 ---
        base = self._tool2_price(request, logger)
        result.price_base = base
        logger.event("step2", "基准价格就绪", price=base["price"], date=base["date"], source=base["source"])

        # --- 步骤 3：算双向情景价格 ---
        scenarios: dict[str, dict] = {}
        for key, _label in directions:
            scenarios[key] = self._tool3_scenario_price(
                base, SIGN[key] * magnitude, key, chain, logger
            )
        result.scenarios = scenarios

        # --- 步骤 4：逐环节双向测算 ---
        grades = _grade_thresholds(chain)
        for node in chain["nodes"]:
            segment = self._calc_node(node, base, scenarios, magnitude, chain, grades, directions, logger, result)
            result.segments.append(segment)

        result.formulas_used = sorted(
            {f for s in result.segments for f in s["formulas_used"]}
        )

        # --- 步骤 5：PDF 证据抽取与交叉校验 ---
        result.pdf_evidence = self._extract_pdf_evidence(request.pdf_paths, result, logger)

        # --- 步骤 6：生成报告（四段：①②③④） ---
        context = self._build_context(request, base, scenarios, chain, directions, magnitude, result)
        llm_info, report = self._generate_report(context, request, logger, result)
        result.llm = llm_info
        result.report = report

        # --- 步骤 7：落盘 + 只保留最新一份报告与一份日志 ---
        report_path = self.report_dir / f"报告_{result.run_id}.md"
        report_path.write_text(report, encoding="utf-8")
        result.report_path = str(report_path)
        self._prune_artifacts(keep_report=report_path, keep_log=Path(result.log_path))
        logger.event("step7", "报告落盘", path=str(report_path), chars=len(report))
        logger.event("run", "运行日志", path=str(logger.path))

    def _prune_artifacts(self, keep_report: Path, keep_log: Path) -> None:
        """红线：每次运行只保留最新一份报告与一份 JSONL 日志。"""
        for p in self.report_dir.glob("报告_*.md"):
            if p.resolve() != Path(keep_report).resolve():
                try:
                    p.unlink()
                except OSError:
                    pass
        for p in self.log_dir.glob("run_*.jsonl"):
            if p.resolve() != Path(keep_log).resolve():
                try:
                    p.unlink()
                except OSError:
                    pass

    # ---------------- 各步骤实现 ----------------

    def _validate_delta(self, delta_pct) -> float:
        if isinstance(delta_pct, bool) or not isinstance(delta_pct, (int, float)):
            raise ValueError(f"变动幅度必须是数字，收到：{delta_pct!r}")
        delta = float(delta_pct)
        if delta != delta or delta in (float("inf"), float("-inf")):
            raise ValueError(f"变动幅度必须是有限数值，收到：{delta_pct!r}")
        if delta <= -1:
            raise ValueError(f"变动幅度不能 ≤ -100%（收到 {delta:+.2%}）")
        if abs(delta) > MAX_ABS_DELTA:
            raise ValueError(f"变动幅度超出允许范围 ±{MAX_ABS_DELTA * 100:.0f}%（收到 {delta:+.2%}）")
        return delta

    def _tool2_price(self, request: AnalysisRequest, logger: RunLogger) -> dict:
        t0 = time.perf_counter()
        try:
            base = self.retriever.get_latest_price(request.price_series, request.as_of)
        except KeyError as exc:
            logger.tool_call(
                "Tool2.get_latest_price",
                {"name": request.price_series, "as_of": request.as_of},
                "error",
                error=str(exc),
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )
            raise
        logger.tool_call(
            "Tool2.get_latest_price",
            {"name": request.price_series, "as_of": request.as_of},
            "ok",
            result=base,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )
        return base

    def _tool3_scenario_price(
        self, base: dict, delta: float, key: str, chain: dict, logger: RunLogger
    ) -> dict:
        t0 = time.perf_counter()
        out = calc.call_formula("F-007", base_price=base["price"], delta_pct=delta)
        logger.tool_call(
            "Tool3.call_formula",
            {"formula": "F-007", "base_price": base["price"], "delta_pct": delta, "direction": key},
            "ok",
            result=out,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )
        label = _scenario_of(chain, delta)
        return {
            "direction": key,
            "direction_label": dict(DIRECTIONS)[key],
            "formula": out["formula"],
            "delta_pct": delta,
            "new_price": out["result"]["new_price"],
            "unit": out["result"]["unit"],
            "scenario": label,
            "calculation": out,
        }

    def _calc_node(
        self,
        node: dict,
        base: dict,
        scenarios: dict,
        magnitude: float,
        chain: dict,
        grades: dict,
        directions: tuple,
        logger: RunLogger,
        result: AnalysisResult,
    ) -> dict:
        company = node["companies"][0]
        t0 = time.perf_counter()
        try:
            params = self.retriever.get_params(company)
        except KeyError as exc:
            logger.tool_call("Tool2.get_params", {"company": company}, "error", error=str(exc))
            return self._empty_segment(node, company, f"参数缺失：{exc}", directions, result)
        logger.tool_call(
            "Tool2.get_params",
            {"company": company},
            "ok",
            result={k: v for k, v in params.items() if k not in ("note",)},
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )

        method = node.get("method")
        calculators = {
            "upstream_elasticity": self._calc_upstream,
            "pricing_formula": self._calc_cathode,
            "cost_push": self._calc_cost_push,
        }
        if method not in calculators:
            return self._empty_segment(node, company, f"未知计算方法：{method}", directions, result)

        try:
            segment = calculators[method](node, params, base, scenarios, magnitude, grades, directions, logger)
        except ValueError as exc:
            return self._empty_segment(node, company, f"参数非法：{exc}", directions, result)

        segment["control_group"] = node["id"] == (chain.get("control_group") or {}).get("node_id")
        segment["params_source"] = self._source_of(params)

        # Tool 3 的受限沙箱独立复算关键指标，与公式库结果交叉校验
        for key, _label in directions:
            direction = segment["directions"][key]
            direction["method"] = method
            direction["sandbox_checks"] = self._sandbox_checks(
                direction, result, company, key, with_grade=(key == "up")
            )

        logger.event(
            "step4",
            f"环节测算 · {node['name']}",
            company=company,
            method=method,
            **{
                f"{k}_{d}": segment["directions"][d].get(k)
                for d in segment["directions"]
                for k in ("delta_pct", "coefficient_field", "margin_shift_pct", "grade")
            },
            dividend_blocked=(segment["directions"].get("down") or {}).get("dividend_blocked"),
            formulas=segment["formulas_used"],
        )
        return segment

    # ---- 三种计算方法的双向实现（每个方向独立取系数、独立出强弱） ----

    def _calc_upstream(
        self, node, params, base, scenarios, magnitude, grades, directions, logger
    ) -> dict:
        sensitivity = params.get("self_sufficiency")
        if sensitivity is None:
            raise ValueError("缺少 self_sufficiency（锂资源自给率）；上游受益强弱由自给率决定，不能拍值")
        if params.get("base_volume") is None:
            raise ValueError("缺少 base_volume（基准销量）")
        # 上游售价即碳酸锂价格本身 → 采用已溯源的检索价，不用参数表自填价
        base_price = base["price"]
        margin_before = params.get("gross_margin")
        cash_cost_line = params.get("cash_cost_line")

        out_dirs: dict[str, dict] = {}
        for key, _label in directions:
            delta = SIGN[key] * magnitude
            out = calc.call_formula(
                "F-006",
                base_price=base_price,
                base_volume=params["base_volume"],
                price_change_pct=delta,
                sensitivity=sensitivity,
            )
            r = out["result"]
            calcs = [out]
            dividend_blocked = None
            notes = []
            if key == "down":
                if cash_cost_line is None:
                    notes.append("缺 cash_cost_line（现金成本线），本次未做红利阻断判定")
                else:
                    breach = calc.call_formula(
                        "F-010",
                        scenario_price=scenarios["down"]["new_price"],
                        cash_cost_line=cash_cost_line,
                    )
                    calcs.append(breach)
                    dividend_blocked = bool(breach["result"]["breached"])
                    notes.append(
                        f"现金成本线判定（F-010）：情景价 {scenarios['down']['new_price']:,.0f} 元/吨 "
                        f"对现金成本线 {cash_cost_line:,.0f} 元/吨 → "
                        + ("已击穿，按框架 2.2 应视为减产保价、阻断红利下传" if dividend_blocked
                           else "未击穿，红利仍可下传")
                    )
            grade = calc.call_formula(
                "F-011", margin_change_pct=r["margin_shift_pct"], m0=margin_before, **grades
            )
            calcs.append(grade)
            out_dirs[key] = {
                "direction": key,
                "direction_label": dict(DIRECTIONS)[key],
                "delta_pct": delta,
                "calc_status": "ok",
                "cost_impact_pct": None,
                "price_pass_pct": None,
                "margin_before": margin_before,
                "margin_after": (margin_before + r["margin_shift_pct"])
                if margin_before is not None
                else None,
                "margin_shift_pct": r["margin_shift_pct"],
                "delta_profit": r["delta_gross_profit"],
                "base_revenue": r["base_revenue"],
                "revenue_after": r["new_revenue"],
                "coefficient_field": "self_sufficiency",
                "coefficient_value": sensitivity,
                "label": _direction_label(r["delta_gross_profit"]),
                "grade": grade["result"]["grade"],
                "dividend_blocked": dividend_blocked,
                "formulas_used": ["F-006"] + (["F-010"] if key == "down" and cash_cost_line else []) + ["F-011"],
                "calculations": calcs,
                "notes": notes,
            }

        return {
            "node_id": node["id"],
            "node_name": node["name"],
            "company": params["company"],
            "code": params["code"],
            "segment": params["segment"],
            "role": params.get("role") or node["role"],
            "role_up": node.get("role_up"),
            "role_down": node.get("role_down"),
            "decisive_variable": node.get("decisive_variable"),
            "method": "upstream_elasticity",
            "base_price": base_price,
            "base_price_unit": params.get("base_price_unit") or base["unit"],
            "base_volume": params["base_volume"],
            "volume_unit": params.get("volume_unit"),
            "status": "ok",
            "directions": out_dirs,
            "formulas_used": sorted({f for d in out_dirs.values() for f in d["formulas_used"]}),
            "notes": [
                "上游售价即碳酸锂价格，采用 Tool 2 检索价而非参数表自填值",
                "F-006 的敏感度直接取锂资源自给率（框架 2.4：自有矿部分全额受益、外购部分受益被抵消）",
            ],
        }

    def _calc_cathode(
        self, node, params, base, scenarios, magnitude, grades, directions, logger
    ) -> dict:
        if params.get("unit_consumption") is None or params.get("processing_fee") is None:
            raise ValueError("缺少 unit_consumption（单耗系数）或 processing_fee（加工费）")
        if params.get("lithium_cost_share") is None or params.get("gross_margin") is None:
            raise ValueError("缺少 lithium_cost_share 或 gross_margin")

        p_base = calc.call_formula(
            "F-001",
            lithium_price=base["price"],
            unit_consumption=params["unit_consumption"],
            processing_fee=params["processing_fee"],
        )
        out_dirs: dict[str, dict] = {}
        for key, _label in directions:
            delta = SIGN[key] * magnitude
            p_new = calc.call_formula(
                "F-001",
                lithium_price=scenarios[key]["new_price"],
                unit_consumption=params["unit_consumption"],
                processing_fee=params["processing_fee"],
            )
            d_price = calc.call_formula(
                "F-000", base=p_base["result"]["cathode_price"], new=p_new["result"]["cathode_price"]
            )
            cost_impact = calc.call_formula(
                "F-002",
                total_cost_share=params["lithium_cost_share"],
                price_change_pct=delta,
                passthrough_pct=1.0,
            )
            margin = calc.call_formula(
                "F-004",
                gross_margin=params["gross_margin"],
                price_change_pct=d_price["result"]["price_change_pct"],
                cost_change_pct=cost_impact["result"]["cost_impact_pct"],
            )
            rev = calc.call_formula(
                "F-009", base_price=p_base["result"]["cathode_price"], base_volume=params["base_volume"]
            )
            profit = calc.call_formula(
                "F-005",
                margin=params["gross_margin"],
                revenue_base=rev["result"]["base_revenue"],
                margin_shift=margin["result"]["margin_change_pct"],
            )
            grade = calc.call_formula(
                "F-011", margin_change_pct=margin["result"]["margin_change_pct"], m0=params["gross_margin"], **grades
            )
            calcs = [p_new, d_price, cost_impact, margin, rev, profit, grade]
            if key == "up":
                # 基准售价由锂定价公式算出，只在涨价方向记录一次，避免审计表重复
                calcs.append(p_base)
            out_dirs[key] = {
                "direction": key,
                "direction_label": dict(DIRECTIONS)[key],
                "delta_pct": delta,
                "calc_status": "ok",
                "price_after": p_new["result"]["cathode_price"],
                "cost_impact_pct": cost_impact["result"]["cost_impact_pct"],
                "price_pass_pct": d_price["result"]["price_change_pct"],
                "margin_before": params["gross_margin"],
                "margin_after": margin["result"]["margin_after_pct"],
                "margin_shift_pct": margin["result"]["margin_change_pct"],
                "delta_profit": profit["result"]["delta_profit"],
                "base_revenue": rev["result"]["base_revenue"],
                "coefficient_field": None,
                "coefficient_value": None,
                "label": _direction_label(profit["result"]["delta_profit"]),
                "grade": grade["result"]["grade"],
                "dividend_blocked": None,
                "formulas_used": ["F-001", "F-000", "F-002", "F-004", "F-009", "F-005", "F-011"],
                "calculations": calcs,
                "notes": ["本环节不使用人工传导系数：涨跌双向均由锂定价机制机械联动"],
            }

        return {
            "node_id": node["id"],
            "node_name": node["name"],
            "company": params["company"],
            "code": params["code"],
            "segment": params["segment"],
            "role": params.get("role") or node["role"],
            "role_up": node.get("role_up"),
            "role_down": node.get("role_down"),
            "decisive_variable": node.get("decisive_variable"),
            "method": "pricing_formula",
            "base_price": p_base["result"]["cathode_price"],
            "base_price_unit": "元/吨（正极售价，由锂定价公式算出）",
            "base_volume": params["base_volume"],
            "volume_unit": params.get("volume_unit"),
            "status": "ok",
            "directions": out_dirs,
            "formulas_used": sorted({f for d in out_dirs.values() for f in d["formulas_used"]}),
            "notes": [
                "正极执行锂定价机制：售价随锂价联动，成本冲击按锂成本占比计入",
                "该环节的涨跌对称性由定价机制决定，不由人工系数决定（框架 2.4）",
            ],
        }

    def _calc_cost_push(
        self, node, params, base, scenarios, magnitude, grades, directions, logger
    ) -> dict:
        for key, label in (
            ("lithium_cost_share", "锂成本占比"),
            ("gross_margin", "毛利率"),
            ("base_price", "基准单价"),
            ("base_volume", "基准销量"),
        ):
            if params.get(key) is None:
                raise ValueError(f"缺少 {key}（{label}）")

        out_dirs: dict[str, dict] = {}
        for key, _label in directions:
            delta = SIGN[key] * magnitude
            # 涨跌非对称：两个方向的系数分列取用，禁止正负取反换算（框架 2.2）
            field = "pass_through_up" if key == "up" else "dividend_release_down"
            coefficient = params.get(field)
            if coefficient is None:
                raise ValueError(
                    f"缺少 {field}（{'涨价转嫁系数' if key == 'up' else '下跌红利释放系数'}）；"
                    "两套参数不可混用，亦不可由另一方向换算补齐"
                )
            cost_impact = calc.call_formula(
                "F-002",
                total_cost_share=params["lithium_cost_share"],
                price_change_pct=delta,
                passthrough_pct=1.0,
            )
            pass_pct = calc.call_formula(
                "F-008",
                cost_impact_pct=cost_impact["result"]["cost_impact_pct"],
                pass_through=coefficient,
            )
            margin = calc.call_formula(
                "F-004",
                gross_margin=params["gross_margin"],
                price_change_pct=pass_pct["result"]["price_pass_pct"],
                cost_change_pct=cost_impact["result"]["cost_impact_pct"],
            )
            rev = calc.call_formula(
                "F-009", base_price=params["base_price"], base_volume=params["base_volume"]
            )
            profit = calc.call_formula(
                "F-005",
                margin=params["gross_margin"],
                revenue_base=rev["result"]["base_revenue"],
                margin_shift=margin["result"]["margin_change_pct"],
            )
            grade = calc.call_formula(
                "F-011", margin_change_pct=margin["result"]["margin_change_pct"], m0=params["gross_margin"], **grades
            )
            logger.event(
                "step4",
                f"系数取用 · {params['company']}",
                direction=key,
                delta_pct=delta,
                coefficient_field=field,
                coefficient_value=coefficient,
            )
            out_dirs[key] = {
                "direction": key,
                "direction_label": dict(DIRECTIONS)[key],
                "delta_pct": delta,
                "calc_status": "ok",
                "cost_impact_pct": cost_impact["result"]["cost_impact_pct"],
                "price_pass_pct": pass_pct["result"]["price_pass_pct"],
                "margin_before": params["gross_margin"],
                "margin_after": margin["result"]["margin_after_pct"],
                "margin_shift_pct": margin["result"]["margin_change_pct"],
                "delta_profit": profit["result"]["delta_profit"],
                "base_revenue": rev["result"]["base_revenue"],
                "coefficient_field": field,
                "coefficient_value": coefficient,
                "label": _direction_label(profit["result"]["delta_profit"]),
                "grade": grade["result"]["grade"],
                "dividend_blocked": None,
                "formulas_used": ["F-002", "F-008", "F-004", "F-009", "F-005", "F-011"],
                "calculations": [cost_impact, pass_pct, margin, rev, profit, grade],
                "notes": [
                    f"{dict(DIRECTIONS)[key]}取用系数列 {field}={coefficient}",
                    "成本全额暴露（F-002 传导系数取 1.0），售价按该方向的议价能力部分传导（F-008）",
                ],
            }

        return {
            "node_id": node["id"],
            "node_name": node["name"],
            "company": params["company"],
            "code": params["code"],
            "segment": params["segment"],
            "role": params.get("role") or node["role"],
            "role_up": node.get("role_up"),
            "role_down": node.get("role_down"),
            "decisive_variable": node.get("decisive_variable"),
            "method": "cost_push",
            "base_price": params["base_price"],
            "base_price_unit": params.get("base_price_unit"),
            "base_volume": params["base_volume"],
            "volume_unit": params.get("volume_unit"),
            "status": "ok",
            "directions": out_dirs,
            "formulas_used": sorted({f for d in out_dirs.values() for f in d["formulas_used"]}),
            "notes": ["涨跌双向分别取用 pass_through_up / dividend_release_down，两套系数不可混用"],
        }

    # ---- 沙箱复核 ----

    def _sandbox_checks(
        self, direction: dict, result: AnalysisResult, company: str, key: str, with_grade: bool
    ) -> dict:
        """用受限沙箱独立复算关键公式，作为数值的第二份独立证据。"""
        targets: list[str] = []
        primary = calc.PRIMARY_CHECK.get(direction.get("method"))
        if primary:
            targets.append(primary)
        if with_grade:
            targets.append("F-011")
        if direction.get("dividend_blocked") is not None:
            targets.append("F-010")

        checks: dict[str, dict] = {}
        for fid in dict.fromkeys(targets):
            target = next(
                (c for c in direction.get("calculations", []) if calc.formula_id_of(c) == fid), None
            )
            if target is None:
                continue
            t0 = time.perf_counter()
            check = calc.verify_in_sandbox(target)
            check["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            if check["status"] != "ok":
                result.warnings.append(
                    f"{company}（{direction['direction_label']}）沙箱独立复核未通过："
                    f"{check.get('formula')} {check['status']} {check.get('error', '')}"
                )
            checks[fid] = check
        return checks

    def _empty_segment(
        self, node: dict, company: str, reason: str, directions: tuple, result: AnalysisResult
    ) -> dict:
        result.warnings.append(f"{company}：{reason}")
        pairs = directions if directions else DIRECTIONS
        return {
            "node_id": node["id"],
            "node_name": node["name"],
            "company": company,
            "code": None,
            "role": node.get("role"),
            "role_up": node.get("role_up"),
            "role_down": node.get("role_down"),
            "decisive_variable": node.get("decisive_variable"),
            "method": node.get("method"),
            "status": "skipped",
            "reason": reason,
            "control_group": False,
            "params_source": {},
            "formulas_used": [],
            "directions": {
                key: {
                    "direction": key,
                    "direction_label": label,
                    "calc_status": "skipped",
                    "label": "未计算",
                    "grade": None,
                    "calculations": [],
                    "sandbox_checks": {},
                    "notes": [],
                }
                for key, label in pairs
            },
            "notes": [],
        }

    def _source_of(self, params: dict) -> dict:
        source = params.get("source")
        return {
            "source": source,
            "source_url": params.get("source_url"),
            "note": params.get("note"),
            "is_placeholder": "占位" in (source or ""),
            "has_public_source": bool(params.get("has_public_source")),
            # 财务/规模参数有来源 ≠ 方向系数有来源，两者分开标注（【2】口径）
            "coefficient_public_source": bool(params.get("coefficient_public_source")),
        }

    def _extract_pdf_evidence(
        self, pdf_paths: list[str], result: AnalysisResult, logger: RunLogger
    ) -> list[dict]:
        evidence: list[dict] = []
        if not pdf_paths:
            logger.event("step5", "未提供财报 PDF，跳过字段抽取")
            return evidence

        for raw_path in pdf_paths:
            path = Path(raw_path)
            t0 = time.perf_counter()
            try:
                parsed = pdf_parser.parse_pdf(path)
                fields = pdf_parser.extract_fields(
                    parsed, ["gross_margin", "revenue", "lithium_cost_share", "unit_consumption"]
                )
            except (FileNotFoundError, ValueError, RuntimeError) as exc:
                logger.tool_call("Tool1.parse_pdf", {"path": str(path)}, "error", error=str(exc))
                result.warnings.append(f"PDF 解析失败（已跳过）：{path.name} · {exc}")
                continue

            logger.tool_call(
                "Tool1.parse_pdf + extract_fields",
                {"path": str(path)},
                "ok",
                result={"document": parsed.to_dict(), "fields": {k: v.to_dict() for k, v in fields.items()}},
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )
            if parsed.scan_warning:
                result.warnings.append(f"{path.name} 文本量极低，疑似扫描件，字段抽取可能不可用")

            entry = {
                "file": parsed.filename,
                "path": parsed.path,
                "engine": parsed.engine,
                "page_count": parsed.page_count,
                "fields": {k: v.to_dict() for k, v in fields.items()},
                "cross_checks": self._cross_check(fields, result),
            }
            evidence.append(entry)
        return evidence

    def _cross_check(self, fields: dict, result: AnalysisResult) -> list[dict]:
        """用 PDF 抽取值校验参数表（参数表是占位值时，这是发现偏差的关键手段）。"""
        checks = []
        pairs = [
            ("lithium_cost_share", "湖南裕能", "锂成本占比"),
            ("gross_margin", "湖南裕能", "毛利率"),
        ]
        for field_name, company, label in pairs:
            hit = fields.get(field_name)
            if not hit:
                continue
            try:
                param = self.retriever.get_params(company)
            except KeyError:
                continue
            key = "lithium_cost_share" if field_name == "lithium_cost_share" else "gross_margin"
            table_value = param.get(key)
            if table_value is None or hit.value is None:
                continue
            pdf_value = hit.value / 100 if hit.unit == "%" else hit.value
            if table_value == 0:
                continue
            diff = abs(pdf_value - table_value) / table_value
            status = "一致" if diff <= 0.15 else "偏差较大"
            if diff > 0.15:
                result.warnings.append(
                    f"{company}{label}：财报抽取 {pdf_value:.2%} 与参数表 {table_value:.2%} 偏差 {diff:.0%}，"
                    f"建议以年报为准更新参数表"
                )
            checks.append(
                {
                    "company": company,
                    "field": key,
                    "pdf_value": round(pdf_value, 4),
                    "table_value": table_value,
                    "relative_diff": round(diff, 4),
                    "status": status,
                    "evidence": hit.evidence,
                    "page": hit.page,
                    "source_file": hit.source_file,
                }
            )
        return checks

    # ---------------- 报告生成 ----------------

    @staticmethod
    def _qualitative(segment: dict) -> bool:
        """该环节的方向系数是否只能定性推演。

        【2】交付口径：券商研报中未检索到以「涨价成本转嫁系数」「下跌红利释放系数」
        命名的正式量化测算 → 系数列为模型假设，正文须带 ＊ 并声明定性推演。
        财务/规模/价格有公开来源，不能与系数来源混为一谈。
        """
        src = segment.get("params_source") or {}
        return not (src.get("has_public_source") and src.get("coefficient_public_source"))

    def _direction_cell(self, direction: dict, qualitative: bool) -> str:
        if direction.get("calc_status") != "ok":
            return "未计算"
        text = f"{direction['label']}（{direction['grade']}）"
        if direction.get("dividend_blocked"):
            text += "·红利阻断"
        return text + ("＊" if qualitative else "")

    def _body_quant_table(self, segments: list[dict]) -> str:
        """③ 段正文表格：只出方向与强弱，绝对金额一律下沉附录（框架六.5）。"""
        lines = [
            "| 环节 | 企业 | 涨价周期 | 下跌周期 | 强弱等级（涨 / 跌） |",
            "|---|---|---|---|---|",
        ]
        for s in segments:
            up = s["directions"].get("up") or {}
            down = s["directions"].get("down") or {}
            if s.get("status") != "ok":
                lines.append(f"| {s.get('node_name')} | {s.get('company')} | 未计算 | 未计算 | — |")
                continue
            qualitative = self._qualitative(s)
            grades = f"{up.get('grade') or '—'} / {down.get('grade') or '—'}"
            lines.append(
                f"| {s.get('node_name')} | {s.get('company')} | "
                f"{self._direction_cell(up, qualitative)} | {self._direction_cell(down, qualitative)} | {grades} |"
            )
        return "\n".join(lines)

    def _control_group_conclusion(self, chain: dict, segments: list[dict]) -> str:
        """④ 段强制结论：同一锂价冲击、结构不同、结果不同（框架六.3）。"""
        cg = chain.get("control_group") or {}
        target = next((s for s in segments if s.get("control_group")), None)
        if not target or target.get("status") != "ok":
            return (
                "【对照组结论】本次运行未取到对照组（比亚迪）的可比结果，"
                "无法完成「同一冲击、结构不同、结果不同」的对照论证，需补齐参数后复跑。"
            )

        def view(segment: dict, key: str) -> str:
            d = (segment["directions"] or {}).get(key) or {}
            if d.get("calc_status") != "ok":
                return "未覆盖（本次未测算该方向）"
            return f"{d.get('label')}（强度{d.get('grade')}）"

        peers = [
            s for s in segments
            if s.get("status") == "ok" and s.get("method") == "cost_push" and not s.get("control_group")
        ]
        peer_view = [f"{s['company']}（{view(s, 'up')}/{view(s, 'down')}）" for s in peers]
        return (
            f"【对照组结论】对照组 {target['company']}（垂直一体化）在同一锂价冲击下："
            f"涨价周期 {view(target, 'up')}、下跌周期 {view(target, 'down')}，"
            f"而同一冲击下的纯玩家环节为 " + ("、".join(peer_view) or "无")
            + "。其一体化程度越高、电池/原材料自供比例越大，对外传导越弱、经营结果越平稳——"
            "**同一锂价冲击下，企业结构不同，结果完全不同**"
           + f"（对照口径：{cg.get('comparison_rule', '')}）。"
        )

    def _sensitivity_engine(self, chain: dict, base: dict, magnitude: float) -> dict:
        """敏感度引擎（输入无关：固定敏感性系数）＋ 按本次 ΔP 生成的示例表。

        Δm = 敏感度 pct/万元 × ΔP 万元/吨 / 100（F-013）；相对强弱由 F-011 以 ρ=|Δm|/m0 分级。
        """
        delta_p_wan = round(magnitude * base["price"] / 1e4, 2)
        rows = []
        for node in chain.get("nodes") or []:
            sens = node.get("sensitivity_pct_per_wan")
            if sens is None:
                continue
            company = (node.get("companies") or [""])[0]
            m0 = None
            try:
                m0 = self.retriever.get_params(company).get("gross_margin")
            except KeyError:
                pass
            ex = calc.call_formula(
                "F-013", delta_p_wan=delta_p_wan, sensitivity=sens, m0=m0 or 1.0
            )
            grade = None
            if m0 and m0 > 0 and ex["result"]["delta_m"] is not None and abs(ex["result"]["delta_m"]) > 1e-12:
                grade = calc.call_formula(
                    "F-011", margin_change_pct=ex["result"]["delta_m"], m0=m0
                )["result"]["grade"]
            rows.append(
                {
                    "node": node.get("name"),
                    "company": company,
                    "sensitivity": round(sens, 4),
                    "coefficients": node.get("sensitivity_coefficients"),
                    "method": node.get("method"),
                    "delta_m": ex["result"]["delta_m"],
                    "rho": ex["result"]["rho"],
                    "grade": grade,
                }
            )
        return {"delta_p_wan": delta_p_wan, "rows": rows}

    def _breach_critical(self, chain: dict, base: dict) -> dict:
        """③ 段常驻的击穿临界口径：跌到 100×(现金成本线/基准价 − 1) 才触发红利阻断。"""
        cash_line = None
        try:
            cash_line = self.retriever.get_params("天齐锂业").get("cash_cost_line")
        except KeyError:
            pass
        critical_pct = (100 * (cash_line / base["price"] - 1)) if cash_line else None
        return {
            "critical_breach_pct": round(critical_pct, 1) if critical_pct is not None else None,
            "cash_cost_line": cash_line,
            "base_price": base["price"],
            "source": "上游现金成本线取自券测/媒体测算口径，非年报正式披露项",
        }

    def _stress_info(self, chain: dict, enabled: bool) -> dict:
        """压力情景 S-1（-80%）信息；仅 enabled 时视作已触发，否侧提供说明。"""
        s1 = next((s for s in chain.get("scenarios") or [] if s.get("id") == "S-1"), None)
        if not s1:
            return {"enabled": False, "note": "传导链配置中未定义压力情景 S-1"}
        return {
            "enabled": int(bool(enabled)),
            "id": s1.get("id"),
            "name": s1.get("name"),
            "delta_pct": s1.get("delta_pct"),
            "scenario_price": None,
            "label": s1.get("label") or s1.get("range"),
            "note": s1.get("note"),
            "triggered": bool(enabled),
        }

    def _build_context(
        self,
        request: AnalysisRequest,
        base: dict,
        scenarios: dict,
        chain: dict,
        directions: tuple,
        magnitude: float,
        result: AnalysisResult,
    ) -> dict:
        def dir_view(segment: dict, key: str) -> dict:
            d = (segment["directions"] or {}).get(key) or {}
            if d.get("calc_status") != "ok":
                return {"状态": "未计算", "原因": segment.get("reason")}
            view = {
                "方向": d.get("label"),
                "强弱": d.get("grade"),
                "情景": _scenario_of(chain, d.get("delta_pct", 0)),
            }
            if d.get("dividend_blocked") is not None:
                view["红利阻断"] = "是（已击穿现金成本线）" if d["dividend_blocked"] else "否"
            if self._qualitative(segment):
                view["定量性"] = "定性推演（该环节方向系数暂无公开可查数据）"
            return view

        return {
            "任务": "碳酸锂价格变动的产业链影响分析",
            "输入": {
                "价格品种": request.price_series,
                "基准日期": base["date"],
                "单边变动幅度": magnitude,
                "方向覆盖": "涨价周期与下跌周期双向并列（框架六.1 强制要求）",
                "情景分级": {k: (scenarios[k]["scenario"] or {}) for k in scenarios},
            },
            "基准价格": base,
            "情景价格": {
                k: {
                    "方向": scenarios[k]["direction_label"],
                    "情景": scenarios[k]["scenario"],
                    "delta_pct": scenarios[k]["delta_pct"],
                    "new_price": scenarios[k]["new_price"],
                    "unit": scenarios[k]["unit"],
                    "formula": scenarios[k]["formula"],
                }
                for k in scenarios
            },
            "传导链": {
                "nodes": [
                    {k: n.get(k) for k in
                     ("id", "name", "method", "companies", "role", "role_up", "role_down", "decisive_variable")}
                    for n in chain["nodes"]
                ],
                "links": chain["links"],
            },
            "涨跌非对称": chain["asymmetry"],
            "对照组": chain["control_group"],
            "环节量化结果": [
                {
                    "环节": s.get("node_name"),
                    "企业": s.get("company"),
                    "企业代码": s.get("code"),
                    "状态": s.get("status"),
                    "计算方法": s.get("method"),
                    "决定性变量": s.get("decisive_variable"),
                    "涨价周期": dir_view(s, "up"),
                    "下跌周期": dir_view(s, "down"),
                    "取用系数": {
                        "涨价周期": (s.get("directions", {}).get("up") or {}).get("coefficient_field"),
                        "下跌周期": (s.get("directions", {}).get("down") or {}).get("coefficient_field"),
                    },
                    "数据来源": (s.get("params_source") or {}).get("source"),
                    "来源链接": (s.get("params_source") or {}).get("source_url"),
                    "是否有公开来源": (s.get("params_source") or {}).get("has_public_source"),
                    "方向系数是否有公开来源": (s.get("params_source") or {}).get(
                        "coefficient_public_source"
                    ),
                    "备注": s.get("notes"),
                    "跳过原因": s.get("reason"),
                }
                for s in result.segments
            ],
            "正文量化表": self._body_quant_table(result.segments),
            "对照组结论": self._control_group_conclusion(chain, result.segments),
            "敏感度引擎": self._sensitivity_engine(chain, base, magnitude),
            "财报PDF证据": result.pdf_evidence,
            "假设清单": chain["assumptions"],
            "边界条件": chain["boundary_conditions"],
            "红利阻断机制": self._breach_critical(chain, base),
            "压力情景": self._stress_info(chain, request.stress),
            "输出维度": chain.get("output_dimensions"),
            "系统提示": {
                "红线": chain["core_redline"],
                "参数占位提示": any(
                    (s.get("params_source") or {}).get("is_placeholder") for s in result.segments
                ),
                "系数模型假设提示": [
                    s["company"]
                    for s in result.segments
                    if s.get("status") == "ok" and self._qualitative(s)
                ],
                "越界提示": [w for w in result.warnings if "边界" in w],
                "正文规则": "③ 段只输出方向与强弱，禁止出现任何绝对金额；金额在后缀附录中由程序生成",
            },
        }

    def _generate_report(
        self,
        context: dict,
        request: AnalysisRequest,
        logger: RunLogger,
        result: AnalysisResult,
    ) -> tuple[dict, str]:
        if not request.use_llm:
            logger.event("step7", "已指定 --no-llm，跳过模型调用")
            result.llm = {"status": "skipped", "error": "use_llm=False"}
            return result.llm, self._render_offline_report(context, result)

        system_prompt = self._load_system_prompt()
        user_prompt = self._build_user_prompt(context)
        t0 = time.perf_counter()
        response = self.llm.chat(system_prompt, user_prompt)
        info = response.to_dict()
        info["system_prompt_chars"] = len(system_prompt)
        info["user_prompt_chars"] = len(user_prompt)
        result.llm = info  # 先写入，供报告附录引用

        if response.status in ("ok", "cached"):
            logger.event(
                "step7",
                "LLM 报告生成完成",
                status=response.status,
                elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                finish_reason=response.finish_reason,
                truncated=response.truncated,
            )
            missing = [h for h in SECTION_HEADERS if h not in response.content]
            if missing:
                result.warnings.append(f"LLM 输出缺少规定标题：{missing}（已在报告中标注）")
            if response.truncated:
                result.warnings.append(
                    f"模型输出被 max_tokens 上限截断（completion_tokens={response.completion_tokens}），"
                    "报告结尾可能不完整：请调大 .env 中的 LLM_MAX_TOKENS 后重跑"
                )
            return info, self._render_llm_report(response.content, context, result, missing)

        logger.event("step7", "LLM 调用失败，降级为离线确定性报告", error=response.error)
        result.warnings.append(f"模型调用失败，已降级为离线确定性报告：{response.error}")
        return info, self._render_offline_report(context, result)

    def _load_system_prompt(self) -> str:
        path = PROMPTS_DIR / "system_prompt.md"
        if not path.exists():
            raise FileNotFoundError(f"系统提示词缺失：{path}")
        return path.read_text(encoding="utf-8")

    def _build_user_prompt(self, context: dict) -> str:
        return "\n".join(
            [
                "【任务】基于下方**工具计算结果**，输出四段结构报告。",
                "【铁律】下方 JSON 是本次分析唯一可用的数据源：",
                "1) 禁止修改、换算、补算其中任何数值；报告中的数字必须逐字来自该 JSON；",
                "2) 禁止引用 JSON 之外的任何数据、链接或行业记忆；",
                "3) 缺失的指标写「未提供」，不得估算；",
                "4) 涨跌非对称：不得用正负取反的方式由一套系数推另一套系数；",
                "5) ③ 段必须逐字使用 JSON 里的「正文量化表」，只写方向与强弱，禁止出现任何绝对金额；",
                "6) ④ 段必须逐字使用 JSON 里的「对照组结论」。",
                "【输出格式】严格使用以下四个二级标题（不增不减、不换顺序）：",
                *SECTION_HEADERS,
                "",
                "```json",
                json.dumps(context, ensure_ascii=False, indent=2, default=str),
                "```",
            ]
        )

    def _render_llm_report(
        self, content: str, context: dict, result: AnalysisResult, missing: list[str]
    ) -> str:
        header = self._report_header(context, result, mode=result.llm.get("status", "llm"))
        if missing:
            header += (
                "\n> ⚠️ 模型输出缺少规定标题："
                + "、".join(missing)
                + "，下方内容按模型原始输出保留，请人工复核。\n"
            )
        if result.llm.get("truncated"):
            header += (
                "\n> ⚠️ 模型输出因 `max_tokens` 上限被截断，末段可能不完整。"
                "请调大 `.env` 中的 `LLM_MAX_TOKENS` 后重跑。\n"
            )
        return "\n".join(
            [header, "", content.strip(), "", self._appendix(context, result)]
        )

    # ---- 离线确定性报告（四段） ----

    def _render_offline_report(self, context: dict, result: AnalysisResult) -> str:
        """离线确定性报告：不调用模型，全部由工具计算结果的模板拼装，保证可复现。"""
        base = context["基准价格"]
        scen = context["情景价格"]
        up, down = scen.get("up"), scen.get("down")
        lines = [self._report_header(context, result, mode="offline")]

        def scen_text(key: str) -> str:
            s = scen.get(key)
            if not s:
                return "未覆盖"
            label = s["情景"] or {}
            return (
                f"{s['方向']} **{_fmt_pct(s['delta_pct'])}**"
                f"（情景 {label.get('id')} {label.get('name')}）"
            )

        lines += [
            "",
            "## ① 产业分析逻辑",
            "",
            f"【事实】本次分析的冲击源为**{context['输入']['价格品种']}**，"
            f"基准日期 {base['date']}，基准价 {base['price']:,.0f} {base['unit']}；"
            f"按框架要求同时覆盖两个方向：{scen_text('up')} 与 {scen_text('down')}，"
            f"两者情景价格均由公式 F-007 测算"
            + (f"（涨价方向上探至 {up['new_price']:,.0f} {up['unit']}"
               f"，下跌方向下探至 {down['new_price']:,.0f} {down['unit']}）。" if up and down else "。"),
            "",
            "【推论】传导路径按配置的传导链逐段展开（框架明确「不可跳过正极环节」）：",
            "",
            "| 环节 | → 下游环节 | 受影响变量 | 涨价方向 | 下跌方向 | 时滞 | 机制/公式 |",
            "|---|---|---|---|---|---|---|",
        ]
        node_names = {n["id"]: n["name"] for n in context["传导链"]["nodes"]}
        for link in context["传导链"]["links"]:
            lines.append(
                f"| {node_names.get(link['from'], link['from'])} "
                f"| {node_names.get(link['to'], link['to'])} "
                f"| {link['variable']} | {link.get('direction_up')} | {link.get('direction_down')} "
                f"| {link['lag']} | {link['formula']} |"
            )

        asym = context["涨跌非对称"]
        lines += [
            "",
            f"【观点】链条呈「越靠近原材料越敏感、越靠近终端承压越大」的梯度结构。"
            f"但{asym.get('name', '涨跌非对称')}：{asym['rule']}",
            f"- 涨价方向：{asym['up_side']}",
            f"- 下跌方向：{asym['down_side']}",
            "本次分析对两个方向分别取用参数"
            f"（{asym['parameter_fields']['up']} / {asym['parameter_fields']['down']}），"
            "因此同一环节在涨跌两个方向上的幅度并不互为镜像。",
            "",
            "## ② 证据充分性",
            "",
            "【事实】本次实际使用的证据如下（均可核验）：",
            "",
            "| # | 类型 | 内容 | 来源 | 链接 | 抓取/披露时间 |",
            "|---|---|---|---|---|---|",
            f"| 1 | 价格数据 | {base['name']} {base['price']:,.0f} {base['unit']}"
            f"（{base['date']}） | {base['source']} | {base['source_url']} | {base['retrieved_at']} |",
        ]
        idx = 2
        for s in result.segments:
            src = s.get("params_source") or {}
            if s.get("status") != "ok":
                continue
            fields = "成本占比 / 毛利率" + (
                " / 自给率 / 现金成本线" if s.get("method") == "upstream_elasticity"
                else " / 涨价转嫁系数 / 下跌红利释放系数" if s.get("method") == "cost_push"
                else " / 单耗系数 / 加工费（锂定价机制）"
            )
            lines.append(
                f"| {idx} | 企业参数 | {s['company']}（{s.get('code')}）{fields} "
                f"| {src.get('source')} | {src.get('source_url')} | 参数表随项目交付 |"
            )
            idx += 1
        for entry in context["财报PDF证据"]:
            for field, hit in entry["fields"].items():
                lines.append(
                    f"| {idx} | 财报抽取 | {entry['file']} · {field} = {hit['value']} {hit['unit']}"
                    f"（第 {hit['page']} 页） | 原文：{hit['evidence']} | — | 文件随项目交付 |"
                )
                idx += 1
        for link in context["传导链"]["links"]:
            lines.append(f"| {idx} | 机制 | {link['evidence']} | 传导链配置 | — | — |")
            idx += 1

        lines += [
            "",
            "【推论】证据强度分层：",
            "1. 一级证据（可核验的客观数据）：价格数据来自公开行情源；企业财报抽取值来自年报原文。",
            "2. 二级证据（公开机制描述）：传导链机制来自上市公司问询函回复与行业惯例。",
            "3. 三级证据（参数估计）：企业财务与规模参数取自年报/招股书原文（一级证据）；"
            "传导系数与滞后周期仅有行业访谈、券商点评等近似依据，无正式量化测算，属模型假设。",
        ]
        placeholders = [s["company"] for s in result.segments
                        if s.get("status") == "ok" and self._qualitative(s)]
        if placeholders:
            lines.append(
                f"4. ⚠️ 证据缺口：{'、'.join(placeholders)} 的方向系数（涨价转嫁系数 / 下跌红利释放系数）"
                "暂无公开可查的量化来源——券商研报中未检索到以此命名的正式量化测算，"
                "按框架六.4 只做定性推演。其财务与规模参数有公开来源，但系数取值为模型假设，"
                "不参与绝对金额引用。"
            )
        if not result.pdf_evidence:
            lines.append("5. ⚠️ 本次未提供财报 PDF，缺少年报原文证据（Tool 1 未参与），建议补充后复跑。")

        lines += [
            "",
            "## ③ 量化合理性",
            "",
            "【事实】各环节在涨跌两个方向上的影响方向与强弱（全部由 Tool 3 按编号公式计算，未经模型改动）：",
            "",
            context["正文量化表"],
            "",
            "> 本段遵循框架六.5，只做方向性、强弱对比判断，**不输出精准盈利预测**；"
            "具体金额与中间量见**附录 A**（可追溯、可复核）。"
            + ("\n> ＊ 该环节的方向系数暂无公开可查的量化来源，方向与强弱仅作定性推演。" if placeholders else ""),
            "",
            "【推论】",
        ]
        asym_rows = []
        for s in result.segments:
            if s.get("status") != "ok":
                continue
            u = s["directions"].get("up") or {}
            d = s["directions"].get("down") or {}
            if u.get("calc_status") != "ok" or d.get("calc_status") != "ok":
                continue
            asym_rows.append(
                f"- {s['company']}：涨价方向{u['label']}（强度{u['grade']}），"
                f"下跌方向{d['label']}（强度{d['grade']}）"
                + (f"，且下跌方向已击穿现金成本线、红利下传阻断（F-010）"
                   if d.get("dividend_blocked") else "")
            )
        lines += asym_rows or ["- 无可用的双向测算结果。"]
        blocks = [
            s for s in result.segments
            if s.get("status") == "ok" and (s["directions"].get("down") or {}).get("dividend_blocked")
        ]
        if blocks:
            lines.append(
                "- **红利阻断**：" + "、".join(s["company"] for s in blocks)
                + " 在下跌情景下价格已低于现金成本线，按框架 2.2 会减产、停产保价，"
                "人为阻断成本红利向下传递——此时下游的「红利」不可线性外推。"
            )
        else:
            lines.append(
                "- **红利阻断**：本次下跌幅度未击穿上游现金成本线（F-010 判定为「未击穿」），"
                "红利下传路径在本次情景下未被减产保价行为打断；该机制仅在锂价跌至上游现金成本线附近时才启动，"
                "需在压力情景下单独检验（基准价与现金成本线间距越大，越不可能触发）。"
            )
        lines += [
            "- 机制差异：正极环节（湖南裕能）执行锂定价机制，售价按锂成本机械联动，"
            "其涨跌对称性由定价机制决定而非人工系数，是链条中传导最顺畅的一环。",
            "",
            "- **敏感度引擎（输入无关）**：下表为各环节固定的敏感性系数（pct/万元），"
            "不随输入变化，反映其结构弹性；按本次 ΔP 生成的示例 Δm/ρ 与相对强弱见附录 A-0"
            "（③ 段只列档位与系数，不出现数值）。",
            "",
            "| 环节 | 企业 | 敏感度(pct/万元) | 系数构成 | 计算方法 |",
            "|---|---|---|---|---|",
        ]
        engine = context["敏感度引擎"]
        for row in engine["rows"]:
            lines.append(
                f"| {row['node']} | {row['company']} | {row['sensitivity']:+.2f} "
                f"| {row['coefficients'] or '—'} | {row['method']} |"
            )
        breach = context["红利阻断机制"]
        crit = (
            f"{breach['critical_breach_pct']:.1f}%"
            if breach.get("critical_breach_pct") is not None
            else "未取得（缺现金成本线）"
        )
        lines += [
            "",
            f"- **红利阻断临界（常驻口径）**：跌价达到约 **{crit}**"
            "（= 现金成本线 ÷ 基准价 − 1；上游现金成本线为可见测算口径，基准价为本次检索时点现货价）"
            "才会击穿上游现金成本线、触发减产保价并阻断成本红利下传；本次默认情景未触发，"
            "该机制仅在压力情景（S-1，-80%）下单独检验。",
        ]
        stress = context["压力情景"]
        if stress.get("triggered"):
            lines += [
                "",
                f"> 🔴 **压力情景 {stress.get('id')} {stress.get('name')}（{stress.get('label')}）**："
                f"{stress.get('note')}；本次运行已触发该压力情景，上游现金成本线击穿判定（F-010）被实例化。",
            ]
        lines += [
            "",
            "## ④ 理论应用边界",
            "",
            "【观点】（1）本次测算依赖的模型假设：",
        ]
        for i, a in enumerate(context["假设清单"], 1):
            lines.append(f"{i}. {a}")

        bounds = context["边界条件"]
        lines.append("")
        lines.append("（2）适用条件（满足则结论成立）：")
        for i, a in enumerate(bounds.get("applicable", []), 1):
            lines.append(f"{i}. {a}")
        lines.append("")
        lines.append("（3）失效 / 不适用条件（出现则结论需修正）：")
        for i, b in enumerate(bounds.get("invalid", []), 1):
            lines.append(f"{i}. {b}")

        lines += ["", "（4）对照组结论：", "", context["对照组结论"], "", "（5）数据局限："]
        if context["系统提示"]["参数占位提示"]:
            lines.append(
                "- ⚠️ 本次使用的企业参数来源标注为「占位数据」，**绝对金额结论在参数替换为年报实际值前不可对外引用**。"
            )
        if context["系统提示"]["系数模型假设提示"]:
            lines.append(
                "- ⚠️ 涨价转嫁系数与下跌红利释放系数尚无正式公开量化来源，"
                "以下环节的系数取值为模型假设，仅支持方向与强弱判断："
                + "、".join(context["系统提示"]["系数模型假设提示"])
                + "。其绝对金额结论不可对外引用（相对强弱对比不受影响）。"
            )
        for w in result.warnings:
            lines.append(f"- {w}")
        if (
            not result.warnings
            and not context["系统提示"]["参数占位提示"]
            and not context["系统提示"]["系数模型假设提示"]
        ):
            lines.append("- 无额外局限项。")

        lines += self._appendix(context, result).split("\n")
        return "\n".join(lines)

    def _report_header(self, context: dict, result: AnalysisResult, mode: str) -> str:
        mode_label = {
            "ok": f"模型生成（{self.settings.model}）",
            "cached": "模型生成（命中缓存，保证可复现）",
            "offline": "离线确定性模式（未调用模型，全部数值来自工具计算）",
            "skipped": "跳过模型（--no-llm）",
            "error": "降级模式（模型调用失败）",
        }.get(mode, mode)
        scen = context.get("情景价格") or {}
        scen_desc = "；".join(
            f"{s['方向']} {s['delta_pct']:+.0%}（情景 {(s['情景'] or {}).get('id')} {(s['情景'] or {}).get('name')}）"
            for s in scen.values()
        ) or "未覆盖"
        return "\n".join(
            [
                "# 碳酸锂价格波动 · 产业链影响分析报告",
                "",
                f"- **运行 ID**：`{result.run_id}`",
                f"- **生成时间**：{datetime.now(CN_TZ).isoformat(timespec='seconds')}",
                f"- **报告生成方式**：{mode_label}",
                f"- **方向覆盖**：{scen_desc}",
                "- **涨跌非对称**：涨价转嫁系数与下跌红利释放系数为两套参数，分别取用，禁止正负取反换算",
                f"- **可复现参数**：temperature={self.settings.temperature}，seed={self.settings.seed}",
                f"- **运行日志**：`{result.log_path}`",
                "- **核心红线**：报告中所有数值均由 Tool 3（受限代码执行沙箱）按编号公式计算产出，"
                "大模型未参与任何运算",
            ]
        )

    def _appendix(self, context: dict, result: AnalysisResult) -> str:
        lines = [
            "",
            "---",
            "",
            "## 附录 A｜量化明细（正文已按框架下沉至此）",
            "",
            "### A-0 敏感度引擎示例明细（输入无关引擎按本次 ΔP 展开，F-013 → F-011）",
            "",
            "| 环节 | 企业 | 敏感度(pct/万元) | 本次ΔP(万元/吨) | Δm(引擎) | ρ=|Δm|/m0 | 相对强弱 |",
            "|---|---|---|---|---|---|---|",
        ]
        engine = context["敏感度引擎"]
        dp = engine["delta_p_wan"]
        for row in engine["rows"]:
            dm = row["delta_m"]
            dm_txt = f"{dm * 100:+.2f} pct" if dm is not None else "—"
            rho = row["rho"]
            rho_txt = f"{rho:.3f}" if rho is not None else "—（基准毛利≤0）"
            lines.append(
                f"| {row['node']} | {row['company']} | {row['sensitivity']:+.2f} | {dp} "
                f"| {dm_txt} | {rho_txt} | {row['grade'] or '—'} |"
            )
        lines += [
            "",
            "### A-1 双向结果总表（含绝对金额，仅供审计，不得写入正文）",
            "",
            "| 环节 | 企业 | 方向 | 情景 | 成本冲击 | 售价传导 | 毛利率（前→后） | 利润影响 | 取用系数 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        scen = context.get("情景价格") or {}
        for s in result.segments:
            for key in ("up", "down"):
                d = (s.get("directions") or {}).get(key) or {}
                if d.get("calc_status") != "ok":
                    lines.append(
                        f"| {s.get('node_name')} | {s.get('company')} | {d.get('direction_label')} "
                        f"| 未计算 | 未计算 | 未计算 | 未计算 | 未计算 | {d.get('reason') or '—'} |"
                    )
                    continue
                ci = _fmt_pct(d["cost_impact_pct"]) if d.get("cost_impact_pct") is not None else "—（上游为受益端）"
                pp = _fmt_pct(d["price_pass_pct"]) if d.get("price_pass_pct") is not None else "—"
                mb, ma = d.get("margin_before"), d.get("margin_after")
                margin_txt = (
                    f"{mb:.2%} → {ma:.2%}（{_fmt_ppt(d['margin_shift_pct'])}）"
                    if mb is not None and ma is not None
                    else "—"
                )
                coef = (
                    f"{d['coefficient_field']}={d['coefficient_value']}"
                    if d.get("coefficient_field") else "定价机制联动（无人工系数）"
                )
                label = (scen.get(key) or {}).get("情景") or {}
                lines.append(
                    f"| {s.get('node_name')} | {s.get('company')} | {d['direction_label']} "
                    f"| {label.get('id', '—')} {label.get('name', '')} | {ci} | {pp} | {margin_txt} "
                    f"| {_fmt_money(d['delta_profit'])} | {coef} |"
                )

        lines += [
            "",
            "### A-2 工具计算结果审计明细（逐条公式）",
            "",
            "| 环节 | 方向 | 公式 | 输入 | 输出 |",
            "|---|---|---|---|---|",
        ]
        for s in result.segments:
            for key in ("up", "down"):
                d = (s.get("directions") or {}).get(key) or {}
                for c in d.get("calculations", []):
                    inputs = "，".join(f"{k}={v}" for k, v in c["inputs"].items())
                    outputs = "，".join(f"{k}={v}" for k, v in c["result"].items())
                    lines.append(
                        f"| {s.get('company')} | {d.get('direction_label')} | {c['formula']} "
                        f"| {inputs} | {outputs} |"
                    )

        lines += [
            "",
            "### A-3 Tool 3 受限沙箱独立复算（交叉校验）",
            "",
            "在隔离子进程中用独立表达式重算关键公式，与公式库结果比对，作为数值的第二份证据：",
            "",
            "| 企业 | 方向 | 校验公式 | 复算表达式 | 期望值 | 沙箱复算值 | 容差 | 结论 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for s in result.segments:
            for key in ("up", "down"):
                d = (s.get("directions") or {}).get(key) or {}
                checks = d.get("sandbox_checks") or {}
                if not checks:
                    lines.append(
                        f"| {s.get('company')} | {d.get('direction_label')} | — | — | — | — | — | 未执行 |"
                    )
                    continue
                for fid, check in checks.items():
                    if check.get("status") != "ok":
                        lines.append(
                            f"| {s.get('company')} | {d.get('direction_label')} | {check.get('formula', fid)} "
                            f"| — | — | — | — | {check.get('status')}：{check.get('reason') or check.get('error', '')} |"
                        )
                        continue
                    lines.append(
                        f"| {s.get('company')} | {d.get('direction_label')} | {check['formula']} "
                        f"| `{check['expression']}` | {check['expected']} | {check['actual']} "
                        f"| {check.get('tolerance')} | 一致 |"
                    )

        lines += ["", "## 附录 B｜数据来源与可追溯信息", ""]
        lines.append(
            f"- 价格：{context['基准价格']['source']}（{context['基准价格']['source_url']}），"
            f"数据日期 {context['基准价格']['date']}，抓取时间 {context['基准价格']['retrieved_at']}"
        )
        for s in result.segments:
            src = s.get("params_source") or {}
            if src.get("source"):
                lines.append(
                    f"- {s.get('company')} 参数：{src['source']}"
                    + (f"（{src['source_url']}）" if src.get("source_url") else "")
                    + ("｜财务与规模参数有公开来源" if src.get("has_public_source") else "｜⚠️ 财务参数暂无公开可查数据")
                    + ("｜方向系数有公开来源" if src.get("coefficient_public_source")
                       else "｜⚠️ 方向系数为模型假设（无正式公开量化来源）")
                )
        for entry in context["财报PDF证据"]:
            lines.append(f"- 财报 PDF：{entry['file']}（引擎 {entry['engine']}，{entry['page_count']} 页）")
        lines += [
            "",
            "## 附录 C｜运行信息",
            "",
            f"- 运行日志（JSONL，逐行含时间戳/步骤/工具/入参/结果）：`{result.log_path}`",
            f"- 模型调用：{json.dumps(result.llm, ensure_ascii=False)}",
            f"- 使用公式：{', '.join(result.formulas_used) or '—'}",
            f"- 告警 / 局限（{len(result.warnings)} 条）："
            + ("；".join(result.warnings) if result.warnings else "无"),
        ]
        return "\n".join(lines)