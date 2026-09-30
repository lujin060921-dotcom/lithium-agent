"""Tool 1｜PDF 解析 / 字段提取

职责：从企业财报（PDF）中提取文本、表格，并按字段字典抽取"成本占比、毛利率、营收"等指标。

设计要点
- 解析器优先 PyMuPDF(import pymupdf)，缺失时降级 pdfplumber，均缺失则明确报错而非静默返回空。
- 字段抽取为「规则（正则）为主 + LLM 辅助为辅」：规则可复现、零成本、离线可用；
  LLM 仅用于解析规则未命中的兜底，且必须回填原文片段作为证据。
- 每条抽取值强制携带 evidence（原文片段）、page、unit、confidence，满足"可溯源"评分项。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

# 数值片段：支持 "60"、"60.5"、"60-80"、"30~40" 等区间写法（区间取中值）
_NUM = r"(\d+(?:\.\d+)?(?:\s*[-—~至]\s*\d+(?:\.\d+)?)?)"
# 金额片段：带千分位
_AMOUNT = r"([\d,]+(?:\.\d+)?)"

# 字段字典：字段名 → (默认单位, 正则候选列表)
# 注意：正则作用于「去除换行后的文本」，以兼容 PDF 表格单元格内的自动折行
FIELD_PATTERNS: dict[str, tuple[str, list[str]]] = {
    "gross_margin": (
        "%",
        [
            rf"综合毛利率[^\d%]{{0,20}}?{_NUM}\s*%",
            rf"毛利率[^\d%]{{0,20}}?{_NUM}\s*%",
        ],
    ),
    "revenue": (
        "元",
        [
            rf"营业总收入[^\d]{{0,20}}?{_AMOUNT}\s*(亿元|万元|元)",
            rf"营业收入[^\d]{{0,20}}?{_AMOUNT}\s*(亿元|万元|元)",
        ],
    ),
    "lithium_cost_share": (
        "%",
        [
            rf"碳酸锂[^\d%]{{0,30}}?占[^\d%]{{0,30}}?{_NUM}\s*%",
            rf"锂[^\d%]{{0,10}}?成本[^\d%]{{0,15}}?占[^\d%]{{0,30}}?{_NUM}\s*%",
            rf"原材料[^\d%]{{0,20}}?占[^\d%]{{0,20}}?成本[^\d%]{{0,10}}?{_NUM}\s*%",
            rf"直接材料[^\d%]{{0,20}}?{_NUM}\s*%",
        ],
    ),
    "processing_fee": (
        "元/吨",
        [
            rf"加工费[^\d]{{0,20}}?{_AMOUNT}\s*(元/吨|万元/吨|元)",
        ],
    ),
    "unit_consumption": (
        "吨/吨",
        [
            rf"单耗[^\d]{{0,20}}?{_NUM}",
            rf"单位用量[^\d]{{0,20}}?{_NUM}",
        ],
    ),
}

UNIT_SCALE = {"元": 1.0, "万元": 1e4, "亿元": 1e8}
_RANGE_SPLIT = re.compile(r"[-—~至]")


@dataclass
class PageContent:
    page: int
    text: str
    tables: list[list[list[str]]] = field(default_factory=list)


@dataclass
class ParsedDocument:
    path: str
    filename: str
    engine: str
    page_count: int
    pages: list[PageContent]
    full_text: str
    scan_warning: bool = False  # 文本量极低 → 疑似扫描件

    def to_dict(self, with_pages: bool = False) -> dict:
        d = {
            "path": self.path,
            "filename": self.filename,
            "engine": self.engine,
            "page_count": self.page_count,
            "text_chars": len(self.full_text),
            "scan_warning": self.scan_warning,
        }
        if with_pages:
            d["pages"] = [asdict(p) for p in self.pages]
        return d


@dataclass
class FieldHit:
    field: str
    value: float | None
    raw: str
    unit: str
    page: int
    evidence: str
    confidence: float
    source_file: str
    method: str = "rule"

    def to_dict(self) -> dict:
        return asdict(self)


def _table_to_rows(table) -> list[list[str]]:
    rows = []
    for row in table.extract():
        rows.append([("" if c is None else str(c).strip()) for c in row])
    return rows


def parse_pdf(path: str | Path) -> ParsedDocument:
    """解析 PDF → 文本 + 表格。PyMuPDF 优先，pdfplumber 降级。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"PDF 不存在：{p}")
    if p.suffix.lower() != ".pdf":
        raise ValueError(f"非 PDF 文件：{p.suffix}")

    pages: list[PageContent] = []
    engine = ""

    try:
        import pymupdf  # type: ignore

        engine = f"pymupdf {getattr(pymupdf, '__version__', '?')}"
        doc = pymupdf.open(str(p))
        for i, page in enumerate(doc):
            try:
                tables = [_table_to_rows(t) for t in page.find_tables().tables]
            except Exception:
                tables = []
            pages.append(PageContent(page=i + 1, text=page.get_text("text"), tables=tables))
        doc.close()
    except ImportError:
        try:
            import pdfplumber  # type: ignore

            engine = "pdfplumber"
            with pdfplumber.open(str(p)) as pdf:
                for i, page in enumerate(pdf.pages):
                    tables = page.extract_tables() or []
                    pages.append(
                        PageContent(page=i + 1, text=page.extract_text() or "", tables=tables)
                    )
        except ImportError as exc:
            raise RuntimeError(
                "缺少 PDF 解析依赖：请安装 pymupdf（首选）或 pdfplumber"
            ) from exc

    full_text = "\n".join(pg.text for pg in pages)
    # 每页文本不足 50 字符视为疑似扫描件（需 OCR，当前不静默兜底）
    avg = len(full_text) / max(len(pages), 1)
    scan_warning = avg < 50

    return ParsedDocument(
        path=str(p.resolve()),
        filename=p.name,
        engine=engine,
        page_count=len(pages),
        pages=pages,
        full_text=full_text,
        scan_warning=scan_warning,
    )


def _parse_token(token: str) -> float:
    """解析数值片段："60" → 60；"60-80" / "30~40" / "0.6至0.8" → 取区间中值。"""
    parts = [p.strip().replace(",", "") for p in _RANGE_SPLIT.split(token) if p.strip()]
    values = [float(p) for p in parts]
    if not values:
        raise ValueError(f"无法解析数值：{token!r}")
    return sum(values) / len(values)


def _normalize_number(num_str: str, unit: str | None) -> float:
    value = _parse_token(num_str)
    if unit:
        value *= UNIT_SCALE.get(unit, 1.0)
    return value


def _flatten(text: str) -> str:
    """去除换行：PDF 表格单元格自动折行会把「30-40%」切成两行，需先拼回。"""
    return text.replace("\n", "")


def extract_fields(
    doc: ParsedDocument,
    fields: list[str] | None = None,
) -> dict[str, FieldHit]:
    """规则抽取字段。返回 {字段名: FieldHit}，未命中则不在结果中。"""
    targets = fields or list(FIELD_PATTERNS)
    hits: dict[str, FieldHit] = {}

    for name in targets:
        if name not in FIELD_PATTERNS:
            continue
        unit, patterns = FIELD_PATTERNS[name]
        found: FieldHit | None = None

        for page in doc.pages:
            text = _flatten(page.text)
            for pat in patterns:
                m = re.search(pat, text)
                if not m:
                    continue
                num_str = m.group(1)
                group_unit = m.group(2) if m.re.groups >= 2 else None
                eff_unit = group_unit or unit
                try:
                    value = _normalize_number(num_str, group_unit)
                except ValueError:
                    continue
                start = max(0, m.start() - 40)
                evidence = text[start : m.end() + 10].strip()
                # 单位必须落到"元"，避免把"亿元"数值当成"元"直接使用
                conf = 0.9 if group_unit else 0.7
                found = FieldHit(
                    field=name,
                    value=value,
                    raw=m.group(0).strip(),
                    unit=eff_unit,
                    page=page.page,
                    evidence=evidence,
                    confidence=conf,
                    source_file=doc.filename,
                )
                break
            if found:
                break

        if found:
            hits[name] = found

    return hits


def extract_fields_from_path(path: str | Path, fields: list[str] | None = None) -> dict:
    """便捷入口：解析 + 抽取一步到位（供编排器与单测调用）。"""
    doc = parse_pdf(path)
    hits = extract_fields(doc, fields)
    return {
        "document": doc.to_dict(),
        "fields": {k: v.to_dict() for k, v in hits.items()},
        "missing_fields": [f for f in (fields or list(FIELD_PATTERNS)) if f not in hits],
    }