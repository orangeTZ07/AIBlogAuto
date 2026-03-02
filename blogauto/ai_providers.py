from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Protocol


class ProviderError(RuntimeError):
    pass


@dataclass
class ProviderSettings:
    provider: str
    api_key: str
    model: str
    base_url: str
    max_retries: int = 1


class ChatProvider(Protocol):
    def chat(self, system_prompt: str, user_prompt: str, temperature: float = 0.5) -> str:
        ...


class OpenAICompatProvider:
    def __init__(self, settings: ProviderSettings) -> None:
        self.settings = settings

    def chat(self, system_prompt: str, user_prompt: str, temperature: float = 0.5) -> str:
        import requests

        url = f"{self.settings.base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }
        resp = self._post(url, headers, payload)
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except Exception as exc:
            raise ProviderError(f"解析 OpenAI 兼容响应失败: {exc}; body={json.dumps(data, ensure_ascii=False)[:400]}")

    def _post(self, url: str, headers: dict[str, str], payload: dict) -> "requests.Response":
        import requests

        last_exc: Exception | None = None
        for _ in range(max(1, self.settings.max_retries + 1)):
            try:
                resp = requests.post(
                    url,
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                return resp
            except requests.Timeout as exc:
                last_exc = exc
            except requests.ConnectionError as exc:
                last_exc = exc
            except requests.HTTPError as exc:
                raise ProviderError(f"AI 请求失败: HTTP {exc.response.status_code if exc.response else '?'}")
        raise ProviderError(f"AI 请求超时/连接失败: {last_exc}")


class AnthropicProvider:
    def __init__(self, settings: ProviderSettings) -> None:
        self.settings = settings

    def chat(self, system_prompt: str, user_prompt: str, temperature: float = 0.5) -> str:
        import requests

        url = f"{self.settings.base_url.rstrip('/')}/v1/messages"
        payload = {
            "model": self.settings.model,
            "max_tokens": 2048,
            "temperature": temperature,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        headers = {
            "x-api-key": self.settings.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        resp = self._post(url, headers, payload)
        data = resp.json()
        try:
            content = data["content"]
            if isinstance(content, list) and content:
                return content[0].get("text", "")
            raise ProviderError("Anthropic 返回内容为空")
        except Exception as exc:
            raise ProviderError(f"解析 Anthropic 响应失败: {exc}; body={json.dumps(data, ensure_ascii=False)[:400]}")

    def _post(self, url: str, headers: dict[str, str], payload: dict) -> "requests.Response":
        import requests

        last_exc: Exception | None = None
        for _ in range(max(1, self.settings.max_retries + 1)):
            try:
                resp = requests.post(
                    url,
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                return resp
            except requests.Timeout as exc:
                last_exc = exc
            except requests.ConnectionError as exc:
                last_exc = exc
            except requests.HTTPError as exc:
                raise ProviderError(f"AI 请求失败: HTTP {exc.response.status_code if exc.response else '?'}")
        raise ProviderError(f"AI 请求超时/连接失败: {last_exc}")


def resolve_provider_settings(config) -> ProviderSettings:
    provider = getattr(config, "ai_provider", "deepseek")

    env_key = {
        "deepseek": "DEEPSEEK_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "custom": "AI_API_KEY",
    }.get(provider, "AI_API_KEY")

    api_key = os.getenv(env_key, "")
    if not api_key:
        api_key = _load_key_from_secret_file(config, provider)

    default_base = {
        "deepseek": "https://api.deepseek.com",
        "openai": "https://api.openai.com/v1",
        "anthropic": "https://api.anthropic.com",
        "custom": "https://api.deepseek.com",
    }.get(provider, "https://api.deepseek.com")

    default_model = {
        "deepseek": "deepseek-chat",
        "openai": "gpt-4o-mini",
        "anthropic": "claude-3-5-sonnet-latest",
        "custom": "",
    }.get(provider, "")

    return ProviderSettings(
        provider=provider,
        api_key=api_key,
        model=getattr(config, "ai_model", "") or default_model,
        base_url=getattr(config, "ai_base_url", "") or default_base,
    )


def _load_key_from_secret_file(config, provider: str) -> str:
    source = getattr(config, "ai_key_source", "env")
    if source != "file":
        return ""
    secret_rel = getattr(config, "ai_secret_file", ".blogauto-secrets.json")
    workspace = getattr(config, "workspace", None)
    if workspace is None:
        return ""
    secret_path = Path(workspace) / secret_rel
    if not secret_path.exists():
        return ""
    try:
        data = json.loads(secret_path.read_text(encoding="utf-8"))
        return data.get("providers", {}).get(provider, {}).get("api_key", "")
    except Exception:
        return ""


def create_provider(config) -> ChatProvider:
    settings = resolve_provider_settings(config)
    if not settings.api_key:
        raise ProviderError("未配置 API Key")

    if settings.provider == "anthropic":
        return AnthropicProvider(settings)
    return OpenAICompatProvider(settings)
