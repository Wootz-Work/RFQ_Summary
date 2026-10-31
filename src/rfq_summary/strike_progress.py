"""Optional observation of existing triage outcomes; never controls business work.

No provider work is queued/retried here. Only signed status notifications are sent
by one bounded daemon sender. Undelivered events are intentionally in-memory.
"""
from __future__ import annotations

import contextvars
import hashlib
import heapq
import hmac
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

_log = logging.getLogger(__name__)
TASKS = ("zai_summary", "products", "queries")
_current: contextvars.ContextVar[Optional["Observation"]] = contextvars.ContextVar("strike_progress", default=None)


def _warn(code):
    try:
        _log.warning(code)
    except Exception:
        pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class Delivery:
    url: str
    secret: str = field(repr=False)
    body: bytes = field(repr=False)
    timeout: float = 2.0
    retries: int = 5


def _send(delivery: Delivery) -> bool:
    timestamp = str(int(time.time()))
    signature = hmac.new(delivery.secret.encode(), timestamp.encode() + b"\n" + delivery.body, hashlib.sha256).hexdigest()
    request = Request(delivery.url, data=delivery.body, method="POST", headers={
        "Content-Type": "application/json", "X-Strike-Timestamp": timestamp, "X-Strike-Signature": signature,
    })
    with build_opener(_NoRedirect()).open(request, timeout=delivery.timeout) as response:
        return 200 <= response.status < 300


class Sender:
    def __init__(self, transport=_send, capacity: int = 512):
        self.transport = transport
        self.capacity = capacity
        self.condition = threading.Condition()
        self.pending = []
        self.started = False

    def enqueue(self, delivery: Delivery, attempt: int = 0, delay: float = 0) -> bool:
        with self.condition:
            if len(self.pending) >= self.capacity:
                _warn("STRIKE_PROGRESS_QUEUE_FULL")
                return False
            heapq.heappush(self.pending, (time.monotonic() + delay, uuid.uuid4().hex, attempt, delivery))
            if not self.started:
                self.started = True
                threading.Thread(target=self._run, name="strike-progress", daemon=True).start()
            self.condition.notify()
        return True

    def _run(self):
        while True:
            with self.condition:
                while not self.pending:
                    self.condition.wait()
                delay = self.pending[0][0] - time.monotonic()
                if delay > 0:
                    self.condition.wait(timeout=delay)
                    continue
                _, _, attempt, delivery = heapq.heappop(self.pending)
            try:
                delivered = self.transport(delivery)
            except Exception:
                delivered = False
            if not delivered:
                if attempt < delivery.retries:
                    self.enqueue(delivery, attempt + 1, min(30, 2 ** attempt))
                else:
                    _warn("STRIKE_PROGRESS_DELIVERY_UNCONFIRMED")


_sender = Sender()


@dataclass
class Observation:
    operation_id: str
    rfq_id: str
    run_id: str
    url: str
    secret: str = field(repr=False)
    timeout: float = 2.0
    retries: int = 5
    sequences: dict = field(default_factory=dict)
    states: dict = field(default_factory=dict)
    query_ids: list = field(default_factory=list)
    query_links: bool = False
    extraction_valid: bool = False
    query_expected: int = 0
    lock: Any = field(default_factory=threading.Lock, repr=False)

    def emit(self, task: str, status: str, *, reason: Optional[str] = None,
             completed: Optional[int] = None, total: Optional[int] = None, result: Optional[dict] = None):
        if task not in TASKS:
            return
        with self.lock:
            if self.states.get(task) == "complete":
                return
            sequence = self.sequences.get(task, -1) + 1
            self.sequences[task] = sequence
            self.states[task] = status
            event = {"operationId": self.operation_id, "rfqRowId": self.rfq_id,
                     "runId": self.run_id, "eventId": str(uuid.uuid4()), "task": task,
                     "sequence": sequence, "status": status}
            if reason:
                event["reasonCode"] = reason
            if completed is not None:
                event["itemsCompleted"] = completed
            if total is not None:
                event["itemsTotal"] = total
            if result is not None:
                event["result"] = result
            body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()
            if len(body) > 280000:
                _warn("STRIKE_PROGRESS_EVENT_TOO_LARGE")
                return
        _sender.enqueue(Delivery(self.url, self.secret, body, self.timeout, self.retries))


def begin_run(payload: dict, run_id: str, row_id: str, settings: Any, mode: str = "triage"):
    """Bind immutable reporting identity to this async task/to_thread context."""
    token = _current.set(None)
    try:
        if mode != "triage" or not getattr(settings, "enable_strike_progress", False):
            return token
        meta = payload.get("_strike")
        if not isinstance(meta, dict):
            return token
        url = getattr(settings, "strike_progress_callback_url", "").strip()
        parsed = urlsplit(url)
        secret = getattr(settings, "strike_progress_signing_secret", "")
        if (not url or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path != "/api/integrations/task-events"
                or meta.get("taskCallbackUrl") != url or meta.get("rfqRowId") != row_id
                or not row_id or len(row_id) > 200 or not run_id or len(run_id) > 200 or len(secret) < 32):
            _warn("STRIKE_PROGRESS_METADATA_INVALID")
            return token
        operation_id = str(uuid.UUID(meta["operationId"]))
        observer = Observation(operation_id, row_id, run_id, url, secret,
                               float(getattr(settings, "strike_progress_timeout_sec", 2)),
                               int(getattr(settings, "strike_progress_retries", 5)))
        _current.set(observer)
        return token
    except Exception:
        _warn("STRIKE_PROGRESS_METADATA_INVALID")
        return token


def _safe(fn):
    def call(*args, **kwargs):
        try:
            observer = _current.get()
            if observer is not None:
                return fn(observer, *args, **kwargs)
        except Exception:
            _warn("STRIKE_PROGRESS_OBSERVATION_UNCONFIRMED")
        return None
    return call


@_safe
def task(observer, name, status, reason=None):
    observer.emit(name, status, reason=reason)


@_safe
def summary_saved(observer, record_id, text):
    if isinstance(record_id, str) and record_id.strip() and isinstance(text, str) and text.replace('<triage>','').replace('</triage>','').strip():
        observer.emit("zai_summary", "complete", completed=1, total=1, result={"recordId": record_id.strip()})
    else:
        observer.emit("zai_summary", "unconfirmed", reason="SUMMARY_ID_MISSING")


def _valid(extraction):
    return bool(extraction is not None and getattr(extraction, "summary", None) is not None
                and not extraction.parse_errors and not extraction.skipped_products
                and not getattr(extraction, "reconciliation_note", lambda: "")())


@_safe
def products_start(observer, extraction):
    observer.extraction_valid = _valid(extraction)
    observer.emit("products", "running", completed=0, total=len(extraction.products))


@_safe
def no_products(observer, extraction, settings):
    if not getattr(settings, "enable_product_extraction", True):
        observer.emit("products", "skipped", reason="PRODUCT_EXTRACTION_DISABLED")
    elif not _valid(extraction):
        observer.emit("products", "unconfirmed", reason="EXTRACTION_INCOMPLETE")
    elif not getattr(settings, "enable_product_writeback", True):
        observer.emit("products", "skipped", reason="PRODUCT_WRITEBACK_DISABLED")
    else:
        observer.emit("products", "complete", reason="NO_PRODUCTS_FOUND", completed=0, total=0, result={"recordIds": []})
        if not getattr(settings, "enable_query_writeback", True):
            observer.emit("queries", "skipped", reason="QUERY_WRITEBACK_DISABLED")
            return
        if not extraction.queries:
            observer.emit("queries", "complete", reason="NO_QUERIES_NEEDED", completed=0, total=0, result={"recordIds": []})
            return
    observer.emit("queries", "blocked", reason="PRODUCTS_NOT_CONFIRMED")


@_safe
def products_saved(observer, row_ids, extraction, settings):
    ids = [item.strip() for item in row_ids if isinstance(item, str) and item.strip()]
    valid = (observer.extraction_valid and bool(getattr(settings, "glide_col_product_rfq_id", "").strip())
             and len(ids) == len(extraction.products) and len(set(ids)) == len(ids))
    if valid:
        observer.emit("products", "complete", completed=len(ids), total=len(ids), result={"recordIds": ids})
    else:
        observer.emit("products", "unconfirmed", reason="PRODUCT_RESULTS_INCOMPLETE", completed=len(ids), total=len(extraction.products))
    if not extraction.queries:
        if not getattr(settings, "enable_query_writeback", True):
            observer.emit("queries", "skipped", reason="QUERY_WRITEBACK_DISABLED")
        elif observer.extraction_valid:
            observer.emit("queries", "complete", reason="NO_QUERIES_NEEDED", completed=0, total=0, result={"recordIds": []})
        else:
            observer.emit("queries", "unconfirmed", reason="EXTRACTION_INCOMPLETE")


@_safe
def queries_start(observer, queries, resolved, settings):
    observer.query_expected = len(queries)
    observer.query_links = bool(getattr(settings, "glide_col_query_rfq_id", "").strip()) and all(
        not q.product_refs or (bool(getattr(settings, "glide_col_query_product_id", "").strip())
                              and all(ref in resolved for ref in q.product_refs)) for q in queries)
    observer.emit("queries", "running", completed=0, total=len(queries))


@_safe
def query_batch(observer, response, count):
    """Inspect the existing response inside the safe boundary, without changing returns."""
    data = response.json()
    entries = data if isinstance(data, list) else [data]
    ids = []
    for item in entries[:count]:
        value = item if isinstance(item, str) else next((item.get(key) for key in ("Row ID", "$rowID", "rowID", "row_id")
                                                       if isinstance(item, dict) and item.get(key)), None)
        if isinstance(value, str) and value.strip():
            ids.append(value.strip())
    observer.query_ids.extend(ids)
    observer.emit("queries", "running", completed=len(observer.query_ids), total=observer.query_expected)


@_safe
def queries_saved(observer, written, settings):
    if not getattr(settings, "enable_query_writeback", True):
        observer.emit("queries", "skipped", reason="QUERY_WRITEBACK_DISABLED")
    elif (observer.extraction_valid and observer.query_links and written == observer.query_expected
          and len(observer.query_ids) == written and len(set(observer.query_ids)) == written):
        observer.emit("queries", "complete", completed=written, total=written,
                      result={"recordIds": observer.query_ids, "linkageConfirmed": True})
    else:
        observer.emit("queries", "unconfirmed", reason="QUERY_RESULTS_INCOMPLETE")


@_safe
def unfinished(observer, reason="RESULT_NOT_CONFIRMED"):
    for name in TASKS:
        if observer.states.get(name) not in ("complete", "failed", "unconfirmed", "skipped", "blocked"):
            observer.emit(name, "unconfirmed", reason=reason)


def end_run(token):
    if token is not None:
        try:
            unfinished()
            _current.reset(token)
        except Exception:
            _warn("STRIKE_PROGRESS_CONTEXT_UNCONFIRMED")
