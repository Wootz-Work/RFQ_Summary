from __future__ import annotations

import re
from pathlib import Path
from typing import List

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, HumanMessage

from .config import Settings


def load_prompt_file(path: str) -> str:
    p = Path(path).expanduser().resolve()
    return p.read_text(encoding="utf-8")


# Adaptive thinking is accepted by Opus 4.6+ / Sonnet 4.6+ / Opus 5 / Sonnet 5 and
# rejected with a 400 by everything older, Haiku 4.5 included — it still takes the
# older budget_tokens form. Sending it to a model that cannot take it turns that
# fallback into a guaranteed failure, which is the opposite of what a fallback is for.
_ADAPTIVE_THINKING_MODELS = re.compile(
    r"^claude-(?:fable|mythos)-\d|^claude-opus-(?:5|4-(?:6|7|8))|^claude-sonnet-(?:5|4-6)"
)


# Above this, a request has to stream: the SDK's HTTP timeout can fire before a
# large answer finishes generating. Thinking makes this likelier, since thinking
# and output draw on the same max_tokens budget.
STREAM_ABOVE_TOKENS = 16000


def _supports_adaptive_thinking(model: str) -> bool:
    return bool(_ADAPTIVE_THINKING_MODELS.match((model or "").strip()))


def response_text(content: object) -> str:
    """
    LangChain gives back a plain string only when the reply is a single text
    block. With thinking enabled the reply is a LIST of blocks — the thinking
    block first, the answer after — so calling .strip() on it raises
    AttributeError and takes down an otherwise good response.

    Pull out the text blocks and join them; ignore thinking, tool-use and any
    other block type. Thinking is reasoning, not answer, and must never end up
    in the text we parse as NDJSON.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
            else:
                # Object-style blocks (SDK models) expose .type / .text.
                if getattr(block, "type", None) == "text":
                    text = getattr(block, "text", None)
                    if isinstance(text, str):
                        parts.append(text)
        return "".join(parts).strip()
    return str(content).strip()


def _log_usage(model: str, resp: object, budget: int) -> None:
    """
    Print what the call actually spent. output_tokens covers thinking AND the
    answer, so this line is what tells you whether thinking crowded the answer
    out, rather than having to infer it from how short the JSON looks.
    """
    meta = getattr(resp, "response_metadata", None) or {}
    usage = meta.get("usage") or {}
    out = usage.get("output_tokens")
    stop = meta.get("stop_reason", "?")
    if out is None:
        print(f"[INFO] llm | {model} stop_reason={stop} (no usage reported)")
        return
    pct = int(round(100 * out / budget)) if budget else 0
    flag = "  <<< HIT THE CAP" if stop == "max_tokens" else ""
    print(
        f"[INFO] llm | {model} in={usage.get('input_tokens', '?')} "
        f"out={out}/{budget} ({pct}% of budget, thinking included) stop={stop}{flag}"
    )


def describe_empty_reply(resp: object) -> str:
    """
    Say why an otherwise-successful call produced no text.

    The expensive case: thinking and output share one max_tokens budget, so on a
    hard prompt the model can spend the entire budget reasoning and stop before
    writing any answer. That returns HTTP 200 with thinking blocks and no text
    block, which is indistinguishable from "the model said nothing" unless we
    look. Silently reporting that as an empty output cost a 214-second run.
    """
    content = getattr(resp, "content", None)
    meta = getattr(resp, "response_metadata", None) or {}
    stop = meta.get("stop_reason") or meta.get("finish_reason") or "unknown"

    thinking_blocks = 0
    other_types = []
    if isinstance(content, list):
        for block in content:
            btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            if btype == "thinking":
                thinking_blocks += 1
            elif btype and btype != "text":
                other_types.append(str(btype))

    if thinking_blocks and stop == "max_tokens":
        return (
            f"thinking used the whole max_tokens budget before any answer was written "
            f"({thinking_blocks} thinking block(s), stop_reason=max_tokens) — raise the "
            f"budget or turn thinking off for this call"
        )
    if thinking_blocks:
        return f"reply carried {thinking_blocks} thinking block(s) but no text (stop_reason={stop})"
    if other_types:
        return f"reply carried only {', '.join(sorted(set(other_types)))} blocks (stop_reason={stop})"
    return f"model returned nothing at all (stop_reason={stop})"


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
    thinking: bool | None = None,
) -> str:
    """
    `thinking` overrides ANTHROPIC_ADAPTIVE_THINKING for this one call. Pass
    False for long structured output: thinking shares the max_tokens budget with
    the answer, so on a big extraction it can spend the lot reasoning and return
    nothing. Leave it None to follow the setting.
    """
    if not (settings.anthropic_api_key or "").strip():
        raise RuntimeError("Missing ANTHROPIC_API_KEY")

    models = _models(settings)
    primary = models[0] if models else ""

    # Opus 5, Opus 4.8/4.7 and Sonnet 5 reject `temperature` with a 400, so it is
    # not sent at all. Adaptive thinking is the replacement lever, but only on the
    # models that accept it — see _supports_adaptive_thinking.
    failures: List[str] = []
    last_err: Exception | None = None

    want_thinking = settings.anthropic_adaptive_thinking if thinking is None else bool(thinking)
    budget = 8000 if max_tokens is None else max(1000, int(max_tokens))
    messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]

    def _ask(model: str, with_thinking: bool):
        kwargs: dict = {}
        if with_thinking and _supports_adaptive_thinking(model):
            kwargs["thinking"] = {"type": "adaptive"}
            # Effort is the only lever on how deep adaptive thinking goes
            # (budget_tokens is gone on these models). Left unset it is the API
            # default; dial it down if thinking keeps crowding out the answer.
            effort = (settings.anthropic_effort or "").strip().lower()
            if effort:
                kwargs["output_config"] = {"effort": effort}
        # A large max_tokens on a non-streaming request risks an HTTP timeout
        # long before the model is done. Streaming removes that ceiling, and
        # LangChain still returns one aggregated message from .invoke().
        if budget > STREAM_ABOVE_TOKENS:
            kwargs["streaming"] = True
        llm = ChatAnthropic(
            model=model,
            anthropic_api_key=settings.anthropic_api_key,
            max_tokens=budget,
            **kwargs,
        )
        return llm.invoke(messages)

    for model in models:
        try:
            resp = _ask(model, want_thinking)
            _log_usage(model, resp, budget)
            text = response_text(resp.content)

            # A successful call that yields no text is not a silent zero. If
            # thinking consumed the whole budget, retry once with it off so the
            # budget goes to the answer. Last resort only — thinking earns its
            # keep on this task, so the real fix is a budget that fits both.
            if not text:
                reason = describe_empty_reply(resp)
                print(f"[WARN] llm | {model} returned no text: {reason}")
                if want_thinking and _supports_adaptive_thinking(model):
                    print(f"[WARN] llm | retrying {model} with thinking off")
                    resp = _ask(model, False)
                    text = response_text(resp.content)
                    if not text:
                        raise RuntimeError(
                            f"no text with thinking off either: {describe_empty_reply(resp)}"
                        )
                else:
                    raise RuntimeError(reason)

            # Only now is the answer actually in hand — announcing the fallback
            # before this point claimed success for a call that then threw.
            if model != primary:
                print(f"[WARN] llm | {primary} failed, answered by fallback {model}. Earlier: {'; '.join(failures)}")
            return text
        except Exception as e:
            last_err = e
            detail = f"{model}: {type(e).__name__}: {e}"
            failures.append(detail)
            print(f"[WARN] llm | model {detail}")

    # Every model's error, not just the last. A retired model at the end of the
    # chain always 404s, and reporting only that hides why the primary failed.
    missing = [f for f in failures if "not_found_error" in f or "404" in f]
    hint = ""
    if missing:
        names = ", ".join(f.split(":", 1)[0] for f in missing)
        hint = (
            f" NOTE: {names} returned not_found — the model id does not exist or is retired. "
            f"Fix ANTHROPIC_MODEL / ANTHROPIC_MODEL_FALLBACKS rather than reading this as an outage."
        )
    raise RuntimeError(
        f"All {len(models)} Claude models failed. Each failure: {' | '.join(failures)}.{hint}"
    ) from last_err
