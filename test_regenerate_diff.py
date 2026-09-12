import sys, types
sys.path.insert(0, "src")
# fitz is not installed here; the attachment stack is not under test.
# Stub the attachment stack's optional deps; none of it is under test here.
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
        return task._describe_what_changed(settings, "r1", payload, NEW)
    finally:
        task._generate_text_with_timing, task.glide_fetch_last_regenerate_response = real_gen, real_fetch

ok = True
def check(label, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS  " if cond else "FAIL  ") + label + (f"  — {detail}" if detail and not cond else ""))

# 1. A real change, baseline supplied in the payload.
txt, raw, ms = run(P(rfq_id="R1", previous_response="old summary"),
                   gen=lambda s, p, **k: ("<changed>\n- **Coating** — was zinc, now zinc flake. Repricing needed.\n</changed>", 120))
check("reports a real change", "Coating" in txt, txt)
check("tag is unwrapped", "<changed>" not in txt)

# 2. Empty tag = nothing material. The normal case.
txt, raw, ms = run(P(rfq_id="R1", previous_response="old"), gen=lambda s, p, **k: ("<changed>\n</changed>", 90))
check("empty tag means silence", txt == "", repr(txt))
check("raw is still kept for the log", bool(raw))

# 3. A model that answers in prose instead of the empty form.
for prose in ["<changed>None.</changed>", "<changed>No material changes.</changed>", "<changed>Nothing changed</changed>"]:
    txt, _, _ = run(P(rfq_id="R1", previous_response="old"), gen=lambda s, p, _t=prose, **k: (_t, 80))
    check(f"prose non-change treated as silence: {prose[:28]}", txt == "", repr(txt))

# 4. No baseline anywhere -> silent, no call attempted.
called = []
txt, _, _ = run(P(rfq_id="R1"), gen=lambda s, p, **k: (called.append(1), ("x", 1))[1], fetch=lambda s, r: "")
check("no baseline means no diff", txt == "")
check("and no LLM call is made", not called)

# 5. Falls back to fetching from Glide when the payload has none.
txt, _, _ = run(P(rfq_id="R1"), gen=lambda s, p, **k: ("<changed>\n- **Lead time** — was 7 weeks, now 10. Date at risk.\n</changed>", 100),
                fetch=lambda s, r: "previous version text")
check("fetches baseline from Glide", "Lead time" in txt, txt)

# 6. Every failure mode leaves the regeneration standing.
def boom(*a, **k): raise RuntimeError("model down")
txt, _, _ = run(P(rfq_id="R1", previous_response="old"), gen=boom)
check("diff call failure is survivable", txt == "")

def boom_fetch(*a, **k): raise RuntimeError("glide down")
txt, _, _ = run(P(rfq_id="R1"), fetch=boom_fetch)
check("glide failure is survivable", txt == "")

missing = Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="a", PROMPT_QUERY_REGENERATE_DIFF_FILE="prompts/nope.md")
txt, _, _ = run(P(rfq_id="R1", previous_response="old"), settings=missing)
check("missing prompt file is survivable", txt == "")

off = Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="a", ENABLE_REGENERATE_DIFF="false")
txt, _, _ = run(P(rfq_id="R1", previous_response="old"), gen=lambda s, p, **k: ("<changed>- x</changed>", 1), settings=off)
check("kill switch works", txt == "")

print("\nALL PASSED" if ok else "\nFAILURES ABOVE")
