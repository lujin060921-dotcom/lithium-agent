"""运行日志：JSONL 逐行落盘 + 控制台 logging。

竞赛评分项「执行过程可追溯、运行结果可复现」的落地载体：
每次运行一个 run_id，每步一条 JSON 记录（时间戳 / 步骤 / 动作 / 工具 / 入参 / 结果摘要 / 耗时）。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CN_TZ = timezone(timedelta(hours=8))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
_console = logging.getLogger("lithium-agent")


def _now_iso() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def _summarize(value: Any, limit: int = 600) -> Any:
    """把任意工具返回值压缩成可落盘的摘要，避免日志文件爆炸。"""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)[:limit]
    if len(text) <= limit:
        return value if isinstance(value, (dict, list, str, int, float, bool)) else text
    return text[:limit] + f"...(截断，共 {len(text)} 字符)"


class RunLogger:
    def __init__(self, log_dir: Path, run_id: str | None = None, quiet: bool = False):
        self.run_id = run_id or datetime.now(CN_TZ).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.log_dir / f"run_{self.run_id}.jsonl"
        self.quiet = quiet
        self._seq = 0
        self._t0 = time.perf_counter()
        self.event("run", "启动", note="RunLogger 初始化完成")

    def event(self, step: str, action: str, **payload: Any) -> dict:
        self._seq += 1
        record = {
            "seq": self._seq,
            "ts": _now_iso(),
            "run_id": self.run_id,
            "step": step,
            "action": action,
            "elapsed_s": round(time.perf_counter() - self._t0, 3),
        }
        if payload:
            record["payload"] = {k: _summarize(v) for k, v in payload.items()}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        if not self.quiet:
            extra = f" {record['payload']}" if payload else ""
            _console.info("[%s] %s/%s%s", self.run_id, step, action, extra[:300])
        return record

    def tool_call(
        self,
        tool: str,
        params: dict,
        status: str,
        result: Any = None,
        error: str | None = None,
        elapsed_ms: float | None = None,
    ) -> dict:
        return self.event(
            "tool",
            tool,
            params=params,
            status=status,
            elapsed_ms=round(elapsed_ms, 1) if elapsed_ms is not None else None,
            result=result if status == "ok" else None,
            error=error,
        )