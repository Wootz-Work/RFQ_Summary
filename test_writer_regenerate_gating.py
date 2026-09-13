"""
test_writer_regenerate_gating.py — write_regenerated_triage() must skip the
Glide write only when it has actually confirmed the response is unchanged
from the previous version, never when the comparison itself is unknown
(first regeneration, kill switch, a failed diff call).

Run:
    python test_writer_regenerate_gating.py
"""
import sys, types
sys.path.insert(0, "src")
# fitz/google/googleapiclient are not installed here; nothing under test
# touches attachments, Sheets logging or Drive, so stub just enough to import.
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

from rfq_summary import writer
from rfq_summary.config import Settings
from rfq_summary.schema import RfqRegenerateTriageInputPayload, RfqRegenerateTriageOutputPayload

S = Settings(
    GLIDE_API_KEY="k", GLIDE_APP_ID="a",
    GLIDE_ZAI_REGENERATE_TABLE="t1",
    GLIDE_COL_ZAI_REGENERATE_RFQ_ID="c1",
    GLIDE_COL_ZAI_REGENERATE_RESPONSE="c2",
    GLIDE_COL_ZAI_REGENERATE_RESPONSE_GENERATED_TIME="c3",
    GLIDE_COL_ZAI_REGENERATE_REQUESTED_TIME="c4",
    GLIDE_COL_ZAI_REGENERATE_INSTRUCTION="c5",
    GLIDE_COL_ZAI_REGENERATE_REQUESTED_BY="c6",
)


def _check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    return bool(cond)


def _out(**kw):
    base = dict(run_id="r1", rfq_id="R1", triage_text="<triage>x</triage>")
    base.update(kw)
    return RfqRegenerateTriageOutputPayload(**base)


def write(out, settings=S):
    calls = {"row": 0, "outputs": 0}
    real_row, real_outputs = writer.glide_add_zai_regenerate_row, writer.glide_update_all_rfq_triage_outputs
    writer.glide_add_zai_regenerate_row = lambda *a, **k: (calls.__setitem__("row", calls["row"] + 1), None)[1]
    writer.glide_update_all_rfq_triage_outputs = lambda *a, **k: calls.__setitem__("outputs", calls["outputs"] + 1)
    try:
        writer.write_regenerated_triage(settings, RfqRegenerateTriageInputPayload(rfq_id="R1"), out)
    finally:
        writer.glide_add_zai_regenerate_row, writer.glide_update_all_rfq_triage_outputs = real_row, real_outputs
    return calls


ok = True

# Confirmed unchanged: compared=True, changed=False -> skip the Glide write.
c = write(_out(changed=False, compared=True))
ok &= _check("confirmed-unchanged skips the regenerate-row write", c["row"] == 0, str(c))
ok &= _check("confirmed-unchanged skips the ALL RFQ live-column write", c["outputs"] == 0, str(c))

# A real change: compared=True, changed=True -> write.
c = write(_out(changed=True, compared=True))
ok &= _check("a real change writes the regenerate row", c["row"] == 1, str(c))
ok &= _check("a real change updates ALL RFQ", c["outputs"] == 1, str(c))

# First-ever regeneration: nothing to compare against -> write, never skip.
c = write(_out(changed=False, compared=False))
ok &= _check("no baseline yet still writes (never silently skipped)", c["row"] == 1, str(c))

# The comparison itself failed/was skipped -> write, never skip.
c = write(_out(changed=False, compared=False))
ok &= _check("an unknown comparison still writes", c["row"] == 1, str(c))

# The writeback kill switch still overrides everything, regardless of compared/changed.
off = Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="a", ENABLE_TRIAGE_WRITEBACK="false")
c = write(_out(changed=True, compared=True), settings=off)
ok &= _check("writeback kill switch still wins over a real change", c["row"] == 0, str(c))

print("\nALL PASSED" if ok else "\nFAILURES ABOVE")
raise SystemExit(0 if ok else 1)
