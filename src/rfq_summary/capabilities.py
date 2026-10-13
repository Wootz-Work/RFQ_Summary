"""
capabilities.py — what it takes to make a part, as tags a supplier search can use.

Same and similar parts answer "what did it cost?". Capability answers "who can
make it?": a weld nut supplier for a weld washer, a rubber moulder for any
moulded rubber part. A word in the name is not enough for that — "moulded"
covers HDPE and NBR alike — so each quoted line is tagged once by the model
(prompts/past_quote_capabilities.md) with its process, special features,
coating and material, and the tags are cached by text so a line is never
tagged twice.

Tags are compared loosely: "rubber moulding" matches "rubber compression
moulding", "zinc flake" matches "zinc flake coating".
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set

KINDS = ("process", "feature", "coating", "material")
Tags = Dict[str, List[str]]

_GENERIC = {"fastener", "fasteners", "steel", "metal", "standard part", "hardware", "part", "component",
            "carbon steel", "mild steel", "plain", "none", "unknown", "generic"}
_FILLER = {"and", "of", "the", "with", "for", "a", "to", "-", "&"}


def text_key(text: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", " ", (text or "").strip().lower()).encode()).hexdigest()[:20]


def _stem(word: str) -> str:
    for suffix in ("ings", "ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def tag_tokens(tag: str) -> FrozenSet[str]:
    words = re.findall(r"[a-z0-9]+", (tag or "").lower().replace("moulding", "mould").replace("molding", "mould")
                       .replace("molded", "mould").replace("moulded", "mould"))
    return frozenset(_stem(w) for w in words if w not in _FILLER)


def tags_match(a: str, b: str) -> bool:
    """Equal, or one is the other with words added ('zinc flake' ⊂ 'zinc flake coating')."""
    ta, tb = tag_tokens(a), tag_tokens(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return len(small) >= 2 and small <= big


def clean_tags(raw: Any) -> Tags:
    out: Tags = {k: [] for k in KINDS}
    if not isinstance(raw, dict):
        return out
    for kind in KINDS:
        vals = raw.get(kind) or []
        if isinstance(vals, str):
            vals = [vals]
        for v in vals[:3]:
            t = re.sub(r"\s+", " ", str(v or "")).strip().lower()[:40]
            if t and t not in _GENERIC and t not in out[kind]:
                out[kind].append(t)
    return out


def all_tags(tags: Optional[Tags]) -> Set[str]:
    return {t for kind in KINDS for t in (tags or {}).get(kind, [])}


def parse_reply(reply: str) -> Dict[str, Tags]:
    """The model's JSON object → {id: tags}. Anything unreadable is dropped, never guessed."""
    text = reply or ""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    return {str(k): clean_tags(v) for k, v in data.items()} if isinstance(data, dict) else {}


class TagStore:
    """Tags by text key, kept in memory and (optionally) in a JSON file across restarts."""

    def __init__(self, path: str = ""):
        self.path = path
        self.lock = threading.Lock()
        self.tags: Dict[str, Tags] = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.tags = {k: clean_tags(v) for k, v in (json.load(f) or {}).items()}
            except Exception as e:
                print(f"[WARN] capabilities | tag cache {path} unreadable, starting empty: {type(e).__name__}: {e}")

    def get(self, key: str) -> Optional[Tags]:
        with self.lock:
            return self.tags.get(key)

    def put(self, found: Dict[str, Tags]) -> None:
        with self.lock:
            self.tags.update(found)
        self.save()

    def save(self) -> None:
        if not self.path:
            return
        try:
            with self.lock:
                data = json.dumps(self.tags)
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"[WARN] capabilities | tag cache not saved: {type(e).__name__}: {e}")


_stores: Dict[str, TagStore] = {}
_stores_lock = threading.Lock()


def store_for(settings: Any) -> TagStore:
    path = (getattr(settings, "past_quotes_tag_cache_path", "") or "").strip()
    with _stores_lock:
        if path not in _stores:
            _stores[path] = TagStore(path)
        return _stores[path]


def _ask_model(settings: Any, items: List[Dict[str, str]], run_id: str) -> Dict[str, Tags]:
    from .llm import generate_text, load_prompt_file

    model = (getattr(settings, "past_quotes_tag_model", "") or "").strip()
    s = settings.model_copy(update={"anthropic_model": model, "anthropic_model_fallbacks": ""}) if model else settings
    system = load_prompt_file(getattr(settings, "prompt_past_quote_capabilities_file", "")
                              or "prompts/past_quote_capabilities.md")
    reply = generate_text(s, system, json.dumps(items, ensure_ascii=False), max_tokens=12000,
                          thinking=True, effort="low", run_id=run_id, label="capability_tags")
    return parse_reply(reply)


def tag_texts(settings: Any, texts: Dict[str, str], *, run_id: str = "", batch: int = 50,
              workers: int = 3) -> Dict[str, Tags]:
    """
    {key: text} → {key: tags}, from the cache where possible; the rest are asked
    of the model in batches. A failed batch is logged and left untagged.
    """
    store = store_for(settings)
    out: Dict[str, Tags] = {}
    todo: Dict[str, str] = {}
    for key, text in texts.items():
        hit = store.get(key)
        if hit is not None:
            out[key] = hit
        elif text.strip():
            todo[key] = text
    if not todo:
        return out
    keys = list(todo)
    chunks = [keys[i:i + batch] for i in range(0, len(keys), batch)]

    def one(chunk: List[str]) -> Dict[str, Tags]:
        try:
            found = _ask_model(settings, [{"id": k, "text": todo[k][:400]} for k in chunk], run_id)
            found = {k: v for k, v in found.items() if k in todo}
            store.put(found)
            return found
        except Exception as e:
            print(f"[WARN] capabilities | {len(chunk)} item(s) not tagged: {type(e).__name__}: {e}")
            return {}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for found in pool.map(one, chunks):
            out.update(found)
    return out
