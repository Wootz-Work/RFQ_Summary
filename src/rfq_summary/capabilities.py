"""
capabilities.py — the model's reading of a quoted part, cached.

Nothing here is a hand-written list of part types, styles, materials or
finishes. Each quoted line is read once by the model
(prompts/past_quote_capabilities.md) into a profile:

  kind              the kind of product, without its style (washer, flange)
  part_type         what the part is, including the style that makes it a
                    different product (weld neck vs slip-on flange)
  material          the grade, normalised (316 / 316L / A4 read the same)
  charge            a charge line (freight, tooling …) rather than a product
  process, feature, coating, special_material
                    what it takes to make it — the capabilities a supplier needs

To keep the names consistent without a fixed vocabulary, every request is
shown the names already in use, drawn from the profiles read so far, so the
vocabulary is whatever the quote history itself contains. Profiles are cached
by text, so a line is never read twice. Names are compared loosely: "rubber
moulding" matches "rubber compression moulding".
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set

KINDS = ("process", "feature", "coating", "special_material")      # capability lists
LAST_ERROR: Dict[str, str] = {"message": ""}     # the latest failed read, for the status page
TEXT_LIMIT = 600                     # characters of a part's text the model is shown
Profile = Dict[str, Any]


def text_key(text: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", " ", (text or "").strip().lower()).encode()).hexdigest()[:20]


def _stem(word: str) -> str:
    for suffix in ("ings", "ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def tag_tokens(tag: str) -> FrozenSet[str]:
    """Words and numbers of a name; '8.8', 'b16.5' and 'a4-80' stay whole, so a grade or class is never lost."""
    words = re.findall(r"[a-z0-9]+(?:[.\-]\d+)*", (tag or "").lower())
    return frozenset(_stem(w) if w.isalpha() else w for w in words if len(w) > 1 or w.isdigit())


def relation(a: str, b: str) -> str:
    """
    How two names relate: 'same' (the same words), 'close' (one is the other
    with words added — 'rod end' and 'spherical rod end'), '' when either is
    blank, else 'different'.
    """
    ta, tb = tag_tokens(a), tag_tokens(b)
    if not ta or not tb:
        return ""
    if ta == tb:
        return "same"
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return "close" if len(small) >= 2 and small <= big else "different"


def tags_match(a: str, b: str) -> bool:
    """Equal, or one is the other with words added ('zinc flake' ⊂ 'zinc flake coating')."""
    return relation(a, b) in ("same", "close")


def _name(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip().lower()[:60]


def clean_profile(raw: Any) -> Profile:
    out: Profile = {"kind": "", "part_type": "", "material": "", "standards": [], "part_code": "", "charge": False,
                    **{k: [] for k in KINDS}}
    if not isinstance(raw, dict):
        return out
    out["kind"] = _name(raw.get("kind"))
    out["part_type"] = _name(raw.get("part_type"))
    out["material"] = _name(raw.get("material"))
    std = raw.get("standards") or []
    out["standards"] = [_name(x) for x in ([std] if isinstance(std, str) else std) if _name(x)][:4]
    out["part_code"] = str(raw.get("part_code") or "").strip()[:60]
    out["charge"] = str(raw.get("charge")).strip().lower() in ("true", "1", "yes")
    for kind in KINDS:
        vals = raw.get(kind) or []
        if isinstance(vals, str):
            vals = [vals]
        for v in vals[:3]:
            t = _name(v)[:40]
            if t and t not in out[kind]:
                out[kind].append(t)
    return out


# Kept for callers that only deal in capability lists.
clean_tags = clean_profile


def all_tags(profile: Optional[Profile]) -> Set[str]:
    """The capability names of a profile (not its part type or grade)."""
    return {t for kind in KINDS for t in (profile or {}).get(kind, []) or []}


SPECIAL_WEIGHT = {"rare": 3, "specialist": 2}


def special_tags(profile: Optional[Profile], level: Any) -> Dict[str, int]:
    """
    What decides who can make a part: its feature, process and special-material
    names that are specialist or rare among suppliers, with a weight (rare 3,
    specialist 2). Coatings never count — any maker can have a part plated or
    coated outside, so a coating ranks only as a finish.
    """
    out: Dict[str, int] = {}
    for kind in ("feature", "process", "special_material"):
        for t in (profile or {}).get(kind) or []:
            w = SPECIAL_WEIGHT.get(level(t) if level else "")
            if w:
                out[t] = max(out.get(t, 0), w)
    return out


def parse_reply(reply: str) -> Dict[str, Profile]:
    """The model's JSON object → {id: profile}. Anything unreadable is dropped, never guessed."""
    text = reply or ""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    return {str(k): clean_profile(v) for k, v in data.items()} if isinstance(data, dict) else {}


# Bumped whenever the profile's fields change: a cache written in an older
# format is read again from scratch rather than half-understood.
PROFILE_FORMAT = 3


class TagStore:
    """Profiles by text key, in memory and (optionally) in a JSON file across restarts."""

    def __init__(self, path: str = ""):
        self.path = path
        self.lock = threading.Lock()
        self.save_lock = threading.Lock()     # batches read in parallel: one write to the file at a time
        self.tags: Dict[str, Profile] = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f) or {}
                if data.get("format") == PROFILE_FORMAT:
                    self.tags = {k: clean_profile(v) for k, v in (data.get("profiles") or {}).items()}
                else:
                    print(f"[INFO] capabilities | profile cache {path} is in an older format — parts will be read again")
            except Exception as e:
                print(f"[WARN] capabilities | profile cache {path} unreadable, starting empty: {type(e).__name__}: {e}")

    def get(self, key: str) -> Optional[Profile]:
        with self.lock:
            return self.tags.get(key)

    def put(self, found: Dict[str, Profile]) -> None:
        with self.lock:
            self.tags.update(found)
        self.save()

    def vocabulary(self, per_field: int = 80) -> Dict[str, List[str]]:
        """The names in use so far, most used first — what new readings should reuse."""
        with self.lock:
            profiles = list(self.tags.values())
        counts: Dict[str, Counter] = {f: Counter() for f in ("kind", "part_type", "material", *KINDS)}
        for p in profiles:
            if p.get("charge"):
                continue
            for f in ("kind", "part_type", "material"):
                if p.get(f):
                    counts[f][p[f]] += 1
            for kind in KINDS:
                counts[kind].update(p.get(kind) or [])
        return {f: [n for n, _ in c.most_common(per_field)] for f, c in counts.items() if c}

    def save(self) -> None:
        if not self.path:
            return
        try:
            with self.save_lock:
                with self.lock:
                    data = json.dumps({"format": PROFILE_FORMAT, "profiles": self.tags})
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(tmp, self.path)
        except Exception as e:
            print(f"[WARN] capabilities | profile cache not saved: {type(e).__name__}: {e}")


_stores: Dict[str, TagStore] = {}
_stores_lock = threading.Lock()


def store_for(settings: Any) -> TagStore:
    path = (getattr(settings, "past_quotes_tag_cache_path", "") or "").strip()
    with _stores_lock:
        if path not in _stores:
            _stores[path] = TagStore(path)
        return _stores[path]


def _tag_settings(settings: Any) -> Any:
    model = (getattr(settings, "past_quotes_tag_model", "") or "").strip()
    return settings.model_copy(update={"anthropic_model": model, "anthropic_model_fallbacks": ""}) if model else settings


def _ask_model(settings: Any, items: List[Dict[str, str]], run_id: str,
               vocabulary: Optional[Dict[str, List[str]]] = None) -> Dict[str, Profile]:
    from .llm import generate_text, load_prompt_file

    template = load_prompt_file(getattr(settings, "prompt_past_quote_capabilities_file", "")
                                or "prompts/past_quote_capabilities.md")
    vocab = json.dumps(vocabulary or {}, ensure_ascii=False) if vocabulary else "(none yet — you are naming the first parts)"
    reply = generate_text(_tag_settings(settings), template.replace("{{vocabulary}}", vocab),
                          json.dumps(items, ensure_ascii=False), max_tokens=16000,
                          thinking=True, effort="low", run_id=run_id, label="part_profiles")
    return parse_reply(reply)


def tag_texts(settings: Any, texts: Dict[str, str], *, run_id: str = "", batch: int = 40,
              workers: int = 3) -> Dict[str, Profile]:
    """
    {key: text} → {key: profile}, from the cache where possible; the rest are read
    by the model in batches. The first batch runs alone so later ones are shown
    the names it settled on. A failed batch is logged and left unread.
    """
    store = store_for(settings)
    out: Dict[str, Profile] = {}
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
    done = {"n": 0, "failed": 0}
    count_lock = threading.Lock()

    def one(chunk: List[str]) -> Dict[str, Profile]:
        try:
            found = _ask_model(settings, [{"id": k, "text": todo[k][:TEXT_LIMIT]} for k in chunk], run_id,
                               store.vocabulary())
            found = {k: v for k, v in found.items() if k in todo}
            store.put(found)
        except Exception as e:
            print(f"[WARN] capabilities | {len(chunk)} item(s) not read: {type(e).__name__}: {e}")
            LAST_ERROR["message"] = f"{type(e).__name__}: {str(e)[:300]}"
            found = {}
        with count_lock:
            done["n"] += len(found)
            done["failed"] += len(chunk) - len(found)
            if len(chunks) > 1:
                print(f"[INFO] capabilities | run_id={run_id} | read {done['n']} of {len(todo)}"
                      + (f" ({done['failed']} not read)" if done["failed"] else ""))
        return found

    out.update(one(chunks[0]))
    if len(chunks) > 1:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for found in pool.map(one, chunks[1:]):
                out.update(found)
    return out


# ----------------------------------------------------------------------------- scarcity
#
# Whether a shared capability is a lead depends on how hard it is to find among
# suppliers — not on how often it appears in our own quotes. Each capability
# name is rated once by the model and cached beside the profiles.

LEVELS = ("common", "specialist", "rare")


def _scarcity_path(settings: Any) -> str:
    path = (getattr(settings, "past_quotes_tag_cache_path", "") or "").strip()
    if not path:
        return ""
    root, ext = os.path.splitext(path)
    return f"{root}_scarcity{ext or '.json'}"


class LevelStore:
    def __init__(self, path: str = ""):
        self.path = path
        self.lock = threading.Lock()
        self.levels: Dict[str, str] = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.levels = {str(k): str(v) for k, v in (json.load(f) or {}).items() if v in LEVELS}
            except Exception as e:
                print(f"[WARN] capabilities | scarcity cache {path} unreadable, starting empty: {type(e).__name__}: {e}")

    def level(self, tag: str) -> str:
        """The name's level, or that of a rated name it matches; '' when unknown."""
        with self.lock:
            if tag in self.levels:
                return self.levels[tag]
            for t, lv in self.levels.items():
                if tags_match(tag, t):
                    return lv
        return ""

    def put(self, found: Dict[str, str]) -> None:
        with self.lock:
            self.levels.update({k: v for k, v in found.items() if v in LEVELS})
            data = json.dumps(self.levels)
            if not self.path:
                return
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(self.path + ".tmp", "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(self.path + ".tmp", self.path)
            except Exception as e:
                print(f"[WARN] capabilities | scarcity cache not saved: {type(e).__name__}: {e}")


_level_stores: Dict[str, LevelStore] = {}


def levels_for(settings: Any) -> LevelStore:
    path = _scarcity_path(settings)
    with _stores_lock:
        if path not in _level_stores:
            _level_stores[path] = LevelStore(path)
        return _level_stores[path]


def _ask_levels(settings: Any, tags: List[str], run_id: str) -> Dict[str, str]:
    from .llm import generate_text, load_prompt_file

    system = load_prompt_file(getattr(settings, "prompt_capability_scarcity_file", "")
                              or "prompts/capability_scarcity.md")
    reply = generate_text(_tag_settings(settings), system, json.dumps(tags, ensure_ascii=False), max_tokens=8000,
                          thinking=True, effort="low", run_id=run_id, label="capability_scarcity")
    start, end = (reply or "").find("{"), (reply or "").rfind("}")
    try:
        data = json.loads(reply[start:end + 1]) if 0 <= start < end else {}
    except ValueError:
        data = {}
    return {str(k).strip().lower(): str(v).strip().lower() for k, v in (data or {}).items()
            if str(v).strip().lower() in LEVELS}


def rate_tags(settings: Any, tags: Iterable[str], *, run_id: str = "", batch: int = 150) -> None:
    """Rate every capability name not yet rated (or matched by a rated one)."""
    store = levels_for(settings)
    todo = sorted({t for t in tags if t and not store.level(t)})
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        try:
            store.put(_ask_levels(settings, chunk, run_id))
        except Exception as e:
            print(f"[WARN] capabilities | {len(chunk)} capability name(s) not rated: {type(e).__name__}: {e}")
