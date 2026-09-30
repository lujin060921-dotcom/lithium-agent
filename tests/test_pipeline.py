"""端到端验收测试：对应行动路径「阶段 5」的验收标准（已对齐【1】理论框架 v1 + v6 裁定）。

- 输入一组原材料变动 → 稳定输出**四段结构**报告（涨跌双向并列）
- 涨跌非对称：同一环节涨跌两个方向的幅度不互为镜像，系数按方向分别取用
- 正文只出方向与强弱（③ 段），绝对金额只出现在附录 A
- 同输入重跑 → 输出一致（可复现）
- 异常输入 → 不崩溃，返回 failed 结果并给出可读原因
- 越界输入 → 仍出报告，但显著提示线性传导假设失效

运行： .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.orchestrator import (  # noqa: E402
    SECTION_HEADERS,
    AnalysisRequest,
    Orchestrator,
)

RUN_ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")
TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

UPSTREAM, CATHODE, BATTERY, VEHICLE, INTEGRATED = (
    "天齐锂业",
    "湖南裕能",
    "宁德时代",
    "蔚来",
    "比亚迪",
)

DIRECTION_FIELDS = (
    "delta_pct",
    "calc_status",
    "cost_impact_pct",
    "price_pass_pct",
    "margin_before",
    "margin_after",
    "margin_shift_pct",
    "delta_profit",
    "base_revenue",
    "coefficient_field",
    "coefficient_value",
    "label",
    "grade",
    "dividend_blocked",
    "formulas_used",
)


def _body(report: str) -> str:
    """截取四段正文，并把「运行态标识」（run_id / 时间戳 / 产物路径）规范化。

    可复现的定义是：分析内容逐字一致；run_id 与产物路径按次生成，必然不同，故需归一化。
    """
    idx = report.find(SECTION_HEADERS[0])
    body = report[idx:] if idx >= 0 else report
    body = RUN_ID_RE.sub("<RUN>", body)
    return TIMESTAMP_RE.sub("<TS>", body)


def _section(report: str, header: str) -> str:
    """取出某一段的正文（用于「正文不得出现金额」这类边界断言）。"""
    start = report.find(header)
    if start < 0:
        return ""
    idx = SECTION_HEADERS.index(header)
    end = len(report)
    for nxt in SECTION_HEADERS[idx + 1:]:
        pos = report.find(nxt)
        if pos > start:
            end = pos
            break
    else:
        appendix = report.find("## 附录 A")
        if appendix > start:
            end = appendix
    return report[start:end]


def _segment(result, company: str) -> dict:
    hit = next((s for s in result.segments if s.get("company") == company), None)
    assert hit is not None, f"结果中缺少环节：{company}"
    return hit


def _segments_key(result) -> list:
    """只保留由计算决定的字段，剔除 run_id / 路径 / 时间等运行态信息。"""
    rows = []
    for s in result.segments:
        row = {"company": s.get("company"), "status": s.get("status")}
        for key in ("up", "down"):
            d = (s.get("directions") or {}).get(key) or {}
            for f in DIRECTION_FIELDS:
                row[f"{key}_{f}"] = d.get(f)
        rows.append(row)
    return rows


def _log_records(path: str) -> list[dict]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").strip().splitlines()
        if line.strip()
    ]


class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 测试写入临时目录，避免污染正式 reports/ 与 logs/
        cls._tmp = tempfile.TemporaryDirectory(prefix="lithium-test-")
        tmp = Path(cls._tmp.name)
        cls.orch = Orchestrator(report_dir=tmp / "reports", log_dir=tmp / "logs")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _run(self, delta=0.15, series=None, pdf_paths=None, bidirectional=True):
        from tools.retriever import DEFAULT_PRICE_NAME

        request = AnalysisRequest(
            price_series=series or DEFAULT_PRICE_NAME,
            delta_pct=delta,
            pdf_paths=pdf_paths if pdf_paths is not None else [],
            use_llm=False,  # 测试固定走离线确定性路径，保证快速且可复现
            bidirectional=bidirectional,
        )
        return self.orch.run(request, quiet=True)

    # ---------------- 结构 ----------------

    def test_ok_and_four_sections(self):
        r = self._run()
        self.assertEqual(r.status, "ok")
        for header in SECTION_HEADERS:
            self.assertIn(header, r.report, f"报告缺少规定标题：{header}")
        self.assertEqual(len(SECTION_HEADERS), 4, "v6：框架输出固定四段结构（移除⑤研报打分）")
        self.assertNotIn("## ⑤ 研报打分辅助评价", r.report)
        self.assertTrue(Path(r.report_path).exists())
        self.assertTrue(Path(r.log_path).exists())
        # 五个环节均已计算
        self.assertEqual(len(r.segments), 5)
        self.assertTrue(all(s["status"] == "ok" for s in r.segments))
        # 默认涨跌双向：每个环节两个方向都要有结果
        for s in r.segments:
            self.assertEqual(
                {d["calc_status"] for d in s["directions"].values()},
                {"ok"},
                f"{s['company']} 双向结果不完整",
            )

    def test_explicit_lithium_pricing_is_not_skipped(self):
        """框架明确「不可跳过正极环节」。"""
        self.assertIn("湖南裕能", {s["company"] for s in self._run().segments})

    def test_every_number_comes_from_tool(self):
        """红线校验：每个环节的每个方向都必须留下公式计算记录。"""
        r = self._run()
        for s in r.segments:
            for key, d in s["directions"].items():
                self.assertTrue(d["calculations"], f"{s['company']}（{key}）无计算记录")
                for c in d["calculations"]:
                    self.assertIn("formula", c)
                    self.assertIn("inputs", c)
                    self.assertIn("result", c)

    def test_sandbox_cross_check_passes(self):
        """Tool 3 沙箱独立复算必须与公式库结果一致（含 F-010 / F-011）。"""
        r = self._run()
        seen: set[str] = set()
        for s in r.segments:
            for key, d in s["directions"].items():
                checks = d.get("sandbox_checks") or {}
                self.assertTrue(checks, f"{s['company']}（{key}）未做沙箱复核")
                for fid, check in checks.items():
                    self.assertEqual(
                        check.get("status"), "ok",
                        f"{s['company']}（{key}）{fid} 沙箱复核未通过：{check}",
                    )
                    self.assertLessEqual(
                        abs(check["actual"] - check["expected"]), check["tolerance"]
                    )
                    seen.add(fid)
        self.assertLessEqual({"F-006", "F-010", "F-011", "F-001", "F-002"}, seen)

    def test_unknown_formula_id_in_segment_calculation(self):
        """审计明细里的公式编号必须都在公式库登记（防止漏接公式）。"""
        from tools.calculator import FORMULA_REGISTRY

        r = self._run()
        for s in r.segments:
            for d in s["directions"].values():
                for c in d["calculations"]:
                    self.assertIn(c["formula"].split()[0], FORMULA_REGISTRY)

    def test_single_direction_mode(self):
        """调试用单方向：只算涨价方向，报告结构不塌。"""
        r = self._run(delta=0.15, bidirectional=False)
        self.assertEqual(r.status, "ok")
        for header in SECTION_HEADERS:
            self.assertIn(header, r.report)
        for s in r.segments:
            self.assertEqual(set(s["directions"]), {"up"})
        self.assertNotIn("None", _section(r.report, SECTION_HEADERS[3]))

    # ---------------- 涨跌非对称 ----------------

    def test_both_directions_have_opposite_signs(self):
        """下跌情景下上游利润影响为负、下游方向反转，符号应一致可解释。"""
        r = self._run()
        for company in (UPSTREAM, BATTERY, VEHICLE):
            d = _segment(r, company)["directions"]
            self.assertNotEqual(d["up"]["delta_profit"], d["down"]["delta_profit"], company)
            self.assertEqual(
                d["up"]["label"] != d["down"]["label"], True,
                f"{company} 涨跌方向应相反：{d['up']['label']} / {d['down']['label']}",
            )

    def test_asymmetric_directions_not_mirrored(self):
        """框架 2.2 核心：同一环节涨跌幅度不互为镜像（两套系数不同）。

        v6：用蔚来（涨价 0.10 / 下跌 0.15）验证两套系数不对称；宁德/比亚迪已按 v6
        「中游金属联动近似对称」收敛为双向同系数，故不在本测试中要求不镜像。
        """
        d = _segment(self._run(), VEHICLE)["directions"]
        up, down = d["up"]["margin_shift_pct"], d["down"]["margin_shift_pct"]
        self.assertLess(up, 0, "涨价方向应为承压")
        self.assertGreater(down, 0, "下跌方向应为受益")
        self.assertNotAlmostEqual(abs(up), abs(down), places=6, msg="涨跌幅度不应互为镜像")

    def test_cathode_symmetry_is_mechanism_driven(self):
        """正极不使用人工系数：其涨跌对称性由锂定价机制决定。"""
        d = _segment(self._run(), CATHODE)["directions"]
        self.assertAlmostEqual(d["up"]["margin_shift_pct"], -d["down"]["margin_shift_pct"], places=6)
        for key in ("up", "down"):
            self.assertIsNone(d[key]["coefficient_field"])
            self.assertIsNone(d[key]["coefficient_value"])

    def test_coefficient_taken_by_direction(self):
        """系数取用正确：涨价取 pass_through_up、下跌取 dividend_release_down（读日志断言）。"""
        r = self._run()
        taken = [
            (rec["action"].split("·")[-1].strip(), rec["payload"])
            for rec in _log_records(r.log_path)
            if rec["action"].startswith("系数取用")
        ]
        self.assertTrue(taken, "日志中缺少「系数取用」记录")
        for company, payload in taken:
            expected = "pass_through_up" if payload["direction"] == "up" else "dividend_release_down"
            self.assertEqual(payload["coefficient_field"], expected, f"{company} 系数取用错误")
        # 下游与对照组都在日志里留下两个方向的取用记录
        up_fields = {p["coefficient_field"] for _, p in taken if p["direction"] == "up"}
        down_fields = {p["coefficient_field"] for _, p in taken if p["direction"] == "down"}
        self.assertEqual(up_fields, {"pass_through_up"})
        self.assertEqual(down_fields, {"dividend_release_down"})

    def test_upstream_sensitivity_is_self_sufficiency(self):
        """上游受益强弱由锂资源自给率决定（F-006 的 sensitivity 直接取自给率）。"""
        d = _segment(self._run(), UPSTREAM)["directions"]
        for key in ("up", "down"):
            self.assertEqual(d[key]["coefficient_field"], "self_sufficiency")
            self.assertAlmostEqual(d[key]["coefficient_value"], 1.0)

    # ---------------- 正文 / 附录边界 ----------------

    def test_body_has_no_amounts_appendix_does(self):
        """框架六.5：③ 段只出方向与强弱，绝对金额下沉附录 A。"""
        r = self._run()
        body3 = _section(r.report, "## ③ 量化合理性")
        self.assertTrue(body3)
        self.assertNotIn("亿元", body3)
        # ③ 段不得出现「数字 + 元」的绝对金额（表头里的单位 pct/万元 不视为金额）
        self.assertIsNone(
            re.search(r"[0-9][0-9,]*\.?[0-9]*\s*万?元", body3),
            "③ 段不应出现金额数字",
        )
        appendix = r.report[r.report.find("## 附录 A"):]
        self.assertIn("亿元", appendix)
        self.assertIn("元/吨", appendix)

    def test_placeholder_segments_are_qualitative(self):
        """方向系数无公开量化来源时，正文必须标注为定性推演。"""
        r = self._run()
        body3 = _section(r.report, "## ③ 量化合理性")
        self.assertIn("定性推演", body3)
        self.assertIn("＊", body3)

    def test_no_number_invented_by_llm(self):
        """离线报告中出现的金额必须能在计算明细里找到对应量级来源。"""
        r = self._run()
        known = {
            f"{abs(c['result'][k]) / 1e8:,.2f}"
            for s in r.segments
            for d in s["directions"].values()
            for c in d["calculations"]
            for k in c["result"]
            if k != "unit" and isinstance(c["result"][k], (int, float))
        }
        money_tokens = re.findall(r"([\d,]+\.\d{2})\s*亿元", r.report)
        self.assertTrue(money_tokens, "附录 A-1 应包含绝对金额")
        for token in money_tokens:
            self.assertIn(token, known, f"报告中出现无法溯源到计算的金额：{token} 亿元")

    # ---------------- 框架指定的企业口径 ----------------

    def test_nio_replaces_changan(self):
        """框架指定下游整车为蔚来，长安汽车不应再出现。"""
        r = self._run()
        self.assertIn(VEHICLE, r.report)
        self.assertNotIn("长安汽车", r.report)
        self.assertEqual(_segment(r, VEHICLE)["node_id"], "vehicle")

    def test_control_group_is_byd_and_conclusion_present(self):
        """对照组（垂直一体化）必须是比亚迪，且 ④ 段给出显式对照结论。"""
        r = self._run()
        cg = _segment(r, INTEGRATED)
        self.assertTrue(cg["control_group"])
        body4 = _section(r.report, "## ④ 理论应用边界")
        self.assertIn("【对照组结论】", body4)
        self.assertIn("企业结构不同，结果完全不同", body4)

    # ---------------- 现金成本线与红利阻断 ----------------

    def test_no_breach_at_moderate_down(self):
        d = _segment(self._run(), UPSTREAM)["directions"]["down"]
        self.assertIs(d["dividend_blocked"], False)
        self.assertIn("F-010", d["sandbox_checks"])
        self.assertIn("未击穿", _section(self._run().report, "## ③ 量化合理性"))

    def test_dividend_breach_judgement(self):
        """锂价跌至上游现金成本线（天齐约 3 万元/吨）以下 → 红利下传被阻断。

        注：基准价 13.4 万元/吨时，即使 −50% 也未击穿 3 万元/吨；
        故用 −85%（情景价 2.01 万元/吨）验证 F-010 的击穿分支确实生效。
        """
        r = self._run(delta=0.85)
        self.assertEqual(r.status, "ok")
        d = _segment(r, UPSTREAM)["directions"]["down"]
        self.assertIs(d["dividend_blocked"], True)
        body3 = _section(r.report, "## ③ 量化合理性")
        self.assertIn("红利阻断", body3)
        self.assertIn("已击穿现金成本线", body3)

    def test_out_of_boundary_warns(self):
        r = self._run(delta=0.6)
        self.assertEqual(r.status, "ok")
        self.assertTrue(any("适用边界" in w for w in r.warnings))

    # ---------------- 可复现 / 降级 ----------------

    def test_deterministic_rerun(self):
        r1 = self._run()
        r2 = self._run()
        self.assertEqual(_segments_key(r1), _segments_key(r2))
        self.assertEqual(_body(r1.report), _body(r2.report), "同输入重跑报告正文不一致")

    def test_extreme_delta_fails_gracefully(self):
        r = self._run(delta=10.0)
        self.assertEqual(r.status, "failed")
        self.assertTrue(any("超出允许范围" in w for w in r.warnings))

    def test_non_numeric_delta_fails_gracefully(self):
        request = AnalysisRequest(delta_pct="涨很多", use_llm=False)  # type: ignore[arg-type]
        r = self.orch.run(request, quiet=True)
        self.assertEqual(r.status, "failed")
        self.assertTrue(any("必须是数字" in w for w in r.warnings))

    def test_missing_series_fails_gracefully(self):
        r = self._run(series="不存在品种")
        self.assertEqual(r.status, "failed")
        self.assertTrue(any("未检索到基准价格" in w for w in r.warnings))

    def test_missing_pdf_warns_but_completes(self):
        r = self._run(pdf_paths=["no_such_report.pdf"])
        self.assertEqual(r.status, "ok")
        self.assertTrue(any("PDF 解析失败" in w for w in r.warnings))

    def test_pdf_evidence_extracted(self):
        from config import DATA_DIR

        pdfs = [str(p) for p in sorted((DATA_DIR / "reports").glob("*.pdf"))]
        self.assertTrue(pdfs, "缺少测试用财报 PDF")
        r = self._run(pdf_paths=pdfs)
        self.assertEqual(r.status, "ok")
        self.assertTrue(r.pdf_evidence)
        self.assertIn("lithium_cost_share", r.pdf_evidence[0]["fields"])
        # 抽取结果必须带可读的原文证据
        hit = r.pdf_evidence[0]["fields"]["lithium_cost_share"]
        self.assertTrue(hit["evidence"])
        self.assertIsInstance(hit["page"], int)

    def test_log_is_jsonl(self):
        r = self._run()
        records = _log_records(r.log_path)
        self.assertGreater(len(records), 5)
        for record in records:
            self.assertIn("ts", record)
            self.assertIn("step", record)
            self.assertIn("action", record)
            self.assertEqual(record["run_id"], r.run_id)

    def test_sensitivity_engine_and_breach_critical_render(self):
        """v6：③ 段常驻敏感度引擎表 + 击穿临界；敏感度示例 Δm/ρ 只允许进附录 A-0。"""
        r = self._run()
        body3 = _section(r.report, "## ③ 量化合理性")
        self.assertIn("敏感度引擎", body3)
        self.assertIn("敏感度(pct/万元)", body3)
        self.assertIn("红利阻断临界", body3)
        self.assertIn("S-1", body3, "③ 段常驻说明应提及压力情景 S-1 仅供单独检验")
        appendix = r.report[r.report.find("## 附录 A"):]
        self.assertIn("A-0 敏感度引擎示例明细", appendix)
        self.assertIn("ρ=|Δm|/m0", appendix)
        # ② ③ 段均不得出现研报打分（v6 已移除该模块）
        self.assertNotIn("研报打分", _section(r.report, "## ② 证据充分性"))
        self.assertNotIn("研报打分", body3)

    def test_stress_scenario_S1_triggers_breach(self):
        """压力情景 S-1（-80%，独立触发）：情景价约 2.68 万低于现金成本线 3 万 → 红利阻断：是。"""
        request = AnalysisRequest(
            delta_pct=-0.8, use_llm=False, bidirectional=False, stress=True
        )
        r = self.orch.run(request, quiet=True)
        self.assertEqual(r.status, "ok")
        d = _segment(r, UPSTREAM)["directions"]["down"]
        self.assertIs(d["dividend_blocked"], True)
        body3 = _section(r.report, "## ③ 量化合理性")
        self.assertIn("压力情景 S-1", body3)
        self.assertIn("红利阻断", body3)
        self.assertIn("价格已低于现金成本线", body3)

    def test_stress_scenario_not_in_default_batch(self):
        """S-1 是独立压力情景，不进默认批量；默认单跑（±15%）不触发击穿。"""
        d = _segment(self._run(), UPSTREAM)["directions"]["down"]
        self.assertIs(d["dividend_blocked"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)