from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ========================
    # LLM (Claude / Anthropic)
    # ========================
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-3-5-sonnet-latest", alias="ANTHROPIC_MODEL")
    anthropic_model_fallbacks: str = Field(
        default="claude-3-5-sonnet-latest,claude-3-5-haiku-latest",
        alias="ANTHROPIC_MODEL_FALLBACKS",
    )
    anthropic_max_tokens: int = Field(default=8000, alias="ANTHROPIC_MAX_TOKENS")
    # Prompts (two endpoints)
    prompt_pricing_file: str = Field(default="prompts/pricing_estimate.md", alias="PROMPT_PRICING_FILE")
    prompt_summary_file: str = Field(default="prompts/rfq_summary.md", alias="PROMPT_SUMMARY_FILE")

    # Incoming query triage prompt (Prospect RFQs)
    prompt_query_triage_file: str = Field(
        default="prompts/query_triage.md",
        alias="PROMPT_QUERY_TRIAGE_FILE",
    )
    prompt_query_costing_file: str = Field(
        default="prompts/query_costing_estimate.md",
        alias="PROMPT_QUERY_COSTING_FILE",
    )
    prompt_query_rfq_classification_file: str = Field(
        default="prompts/query_rfq_classification.md",
        alias="PROMPT_QUERY_RFQ_CLASSIFICATION_FILE",
    )
    # ========================
    # Web Search (Perplexity)
    # ========================
    perplexity_api_key: str = Field(default="", alias="PERPLEXITY_API_KEY")
    perplexity_base_url: str = Field(default="https://api.perplexity.ai", alias="PERPLEXITY_BASE_URL")
    perplexity_model: str = Field(default="sonar", alias="PERPLEXITY_MODEL")
    perplexity_max_results: int = Field(default=6, alias="PERPLEXITY_MAX_RESULTS")

    # ========================
    # Attachment parsing limits
    # ========================
    max_attachment_bytes: int = Field(default=50 * 1024 * 1024, alias="MAX_ATTACHMENT_BYTES")
    max_pdf_pages: int = Field(default=60, alias="MAX_PDF_PAGES")

    min_pdf_text_chars_per_page: int = Field(default=40, alias="MIN_PDF_TEXT_CHARS_PER_PAGE")
    min_ocr_chars_to_accept: int = Field(default=80, alias="MIN_OCR_CHARS_TO_ACCEPT")

    # Claude Vision fallback (for low OCR / scanned pages)
    enable_claude_vision_fallback: bool = Field(default=True, alias="ENABLE_CLAUDE_VISION_FALLBACK")

    # ========================
    # Google Document AI (OCR processor)
    # ========================
    enable_docai_ocr: bool = Field(default=True, alias="ENABLE_DOCAI_OCR")

    docai_project_id: str = Field(default="", alias="DOCAI_PROJECT_ID")
    docai_location: str = Field(default="asia-south1", alias="DOCAI_LOCATION")  # e.g. "asia-south1"
    docai_processor_id: str = Field(default="", alias="DOCAI_PROCESSOR_ID")

    # If empty, we fall back to GOOGLE_SA_JSON_B64.
    docai_sa_json_b64: str = Field(default="", alias="DOCAI_SA_JSON_B64")

    docai_timeout_sec: int = Field(default=120, alias="DOCAI_TIMEOUT_SEC")
    max_excel_rows: int = Field(default=250, alias="MAX_EXCEL_ROWS")
    max_excel_cols: int = Field(default=40, alias="MAX_EXCEL_COLS")
    max_excel_tables_per_sheet: int = Field(default=5, alias="MAX_EXCEL_TABLES_PER_SHEET")

    # ========================
    # Queue / Concurrency (same instance)
    # ========================
    max_queue_size: int = Field(default=50, alias="MAX_QUEUE_SIZE")
    max_concurrent_jobs: int = Field(default=2, alias="MAX_CONCURRENT_JOBS")
    job_timeout_sec: int = Field(default=420, alias="JOB_TIMEOUT_SEC")  # safety kill

    # ========================
    # Glide writeback (SAFETY: default off)
    # ========================
    enable_glide_writeback: bool = Field(default=False, alias="ENABLE_GLIDE_WRITEBACK")

    glide_api_key: str = Field(default="", alias="GLIDE_API_KEY")
    glide_app_id: str = Field(default="", alias="GLIDE_APP_ID")
    glide_rfq_table: str = Field(default="", alias="GLIDE_RFQ_TABLE")

    # Target writeback table (ZAI Responses)
    glide_zai_responses_table: str = Field(default="", alias="GLIDE_ZAI_RESPONSES_TABLE")

    # Prospect RFQs table (incoming email queries)
    glide_prospect_rfq_table: str = Field(
        default="native-table-498cd72b-6e47-4820-b737-f167d509b1ec",
        alias="GLIDE_PROSPECT_RFQ_TABLE",
    )

    # Column in Prospect RFQs to write triage output (your Zai response column id)
    glide_col_prospect_triage: str = Field(default="ZpJy4", alias="GLIDE_COL_PROSPECT_TRIAGE")
    glide_col_prospect_geography: str = Field(default="sMkF2", alias="GLIDE_COL_PROSPECT_GEOGRAPHY")
    glide_col_prospect_industry: str = Field(default="QOtPb", alias="GLIDE_COL_PROSPECT_INDUSTRY")
    glide_col_prospect_client_name: str = Field(default="ikKdb", alias="GLIDE_COL_PROSPECT_CLIENT_NAME")
    glide_col_prospect_standards: str = Field(default="fmNSP", alias="GLIDE_COL_PROSPECT_STANDARDS")
    glide_col_prospect_title: str = Field(default="Fg2uK", alias="GLIDE_COL_PROSPECT_TITLE")
    glide_col_prospect_sequence: str = Field(default="qxQ9p", alias="GLIDE_COL_PROSPECT_SEQUENCE")

    glide_all_companies_table: str = Field(
        default="native-table-g1GlBSdbNmRtTx16ecxD",
        alias="GLIDE_ALL_COMPANIES_TABLE",
    )
    glide_col_all_companies_pet_name: str = Field(default="Name", alias="GLIDE_COL_ALL_COMPANIES_PET_NAME")
    glide_col_all_companies_original_name: str = Field(default="MdnWu", alias="GLIDE_COL_ALL_COMPANIES_ORIGINAL_NAME")

    # ALL RFQ table writeback for incoming query triage.
    glide_all_rfq_table: str = Field(
        default="native-table-24696dcc-caaf-4bf8-a015-1e9ef394aa1b",
        alias="GLIDE_ALL_RFQ_TABLE",
    )
    glide_col_all_rfq_zai_response: str = Field(default="MANCF", alias="GLIDE_COL_ALL_RFQ_ZAI_RESPONSE")
    glide_col_all_rfq_costing_order_of_magnitude: str = Field(
        default="AEa95",
        alias="GLIDE_COL_ALL_RFQ_COSTING_ORDER_OF_MAGNITUDE",
    )

    # Gate triage writeback separately (keeps RFQ writeback safety intact)
    enable_triage_writeback: bool = Field(default=True, alias="ENABLE_TRIAGE_WRITEBACK")
    # Column in ZAI Responses table that stores the RFQ rowID from "ALL RFQ" table
    # (your rfqId column: usIzP)
    glide_col_rfq_id: str = Field(default="usIzP", alias="GLIDE_COL_RFQ_ID")
    # Summary cards (new XML prompt outputs)
    glide_col_scope: str = Field(default="Name", alias="GLIDE_COL_SCOPE")
    glide_col_cost: str = Field(default="vnlEl", alias="GLIDE_COL_COST")
    glide_col_quality: str = Field(default="LwfgB", alias="GLIDE_COL_QUALITY")
    glide_col_schedule: str = Field(default="FWPuu", alias="GLIDE_COL_SCHEDULE")  # timeline -> schedule column
    glide_col_summary: str = Field(default="hK56D", alias="GLIDE_COL_SUMMARY")

    # Pricing prompt outputs
    glide_col_pricing_estimate: str = Field(default="dwtEW", alias="GLIDE_COL_PRICING_ESTIMATE")  # OUTPUT 1
    glide_col_pricing_estimate_summary: str = Field(default="qcX9Z", alias="GLIDE_COL_PRICING_ESTIMATE_SUMMARY")  # OUTPUT 2

    # ZAI Regenerate table writeback
    glide_zai_regenerate_table: str = Field(default="", alias="GLIDE_ZAI_REGENERATE_TABLE")
    glide_col_zai_regenerate_rfq_id: str = Field(default="", alias="GLIDE_COL_ZAI_REGENERATE_RFQ_ID")
    glide_col_zai_regenerate_response: str = Field(default="", alias="GLIDE_COL_ZAI_REGENERATE_RESPONSE")
    glide_col_zai_regenerate_response_generated_time: str = Field(
        default="",
        alias="GLIDE_COL_ZAI_REGENERATE_RESPONSE_GENERATED_TIME",
    )
    glide_col_zai_regenerate_requested_time: str = Field(default="", alias="GLIDE_COL_ZAI_REGENERATE_REQUESTED_TIME")
    glide_col_zai_regenerate_instruction: str = Field(default="", alias="GLIDE_COL_ZAI_REGENERATE_INSTRUCTION")
    glide_col_zai_regenerate_requested_by: str = Field(default="", alias="GLIDE_COL_ZAI_REGENERATE_REQUESTED_BY")

    # ========================
    # Google Sheet logging (optional)
    # ========================
    enable_sheets_logging: bool = Field(default=True, alias="ENABLE_SHEETS_LOGGING")
    log_sheet_id: str = Field(default="", alias="LOG_SHEET_ID")
    log_sheet_tab: str = Field(default="Logs", alias="LOG_SHEET_TAB")
    google_sa_json_b64: str = Field(default="", alias="GOOGLE_SA_JSON_B64")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    max_cell_chars: int = Field(default=50000, alias="MAX_CELL_CHARS")


    # Google Drive settings. Prefer the Drive-specific credentials so Sheets/logging
    # can use GOOGLE_SA_JSON_B64 without also needing Drive access.
    google_drive_sa_json_b64: str = Field(default="", alias="GOOGLE_DRIVE_SA_JSON_B64")
    google_drive_service_account_path: str = Field(default="", alias="GOOGLE_DRIVE_SERVICE_ACCOUNT_PATH")
    google_service_account_path: str = Field(default="service_account.json", alias="GOOGLE_SERVICE_ACCOUNT_PATH")

def load_settings() -> Settings:
    return Settings()
