import sys, types
sys.path.insert(0, "src")
# fitz is not installed here; the attachment stack is not under test.
for name in ("fitz", "google", "google.oauth2", "google.oauth2.service_account",
             "google.api_core", "google.api_core.client_options",
             "google.cloud", "google.cloud.documentai_v1",
             "googleapiclient", "googleapiclient.discovery", "googleapiclient.http",
             "googleapiclient.errors"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["google.oauth2.service_account"].Credentials = object
sys.modules["google.api_core.client_options"].ClientOptions = object
sys.modules["google.cloud.documentai_v1"].DocumentProcessorServiceClient = object
sys.modules["googleapiclient.discovery"].build = lambda *a, **k: None
sys.modules["googleapiclient.http"].MediaIoBaseDownload = object
sys.modules["googleapiclient.errors"].HttpError = Exception

from rfq_summary import task
from rfq_summary.config import Settings
from rfq_summary.schema import RfqRegenerateTriageInputPayload as P

S = Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="a")
NEW = "<triage>new summary, materially different</triage>"

def run(payload, gen=None, fetch=None, settings=S):
    real_gen, real_fetch = task._generate_text_with_timing, task.glide_fetch_last_regenerate_response
    if gen: task._generate_text_with_timing = gen
    if fetch: task.glide_fetch_last_regenerate_response = fetch
    try:
        return task._annotate_new_info(settings, "r1", payload, NEW)
    finally:
        task._generate_text_with_timing, task.glide_fetch_last_regenerate_response = real_gen, real_fetch

ok = True
def check(label, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS  " if cond else "FAIL  ") + label + (f"  — {detail}" if detail and not cond else ""))


# ---- the pure, code-computed diff (no LLM involved) -----------------------

def test_diff_input_json():
    d = task._diff_input_json({"material": "SS304", "qty": "500"}, {"material": "SS316L", "qty": "500"})
    check("a changed scalar is reported", any("material" in l and "SS304" in l and "SS316L" in l for l in d), d)
    check("an unchanged scalar is not reported", not any("qty" in l for l in d), d)

    d2 = task._diff_input_json({"a": "x"}, {"a": "x", "b": "y"})
    check("an added key is reported", any(l.startswith("+ added: b") for l in d2), d2)

    d3 = task._diff_input_json({"a": "x", "b": "y"}, {"a": "x"})
    check("a removed key is reported", any(l.startswith("- removed: b") for l in d3), d3)

    d4 = task._diff_input_json({"spec": {"grade": "A"}}, {"spec": {"grade": "B"}})
    check("nested dicts are diffed by path", any("spec.grade" in l for l in d4), d4)

    check("identical input reports nothing", task._diff_input_json({"a": 1}, {"a": 1}) == [])
    check("non-dict input is survivable", task._diff_input_json(None, {"a": 1}) == [])


def test_rfq_title_never_falls_back_to_an_id():
    """The email subject/greeting must show the RFQ's own title, never the
    internal rfq_id — an id shown to a reader who never sees ids elsewhere
    reads as a bug, not an identifier."""
    from rfq_summary.task import _rfq_title

    check("plain lowercase title", _rfq_title({"title": "FRUITLAND 46"}) == "FRUITLAND 46")
    check("capitalised Title key is also read", _rfq_title({"Title": "FRUITLAND 46"}) == "FRUITLAND 46")
    check("a missing title returns empty, not an id", _rfq_title({}) == "")
    check("a blank title returns empty, not an id", _rfq_title({"title": "   "}) == "")


def test_diff_attachment_ids():
    d = task._diff_attachment_ids(["1", "2"], ["2", "3"])
    check("an added attachment is reported", any("added" in l for l in d), d)
    check("a removed attachment is reported", any("removed" in l for l in d), d)
    check("unchanged lists report nothing", task._diff_attachment_ids(["1"], ["1"]) == [])


# ---- the annotate pass ------------------------------------------------------

def test_annotates_a_real_change():
    txt, raw, ms, changed, compared = run(
        P(rfq_id="R1", previous_response="old summary"),
        gen=lambda s, p, **k: ("<annotated>\nnew summary, __was zinc, now zinc flake__\n</annotated>", 120),
    )
    check("marked span is carried through", "__was zinc, now zinc flake__" in txt, txt)
    check("tag is unwrapped", "<annotated>" not in txt)
    check("flagged as changed", changed is True)
    check("flagged as compared", compared is True)


def test_unmodified_copy_means_no_change():
    txt, raw, ms, changed, compared = run(
        P(rfq_id="R1", previous_response="old"),
        gen=lambda s, p, **k: (f"<annotated>\n{NEW}\n</annotated>", 90),
    )
    check("text is returned as-is, no markers", txt == NEW, txt)
    check("not flagged as changed", changed is False)
    check("flagged as compared — this is a confirmed no-change", compared is True)
    check("raw is still kept for the log", bool(raw))


def test_no_baseline_means_no_call():
    called = []
    txt, raw, ms, changed, compared = run(
        P(rfq_id="R1"),
        gen=lambda s, p, **k: (called.append(1), ("x", 1))[1],
        fetch=lambda s, r: "",
    )
    check("plain text is returned unchanged", txt == NEW, txt)
    check("no LLM call is made", not called)
    check("not flagged as changed", changed is False)
    check("not flagged as compared — nothing to compare against yet", compared is False)


def test_fetches_baseline_from_glide():
    txt, raw, ms, changed, compared = run(
        P(rfq_id="R1"),
        gen=lambda s, p, **k: ("<annotated>\n" + NEW + " __Lead time was 7 weeks, now 10__\n</annotated>", 100),
        fetch=lambda s, r: "previous version text",
    )
    check("fetches baseline from Glide and annotates", "__Lead time was 7 weeks, now 10__" in txt, txt)
    check("flagged as changed", changed is True)
    check("flagged as compared", compared is True)


def test_input_diff_reaches_the_prompt():
    captured = {}
    def gen(s, p, **k):
        captured["prompt"] = p
        return ("<annotated>\n" + NEW + "\n</annotated>", 50)
    run(
        P(rfq_id="R1", previous_response="old",
          prev_json={"material": "SS304"}, rfq={"material": "SS316L"}),
        gen=gen,
    )
    check("the computed diff fact reaches the prompt",
          "SS304" in captured["prompt"] and "SS316L" in captured["prompt"], captured.get("prompt", "")[:300])

    captured2 = {}
    def gen2(s, p, **k):
        captured2["prompt"] = p
        return ("<annotated>\n" + NEW + "\n</annotated>", 50)
    run(P(rfq_id="R1", previous_response="old"), gen=gen2)
    check("no prev_json means the prompt says so plainly",
          "none available" in captured2["prompt"], captured2.get("prompt", "")[:300])


def test_failure_modes_leave_the_regeneration_standing():
    """Every failure mode must be uncompared, not just unchanged — a caller
    gating a Glide write on "confirmed no change" must never skip a write
    just because the check itself broke."""
    def boom(*a, **k): raise RuntimeError("model down")
    txt, _, _, changed, compared = run(P(rfq_id="R1", previous_response="old"), gen=boom)
    check("diff call failure returns plain text", txt == NEW)
    check("and is not flagged as changed", changed is False)
    check("and is not flagged as compared", compared is False)

    def boom_fetch(*a, **k): raise RuntimeError("glide down")
    txt, _, _, changed, compared = run(P(rfq_id="R1"), fetch=boom_fetch)
    check("glide failure returns plain text", txt == NEW)
    check("glide failure is not flagged as compared", compared is False)

    missing = Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="a", PROMPT_QUERY_REGENERATE_DIFF_FILE="prompts/nope.md")
    txt, _, _, changed, compared = run(P(rfq_id="R1", previous_response="old"), settings=missing)
    check("missing prompt file returns plain text", txt == NEW)
    check("missing prompt file is not flagged as compared", compared is False)

    off = Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="a", ENABLE_REGENERATE_DIFF="false")
    txt, _, _, changed, compared = run(
        P(rfq_id="R1", previous_response="old"),
        gen=lambda s, p, **k: ("<annotated>__x__</annotated>", 1),
        settings=off,
    )
    check("kill switch returns plain text untouched", txt == NEW)
    check("kill switch is never flagged as changed", changed is False)
    check("kill switch is never flagged as compared", compared is False)


test_diff_input_json()
test_rfq_title_never_falls_back_to_an_id()
test_diff_attachment_ids()
test_annotates_a_real_change()
test_unmodified_copy_means_no_change()
test_no_baseline_means_no_call()
test_fetches_baseline_from_glide()
test_input_diff_reaches_the_prompt()
test_failure_modes_leave_the_regeneration_standing()

print("\nALL PASSED" if ok else "\nFAILURES ABOVE")
raise SystemExit(0 if ok else 1)
