"""Reporting-only tests. No model, Glide, email or callback network calls."""
import asyncio
import ast
import contextlib
import hashlib
import hmac
import json
import io
import sys
import threading
import unittest
import uuid
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "src")
from rfq_summary import strike_progress as progress


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.deliveries = []
        def capture(delivery):
            self.deliveries.append(delivery)
            self.events.append(json.loads(delivery.body))
            return True
        self.sender = patch.object(progress, "_sender", SimpleNamespace(enqueue=capture))
        self.sender.start()
        self.settings = SimpleNamespace(enable_strike_progress=True,
            strike_progress_callback_url="https://strike.example.com/api/integrations/task-events",
            strike_progress_signing_secret="s" * 32, strike_progress_timeout_sec=2, strike_progress_retries=5,
            enable_product_extraction=True, enable_product_writeback=True, enable_query_writeback=True,
            glide_col_query_rfq_id="rfq", glide_col_query_product_id="product", glide_col_product_rfq_id="rfq")
        self.operation = str(uuid.uuid4())
        self.token = self.begin()

    def begin(self, row="rfq-A", run="run-A", **changes):
        meta = {"operationId": self.operation, "rfqRowId": row,
                "taskCallbackUrl": self.settings.strike_progress_callback_url, **changes}
        return progress.begin_run({"_strike": meta}, run, row, self.settings)

    def tearDown(self):
        progress.end_run(self.token)
        self.sender.stop()

    def extraction(self, products=None, queries=None, **changes):
        return SimpleNamespace(summary=object(), products=products if products is not None else [object()],
            queries=queries if queries is not None else [SimpleNamespace(product_refs=[1])],
            parse_errors=changes.get("parse_errors", []), skipped_products=changes.get("skipped_products", []))

    def latest(self, task):
        return next(event for event in reversed(self.events) if event["task"] == task)

    def test_summary_only_completes_with_a_record_id_and_nonempty_summary(self):
        progress.summary_saved(None, "Summary")
        self.assertEqual(self.latest("zai_summary")["status"], "unconfirmed")
        progress.summary_saved("summary-A", "<triage>Useful summary</triage>")
        self.assertEqual(self.latest("zai_summary")["result"], {"recordId": "summary-A"})
        before = len(self.events)
        progress.task("zai_summary", "running")
        self.assertEqual(len(self.events), before)

    def test_empty_summary_is_not_confirmed(self):
        progress.summary_saved("summary-A", "<triage> </triage>")
        self.assertEqual(self.latest("zai_summary")["status"], "unconfirmed")

    def test_product_and_query_completion_use_the_returned_ids_and_required_links(self):
        extraction = self.extraction()
        progress.products_start(extraction)
        progress.products_saved(["product-A"], extraction, self.settings)
        progress.queries_start(extraction.queries, {1: "product-A"}, self.settings)
        response = SimpleNamespace(json=lambda: [{"rowID": "query-A"}])
        self.assertIsNone(progress.query_batch(response, 1))
        progress.queries_saved(1, self.settings)
        self.assertEqual(self.latest("products")["status"], "complete")
        self.assertEqual(self.latest("queries")["result"], {"recordIds": ["query-A"], "linkageConfirmed": True})

    def test_missing_product_ids_do_not_become_success(self):
        extraction = self.extraction()
        progress.products_start(extraction)
        progress.products_saved([None], extraction, self.settings)
        self.assertEqual(self.latest("products")["status"], "unconfirmed")

    def test_product_rows_without_an_rfq_link_are_not_confirmed(self):
        extraction = self.extraction()
        self.settings.glide_col_product_rfq_id = ''
        progress.products_start(extraction)
        progress.products_saved(['product-A'], extraction, self.settings)
        self.assertEqual(self.latest('products')['status'], 'unconfirmed')

    def test_reconciled_count_mismatch_is_not_a_valid_empty_or_full_extraction(self):
        extraction = self.extraction()
        extraction.reconciliation_note = lambda: 'expected two lines but parsed one'
        progress.products_start(extraction)
        progress.products_saved(['product-A'], extraction, self.settings)
        self.assertEqual(self.latest('products')['status'], 'unconfirmed')

    def test_partial_extraction_stays_unconfirmed_even_if_remaining_rows_save(self):
        extraction = self.extraction(parse_errors=["truncated output"])
        progress.products_start(extraction)
        progress.products_saved(["product-A"], extraction, self.settings)
        self.assertEqual(self.latest("products")["status"], "unconfirmed")

    def test_query_count_without_ids_is_not_success(self):
        extraction = self.extraction()
        progress.products_start(extraction)
        progress.queries_start(extraction.queries, {1: "product-A"}, self.settings)
        progress.query_batch(SimpleNamespace(json=lambda: [{"error": "unknown result"}]), 1)
        progress.queries_saved(1, self.settings)
        self.assertEqual(self.latest("queries")["status"], "unconfirmed")

    def test_missing_required_product_links_are_not_confirmed(self):
        extraction = self.extraction()
        progress.products_start(extraction)
        progress.queries_start(extraction.queries, {}, self.settings)
        progress.query_batch(SimpleNamespace(json=lambda: ["query-A"]), 1)
        progress.queries_saved(1, self.settings)
        self.assertEqual(self.latest("queries")["status"], "unconfirmed")

    def test_valid_zero_queries_are_distinct_from_parse_failure(self):
        progress.no_products(self.extraction(products=[], queries=[]), self.settings)
        self.assertEqual(self.latest("products")["reasonCode"], "NO_PRODUCTS_FOUND")
        self.assertEqual(self.latest("queries")["reasonCode"], "NO_QUERIES_NEEDED")
        self.assertEqual(self.latest("queries")["itemsTotal"], 0)

    def test_no_products_on_error_blocks_queries(self):
        progress.no_products(self.extraction(products=[], queries=[], parse_errors=["timeout"]), self.settings)
        self.assertEqual(self.latest("products")["status"], "unconfirmed")
        self.assertEqual(self.latest("queries")["status"], "blocked")

    def test_disabled_query_writeback_is_not_green(self):
        self.settings.enable_query_writeback = False
        extraction = self.extraction(queries=[])
        progress.products_start(extraction)
        progress.products_saved(["product-A"], extraction, self.settings)
        self.assertEqual(self.latest("queries")["status"], "skipped")

    def test_metadata_is_captured_for_this_run_and_cannot_redirect(self):
        token = self.begin("rfq-B", "run-B", taskCallbackUrl="https://other.example.com/api/integrations/task-events")
        before = len(self.events)
        progress.task("products", "running")
        self.assertEqual(before, len(self.events))
        progress.end_run(token)
        progress.task("products", "running")
        self.assertEqual(self.latest("products")["rfqRowId"], "rfq-A")

    def test_legacy_or_disabled_run_does_not_inherit_an_observer(self):
        token = progress.begin_run({}, "legacy", "legacy-rfq", self.settings)
        before = len(self.events)
        progress.summary_saved("legacy-summary", "Legacy")
        self.assertEqual(len(self.events), before)
        progress.end_run(token)
        self.settings.enable_strike_progress = False
        token = self.begin("disabled", "disabled")
        progress.task("products", "running")
        self.assertEqual(len(self.events), before)
        progress.end_run(token)

    def test_overlapping_async_runs_and_to_thread_remain_isolated(self):
        async def run(row):
            token = self.begin(row, "run-" + row)
            try:
                await asyncio.sleep(0)
                await asyncio.to_thread(progress.summary_saved, "summary-" + row, "Summary")
            finally:
                progress.end_run(token)
        async def both():
            await asyncio.gather(run("A"), run("B"))
        asyncio.run(both())
        summaries = [e for e in self.events if e["task"] == "zai_summary"]
        self.assertEqual({(e["rfqRowId"], e["runId"], e["result"]["recordId"]) for e in summaries},
                         {("A", "run-A", "summary-A"), ("B", "run-B", "summary-B")})

    def test_reporting_exceptions_cannot_escape_into_business_code(self):
        with patch.object(progress._sender, "enqueue", side_effect=RuntimeError("private transport detail")), \
             patch.object(progress._log, "warning", side_effect=RuntimeError("log handler")):
            self.assertIsNone(progress.task("products", "running"))
            self.assertIsNone(progress.query_batch(SimpleNamespace(json=lambda: 1 / 0), 1))

    def test_timeout_reporting_keeps_previously_confirmed_summary(self):
        progress.summary_saved("summary-A", "Summary")
        progress.unfinished("PROVIDER_TIMEOUT")
        self.assertEqual(self.latest("zai_summary")["status"], "complete")
        self.assertEqual(self.latest("products")["status"], "unconfirmed")

    def test_sender_signs_exact_bytes_and_does_not_follow_redirects(self):
        progress.task("products", "running")
        delivery = self.deliveries[-1]
        captured = []
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): pass
        opener = SimpleNamespace(open=lambda request, timeout: captured.append(request) or Response())
        with patch.object(progress, "build_opener", return_value=opener), patch.object(progress.time, "time", return_value=1700000000):
            self.assertTrue(progress._send(delivery))
        request = captured[0]
        expected = hmac.new(delivery.secret.encode(), b"1700000000\n" + delivery.body, hashlib.sha256).hexdigest()
        self.assertEqual(request.get_header("X-strike-signature"), expected)
        self.assertEqual(request.data, delivery.body)
        self.assertIsNone(progress._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example.com"))

    def test_notification_delivery_is_background_and_retries_only_the_same_event(self):
        progress.task("products", "running")
        source = self.deliveries[-1]
        delivery = progress.Delivery(source.url, source.secret, source.body, retries=1)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        attempts = []
        def transport(value):
            attempts.append(value.body)
            if len(attempts) == 1:
                entered.set()
                release.wait(2)
                return False
            finished.set()
            return True
        sender = progress.Sender(transport=transport)
        self.assertTrue(sender.enqueue(delivery))
        self.assertTrue(entered.wait(1))
        self.assertFalse(finished.is_set())
        release.set()
        self.assertTrue(finished.wait(3))
        self.assertEqual(attempts, [delivery.body, delivery.body])

    def run_existing_product_writer(self, product_error=False, query_error=False):
        """Execute the real changed writer with only its business dependencies stubbed."""
        tree = ast.parse(Path('src/rfq_summary/writer.py').read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_write_extracted_products')
        module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), function], type_ignores=[]))
        calls = []
        extraction = self.extraction()
        extraction.products = [SimpleNamespace(index=1)]
        extraction.customer_queries = lambda: extraction.queries
        extraction.team_queries = lambda: []
        out = SimpleNamespace(product_extraction=extraction, run_id='run-A')
        self.settings.glide_all_product_table = 'products'
        def add_products(settings, row_id, products):
            self.assertEqual(row_id, 'rfq-A')
            calls.append('products')
            if product_error: raise RuntimeError('ambiguous add response')
            return ['product-A']
        def add_queries(settings, row_id, queries, resolved):
            self.assertEqual(row_id, 'rfq-A')
            self.assertEqual(resolved, {1: 'product-A'})
            calls.append('queries')
            if query_error: raise RuntimeError('ambiguous query response')
            progress.query_batch(SimpleNamespace(json=lambda: ['query-A']), 1)
            return 1
        namespace = {'strike_progress': progress, 'grouping_summary': lambda _: 'one product',
            '_attach_costing_workbook': lambda *_: calls.append('workbook'),
            'glide_add_product_rows': add_products, 'glide_add_query_rows': add_queries}
        exec(compile(module, '<real product writer with isolated dependencies>', 'exec'), namespace)
        with contextlib.redirect_stdout(io.StringIO()):
            result = namespace['_write_extracted_products'](self.settings, 'rfq-A', out)
        return calls, result

    def test_actual_writer_keeps_business_call_order_and_return_value(self):
        calls, result = self.run_existing_product_writer()
        self.assertEqual(calls, ['workbook', 'products', 'queries'])
        self.assertEqual(result, (1, 1))
        self.assertEqual(self.latest('products')['status'], 'complete')
        self.assertEqual(self.latest('queries')['status'], 'complete')

    def test_actual_writer_preserves_caught_query_failure_and_reports_uncertainty(self):
        calls, result = self.run_existing_product_writer(query_error=True)
        self.assertEqual(calls, ['workbook', 'products', 'queries'])
        self.assertEqual(result, (1, 0))
        self.assertEqual(self.latest('products')['status'], 'complete')
        self.assertEqual(self.latest('queries')['status'], 'unconfirmed')

    def test_actual_writer_preserves_caught_product_failure_without_adding_queries(self):
        calls, result = self.run_existing_product_writer(product_error=True)
        self.assertEqual(calls, ['workbook', 'products'])
        self.assertEqual(result, (0, 0))
        self.assertEqual(self.latest('products')['status'], 'unconfirmed')
        self.assertEqual(self.latest('queries')['status'], 'blocked')

    def test_actual_writer_is_unchanged_when_reporting_sender_raises(self):
        with patch.object(progress._sender, 'enqueue', side_effect=RuntimeError('sender unavailable')):
            calls, result = self.run_existing_product_writer()
        self.assertEqual(calls, ['workbook', 'products', 'queries'])
        self.assertEqual(result, (1, 1))


if __name__ == "__main__":
    unittest.main()
