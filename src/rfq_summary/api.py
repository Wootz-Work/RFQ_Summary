from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Literal
import time
from fastapi import FastAPI, HTTPException, Response
from pydantic import ValidationError

from .config import load_settings, Settings
from .schema import InputPayload, QueryPayload
from .task import run_pricing, run_summary, run_all, run_query_triage
from .writer import write_all, write_triage
from .gsheet_logger import log_job_event, log_progress_event

Mode = Literal["pricing", "summary", "all", "triage"]


@dataclass(frozen=True)
class Job:
    run_id: str
    mode: Mode
    payload: Dict[str, Any]
    row_id: str
    enqueued_at: float  # perf_counter timestamp


app = FastAPI(title="RFQ Summary Service", version="0.2.0")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def root():
    return {
        "ok": True,
        "service": "rfq-summary",
        "endpoints": ["/rfq/run", "/rfq/pricing", "/rfq/summary", "/health"],
    }


@app.head("/")
def root_head():
    # Render health checks often hit HEAD /
    return Response(status_code=200)


# -----------------------
# Payload helpers
# -----------------------
def _require_row_id_if_writeback(settings: Settings, obj: InputPayload):
    if settings.enable_glide_writeback and not (obj.row_id or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Missing rowID/row_id in payload (required when writeback enabled).",
        )


def _unwrap_payload(payload: dict) -> dict:
    """
    Glide can send nested bodies like:
      { "RFQ Final json": { ... } }
    """
    if not isinstance(payload, dict):
        return payload

    for k in ("RFQ Final json", "rfq_final_json", "rfq_json", "data", "payload"):
        inner = payload.get(k)
        if isinstance(inner, dict) and ("Title" in inner or "Product_json" in inner):
            return inner

    if len(payload) == 1:
        only_val = next(iter(payload.values()))
        if isinstance(only_val, dict):
            return only_val

    return payload


def _validate(payload: dict) -> InputPayload:
    try:
        return InputPayload.model_validate(payload)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors()) from e

def _validate_query(payload: dict) -> QueryPayload:
    try:
        # Validate the updated QueryPayload structure
        return QueryPayload.model_validate(payload)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors()) from e

# -----------------------
# In-memory queue + dispatcher
# -----------------------
def _get_queue() -> asyncio.Queue[Job]:
    q = getattr(app.state, "job_queue", None)
    if q is None:
        q = asyncio.Queue()
        app.state.job_queue = q
    return q


def _get_semaphore(settings: Settings) -> asyncio.Semaphore:
    sem = getattr(app.state, "job_semaphore", None)
    if sem is None:
        sem = asyncio.Semaphore(max(1, int(settings.max_concurrent_jobs)))
        app.state.job_semaphore = sem
    return sem


def _queue_size() -> int:
    return _get_queue().qsize()


'''async def _run_job(job: Job) -> None:
    settings = load_settings()

    t_start = time.perf_counter()
    queue_wait_ms = int((t_start - float(getattr(job, "enqueued_at", t_start))) * 1000)
    print(f"[RUNNING] run_id={job.run_id} mode={job.mode} row_id={job.row_id} queue_wait_ms={queue_wait_ms}")
    try:
        log_progress_event(
            settings,
            job.run_id,
            job.mode,
            job.row_id,
            event="RUNNING",
            message=f"queue_wait_ms={queue_wait_ms}",
        )
    except Exception:
        pass

    # RUNNING (best-effort)
    try:
        log_job_event(settings, job.run_id, job.mode, job.row_id, status="RUNNING", message="Job started")
    except Exception:
        pass

    try:
        async def _do_work():
            if job.mode == "pricing":
                obj = _validate(job.payload)
                _require_row_id_if_writeback(settings, obj)
                out = await asyncio.to_thread(run_pricing, settings, obj, job.run_id)
                await asyncio.to_thread(write_all, settings, obj, out)

            elif job.mode == "summary":
                obj = _validate(job.payload)
                _require_row_id_if_writeback(settings, obj)
                out = await asyncio.to_thread(run_summary, settings, obj, job.run_id)
                await asyncio.to_thread(write_all, settings, obj, out)

            elif job.mode == "all":
                obj = _validate(job.payload)
                _require_row_id_if_writeback(settings, obj)
                out = await asyncio.to_thread(run_all, settings, obj, job.run_id)
                await asyncio.to_thread(write_all, settings, obj, out)

            elif job.mode == "triage":
                qobj = _validate_query(job.payload)
                out = await asyncio.to_thread(run_query_triage, settings, qobj, job.run_id)
                await asyncio.to_thread(write_triage, settings, qobj, out)

            else:
                raise RuntimeError(f"Unknown job mode: {job.mode}")

        await asyncio.wait_for(_do_work(), timeout=max(30, int(settings.job_timeout_sec)))

        total_ms = int((time.perf_counter() - t_start) * 1000)
        print(f"[DONE] run_id={job.run_id} mode={job.mode} row_id={job.row_id} total_ms={total_ms}")

        try:
            log_progress_event(
                settings,
                job.run_id,
                job.mode,
                job.row_id,
                event="DONE",
                message=f"total_ms={total_ms}",
            )
        except Exception:
            pass

        # DONE (best-effort)
        try:
            log_job_event(settings, job.run_id, job.mode, job.row_id, status="DONE", message=f"Job completed total_ms={total_ms}")
        except Exception:
            pass

    except asyncio.TimeoutError:
        try:
            log_job_event(
                settings,
                job.run_id,
                job.mode,
                job.row_id,
                status="FAILED",
                message=f"Job timeout after {settings.job_timeout_sec}s",
            )
            try:
                total_ms = int((time.perf_counter() - t_start) * 1000)
                log_progress_event(
                    settings,
                    job.run_id,
                    job.mode,
                    job.row_id,
                    event="FAILED_TIMEOUT",
                    message=f"total_ms={total_ms} timeout_sec={settings.job_timeout_sec}",
                )
            except Exception:
                pass
        except Exception:
            pass

    except Exception as e:
        try:
            log_job_event(
                settings,
                job.run_id,
                job.mode,
                job.row_id,
                status="FAILED",
                message=f"{type(e).__name__}: {e}",
            )
            try:
                total_ms = int((time.perf_counter() - t_start) * 1000)
                log_progress_event(
                    settings,
                    job.run_id,
                    job.mode,
                    job.row_id,
                    event="FAILED",
                    message=f"total_ms={total_ms} err={type(e).__name__}: {e}",
                )
            except Exception:
                pass
        except Exception:
            pass
'''

async def _run_job(job: Job) -> None:
    settings = load_settings()

    t_start = time.perf_counter()
    queue_wait_ms = int((t_start - float(getattr(job, "enqueued_at", t_start))) * 1000)
    print(f"[RUNNING] run_id={job.run_id} mode={job.mode} row_id={job.row_id} queue_wait_ms={queue_wait_ms}")
    print(f"[DEBUG] _run_job entered, about to start _do_work")

    try:
        log_progress_event(settings, job.run_id, job.mode, job.row_id, event="RUNNING", message=f"queue_wait_ms={queue_wait_ms}")
    except Exception:
        pass

    try:
        log_job_event(settings, job.run_id, job.mode, job.row_id, status="RUNNING", message="Job started")
    except Exception:
        pass

    try:
        async def _do_work():
            if job.mode == "pricing":
                print(f"[STEP 1/3] run_id={job.run_id} | Validating payload...")
                obj = _validate(job.payload)
                print(f"[STEP 1/3] run_id={job.run_id} | Payload valid. row_id={obj.row_id} title={obj.title!r}")
                _require_row_id_if_writeback(settings, obj)

                print(f"[STEP 2/3] run_id={job.run_id} | Running PRICING task...")
                t0 = time.perf_counter()
                out = await asyncio.to_thread(run_pricing, settings, obj, job.run_id)
                print(f"[STEP 2/3] run_id={job.run_id} | PRICING done in {int((time.perf_counter()-t0)*1000)}ms")

                print(f"[STEP 3/3] run_id={job.run_id} | Writing output (glide_writeback={settings.enable_glide_writeback})...")
                t0 = time.perf_counter()
                await asyncio.to_thread(write_all, settings, obj, out)
                print(f"[STEP 3/3] run_id={job.run_id} | Write done in {int((time.perf_counter()-t0)*1000)}ms")

            elif job.mode == "summary":
                print(f"[STEP 1/3] run_id={job.run_id} | Validating payload...")
                obj = _validate(job.payload)
                print(f"[STEP 1/3] run_id={job.run_id} | Payload valid. row_id={obj.row_id} title={obj.title!r}")
                _require_row_id_if_writeback(settings, obj)

                print(f"[STEP 2/3] run_id={job.run_id} | Running SUMMARY task...")
                t0 = time.perf_counter()
                out = await asyncio.to_thread(run_summary, settings, obj, job.run_id)
                print(f"[STEP 2/3] run_id={job.run_id} | SUMMARY done in {int((time.perf_counter()-t0)*1000)}ms")

                print(f"[STEP 3/3] run_id={job.run_id} | Writing output (glide_writeback={settings.enable_glide_writeback})...")
                t0 = time.perf_counter()
                await asyncio.to_thread(write_all, settings, obj, out)
                print(f"[STEP 3/3] run_id={job.run_id} | Write done in {int((time.perf_counter()-t0)*1000)}ms")

            elif job.mode == "all":
                print(f"[STEP 1/3] run_id={job.run_id} | Validating payload...")
                obj = _validate(job.payload)
                print(f"[STEP 1/3] run_id={job.run_id} | Payload valid. row_id={obj.row_id} title={obj.title!r}")
                _require_row_id_if_writeback(settings, obj)

                print(f"[STEP 2/3] run_id={job.run_id} | Running ALL (pricing + summary) task...")
                t0 = time.perf_counter()
                out = await asyncio.to_thread(run_all, settings, obj, job.run_id)
                print(f"[STEP 2/3] run_id={job.run_id} | ALL done in {int((time.perf_counter()-t0)*1000)}ms")

                print(f"[STEP 3/3] run_id={job.run_id} | Writing output (glide_writeback={settings.enable_glide_writeback})...")
                t0 = time.perf_counter()
                await asyncio.to_thread(write_all, settings, obj, out)
                print(f"[STEP 3/3] run_id={job.run_id} | Write done in {int((time.perf_counter()-t0)*1000)}ms")

            elif job.mode == "triage":
                print(f"[STEP 1/3] run_id={job.run_id} | Validating triage payload...")
                qobj = _validate_query(job.payload)
                print(f"[STEP 1/3] run_id={job.run_id} | Payload valid.")
                print(f"[STEP 1/3] run_id={job.run_id} | subject={qobj.subject!r} from={qobj.from_!r} from_name={qobj.from_name!r}")
                print(f"[STEP 1/3] run_id={job.run_id} | received_at={qobj.received_at!r} attachment_urls={qobj.attachment_urls}")

                print(f"[STEP 2/3] run_id={job.run_id} | Running TRIAGE task...")
                t0 = time.perf_counter()
                out = await asyncio.to_thread(run_query_triage, settings, qobj, job.run_id)
                print(f"[STEP 2/3] run_id={job.run_id} | TRIAGE done in {int((time.perf_counter()-t0)*1000)}ms")
                print(f"[STEP 2/3] run_id={job.run_id} | triage_text preview: {(out.triage_text or '')[:200]!r}")

                print(f"[STEP 3/3] run_id={job.run_id} | Writing triage output (triage_writeback={settings.enable_triage_writeback})...")
                t0 = time.perf_counter()
                await asyncio.to_thread(write_triage, settings, qobj, out)
                print(f"[STEP 3/3] run_id={job.run_id} | Write done in {int((time.perf_counter()-t0)*1000)}ms")

            else:
                raise RuntimeError(f"Unknown job mode: {job.mode}")

        await asyncio.wait_for(_do_work(), timeout=max(30, int(settings.job_timeout_sec)))

        total_ms = int((time.perf_counter() - t_start) * 1000)
        print(f"\n{'='*60}")
        print(f"[JOB DONE] run_id={job.run_id} mode={job.mode} row_id={job.row_id} total_ms={total_ms}")
        print(f"{'='*60}\n")

        try:
            log_progress_event(settings, job.run_id, job.mode, job.row_id, event="DONE", message=f"total_ms={total_ms}")
        except Exception:
            pass
        try:
            log_job_event(settings, job.run_id, job.mode, job.row_id, status="DONE", message=f"Job completed total_ms={total_ms}")
        except Exception:
            pass

    except asyncio.TimeoutError:
        total_ms = int((time.perf_counter() - t_start) * 1000)
        print(f"\n{'='*60}")
        print(f"[JOB TIMEOUT] run_id={job.run_id} mode={job.mode} row_id={job.row_id} total_ms={total_ms}")
        print(f"[JOB TIMEOUT] timeout_sec={settings.job_timeout_sec}")
        print(f"{'='*60}\n")
        try:
            log_job_event(settings, job.run_id, job.mode, job.row_id, status="FAILED", message=f"Job timeout after {settings.job_timeout_sec}s")
            log_progress_event(settings, job.run_id, job.mode, job.row_id, event="FAILED_TIMEOUT", message=f"total_ms={total_ms}")
        except Exception:
            pass

    except Exception as e:
        total_ms = int((time.perf_counter() - t_start) * 1000)
        print(f"\n{'='*60}")
        print(f"[JOB FAILED] run_id={job.run_id} mode={job.mode} row_id={job.row_id} total_ms={total_ms}")
        print(f"[JOB FAILED] error={type(e).__name__}: {e}")
        print(f"{'='*60}\n")
        try:
            log_job_event(settings, job.run_id, job.mode, job.row_id, status="FAILED", message=f"{type(e).__name__}: {e}")
            log_progress_event(settings, job.run_id, job.mode, job.row_id, event="FAILED", message=f"total_ms={total_ms} err={type(e).__name__}: {e}")
        except Exception:
            pass

async def _dispatcher_loop() -> None:
    while True:
        job = await _get_queue().get()
        settings = load_settings()
        sem = _get_semaphore(settings)

        await sem.acquire()

        async def _wrapped():
            try:
                await _run_job(job)
            finally:
                sem.release()

        asyncio.create_task(_wrapped())


@app.on_event("startup")
async def _startup():
    if getattr(app.state, "dispatcher_started", False):
        return
    app.state.dispatcher_started = True
    asyncio.create_task(_dispatcher_loop())


async def _enqueue_or_reject(mode: Mode, data: dict, obj: InputPayload) -> Dict[str, Any]:
    settings = load_settings()
    max_q = max(1, int(settings.max_queue_size))

    run_id = uuid.uuid4().hex[:10]
    row_id = (obj.row_id or "").strip()
    enq = time.perf_counter()
    if _queue_size() >= max_q:
        try:
            log_job_event(
                settings,
                run_id=run_id,
                mode=mode,
                row_id=row_id,
                status="REJECTED_QUEUE_FULL",
                message=f"Queue full: qsize={_queue_size()} max={max_q}",
            )
        except Exception:
            pass

        raise HTTPException(
            status_code=429,
            detail={
                "ok": False,
                "run_id": run_id,
                "status": "rejected",
                "reason": "QUEUE_FULL",
                "retry_hint": "Try again in 2-3 minutes.",
            },
        )

    job = Job(run_id=run_id, mode=mode, payload=data, row_id=row_id, enqueued_at=enq)
    await _get_queue().put(job)
    print(f"[QUEUED] run_id={run_id} mode={mode} row_id={row_id} qsize={_queue_size()}/{max_q}")
    try:
        log_progress_event(settings, run_id, mode, row_id, event="QUEUED", message=f"qsize={_queue_size()}/{max_q}")
    except Exception:
        pass
    try:
        log_job_event(
            settings,
            run_id,
            mode,
            row_id,
            status="QUEUED",
            message=f"Queued (qsize={_queue_size()}/{max_q})",
        )
    except Exception:
        pass

    return {
        "ok": True,
        "run_id": run_id,
        "status": "queued",
        "mode": mode,
        "queue": {"qsize": _queue_size(), "max": max_q},
    }

async def _enqueue_or_reject_triage(data: dict, qobj: QueryPayload) -> Dict[str, Any]:
    settings = load_settings()
    max_q = max(1, int(settings.max_queue_size))

    run_id = uuid.uuid4().hex[:10]
    row_id = (qobj.row_id or "").strip()
    enq = time.perf_counter()

    if _queue_size() >= max_q:
        try:
            log_job_event(
                settings,
                run_id=run_id,
                mode="triage",
                row_id=row_id,
                status="REJECTED_QUEUE_FULL",
                message=f"Queue full: qsize={_queue_size()} max={max_q}",
            )
        except Exception:
            pass

        raise HTTPException(
            status_code=429,
            detail={
                "ok": False,
                "run_id": run_id,
                "status": "rejected",
                "reason": "QUEUE_FULL",
            },
        )

    job = Job(run_id=run_id, mode="triage", payload=data, row_id=row_id, enqueued_at=enq)
    await _get_queue().put(job)

    print(f"[QUEUED] run_id={run_id} mode=triage row_id={row_id} qsize={_queue_size()}/{max_q}")
    try:
        log_progress_event(settings, run_id, "triage", row_id, event="QUEUED", message=f"qsize={_queue_size()}/{max_q}")
    except Exception:
        pass

    return {"ok": True, "run_id": run_id, "status": "queued", "mode": "triage"}
# -----------------------
# Endpoints
# -----------------------
@app.post("/rfq/run")
async def rfq_run(payload: dict, response: Response):
    """
    Single-button endpoint:
      - one job
      - attachments parsed once
      - pricing + summary executed (web search remains double)
      - writes all 3 columns
    """
    settings = load_settings()
    data = _unwrap_payload(payload)
    obj = _validate(data)
    _require_row_id_if_writeback(settings, obj)

    ack = await _enqueue_or_reject("all", data, obj)
    response.status_code = 202
    return ack


@app.post("/rfq/pricing")
async def rfq_pricing(payload: dict, response: Response):
    settings = load_settings()
    data = _unwrap_payload(payload)
    obj = _validate(data)
    _require_row_id_if_writeback(settings, obj)

    ack = await _enqueue_or_reject("pricing", data, obj)
    response.status_code = 202
    return ack


@app.post("/rfq/summary")
async def rfq_summary(payload: dict, response: Response):
    settings = load_settings()
    data = _unwrap_payload(payload)
    obj = _validate(data)
    _require_row_id_if_writeback(settings, obj)

    ack = await _enqueue_or_reject("summary", data, obj)
    response.status_code = 202
    return ack

@app.post("/query/triage")
async def query_triage(payload: dict, response: Response):
    print("[DEBUG] Received payload for /query/triage:", payload)

    # Glide wraps in {"body": {...}, "endpoint": "..."}
    # But "body" is also a field name, so check for "endpoint" key as the signal
    if "endpoint" in payload and "body" in payload and isinstance(payload["body"], dict):
        data = payload["body"]
    else:
        data = payload

    print("[DEBUG] Unwrapped data:", data)

    try:
        qobj = QueryPayload.model_validate(data)
    except Exception as e:
        print("[DEBUG] Validation error:", e)
        raise HTTPException(status_code=422, detail=str(e))

    print("[DEBUG] Validated QueryPayload:", qobj)

    ack = await _enqueue_or_reject_triage(data, qobj)
    response.status_code = 202
    return ack