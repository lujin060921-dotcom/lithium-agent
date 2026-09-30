"""LLM 客户端：DeepSeek（OpenAI 兼容协议）直连，零第三方依赖（urllib）。

- 固定 temperature / seed，缓存每次响应（logs/cache/<hash>.json）→ 满足「同输入重跑输出一致」。
- 调用失败不抛出：返回 LLMResponse(status="error")，由编排器降级到「离线确定性报告」。
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from config import Settings


@dataclass
class LLMResponse:
    status: str  # ok / error / cached / offline
    content: str = ""
    error: str | None = None
    model: str = ""
    elapsed_ms: float = 0.0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_key: str = ""
    attempts: int = 0
    finish_reason: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def truncated(self) -> bool:
        """模型因 max_tokens 上限被截断（输出不完整，必须提示）。"""
        return self.finish_reason == "length"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "model": self.model,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_key": self.cache_key,
            "attempts": self.attempts,
            "finish_reason": self.finish_reason,
            "truncated": self.truncated,
            "error": self.error,
        }


class LLMClient:
    def __init__(self, settings: Settings, cache_dir: Path, retries: int = 2):
        self.settings = settings
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.retries = retries

    # ---------- 缓存 ----------

    def _cache_key(self, system: str, user: str) -> str:
        blob = json.dumps(
            {
                "model": self.settings.model,
                "base_url": self.settings.base_url,
                "temperature": self.settings.temperature,
                "seed": self.settings.seed,
                "max_tokens": self.settings.max_tokens,
                "system": system,
                "user": user,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def _cache_read(self, key: str) -> dict | None:
        path = self.cache_dir / f"{key}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _cache_write(self, key: str, content: str, meta: dict) -> None:
        path = self.cache_dir / f"{key}.json"
        path.write_text(
            json.dumps({"content": content, "meta": meta}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ---------- 调用 ----------

    def chat(self, system: str, user: str, use_cache: bool = True) -> LLMResponse:
        if self.settings.offline:
            return LLMResponse(status="offline", error="未配置 DEEPSEEK_API_KEY，走离线确定性模式")

        key = self._cache_key(system, user)
        if use_cache:
            cached = self._cache_read(key)
            if cached:
                return LLMResponse(
                    status="cached",
                    content=cached.get("content", ""),
                    model=cached.get("meta", {}).get("model", self.settings.model),
                    cache_key=key,
                    finish_reason=cached.get("meta", {}).get("finish_reason", ""),
                )

        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.settings.temperature,
            "seed": self.settings.seed,
            "max_tokens": self.settings.max_tokens,
            "stream": False,
        }
        url = self.settings.base_url.rstrip("/") + "/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.settings.api_key}",
                "Accept": "application/json",
            },
            method="POST",
        )

        last_error = ""
        t0 = time.perf_counter()
        for attempt in range(1, self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.settings.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                choice = (data.get("choices") or [{}])[0]
                content = choice.get("message", {}).get("content", "")
                finish_reason = choice.get("finish_reason") or ""
                if not content:
                    last_error = f"响应中无 content 字段：{json.dumps(data, ensure_ascii=False)[:300]}"
                    continue
                usage = data.get("usage") or {}
                result = LLMResponse(
                    status="ok",
                    content=content,
                    model=data.get("model", self.settings.model),
                    elapsed_ms=(time.perf_counter() - t0) * 1000,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    cache_key=key,
                    attempts=attempt,
                    finish_reason=finish_reason,
                )
                self._cache_write(key, content, result.to_dict())
                return result
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8")[:300]
                except Exception:  # noqa: BLE001
                    pass
                last_error = f"HTTP {exc.code} {exc.reason} {detail}"
                if exc.code in (401, 403):  # 鉴权失败不重试
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = f"网络错误：{exc}"
            except json.JSONDecodeError as exc:
                last_error = f"响应非合法 JSON：{exc}"
            if attempt < self.retries:
                time.sleep(1.5 * attempt)

        return LLMResponse(
            status="error",
            error=last_error or "未知错误",
            elapsed_ms=(time.perf_counter() - t0) * 1000,
            cache_key=key,
            attempts=self.retries,
        )

    def check(self) -> LLMResponse:
        """连通性自检：跑通一次 hello（阶段 0 的验收动作）。"""
        return self.chat(
            system="你是一个用于连通性自检的助手，只回复用户要求的内容。",
            user="请只回复两个字：连通",
            use_cache=False,
        )