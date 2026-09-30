"""Tool 2｜数据检索

职责：加载本地预置数据集（若不联网也能跑），支持按"品种/日期区间"查价格、按"企业/环节"查参数。
所有返回值强制带上 source（数据来源）、source_url、retrieved_at（抓取时间），满足可追溯要求。

数据集（全部为本地文件，不依赖联网）
- data/prices/quotes.csv          行情/价格序列（列：Date,Name,Price,Unit,Source,SourceURL,RetrievedAt）
- data/params/company_params.csv  企业参数表（企业 × 成本占比 × 毛利率 × 单耗系数 × 传导系数）
- data/framework/transmission_chain.json  传导链配置（环节 → 变量 → 方向 → 时滞）

价格数值全部以"元/吨"为内部统一口径；参数表数值统一为小数（0.35 = 35%）。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PRICE_NAME = "碳酸锂_电池级_现货均价"


@dataclass
class PricePoint:
    date: str
    name: str
    price: float
    unit: str
    source: str
    source_url: str
    retrieved_at: str

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "name": self.name,
            "price": self.price,
            "unit": self.unit,
            "source": self.source,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
        }


class Retriever:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.prices_path = self.data_dir / "prices" / "quotes.csv"
        self.params_path = self.data_dir / "params" / "company_params.csv"
        self.chain_path = self.data_dir / "framework" / "transmission_chain.json"
        self._prices: list[PricePoint] | None = None
        self._params: dict[str, dict] | None = None
        self._chain: dict | None = None

    # ---------- 加载 ----------

    def _load_prices(self) -> list[PricePoint]:
        if self._prices is not None:
            return self._prices
        if not self.prices_path.exists():
            raise FileNotFoundError(f"价格数据集缺失：{self.prices_path}")
        points: list[PricePoint] = []
        with self.prices_path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                points.append(
                    PricePoint(
                        date=row["Date"].strip(),
                        name=row["Name"].strip(),
                        price=float(row["Price"]),
                        unit=row["Unit"].strip(),
                        source=row["Source"].strip(),
                        source_url=row["SourceURL"].strip(),
                        retrieved_at=row["RetrievedAt"].strip(),
                    )
                )
        points.sort(key=lambda p: (p.name, p.date))
        self._prices = points
        return points

    def _load_params(self) -> dict[str, dict]:
        if self._params is not None:
            return self._params
        if not self.params_path.exists():
            raise FileNotFoundError(f"参数表缺失：{self.params_path}")
        params: dict[str, dict] = {}
        with self.params_path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                company = row["company"].strip()
                params[company] = {
                    "company": company,
                    "code": row["code"].strip(),
                    "segment": row["segment"].strip(),
                    "role": row["role"].strip(),
                    # 数值字段统一转 float，缺失则置 None（由调用方决定是否降级）
                    "lithium_cost_share": _to_float(row.get("lithium_cost_share")),
                    "gross_margin": _to_float(row.get("gross_margin")),
                    "unit_consumption": _to_float(row.get("unit_consumption_t_per_t")),
                    "processing_fee": _to_float(row.get("processing_fee_yuan_per_t")),
                    # 涨跌非对称：两套系数分列存放，禁止正负取反换算（见【1】理论框架 2.3）
                    "pass_through_up": _to_float(row.get("pass_through_up")),
                    "dividend_release_down": _to_float(row.get("dividend_release_down")),
                    # 上游：受益强弱取决于锂资源自给率；下跌击穿现金成本线会减产保价
                    "self_sufficiency": _to_float(row.get("self_sufficiency")),
                    "cash_cost_line": _to_float(row.get("cash_cost_line")),
                    # 无公开来源的参数只做定性推演，不参与量化
                    "has_public_source": _to_bool(row.get("has_public_source")),
                    # 与上列分开：企业财务/规模/价格可能有公开来源，而两套方向系数未必有
                    # （【2】明确：券商研报中未检索到以「涨价成本转嫁系数」「下跌红利释放系数」命名的正式量化测算）
                    "coefficient_public_source": _to_bool(row.get("coefficient_public_source")),
                    "base_price": _to_float(row.get("base_price")),
                    "base_price_unit": (row.get("base_price_unit") or "").strip(),
                    "base_volume": _to_float(row.get("base_volume")),
                    "volume_unit": (row.get("volume_unit") or "").strip(),
                    "source": (row.get("source") or "").strip(),
                    "source_url": (row.get("source_url") or "").strip(),
                    "note": (row.get("note") or "").strip(),
                }
        self._params = params
        return params

    def _load_chain(self) -> dict:
        if self._chain is not None:
            return self._chain
        if not self.chain_path.exists():
            raise FileNotFoundError(f"传导链配置缺失：{self.chain_path}")
        with self.chain_path.open("r", encoding="utf-8") as f:
            self._chain = json.load(f)
        return self._chain

    # ---------- 查询 ----------

    def list_price_names(self) -> list[str]:
        return sorted({p.name for p in self._load_prices()})

    def search_prices(
        self,
        name: str = DEFAULT_PRICE_NAME,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict]:
        """按品种 + 日期区间（含端点，YYYY-MM-DD）检索价格序列。"""
        points = [
            p
            for p in self._load_prices()
            if p.name == name
            and (start is None or p.date >= start)
            and (end is None or p.date <= end)
        ]
        if not points:
            raise KeyError(
                f"未检索到价格数据：name={name!r} start={start!r} end={end!r}；"
                f"可选品种={self.list_price_names()}"
            )
        return [p.to_dict() for p in points]

    def get_latest_price(self, name: str = DEFAULT_PRICE_NAME, as_of: str | None = None) -> dict:
        """取基准价格。as_of=YYYY-MM-DD 时取不晚于该日的最后一个报价。"""
        points = [p for p in self._load_prices() if p.name == name]
        if as_of:
            points = [p for p in points if p.date <= as_of]
        if not points:
            raise KeyError(f"未检索到基准价格：name={name!r} as_of={as_of!r}")
        latest = max(points, key=lambda p: p.date)
        return latest.to_dict()

    def get_params(self, company: str) -> dict:
        params = self._load_params()
        if company not in params:
            raise KeyError(f"参数表中不存在企业：{company}；可选={sorted(params)}")
        return dict(params[company])

    def list_companies(self, segment: str | None = None, role: str | None = None) -> list[str]:
        rows = self._load_params().values()
        return sorted(
            r["company"]
            for r in rows
            if (segment is None or r["segment"] == segment)
            and (role is None or r["role"] == role)
        )

    def get_chain_config(self) -> dict:
        return self._load_chain()

    def search_documents(self, keyword: str) -> list[dict]:
        """关键词检索：在参数表备注、企业说明等文本字段中做大小写不敏感匹配。"""
        if not keyword or not keyword.strip():
            raise ValueError("keyword 不能为空")
        kw = keyword.strip().lower()
        results: list[dict] = []
        for p in self._load_params().values():
            blob = " ".join(
                str(p.get(k, "")) for k in ("company", "code", "segment", "role", "note", "source")
            ).lower()
            if kw in blob:
                results.append(
                    {
                        "type": "params",
                        "company": p["company"],
                        "segment": p["segment"],
                        "role": p["role"],
                        "note": p["note"],
                        "source": p["source"],
                        "source_url": p["source_url"],
                    }
                )
        chain = self._load_chain()
        for link in chain.get("links", []):
            blob = json.dumps(link, ensure_ascii=False).lower()
            if kw in blob:
                results.append({"type": "transmission_link", "link": link})
        return results


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if value == "" or value.upper() == "NA":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _to_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")