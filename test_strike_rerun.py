"""A Strike retry attempt must never add rows on top of ones an earlier attempt saved. No model, Glide or network calls."""
import json
import sys
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, "src")
# Heavy attachment/Drive/Sheets libraries are not needed to test the write guards; stub them so CI stays light.
import types
for name in ("fitz", "google", "google.oauth2", "google.oauth2.service_account", "google.api_core", "google.api_core.client_options",
             "google.cloud", "google.cloud.documentai_v1", "googleapiclient", "googleapiclient.discovery", "googleapiclient.http",
             "googleapiclient.errors"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["google.oauth2.service_account"].Credentials = object
sys.modules["google.api_core.client_options"].ClientOptions = object
sys.modules["google.cloud.documentai_v1"].DocumentProcessorServiceClient = object
sys.modules["googleapiclient.discovery"].build = lambda *a, **k: None
sys.modules["googleapiclient.http"].MediaIoBaseDownload = object
sys.modules["googleapiclient.errors"].HttpError = Exception
from rfq_summary import glide_client, strike_progress as progress, writer
from rfq_summary.schema import QueryPayload, TriageOutputPayload

URL = "https://strike.example.com/api/integrations/task-events"


def settings(**changes):
    base = dict(
        enable_strike_progress=True, strike_progress_callback_url=URL, strike_progress_signing_secret="s" * 32,
        strike_progress_timeout_sec=2, strike_progress_retries=5,
        enable_triage_writeback=True, glide_zai_regenerate_table="zai", glide_col_zai_regenerate_rfq_id="zrfq",
        glide_col_zai_regenerate_response="zresp", glide_col_zai_regenerate_response_generated_time="zgen",
        glide_col_zai_regenerate_requested_time="zreq", glide_col_zai_regenerate_requested_by="zby",
        glide_col_zai_regenerate_type="ztype", glide_col_zai_regenerate_version="zver",
        enable_product_extraction=True, enable_product_writeback=True, glide_all_product_table="products",
        glide_col_product_rfq_id="prfq", enable_query_writeback=True, glide_queries_table="queries",
        glide_col_query_rfq_id="qrfq", glide_col_query_product_id="qprod", glide_api_key="k", glide_app_id="a",
    )
    base.update(changes)
    return SimpleNamespace(**base)


class Extraction:
    def __init__(self, products=2, queries=1):
        self.summary = object(); self.parse_errors = []; self.skipped_products = []
        self.products = [SimpleNamespace(index=i + 1) for i in range(products)]
        self.queries = [SimpleNamespace(product_refs=[1]) for _ in range(queries)]
    def reconciliation_note(self): return ""
    def customer_queries(self): return self.queries
    def team_queries(self): return []


class RerunTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.sender = patch.object(progress, "_sender", SimpleNamespace(
            enqueue=lambda delivery, *a, **k: self.events.append(json.loads(delivery.body)) or True))
        self.sender.start()
        self.s = settings()
        self.token = None
        patches = {
            "glide_find_rows": MagicMock(return_value=[]), "glide_add_zai_regenerate_row": MagicMock(return_value="summary-new"),
            "glide_set_regenerate_response": MagicMock(return_value=True), "glide_update_all_rfq_triage_outputs": MagicMock(),
            "glide_add_product_rows": MagicMock(return_value=["p1", "p2"]), "glide_add_query_rows": MagicMock(return_value=1),
            "_attach_costing_workbook": MagicMock(return_value=True), "grouping_summary": MagicMock(return_value="2 lines"),
            "build_chunked_log_rows": MagicMock(return_value=[]), "append_rows": MagicMock(),
        }
        self.m = {}
        for name, mock in patches.items():
            p = patch.object(writer, name, mock); p.start(); self.addCleanup(p.stop); self.m[name] = mock

    def tearDown(self):
        if self.token is not None:
            progress.end_run(self.token)
        self.sender.stop()

    def begin(self, **meta):
        self.token = progress.begin_run({"_strike": {"operationId": str(uuid.uuid4()), "rfqRowId": "R1", "taskCallbackUrl": URL, **meta}}, "run-1", "R1", self.s)

    def latest(self, task):
        return next((e for e in reversed(self.events) if e["task"] == task), None)

    def triage(self):
        writer.write_triage(self.s, QueryPayload(row_id="R1"), TriageOutputPayload(run_id="run-1", row_id="R1", triage_text="<triage>Useful</triage>"))

    def out(self, products=2, queries=1):
        return TriageOutputPayload(run_id="run-1", row_id="R1", triage_text="x", product_extraction=None), Extraction(products, queries)

    def products(self, products=2, queries=1):
        out, extraction = self.out(products, queries)
        out.product_extraction = extraction
        return writer._write_extracted_products(self.s, "R1", SimpleNamespace(run_id="run-1", product_extraction=extraction, structured={}), "title")

    # ---- first attempts and legacy calls are unchanged
    def test_first_attempt_and_legacy_calls_never_look_for_saved_rows(self):
        for meta in ({}, {"attempt": 1}):
            self.m["glide_find_rows"].reset_mock(); self.m["glide_add_zai_regenerate_row"].reset_mock()
            self.begin(**meta); self.triage(); self.products()
            self.m["glide_find_rows"].assert_not_called()
            self.m["glide_add_zai_regenerate_row"].assert_called_once()
            self.assertEqual(self.m["glide_add_product_rows"].call_count >= 1, True)
            progress.end_run(self.token); self.token = None
        self.assertEqual(self.latest("zai_summary")["status"], "complete")

    def test_without_strike_metadata_nothing_changes(self):
        self.triage(); self.m["glide_find_rows"].assert_not_called(); self.m["glide_add_zai_regenerate_row"].assert_called_once()

    # ---- summary
    def test_retry_reuses_the_saved_summary_and_adds_no_second_row(self):
        self.m["glide_find_rows"].return_value = [{"Row ID": "summary-old", "zresp": "Earlier summary"}]
        self.begin(attempt=2); self.triage()
        self.m["glide_add_zai_regenerate_row"].assert_not_called(); self.m["glide_set_regenerate_response"].assert_not_called()
        self.assertEqual(self.m["glide_find_rows"].call_args.args[2], {"zrfq": "R1", "zver": "0"})
        event = self.latest("zai_summary"); self.assertEqual(event["status"], "complete"); self.assertEqual(event["result"], {"recordId": "summary-old"})

    def test_retry_fills_a_saved_summary_row_whose_text_never_arrived(self):
        self.m["glide_find_rows"].return_value = [{"Row ID": "summary-old", "zresp": ""}]
        self.begin(attempt=2); self.triage()
        self.m["glide_add_zai_regenerate_row"].assert_not_called()
        self.m["glide_set_regenerate_response"].assert_called_once_with(self.s, "summary-old", "<triage>Useful</triage>")
        self.assertEqual(self.latest("zai_summary")["status"], "complete")

    def test_retry_adds_the_summary_only_when_none_was_saved(self):
        self.begin(attempt=2); self.triage()
        self.m["glide_add_zai_regenerate_row"].assert_called_once()
        self.assertEqual(self.latest("zai_summary")["result"], {"recordId": "summary-new"})

    def test_retry_writes_nothing_when_the_summary_check_fails(self):
        self.m["glide_find_rows"].side_effect = RuntimeError("Glide down")
        self.begin(attempt=2); self.triage()
        self.m["glide_add_zai_regenerate_row"].assert_not_called()
        event = self.latest("zai_summary"); self.assertEqual((event["status"], event["reasonCode"]), ("unconfirmed", "SUMMARY_CHECK_FAILED"))

    # ---- products and queries
    def test_retry_does_not_add_products_when_rows_already_exist(self):
        self.m["glide_find_rows"].return_value = [{"Row ID": "p-old"}]
        self.begin(attempt=2); self.assertEqual(self.products(), (0, 0))
        self.m["glide_add_product_rows"].assert_not_called(); self.m["glide_add_query_rows"].assert_not_called(); self.m["_attach_costing_workbook"].assert_not_called()
        self.assertEqual((self.latest("products")["status"], self.latest("products")["reasonCode"]), ("unconfirmed", "PRODUCT_ROWS_EXIST"))
        self.assertEqual(self.latest("queries")["status"], "blocked")

    def test_retry_does_not_guess_when_the_product_check_fails(self):
        self.m["glide_find_rows"].side_effect = RuntimeError("Glide down")
        self.begin(attempt=2); self.assertEqual(self.products(), (0, 0))
        self.m["glide_add_product_rows"].assert_not_called()
        self.assertEqual(self.latest("products")["reasonCode"], "PRODUCT_CHECK_FAILED")

    def test_retry_with_no_saved_rows_writes_products_then_queries(self):
        self.begin(attempt=2); self.assertEqual(self.products(), (2, 1))
        self.m["glide_add_product_rows"].assert_called_once(); self.m["glide_add_query_rows"].assert_called_once()
        tables = [call.args[1] for call in self.m["glide_find_rows"].call_args_list]
        self.assertEqual(tables, ["products", "queries"])

    def test_retry_does_not_add_queries_when_query_rows_already_exist(self):
        self.m["glide_find_rows"].side_effect = [[], [{"Row ID": "q-old"}]]
        self.begin(attempt=2); self.assertEqual(self.products(), (2, 0))
        self.m["glide_add_query_rows"].assert_not_called()
        self.assertEqual((self.latest("queries")["status"], self.latest("queries")["reasonCode"]), ("unconfirmed", "QUERY_ROWS_EXIST"))

    def test_an_unreadable_attempt_value_is_treated_as_a_retry(self):
        self.m["glide_find_rows"].return_value = [{"Row ID": "p-old"}]
        for bad in ("two", True, 0, None, 99):
            self.m["glide_add_product_rows"].reset_mock()
            self.begin(attempt=bad); self.assertEqual(self.products(), (0, 0)); self.m["glide_add_product_rows"].assert_not_called()
            progress.end_run(self.token); self.token = None


class LookupTests(unittest.TestCase):
    def call(self, response, **kw):
        client = MagicMock(); client.__enter__.return_value = client
        client.post.return_value = SimpleNamespace(raise_for_status=lambda: None, json=lambda: response)
        with patch.object(glide_client.httpx, "Client", return_value=client):
            rows = glide_client.glide_find_rows(settings(), **kw)
        return rows, client.post.call_args

    def test_builds_a_parameterised_read_only_query(self):
        rows, call = self.call([{"rows": [{"Row ID": "x"}]}], table="products", filters={"prfq": "R1", "ver": "0"}, limit=1)
        self.assertEqual(rows, [{"Row ID": "x"}])
        query = call.kwargs["json"]["queries"][0]
        self.assertEqual(query["sql"], 'SELECT * FROM "products" WHERE "prfq" = $1 AND "ver" = $2 LIMIT 1')
        self.assertEqual(query["params"], ["R1", "0"])
        self.assertTrue(query["sql"].startswith("SELECT"))

    def test_refuses_unsafe_or_missing_names_and_malformed_answers(self):
        for kwargs in ({"table": "", "filters": {"a": "1"}}, {"table": "t", "filters": {}}, {"table": 't"; DROP', "filters": {"a": "1"}},
                       {"table": "t", "filters": {'a"b': "1"}}, {"table": "t", "filters": {"": "1"}}):
            with self.assertRaises(RuntimeError):
                glide_client.glide_find_rows(settings(), **kwargs)
        for bad in (None, [], {}, [{"rows": None}], [{"nope": 1}]):
            with self.assertRaises(RuntimeError):
                self.call(bad, table="t", filters={"a": "1"})


if __name__ == "__main__":
    unittest.main()
