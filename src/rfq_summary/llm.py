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

    models = _models(settings)
    primary = models[0] if models else ""

    # Opus 5, Opus 4.8/4.7 and Sonnet 5 reject `temperature` with a 400, so it is
    # not sent at all. Adaptive thinking is the recommended replacement lever and
    # is accepted by every model in the fallback chain.
    kwargs: dict = {}
    if settings.anthropic_adaptive_thinking:
        kwargs["thinking"] = {"type": "adaptive"}

    last_err: Exception | None = None
    for model in models:
        try:
            llm = ChatAnthropic(
                model=model,
                anthropic_api_key=settings.anthropic_api_key,
                max_tokens=8000 if max_tokens is None else max(1000, int(max_tokens)),
                **kwargs,
            )
            resp = llm.invoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=user_prompt),
                ]
            )
            # A silent fallback is a quietly worse answer. Say which model replied.
            if model != primary:
                print(f"[WARN] llm | {primary} failed, answered by fallback {model}: {type(last_err).__name__}: {last_err}")
            return (resp.content or "").strip()
        except Exception as e:
            last_err = e
            print(f"[WARN] llm | model {model} failed: {type(e).__name__}: {e}")

    raise RuntimeError(f"All Claude models failed ({', '.join(models)}). Last error: {last_err}")
