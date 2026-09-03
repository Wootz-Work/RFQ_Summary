from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Literal
import time
from fastapi import FastAPI, HTTPException, Response
from pydantic import ValidationError

from .config import load_settings, Settings
from .schema import InputPayload, QueryPayload, RfqClassificationInputPayload, RfqRegenerateTriageInputPayload, RfqQueryInputPayload
from .task import run_pricing, run_summary, run_all, run_query_triage, run_rfq_classification, run_regenerate_triage, run_regenerate_query
from .writer import write_all, write_triage, write_rfq_classification, write_regenerated_triage, write_regenerated_query
from .gsheet_logger import log_job_event, log_progress_event

Mode = Literal["pricing", "summary", "all", "triage", "classify", "regenerate_triage", "query_regenerate"]


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
        "endpoints": ["/rfq/run", "/rfq/pricing", "/rfq/summary", "/query/triage", "/query/classify-rfq", "/query/regenerate-triage", "/query/regenerate-query", "/health"],
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


def _require_triage_writeback_settings(settings: Settings, obj: QueryPayload):
    if not settings.enable_triage_writeback:
        return
    if not (obj.row_id or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Missing rowID/row_id in payload (required when triage regenerate writeback enabled).",
        )
    required = {
        "GLIDE_API_KEY": settings.glide_api_key,
        "GLIDE_APP_ID": settings.glide_app_id,
        "GLIDE_ZAI_REGENERATE_TABLE": settings.glide_zai_regenerate_table,
        "GLIDE_COL_ZAI_REGENERATE_RFQ_ID": settings.glide_col_zai_regenerate_rfq_id,
        "GLIDE_COL_ZAI_REGENERATE_RESPONSE": settings.glide_col_zai_regenerate_response,
        "GLIDE_COL_ZAI_REGENERATE_RESPONSE_GENERATED_TIME": settings.glide_col_zai_regenerate_response_generated_time,
        "GLIDE_COL_ZAI_REGENERATE_REQUESTED_TIME": settings.glide_col_zai_regenerate_requested_time,
        "GLIDE_COL_ZAI_REGENERATE_REQUESTED_BY": settings.glide_col_zai_regenerate_requested_by,
        "GLIDE_COL_ZAI_REGENERATE_TYPE": settings.glide_col_zai_regenerate_type,
        "GLIDE_COL_ZAI_REGENERATE_VERSION": settings.glide_col_zai_regenerate_version,
        "GLIDE_ALL_RFQ_TABLE": settings.glide_all_rfq_table,
        "GLIDE_COL_ALL_RFQ_ZAI_RESPONSE": settings.glide_col_all_rfq_zai_response,
        "GLIDE_COL_ALL_RFQ_COSTING_ORDER_OF_MAGNITUDE": settings.glide_col_all_rfq_costing_order_of_magnitude,
        "GLIDE_COL_ALL_RFQ_COSTING_MAGNITUDE_REASON": settings.glide_col_all_rfq_costing_magnitude_reason,
    }
    missing = [name for name, value in required.items() if not (value or "").strip()]
    if missing:
        raise HTTPException(status_code=500, detail=f"Missing triage regenerate writeback configuration: {', '.join(missing)}")


def _require_classification_writeback_settings(settings: Settings, obj: RfqClassificationInputPayload):
    if not settings.enable_triage_writeback:
        return
    if not (obj.row_id or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Missing rowID/row_id in payload (required when classification writeback enabled).",
        )
    required = {
        "GLIDE_API_KEY": settings.glide_api_key,
        "GLIDE_APP_ID": settings.glide_app_id,
        "GLIDE_PROSPECT_RFQ_TABLE": settings.glide_prospect_rfq_table,
        "GLIDE_ALL_COMPANIES_TABLE": settings.glide_all_companies_table,
        "GLIDE_GEOGRAPHIES_TABLE": settings.glide_geographies_table,
        "GLIDE_COL_GEOGRAPHIES_NAME": settings.glide_col_geographies_name,
        "GLIDE_INDUSTRIES_TABLE": settings.glide_industries_table,
        "GLIDE_COL_INDUSTRIES_INDUSTRY": settings.glide_col_industries_industry,
        "GLIDE_COL_PROSPECT_GEOGRAPHY": settings.glide_col_prospect_geography,
        "GLIDE_COL_PROSPECT_INDUSTRY": settings.glide_col_prospect_industry,
        "GLIDE_COL_PROSPECT_CLIENT_NAME": settings.glide_col_prospect_client_name,
        "GLIDE_COL_PROSPECT_STANDARDS": settings.glide_col_prospect_standards,
        "GLIDE_COL_PROSPECT_TITLE": settings.glide_col_prospect_title,
    }
    missing = [name for name, value in required.items() if not (value or "").strip()]
    if missing:
        raise HTTPException(status_code=500, detail=f"Missing classification writeback configuration: {', '.join(missing)}")


def _require_regenerate_writeback_settings(settings: Settings, obj: RfqRegenerateTriageInputPayload):
    if not settings.enable_triage_writeback:
        return
    if not (obj.rfq_id or "").strip():
        raise HTTPException(status_code=400, detail="Missing rfq_id in payload.")
    required = {
        "GLIDE_API_KEY": settings.glide_api_key,
        "GLIDE_APP_ID": settings.glide_app_id,
        "GLIDE_ZAI_REGENERATE_TABLE": settings.glide_zai_regenerate_table,
        "GLIDE_COL_ZAI_REGENERATE_RFQ_ID": settings.glide_col_zai_regenerate_rfq_id,
        "GLIDE_COL_ZAI_REGENERATE_RESPONSE": settings.glide_col_zai_regenerate_response,
        "GLIDE_COL_ZAI_REGENERATE_RESPONSE_GENERATED_TIME": settings.glide_col_zai_regenerate_response_generated_time,
        "GLIDE_COL_ZAI_REGENERATE_REQUESTED_TIME": settings.glide_col_zai_regenerate_requested_time,
        "GLIDE_COL_ZAI_REGENERATE_INSTRUCTION": settings.glide_col_zai_regenerate_instruction,
        "GLIDE_COL_ZAI_REGENERATE_REQUESTED_BY": settings.glide_col_zai_regenerate_requested_by,
        "GLIDE_COL_ZAI_REGENERATE_TYPE": settings.glide_col_zai_regenerate_type,
        "GLIDE_COL_ZAI_REGENERATE_VERSION": settings.glide_col_zai_regenerate_version,
        "GLIDE_ALL_RFQ_TABLE": settings.glide_all_rfq_table,
        "GLIDE_COL_ALL_RFQ_COSTING_ORDER_OF_MAGNITUDE": settings.glide_col_all_rfq_costing_order_of_magnitude,
        "GLIDE_COL_ALL_RFQ_COSTING_MAGNITUDE_REASON": settings.glide_col_all_rfq_costing_magnitude_reason,
    }
    missing = [name for name, value in required.items() if not (value or "").strip()]
    if missing:
        raise HTTPException(status_code=500, detail=f"Missing regenerate writeback configuration: {', '.join(missing)}")


def _require_regenerate_query_writeback_settings(settings: Settings, obj: RfqQueryInputPayload):
    if not settings.enable_triage_writeback:
        return
    if not (obj.rfq_id or "").strip():
        raise HTTPException(status_code=400, detail="Missing rfq_id in payload.")
    required = {
        "GLIDE_API_KEY": settings.glide_api_key,
        "GLIDE_APP_ID": settings.glide_app_id,
        "GLIDE_ZAI_REGENERATE_TABLE": settings.glide_zai_regenerate_table,
        "GLIDE_COL_ZAI_REGENERATE_RFQ_ID": settings.glide_col_zai_regenerate_rfq_id,
        "GLIDE_COL_ZAI_REGENERATE_RESPONSE": settings.glide_col_zai_regenerate_response,
        "GLIDE_COL_ZAI_REGENERATE_RESPONSE_GENERATED_TIME": settings.glide_col_zai_regenerate_response_generated_time,
        "GLIDE_COL_ZAI_REGENERATE_REQUESTED_TIME": settings.glide_col_zai_regenerate_requested_time,
        "GLIDE_COL_ZAI_REGENERATE_INSTRUCTION": settings.glide_col_zai_regenerate_instruction,
        "GLIDE_COL_ZAI_REGENERATE_QUERY": settings.glide_col_zai_regenerate_query,
        "GLIDE_COL_ZAI_REGENERATE_REQUESTED_BY": settings.glide_col_zai_regenerate_requested_by,
        "GLIDE_COL_ZAI_REGENERATE_TYPE": settings.glide_col_zai_regenerate_type,
        "GLIDE_COL_ZAI_REGENERATE_VERSION": settings.glide_col_zai_regenerate_version,
    }
    missing = [name for name, value in required.items() if not (value or "").strip()]
    if missing:
        raise HTTPException(status_code=500, detail=f"Missing regenerate query writeback configuration: {', '.join(missing)}")


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


def _validate_classification(payload: dict) -> RfqClassificationInputPayload:
    try:
        return RfqClassificationInputPayload.model_validate(payload)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors()) from e


def _validate_regenerate_triage(payload: dict) -> RfqRegenerateTriageInputPayload:
    try:
        return RfqRegenerateTriageInputPayload.model_validate(payload)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors()) from e


def _validate_regenerate_query(payload: dict) -> RfqQueryInputPayload:
    try:
        return RfqQueryInputPayload.model_validate(payload)
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
                print(f"[STEP 2/3] run_id={job.run_id} | estimate preview: {(out.costing_estimate_text or '')[:80]!r}")
                extraction = out.product_extraction
                print(f"[STEP 2/3] run_id={job.run_id} | product lines extracted: {len(extraction.products) if extraction else 0}")
                if extraction and extraction.reconciliation_note():
                    print(f"[STEP 2/3] run_id={job.run_id} | product reconciliation: {extraction.reconciliation_note()}")

                print(f"[STEP 3/3] run_id={job.run_id} | Adding triage output to ZAI Regenerate (triage_writeback={settings.enable_triage_writeback}) and product rows to ALL Product (product_writeback={settings.enable_product_writeback})...")
                t0 = time.perf_counter()
                await asyncio.to_thread(write_triage, settings, qobj, out)
                print(f"[STEP 3/3] run_id={job.run_id} | Write done in {int((time.perf_counter()-t0)*1000)}ms")

            elif job.mode == "classify":
                print(f"[STEP 1/3] run_id={job.run_id} | Validating RFQ classification payload...")
                cobj = _validate_classification(job.payload)
                print(f"[STEP 1/3] run_id={job.run_id} | Payload valid. row_id={cobj.row_id!r} subject={cobj.subject!r}")

                print(f"[STEP 2/3] run_id={job.run_id} | Running RFQ classification task...")
                t0 = time.perf_counter()
                out = await asyncio.to_thread(run_rfq_classification, settings, cobj, job.run_id)
                print(f"[STEP 2/3] run_id={job.run_id} | Classification done in {int((time.perf_counter()-t0)*1000)}ms")
                print(f"[STEP 2/3] run_id={job.run_id} | classification={out.model_dump(exclude={'raw_model_output', 'structured'})}")

                print(f"[STEP 3/3] run_id={job.run_id} | Writing classification outputs to Prospect RFQ (triage_writeback={settings.enable_triage_writeback})...")
                t0 = time.perf_counter()
                await asyncio.to_thread(write_rfq_classification, settings, cobj, out)
                print(f"[STEP 3/3] run_id={job.run_id} | Write done in {int((time.perf_counter()-t0)*1000)}ms")

            elif job.mode == "regenerate_triage":
                print(f"[STEP 1/3] run_id={job.run_id} | Validating regenerate triage payload...")
                robj = _validate_regenerate_triage(job.payload)
                print(f"[STEP 1/3] run_id={job.run_id} | Payload valid. rfq_id={robj.rfq_id!r}")

                print(f"[STEP 2/3] run_id={job.run_id} | Running regenerate triage task...")
                t0 = time.perf_counter()
                out = await asyncio.to_thread(run_regenerate_triage, settings, robj, job.run_id)
                print(f"[STEP 2/3] run_id={job.run_id} | Regenerate triage done in {int((time.perf_counter()-t0)*1000)}ms")
                print(f"[STEP 2/3] run_id={job.run_id} | triage_text preview: {(out.triage_text or '')[:200]!r}")

                print(f"[STEP 3/3] run_id={job.run_id} | Adding ZAI Regenerate row (triage_writeback={settings.enable_triage_writeback})...")
                t0 = time.perf_counter()
                await asyncio.to_thread(write_regenerated_triage, settings, robj, out)
                print(f"[STEP 3/3] run_id={job.run_id} | Write done in {int((time.perf_counter()-t0)*1000)}ms")

            elif job.mode == "query_regenerate":
                print(f"[STEP 1/3] run_id={job.run_id} | Validating regenerate query payload...")
                qrobj = _validate_regenerate_query(job.payload)
                print(f"[STEP 1/3] run_id={job.run_id} | Payload valid. rfq_id={qrobj.rfq_id!r}")

                print(f"[STEP 2/3] run_id={job.run_id} | Running regenerate query task...")
                t0 = time.perf_counter()
                out = await asyncio.to_thread(run_regenerate_query, settings, qrobj, job.run_id)
                print(f"[STEP 2/3] run_id={job.run_id} | Regenerate query done in {int((time.perf_counter()-t0)*1000)}ms")
                print(f"[STEP 2/3] run_id={job.run_id} | response preview: {(out.response_text or '')[:200]!r}")

                print(f"[STEP 3/3] run_id={job.run_id} | Adding ZAI Regenerate query row (triage_writeback={settings.enable_triage_writeback})...")
                t0 = time.perf_counter()
                await asyncio.to_thread(write_regenerated_query, settings, qrobj, out)
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


async def _enqueue_or_reject_classification(data: dict, cobj: RfqClassificationInputPayload) -> Dict[str, Any]:
    settings = load_settings()
    max_q = max(1, int(settings.max_queue_size))

    run_id = uuid.uuid4().hex[:10]
    row_id = (cobj.row_id or "").strip()
    enq = time.perf_counter()

    if _queue_size() >= max_q:
        try:
            log_job_event(
                settings,
                run_id=run_id,
                mode="classify",
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

    job = Job(run_id=run_id, mode="classify", payload=data, row_id=row_id, enqueued_at=enq)
    await _get_queue().put(job)

    print(f"[QUEUED] run_id={run_id} mode=classify row_id={row_id} qsize={_queue_size()}/{max_q}")
    try:
        log_progress_event(settings, run_id, "classify", row_id, event="QUEUED", message=f"qsize={_queue_size()}/{max_q}")
    except Exception:
        pass

    return {"ok": True, "run_id": run_id, "status": "queued", "mode": "classify"}


async def _enqueue_or_reject_regenerate_triage(data: dict, robj: RfqRegenerateTriageInputPayload) -> Dict[str, Any]:
    settings = load_settings()
    max_q = max(1, int(settings.max_queue_size))

    run_id = uuid.uuid4().hex[:10]
    row_id = (robj.rfq_id or "").strip()
    enq = time.perf_counter()

    if _queue_size() >= max_q:
        try:
            log_job_event(
                settings,
                run_id=run_id,
                mode="regenerate_triage",
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

    job = Job(run_id=run_id, mode="regenerate_triage", payload=data, row_id=row_id, enqueued_at=enq)
    await _get_queue().put(job)

    print(f"[QUEUED] run_id={run_id} mode=regenerate_triage rfq_id={row_id} qsize={_queue_size()}/{max_q}")
    try:
        log_progress_event(settings, run_id, "regenerate_triage", row_id, event="QUEUED", message=f"qsize={_queue_size()}/{max_q}")
    except Exception:
        pass

    return {"ok": True, "run_id": run_id, "status": "queued", "mode": "regenerate_triage"}


async def _enqueue_or_reject_regenerate_query(data: dict, qrobj: RfqQueryInputPayload) -> Dict[str, Any]:
    settings = load_settings()
    max_q = max(1, int(settings.max_queue_size))

    run_id = uuid.uuid4().hex[:10]
    row_id = (qrobj.rfq_id or "").strip()
    enq = time.perf_counter()

    if _queue_size() >= max_q:
        try:
            log_job_event(
                settings,
                run_id=run_id,
                mode="query_regenerate",
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

    job = Job(run_id=run_id, mode="query_regenerate", payload=data, row_id=row_id, enqueued_at=enq)
    await _get_queue().put(job)

    print(f"[QUEUED] run_id={run_id} mode=query_regenerate rfq_id={row_id} qsize={_queue_size()}/{max_q}")
    try:
        log_progress_event(settings, run_id, "query_regenerate", row_id, event="QUEUED", message=f"qsize={_queue_size()}/{max_q}")
    except Exception:
        pass

    return {"ok": True, "run_id": run_id, "status": "queued", "mode": "query_regenerate"}
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
    _require_triage_writeback_settings(load_settings(), qobj)

    ack = await _enqueue_or_reject_triage(data, qobj)
    response.status_code = 202
    return ack


@app.post("/query/classify-rfq")
async def query_classify_rfq(payload: dict, response: Response):
    print("[DEBUG] Received payload for /query/classify-rfq:", payload)

    if "endpoint" in payload and "body" in payload and isinstance(payload["body"], dict):
        data = payload["body"]
    else:
        data = payload

    cobj = _validate_classification(data)
    if not (cobj.mail_body or "").strip():
        raise HTTPException(status_code=400, detail="Missing mail_body/body in payload.")
    _require_classification_writeback_settings(load_settings(), cobj)

    ack = await _enqueue_or_reject_classification(data, cobj)
    response.status_code = 202
    return ack


@app.post("/query/regenerate-triage")
async def query_regenerate_triage(payload: dict, response: Response):
    print("[DEBUG] Received payload for /query/regenerate-triage:", payload)

    if "endpoint" in payload and "body" in payload and isinstance(payload["body"], dict):
        data = payload["body"]
    else:
        data = payload

    robj = _validate_regenerate_triage(data)
    if not (robj.rfq_id or "").strip():
        raise HTTPException(status_code=400, detail="Missing rfq_id in payload.")
    _require_regenerate_writeback_settings(load_settings(), robj)

    ack = await _enqueue_or_reject_regenerate_triage(data, robj)
    response.status_code = 202
    return ack


@app.post("/query/regenerate-query")
async def query_regenerate_query(payload: dict, response: Response):
    print("[DEBUG] Received payload for /query/regenerate-query:", payload)

    if "endpoint" in payload and "body" in payload and isinstance(payload["body"], dict):
        data = payload["body"]
    else:
        data = payload

    qrobj = _validate_regenerate_query(data)
    if not (qrobj.rfq_id or "").strip():
        raise HTTPException(status_code=400, detail="Missing rfq_id in payload.")
    if not (qrobj.query or "").strip():
        raise HTTPException(status_code=400, detail="Missing query in payload.")
    _require_regenerate_query_writeback_settings(load_settings(), qrobj)

    ack = await _enqueue_or_reject_regenerate_query(data, qrobj)
    response.status_code = 202
    return ack
