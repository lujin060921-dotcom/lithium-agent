"""全局配置：路径、模型、可复现参数。

所有可变项通过环境变量覆盖，源码中不写任何密钥。
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
REPORTS_DIR = PROJECT_ROOT / "reports"
PROMPTS_DIR = PROJECT_ROOT / "prompts"
REPORTS_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)


def load_dotenv(path: Path | None = None) -> None:
    """极简 .env 加载（不引入 python-dotenv 依赖，保持零外部依赖）。已存在的环境变量优先。"""
    env_path = path or (PROJECT_ROOT / ".env")
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    """运行时配置。temperature/seed 固定 → 输出尽量可复现。"""

    def __init__(self) -> None:
        load_dotenv()
        self.api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        self.base_url = os.environ.get(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
        ).strip()
        self.model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat").strip()
        self.temperature = float(os.environ.get("LLM_TEMPERATURE", "0"))
        self.seed = int(os.environ.get("LLM_SEED", "42"))
        self.timeout = float(os.environ.get("LLM_TIMEOUT", "120"))
        self.max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "8000"))
        # 无 Key 时自动降级为离线确定性模式（保证封闭环境也能演示）
        self.offline = _flag("AGENT_OFFLINE", default=not self.api_key)

    def describe(self) -> dict:
        return {
            "model": self.model,
            "base_url": self.base_url,
            "temperature": self.temperature,
            "seed": self.seed,
            "offline_mode": self.offline,
            "api_key_configured": bool(self.api_key),
        }


settings = Settings()