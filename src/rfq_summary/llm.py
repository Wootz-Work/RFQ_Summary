from __future__ import annotations

from pathlib import Path
from typing import List

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, HumanMessage

from .config import Settings


def load_prompt_file(path: str) -> str:
    p = Path(path).expanduser().resolve()
    return p.read_text(encoding="utf-8")


def _models(settings: Settings) -> List[str]:
    primary = (settings.anthropic_model or "").strip()
    fallbacks = [m.strip() for m in (settings.anthropic_model_fallbacks or "").split(",") if m.strip()]
    out = []
    if primary:
        out.append(primary)
    out.extend([m for m in fallbacks if m and m not in out])
    return out


def generate_text(
    settings: Settings,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int | None = None,
) -> str:
    if not (settings.anthropic_api_key or "").strip():
        raise RuntimeError("Missing ANTHROPIC_API_KEY")

    last_err: Exception | None = None
    for model in _models(settings):
        try:
            llm = ChatAnthropic(
                model=model,
                anthropic_api_key=settings.anthropic_api_key,
                temperature=0.2,
                max_tokens=8000 if max_tokens is None else max(1000, int(max_tokens)),
            )
            resp = llm.invoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=user_prompt),
                ]
            )
            return (resp.content or "").strip()
        except Exception as e:
            last_err = e

    raise RuntimeError(f"All Claude models failed. Last error: {last_err}")
