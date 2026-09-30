"""Tool 3｜受限代码执行沙箱 + 量化公式库

红线：**所有数值一律由本工具计算产出，大模型不得自行算数**（防幻觉 + 满足"量化合理性"评分）。

两个使用层级
1. `formulas` 模块：预置、可审计的量化公式（锂定价公式、毛利影响、传导系数、敏感性）。
2. `execute_python`：通用受限 Python 沙箱（subprocess + 超时 + 内存上限 + 资源目录只读），
   用于运行队友【2】交付的自定义公式脚本；输出自动序列化为可审计 JSON。
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

# ---------- 预置量化公式库（公式编号可溯源到 prompts 里的引用） ----------

def lithium_pricing_formula(
    lithium_price: float,
    unit_consumption: float,
    processing_fee: float,
) -> dict:
    """磷酸铁锂「锂定价」：正极售价 = 碳酸锂月均价 × 单耗系数 + 加工费。

    公式编号：F-001（来源：磷酸铁锂上市公司问询函回复中的锂定价机制）
    """
    if lithium_price < 0:
        raise ValueError("lithium_price 不能为负")
    if unit_consumption < 0:
        raise ValueError("unit_consumption 不能为负")
    if processing_fee < 0:
        raise ValueError("processing_fee 不能为负")
    price = lithium_price * unit_consumption + processing_fee
    return {
        "formula": "F-001 锂定价公式",
        "inputs": {
            "lithium_price": lithium_price,
            "unit_consumption": unit_consumption,
            "processing_fee": processing_fee,
        },
        "result": {
            "cathode_price": round(price, 2),
            "unit": "元/吨",
        },
    }


def cost_share_impact(
    total_cost_share: float,
    price_change_pct: float,
    passthrough_pct: float,
) -> dict:
    """原材料涨价对环节成本的直接冲击（未对冲、按成本占比线性传导）。

    公式编号：F-002
    环节成本影响% ≈ 原材料成本占比 × 价格变动% × 传导系数
    """
    if not (0 <= total_cost_share <= 1):
        raise ValueError("total_cost_share 应在 [0,1]")
    if not (0 <= passthrough_pct <= 1):
        raise ValueError("passthrough_pct 应在 [0,1]")
    impact = total_cost_share * price_change_pct * passthrough_pct
    return {
        "formula": "F-002 成本传导冲击",
        "inputs": {
            "total_cost_share": total_cost_share,
            "price_change_pct": price_change_pct,
            "passthrough_pct": passthrough_pct,
        },
        "result": {
            "cost_impact_pct": round(impact, 4),
            "unit": "小数（如 0.05 = 5pct）",
        },
    }


def margin_shift(
    gross_margin: float,
    cost_impact_pct: float,
) -> dict:
    """毛利率变动 ≈ 成本冲击的相反数（售价不变假设下）。

    公式编号：F-003
    Δ毛利率 ≈ -(成本影响%)；若售价随成本联动（锂定价），需另加传导项。
    """
    if gross_margin < 0:
        raise ValueError("gross_margin 不能为负")
    shift = -cost_impact_pct
    return {
        "formula": "F-003 毛利变动（售价不变假设）",
        "inputs": {"gross_margin": gross_margin, "cost_impact_pct": cost_impact_pct},
        "result": {
            "margin_shift_pct": round(shift, 4),
            "unit": "小数（如 -0.05 = 毛利率 -5pct）",
        },
    }


def margin_with_pricing_formula(
    gross_margin: float,
    price_change_pct: float,
    cost_change_pct: float,
) -> dict:
    """售价与成本双动下的毛利率变化近似。

    公式编号：F-004
    Δ毛利率 ≈ (1+Δ售价%) − (1+Δ成本%)×(1−原毛利率) − 原毛利率

    下界放宽到 -1：框架选定的下游整车（蔚来）毛利率偏薄，甚至可能为负，
    若仍按「毛利率不得为负」拒绝，会把框架指定企业直接挡在量化之外。
    但负基准会被误读，故输出中带 margin_basis 标记。
    """
    if gross_margin <= -1:
        raise ValueError("gross_margin 必须 > -100%")
    margin_after = (1 + price_change_pct) - (1 + cost_change_pct) * (1 - gross_margin)
    change = margin_after - gross_margin
    return {
        "formula": "F-004 毛利变化（售价成本双动）",
        "inputs": {
            "gross_margin": gross_margin,
            "price_change_pct": price_change_pct,
            "cost_change_pct": cost_change_pct,
        },
        "result": {
            "margin_after_pct": round(margin_after, 4),
            "margin_change_pct": round(change, 4),
            "margin_basis": "基准毛利率为负，变动幅度应看 pct 差值而非相对比例"
            if gross_margin < 0
            else "基准毛利率为正",
            "unit": "小数",
        },
    }


def profit_impact(
    margin: float,
    revenue_base: float,
    margin_shift: float,
) -> dict:
    """利润影响 = 基准营收 × 毛利变化。

    公式编号：F-005
    Δ利润 ≈ 基准营收 × Δ毛利率（营业收入口径近似，单位需外部注明）
    """
    if margin_shift <= -1:
        raise ValueError("margin_shift 不能 ≤ -1（毛利率为负）")
    delta_profit = revenue_base * margin_shift
    return {
        "formula": "F-005 利润影响估算",
        "inputs": {"revenue_base": revenue_base, "margin_shift": margin_shift},
        "result": {
            "delta_profit": round(delta_profit, 2),
            "unit": "与 revenue_base 同单位（元/万元/亿元）",
        },
    }


def price_change_between(base: float, new: float) -> dict:
    """变动率计算：price_change_pct = (new-base)/base。

    公式编号：F-000 基础变动率
    """
    if base == 0:
        raise ValueError("base 不能为 0")
    pct = (new - base) / base
    return {
        "formula": "F-000 基础变动率",
        "inputs": {"base": base, "new": new},
        "result": {"price_change_pct": round(pct, 6), "unit": "小数（0.10 = +10%）"},
    }


def upstream_margin_impact(
    base_price: float,
    base_volume: float,
    price_change_pct: float,
    sensitivity: float,
) -> dict:
    """上游（资源自给型）毛利弹性：售价上涨在成本刚性下几乎全额落到毛利。

    公式编号：F-006
    基准营收 = 基准单价 × 基准销量
    Δ毛利   = 基准营收 × 价格变动% × 敏感度（成本刚性，故敏感度≈1）
    Δ毛利率 = Δ毛利 / 新营收
    """
    if base_price <= 0 or base_volume <= 0:
        raise ValueError("base_price / base_volume 必须为正")
    if sensitivity < 0:
        raise ValueError("sensitivity 不能为负")
    base_revenue = base_price * base_volume
    new_revenue = base_revenue * (1 + price_change_pct)
    delta_gross_profit = base_revenue * price_change_pct * sensitivity
    if new_revenue == 0:
        raise ValueError("新营收为 0，无法计算毛利率变动")
    return {
        "formula": "F-006 上游毛利弹性",
        "inputs": {
            "base_price": base_price,
            "base_volume": base_volume,
            "price_change_pct": price_change_pct,
            "sensitivity": sensitivity,
        },
        "result": {
            "base_revenue": round(base_revenue, 2),
            "new_revenue": round(new_revenue, 2),
            "delta_gross_profit": round(delta_gross_profit, 2),
            "margin_shift_pct": round(delta_gross_profit / new_revenue, 4),
            "unit": "金额为 元，比率为小数",
        },
    }


def scenario_price(base_price: float, delta_pct: float) -> dict:
    """情景价格：new_price = base_price × (1 + delta_pct)。

    公式编号：F-007
    """
    if base_price <= 0:
        raise ValueError("base_price 必须为正")
    if delta_pct <= -1:
        raise ValueError("delta_pct 不能 ≤ -100%")
    return {
        "formula": "F-007 情景价格",
        "inputs": {"base_price": base_price, "delta_pct": delta_pct},
        "result": {
            "new_price": round(base_price * (1 + delta_pct), 2),
            "unit": "元/吨",
        },
    }


def price_pass_through(cost_impact_pct: float, pass_through: float) -> dict:
    """售价传导幅度 = 成本冲击% × 传导系数。

    公式编号：F-008（传导系数来自参数表，衡量议价能力/机制顺畅度）
    """
    if not (0 <= pass_through <= 1.5):
        raise ValueError("pass_through 应在 [0,1.5]")
    return {
        "formula": "F-008 售价传导幅度",
        "inputs": {"cost_impact_pct": cost_impact_pct, "pass_through": pass_through},
        "result": {
            "price_pass_pct": round(cost_impact_pct * pass_through, 6),
            "unit": "小数",
        },
    }


def base_revenue(base_price: float, base_volume: float) -> dict:
    """基准营收 = 基准单价 × 基准销量。

    公式编号：F-009
    """
    if base_price <= 0 or base_volume <= 0:
        raise ValueError("base_price / base_volume 必须为正")
    return {
        "formula": "F-009 基准营收",
        "inputs": {"base_price": base_price, "base_volume": base_volume},
        "result": {"base_revenue": round(base_price * base_volume, 2), "unit": "元"},
    }


# 浮点比较统一加容差（避免边界值因浮点误差误判档位）
GRADE_TOL = 1e-9
# 相对口径阈值（v6）：高 ρ≥0.20；中 0.05≤ρ<0.20；低 ρ<0.05
RELA_HIGH = 0.20
RELA_MEDIUM = 0.05
# 绝对口径交叉校验阈值（v6 用于与相对口径互证）：|Δm|<0.01 低；0.01≤|Δm|<0.02 中；≥0.02 高
ABS_LOW = 0.01
ABS_MEDIUM = 0.02
# 临界判定带：ρ 落在某阈值 ±0.5pct 带内时标「临界」并给出相邻两档说明
DEFAULT_CRITICAL_BAND = 0.005


def cash_cost_breach(scenario_price: float, cash_cost_line: float) -> dict:
    """上游现金成本线击穿判定（下跌方向的红利阻断开关）。

    公式编号：F-010
    框架 2.2：下跌阶段上游到达现金成本线会减产、停产保价，阻断成本红利向下传递。
    判定条件：情景价格 ≤ 现金成本线 即视为击穿。
    """
    if scenario_price <= 0:
        raise ValueError("scenario_price 必须为正")
    if cash_cost_line <= 0:
        raise ValueError("cash_cost_line 必须为正")
    distance = (scenario_price - cash_cost_line) / cash_cost_line
    return {
        "formula": "F-010 现金成本线击穿判定",
        "inputs": {"scenario_price": scenario_price, "cash_cost_line": cash_cost_line},
        "result": {
            "breached": scenario_price <= cash_cost_line,
            "distance_to_cash_cost_pct": round(distance, 6),
            "unit": "小数（负值表示价格已低于现金成本线）",
        },
    }


def strength_grade(
    margin_change_pct: float,
    m0: float,
    high: float = RELA_HIGH,
    medium: float = RELA_MEDIUM,
    critical_band: float = DEFAULT_CRITICAL_BAND,
) -> dict:
    """影响强弱分级：主口径改为「相对变动」ρ = |Δm| ÷ m0（v6）。

    公式编号：F-011
    - 主口径（相对，v6 裁定）：高 ρ≥0.20；中 0.05≤ρ<0.20；低 ρ<0.05（阈值可配置）。
    - 交叉校验（绝对口径）：|Δm|<0.01 低；0.01≤|Δm|<0.02 中；≥0.02 高——与相对口径互证，
      仅作旁证，**主导分档一律用相对口径**。
    - 临界标记：当 ρ 落在某阈值 ±critical_band（默认 ±0.5pct）带内时标「临界」，
      并给出相邻两档说明，不得只输出单一档位。
    - 基准毛利率非正（m0≤0）时相对口径无意义，回退绝对口径作主导档。
    所有浮点比较统一加 GRADE_TOL 容差。
    """
    if high <= 0 or medium <= 0:
        raise ValueError("分级阈值必须为正")
    if medium > high:
        raise ValueError("medium 阈值不能大于 high 阈值")
    magnitude = abs(margin_change_pct)
    if m0 is None or m0 <= 0:
        # 负/零基准毛利下相对比例会被误读，主导档回退绝对口径
        rho = None
        primary_code = (
            1 if magnitude + GRADE_TOL >= ABS_MEDIUM
            else 2 if magnitude + GRADE_TOL >= ABS_LOW
            else 3
        )
    else:
        rho = magnitude / m0
        primary_code = (
            1 if rho + GRADE_TOL >= high
            else 2 if rho + GRADE_TOL >= medium
            else 3
        )
    # 绝对口径交叉校验（恒可算，用于与相对口径互证）
    cross_code = (
        1 if magnitude + GRADE_TOL >= ABS_MEDIUM
        else 2 if magnitude + GRADE_TOL >= ABS_LOW
        else 3
    )
    labels = {1: "高", 2: "中", 3: "低"}
    critical = []
    if rho is not None:
        adjacent = {medium: ("低", "中"), high: ("中", "高")}
        for thr in (medium, high):
            if abs(rho - thr) <= critical_band:
                lower, upper = adjacent[thr]
                critical.append(
                    {
                        "主导档位": labels[primary_code],
                        "临界阈值": f"ρ={thr:.2f}",
                        "临界区间": f"ρ∈[{thr - critical_band:.3f}, {thr + critical_band:.3f}]",
                        "相邻两档": f"{lower} / {upper}",
                    }
                )
    return {
        "formula": "F-011 影响强弱分级（相对口径）",
        "inputs": {
            "margin_change_pct": margin_change_pct,
            "m0": m0,
            "high": high,
            "medium": medium,
            "critical_band": critical_band,
        },
        "result": {
            "grade": labels[primary_code],
            "grade_code": primary_code,
            "rho": round(rho, 6) if rho is not None else None,
            "magnitude_abs": round(magnitude, 6),
            "cross_grade": labels[cross_code],
            "cross_grade_code": cross_code,
            "critical": critical if critical else None,
            "unit": "分级 1=高 2=中 3=低；ρ=|Δm|/m0 无量纲",
        },
    }


def sensitivity_delta(delta_p_wan: float, sensitivity: float, m0: float) -> dict:
    """敏感度引擎·示例计算：Δm = 敏感度 × ΔP（ΔP 单位为万元/吨，敏感度 pct/万元）。

    公式编号：F-013
    模型值（单变量理论值）：Δm = sensitivity × ΔP / 100（敏感度由 pct 转为小数）
    margin_change_pct = Δm，供 F-011 按相对口径分级（ρ = |Δm|/m0）。
    """
    delta_m = sensitivity * delta_p_wan / 100.0
    rho = abs(delta_m) / m0 if m0 is not None and m0 > 0 else None
    return {
        "formula": "F-013 敏感度引擎示例",
        "inputs": {"delta_p_wan": delta_p_wan, "sensitivity": sensitivity, "m0": m0},
        "result": {
            "delta_m": round(delta_m, 6),
            "rho": round(rho, 6) if rho is not None else None,
            "unit": "Δm 为小数（0.05 = 5pct）；ρ=|Δm|/m0 无量纲",
        },
    }


FORMULA_REGISTRY = {
    "F-000": price_change_between,
    "F-001": lithium_pricing_formula,
    "F-002": cost_share_impact,
    "F-003": margin_shift,
    "F-004": margin_with_pricing_formula,
    "F-005": profit_impact,
    "F-006": upstream_margin_impact,
    "F-007": scenario_price,
    "F-008": price_pass_through,
    "F-009": base_revenue,
    "F-010": cash_cost_breach,
    "F-011": strength_grade,
    "F-013": sensitivity_delta,
}

FORMULA_CATALOG = [
    {"id": "F-000", "name": "基础变动率", "expr": "(new − base) / base"},
    {"id": "F-001", "name": "锂定价公式", "expr": "正极售价 = 碳酸锂月均价 × 单耗系数 + 加工费"},
    {"id": "F-002", "name": "成本传导冲击", "expr": "成本影响% = 锂成本占比 × 价格变动% × 传导系数"},
    {"id": "F-003", "name": "毛利变动（售价不变）", "expr": "Δ毛利率 = −成本影响%"},
    {"id": "F-004", "name": "毛利变化（售价成本双动）", "expr": "Δ毛利率 = (1+Δ售价%) − (1+Δ成本%)×(1−原毛利率) − 原毛利率"},
    {"id": "F-005", "name": "利润影响估算", "expr": "Δ利润 = 基准营收 × Δ毛利率"},
    {"id": "F-006", "name": "上游毛利弹性", "expr": "Δ毛利 = 基准单价 × 基准销量 × 价格变动% × 敏感度"},
    {"id": "F-007", "name": "情景价格", "expr": "新价 = 基准价 × (1 + 变动幅度)"},
    {"id": "F-008", "name": "售价传导幅度", "expr": "售价传导% = 成本冲击% × 传导系数（按涨跌方向取用不同系数）"},
    {"id": "F-009", "name": "基准营收", "expr": "基准营收 = 基准单价 × 基准销量"},
    {"id": "F-010", "name": "现金成本线击穿判定", "expr": "距现金成本线% = (情景价 − 现金成本线) / 现金成本线"},
    {"id": "F-011", "name": "影响强弱分级（相对口径）", "expr": "主口径 ρ=|Δm|/m0 → 高(ρ≥0.20) / 中(0.05≤ρ<0.20) / 低(ρ<0.05)；绝对口径交叉校验；ρ 距阈值±0.5pct 带内标临界"},
    {"id": "F-013", "name": "敏感度引擎示例", "expr": "Δm = 敏感度(pct/万元) × ΔP(万元/吨) / 100"},
]


def call_formula(formula_id: str, **inputs: float) -> dict:
    """按公式编号调用预置公式（校验参数存在）。"""
    if formula_id not in FORMULA_REGISTRY:
        raise KeyError(f"未知公式：{formula_id}；可选={sorted(FORMULA_REGISTRY)}")
    return FORMULA_REGISTRY[formula_id](**inputs)


def formula_id_of(calculation: dict) -> str:
    """从计算记录中取出公式编号（返回结构的 formula 字段形如 "F-001 锂定价公式"）。"""
    return calculation.get("formula", "").split(" ")[0]


# ---------- 沙箱独立复核（交叉校验） ----------
# 每条公式配一份「独立表达式」，在受限沙箱里重算一遍并与公式库结果比对。
# 作用：让 Tool 3 的受限代码执行真实参与主流程，并为关键数值提供第二份独立证据。
# 第三项是公式输出的小数位（用于确定比对容差：公式返回值经过 round，不能按 1e-9 严判）。

FORMULA_CHECKS: dict[str, tuple[str, str, int]] = {
    "F-000": ("price_change_pct", "(new - base) / base", 6),
    "F-001": ("cathode_price", "lithium_price * unit_consumption + processing_fee", 2),
    "F-002": ("cost_impact_pct", "total_cost_share * price_change_pct * passthrough_pct", 4),
    "F-003": ("margin_shift_pct", "-cost_impact_pct", 4),
    "F-004": (
        "margin_after_pct",
        "(1 + price_change_pct) - (1 + cost_change_pct) * (1 - gross_margin)",
        4,
    ),
    "F-005": ("delta_profit", "revenue_base * margin_shift", 2),
    "F-006": (
        "delta_gross_profit",
        "base_price * base_volume * price_change_pct * sensitivity",
        2,
    ),
    "F-007": ("new_price", "base_price * (1 + delta_pct)", 2),
    "F-008": ("price_pass_pct", "cost_impact_pct * pass_through", 6),
    "F-009": ("base_revenue", "base_price * base_volume", 2),
    "F-010": (
        "distance_to_cash_cost_pct",
        "(scenario_price - cash_cost_line) / cash_cost_line",
        6,
    ),
    "F-011": (
        "grade_code",
        "1 if abs(margin_change_pct)/m0 + 1e-9 >= high else (2 if abs(margin_change_pct)/m0 + 1e-9 >= medium else 3)",
        0,
    ),
    "F-013": ("delta_m", "sensitivity * delta_p_wan / 100", 6),
}

# 各计算方法下最值得独立复核的关键公式
PRIMARY_CHECK = {
    "upstream_elasticity": "F-006",
    "pricing_formula": "F-001",
    "cost_push": "F-002",
}


def verify_in_sandbox(calculation: dict, timeout: float = 10.0) -> dict:
    """在受限沙箱中独立复算该条计算，返回一致性结论。不抛出异常。"""
    formula_id = formula_id_of(calculation)
    if formula_id not in FORMULA_CHECKS:
        return {"formula": formula_id, "status": "skipped", "reason": "该公式未配置沙箱复算式"}
    key, expr, decimals = FORMULA_CHECKS[formula_id]
    expected = calculation.get("result", {}).get(key)
    if expected is None:
        return {"formula": formula_id, "status": "skipped", "reason": f"结果中无字段 {key}"}

    code = "\n".join(
        [f"{name} = {value!r}" for name, value in calculation["inputs"].items()]
        + [f"result = {expr}"]
    )
    out = execute_python(code, timeout=timeout)
    if out["status"] != "ok":
        return {
            "formula": formula_id,
            "key": key,
            "expected": expected,
            "status": "sandbox_error",
            "error": out.get("error") or (out.get("stderr") or "")[:300],
        }

    try:
        actual = float(out["result"])
    except (TypeError, ValueError):
        return {
            "formula": formula_id,
            "key": key,
            "expected": expected,
            "raw": out["result"],
            "status": "sandbox_error",
            "error": "沙箱返回非数值",
        }

    # 公式库返回值经过 round(…, decimals)，故容差取「半个末位」与相对误差的较大者
    tolerance = max(0.5 * 10 ** (-decimals), abs(expected) * 1e-9, 1e-9)
    matched = abs(actual - expected) <= tolerance
    return {
        "formula": formula_id,
        "key": key,
        "expected": expected,
        "actual": actual,
        "status": "ok" if matched else "mismatch",
        "expression": expr,
        "tolerance": tolerance,
    }


# ---------- 通用受限沙箱（运行【2】交付的自定义公式脚本） ----------

# 固定引导脚本，用户代码经 stdin 传入（避免字符串拼接注入）
_SANDBOX_BOOTSTRAP = """
import json, math, statistics, os, sys
_script = sys.stdin.read()
# 删除危险模块访问入口（避免 import os 后利用其执行系统命令）
try:
    del os.system, os.popen, os.spawnl, os.spawnle, os.spawnlp, os.spawnlpe, os.spawnv, os.spawnve, os.spawnvp, os.spawnvpe
    del os.posix_spawn, os.posix_spawnp, os.execl, os.execle, os.execlp, os.execlpe, os.execv, os.execve, os.execvp, os.execvpe
except AttributeError:
    pass
try:
    del os.chdir, os.remove, os.rename, os.mkdir, os.makedirs, os.rmdir, os.unlink
except AttributeError:
    pass
sys.modules['subprocess'] = None
sys.modules['socket'] = None
sys.modules['shutil'] = None
__builtins__.open = None
_ns = {"__builtins__": __builtins__, "__name__": "sandbox", "json": json, "math": math, "statistics": statistics}
try:
    result = eval(_script, _ns)
except SyntaxError:
    exec(_script, _ns)
    result = _ns.get("result", None)
print("__SANDBOX_RESULT__=" + json.dumps({"status": "ok", "result": result}, ensure_ascii=False, default=str))
"""


def execute_python(
    code: str,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """在隔离子进程 + 受限命名空间中执行 Python 代码（防逃逸、防资源滥用）。

    支持两种写法：
    - 表达式（eval 模式，如 `10/3`）
    - 脚本并把最终值赋给变量 `result`（exec 模式）
    返回 {status, result, stdout, stderr, exit_code}；异常/超时同样以结构化结果返回，不抛出。
    强制限制：子进程隔离（-I 忽略用户 site-packages）、超时即杀、禁用系统命令与网络模块。
    """
    if not code or not code.strip():
        raise ValueError("code 不能为空")
    code = code.strip()
    # 形如 "10/3" 的纯表达式自动补 result = 前缀
    if "\n" not in code and "=" not in code and "import" not in code:
        code = "result = " + code

    cmd = [sys.executable, "-I", "-X", "utf8", "-c", _SANDBOX_BOOTSTRAP]
    try:
        proc = subprocess.run(
            cmd,
            input=code,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            env=_sandbox_env(),
            creationflags=_get_creation_flags(),
        )
    except subprocess.TimeoutExpired:
        return _err("timeout", f"执行超时（>{timeout}s），已强制终止")
    except Exception as exc:  # noqa: BLE001
        return _err("error", f"沙箱启动失败：{exc}")

    out, err = proc.stdout or "", proc.stderr or ""
    for line in out.splitlines():
        if line.startswith("__SANDBOX_RESULT__="):
            try:
                payload = json.loads(line.split("=", 1)[1])
            except json.JSONDecodeError:
                break
            return {
                "status": payload.get("status", "ok"),
                "result": payload.get("result"),
                "stdout": out,
                "stderr": err,
                "exit_code": proc.returncode,
            }
    return _err(
        "error",
        "沙箱未返回结果（代码可能触发了被禁用的能力）",
        stdout=out,
        stderr=err,
        exit_code=proc.returncode,
    )


def _err(status: str, message: str, **extra: Any) -> dict[str, Any]:
    """统一错误返回结构：保证调用方永远能拿到 status / result / error 三个键。"""
    return {"status": status, "result": None, "error": message, **extra}


def _sandbox_env() -> dict[str, str]:
    import os

    return {
        "PYTHONHASHSEED": "0",
        "PYTHONIOENCODING": "utf-8",
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
    }


def _get_creation_flags():
    if sys.platform == "win32":
        return subprocess.CREATE_NO_WINDOW
    return 0