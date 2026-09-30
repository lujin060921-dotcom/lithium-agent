"""三大工具 + PDF 解析的独立单测。

运行： .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import DATA_DIR  # noqa: E402
from tools import calculator as calc  # noqa: E402
from tools import pdf_parser  # noqa: E402
from tools.retriever import Retriever  # noqa: E402

PDF_FIXTURE = DATA_DIR / "reports" / "产业链结构与量化公式_内部资料.pdf"


class TestFormulas(unittest.TestCase):
    def test_f000_price_change(self):
        r = calc.call_formula("F-000", base=135000, new=148500)["result"]
        self.assertAlmostEqual(r["price_change_pct"], 0.1, places=6)

    def test_f001_lithium_pricing(self):
        r = calc.lithium_pricing_formula(135000, 0.75, 9800)["result"]
        # 135000*0.75 + 9800 = 111050
        self.assertAlmostEqual(r["cathode_price"], 111050.0, places=2)

    def test_f002_cost_impact(self):
        r = calc.cost_share_impact(0.7, 0.1, 1.0)["result"]
        self.assertAlmostEqual(r["cost_impact_pct"], 0.07, places=6)

    def test_f002_rejects_bad_share(self):
        with self.assertRaises(ValueError):
            calc.cost_share_impact(1.5, 0.1, 1.0)

    def test_f006_upstream_elasticity(self):
        r = calc.upstream_margin_impact(135000, 70000, 0.1, 0.95)["result"]
        self.assertAlmostEqual(r["base_revenue"], 9.45e9, places=0)
        self.assertAlmostEqual(r["delta_gross_profit"], 9.45e9 * 0.1 * 0.95, places=0)

    def test_f004_allows_thin_or_negative_margin(self):
        """框架选定的下游整车毛利偏薄，F-004 不得因毛利率为负而拒绝该企业。"""
        r = calc.margin_with_pricing_formula(-0.05, -0.011, -0.0825)["result"]
        self.assertIn("基准毛利率为负", r["margin_basis"])
        with self.assertRaises(ValueError):
            calc.margin_with_pricing_formula(-1.0, 0.0, 0.0)

    def test_f010_cash_cost_breach(self):
        """现金成本线取【2】实际口径：天齐锂业约 3 万元/吨。"""
        r = calc.cash_cost_breach(20100, 30000)["result"]
        self.assertTrue(r["breached"])
        self.assertLess(r["distance_to_cash_cost_pct"], 0)
        r2 = calc.cash_cost_breach(113900, 30000)["result"]
        self.assertFalse(r2["breached"])

    def test_f011_strength_grade_relative(self):
        """v6：主口径相对变动 ρ=|Δm|/m0；高 ρ≥0.20、中 0.05≤ρ<0.20、低 ρ<0.05。"""
        self.assertEqual(calc.strength_grade(0.05, m0=0.2)["result"]["grade"], "高")   # ρ=0.25
        self.assertEqual(calc.strength_grade(0.02, m0=0.2)["result"]["grade"], "中")   # ρ=0.10
        self.assertEqual(calc.strength_grade(0.005, m0=0.2)["result"]["grade"], "低")  # ρ=0.025
        r = calc.strength_grade(0.099, m0=0.5)["result"]  # ρ=0.198，落在 high 阈值±0.5pct 带内
        self.assertEqual(r["grade"], "中")
        self.assertTrue(r["critical"], "ρ 距阈值 ±0.5pct 带内应标「临界」")

    def test_f011_negative_baseline_falls_back_to_absolute(self):
        """基准毛利率非正时相对口径无意义，主导档回退绝对口径。"""
        r = calc.strength_grade(-0.03, m0=-0.05)["result"]
        self.assertEqual(r["grade"], "高")  # |Δm|=0.03 ≥ 绝对中阈 0.02

    def test_f013_sensitivity_delta(self):
        """敏感度引擎：Δm = 敏感度 × ΔP / 100（pct/万元 × 万元/吨）。"""
        r = calc.sensitivity_delta(2.01, 8.40, m0=0.3932)["result"]
        self.assertAlmostEqual(r["delta_m"], 8.40 * 2.01 / 100, places=5)
        self.assertAlmostEqual(r["rho"], (8.40 * 2.01 / 100) / 0.3932, places=5)

    def test_f010_critical_breach_threshold(self):
        """③ 段常驻击穿临界：-77.6%（= 现金成本线/基准价 − 1，3万/13.4万）。"""
        self.assertAlmostEqual(calc.cash_cost_breach(26800, 30000)["result"]["distance_to_cash_cost_pct"],
                               (26800 - 30000) / 30000, places=6)
        self.assertTrue(calc.cash_cost_breach(26800, 30000)["result"]["breached"])
        self.assertFalse(calc.cash_cost_breach(113900, 30000)["result"]["breached"])

    def test_every_formula_has_sandbox_check(self):
        """公式库与沙箱复算式必须一一对应，避免新增公式漏接交叉校验。"""
        self.assertEqual(set(calc.FORMULA_CHECKS), set(calc.FORMULA_REGISTRY))

    def test_new_formulas_pass_sandbox(self):
        cases = [
            calc.cash_cost_breach(113900, 30000),
            calc.strength_grade(0.05, m0=0.2),
            calc.sensitivity_delta(2.01, 8.40, m0=0.3932),
        ]
        for c in cases:
            check = calc.verify_in_sandbox(c)
            self.assertEqual(check["status"], "ok", f"{c['formula']}：{check}")

    def test_unknown_formula(self):
        with self.assertRaises(KeyError):
            calc.call_formula("F-999")

    def test_catalog_covers_registry(self):
        ids = {f["id"] for f in calc.FORMULA_CATALOG}
        self.assertEqual(ids, set(calc.FORMULA_REGISTRY))


class TestSandbox(unittest.TestCase):
    def test_expression(self):
        self.assertEqual(calc.execute_python("2+3*4")["result"], 14)

    def test_script_with_result_variable(self):
        out = calc.execute_python("a = 1\nb = 2\nresult = {'sum': a + b}")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["result"]["sum"], 3)

    def test_file_read_blocked(self):
        out = calc.execute_python('result = open("C:/Windows/win.ini").read(5)')
        self.assertEqual(out["status"], "error")
        self.assertIsNone(out["result"])

    def test_os_system_blocked(self):
        out = calc.execute_python('import os\nresult = os.system("echo hacked")')
        self.assertEqual(out["status"], "error")

    def test_timeout(self):
        out = calc.execute_python("while True:\n    pass", timeout=2)
        self.assertEqual(out["status"], "timeout")

    def test_empty_code_rejected(self):
        with self.assertRaises(ValueError):
            calc.execute_python("   ")


class TestRetriever(unittest.TestCase):
    def setUp(self):
        self.r = Retriever(DATA_DIR)

    def test_latest_price_with_source(self):
        p = self.r.get_latest_price()
        self.assertEqual(p["price"], 134000)
        self.assertEqual(p["date"], "2026-09-14")
        self.assertIn("http", p["source_url"])
        self.assertTrue(p["retrieved_at"])

    def test_latest_price_as_of(self):
        p = self.r.get_latest_price(as_of="2025-12-31")
        self.assertEqual(p["date"], "2025-12-31")
        self.assertEqual(p["price"], 130000)

    def test_search_range(self):
        rows = self.r.search_prices(start="2025-05-01", end="2025-09-30")
        self.assertEqual([r["date"] for r in rows], ["2025-05-31", "2025-06-30", "2025-09-30"])

    def test_search_unknown_name(self):
        with self.assertRaises(KeyError):
            self.r.search_prices(name="不存在品种")

    def test_params_lookup(self):
        p = self.r.get_params("湖南裕能")
        self.assertAlmostEqual(p["unit_consumption"], 0.25)
        self.assertEqual(p["segment"], "中游_磷酸铁锂正极")

    def test_params_unknown_company(self):
        with self.assertRaises(KeyError):
            self.r.get_params("不存在公司")

    def test_na_parsed_as_none(self):
        self.assertIsNone(self.r.get_params("天齐锂业")["unit_consumption"])

    def test_asymmetric_coefficients_are_separate_columns(self):
        """涨跌非对称：两套系数分列存放、不可混用；宁德按 v6 金属联动收敛为双向 0.85。

        宁德/比亚迪双向系数 v6 收敛为对称；蔚来保留不对称（涨 0.10 / 跌 0.15），
        用于证明两套参数确实是独立取数的两个列，而非单列取负换算。
        """
        cy = self.r.get_params("宁德时代")
        self.assertAlmostEqual(cy["pass_through_up"], 0.85)
        self.assertAlmostEqual(cy["dividend_release_down"], 0.85)
        self.assertNotIn("pass_through", cy)
        nio = self.r.get_params("蔚来")
        self.assertAlmostEqual(nio["pass_through_up"], 0.10)
        self.assertAlmostEqual(nio["dividend_release_down"], 0.15)
        self.assertNotEqual(nio["pass_through_up"], nio["dividend_release_down"])

    def test_upstream_new_fields(self):
        """上游：自给率驱动敏感度；现金成本线取自【2】实际口径。"""
        tq = self.r.get_params("天齐锂业")
        self.assertAlmostEqual(tq["self_sufficiency"], 1.0)
        self.assertAlmostEqual(tq["cash_cost_line"], 30000)
        self.assertTrue(tq["has_public_source"])

    def test_financial_and_coefficient_sources_are_tracked_separately(self):
        """【2】口径：财务/规模参数有公开来源，但方向系数无正式公开量化测算 → 两列必须分开标注。"""
        nio = self.r.get_params("蔚来")
        self.assertTrue(nio["has_public_source"], "蔚来财务参数来自 IR 年报，应有公开来源")
        self.assertFalse(nio["coefficient_public_source"], "红利释放系数无公开量化来源，应标 False")
        # 上游自给率有公开来源，两列都应为 True
        tq = self.r.get_params("天齐锂业")
        self.assertTrue(tq["coefficient_public_source"])

    def test_real_data_replaces_placeholder(self):
        """占位参数已被【2】交付的真实数据替换（来源不再是「占位参数·待…」）。"""
        for name in ("天齐锂业", "湖南裕能", "宁德时代", "蔚来", "比亚迪"):
            src = self.r.get_params(name)["source"]
            self.assertNotIn("占位参数", src, f"{name} 仍是占位来源")
            self.assertIn("S", src, f"{name} 的来源应带【2】数据集的编号")

    def test_cathode_uses_no_manual_coefficient(self):
        """正极的涨跌对称性由锂定价机制决定，两套系数列应为空。"""
        y = self.r.get_params("湖南裕能")
        self.assertIsNone(y["pass_through_up"])
        self.assertIsNone(y["dividend_release_down"])

    def test_chain_config(self):
        chain = self.r.get_chain_config()
        self.assertEqual(len(chain["nodes"]), 5)
        self.assertTrue(any(l["from"] == "battery" for l in chain["links"]))
        # 已对齐【1】理论框架 v1
        self.assertEqual(chain["version"], "v1-framework")
        self.assertEqual([s["id"] for s in chain["scenarios"]], ["A-1", "A-2", "B-1", "B-2", "S-1"])
        s1 = next(s for s in chain["scenarios"] if s["id"] == "S-1")
        self.assertTrue(s1.get("stress_only"), "S-1 必须是独立压力情景，不进默认批量")
        self.assertEqual(s1["delta_pct"], -0.8)
        self.assertIn("两套参数不能混用", chain["asymmetry"]["rule"])
        self.assertEqual(len(chain["assumptions"]), 5)
        self.assertEqual(len(chain["boundary_conditions"]["applicable"]), 4)
        self.assertEqual(len(chain["boundary_conditions"]["invalid"]), 6)
        self.assertEqual(chain["strength_grades"]["formula"], "F-011")
        self.assertEqual(chain["strength_grades"]["thresholds"]["high"], 0.20)
        self.assertEqual(chain["strength_grades"]["thresholds"]["medium"], 0.05)
        # 输出维度为四段（研报打分模块已移除）
        self.assertEqual(chain["output_dimensions"], [
            "产业分析逻辑", "证据充分性", "量化合理性", "理论应用边界",
        ])

    def test_chain_nodes_carry_bidirectional_roles(self):
        chain = self.r.get_chain_config()
        for node in chain["nodes"]:
            for field in ("role_up", "role_down", "decisive_variable"):
                self.assertTrue(node.get(field), f"{node['id']} 缺 {field}")

    def test_downstream_company_is_nio(self):
        """框架指定下游整车为蔚来，长安汽车不应再出现。"""
        self.assertEqual(self.r.list_companies(segment="下游_整车"), ["蔚来"])
        with self.assertRaises(KeyError):
            self.r.get_params("长安汽车")

    def test_missing_dataset(self):
        with self.assertRaises(FileNotFoundError):
            Retriever(Path("no_such_dir")).get_latest_price()


class TestPdfParser(unittest.TestCase):
    def test_parse_and_extract(self):
        self.assertTrue(PDF_FIXTURE.exists(), f"缺少测试用 PDF：{PDF_FIXTURE}")
        doc = pdf_parser.parse_pdf(PDF_FIXTURE)
        self.assertEqual(doc.page_count, 3)
        self.assertFalse(doc.scan_warning)
        self.assertIn("碳酸锂", doc.full_text)

        result = pdf_parser.extract_fields_from_path(
            PDF_FIXTURE, fields=["lithium_cost_share", "unit_consumption"]
        )
        # 原文含"碳酸锂占其成本 60-80%"与"碳酸锂约占电池成本 30-40%"
        self.assertIn("lithium_cost_share", result["fields"])
        hit = result["fields"]["lithium_cost_share"]
        self.assertTrue(hit["evidence"])
        self.assertGreater(hit["page"], 0)

    def test_missing_pdf(self):
        with self.assertRaises(FileNotFoundError):
            pdf_parser.parse_pdf("no_such_file.pdf")

    def test_non_pdf_rejected(self):
        with self.assertRaises(ValueError):
            pdf_parser.parse_pdf(DATA_DIR / "params" / "company_params.csv")


if __name__ == "__main__":
    unittest.main(verbosity=2)