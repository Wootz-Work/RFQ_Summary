
RFQ_Summary

Worker/service that ingests RFQ JSON + attachments and produces a summary output.

## Product extraction (`/query/triage`)

When the generation endpoint is hit, the email body and its parsed attachments feed
three Claude calls in parallel:

| Prompt | Output | Written to |
|---|---|---|
| `prompts/query_triage.md` | triage response | ZAI Regenerate |
| `prompts/query_costing_estimate.md` | costing order of magnitude + reason | ALL RFQ |
| `prompts/rfq_product_extraction.md` | product line items + queries (NDJSON) | ALL Product, Queries |

The product prompt (v3) returns NDJSON: an `rfq_header`, then each `product`
followed by the `query` objects it blocks, then the RFQ-level queries, then an
`rfq_summary`. `product_extraction.py` parses it, and the writeback runs in two
steps because the second depends on the first:

1. Each line item becomes a row in **ALL Product** — `Product name`, `Qty`,
   `RFQ Details` (five-section markdown), `AI Internal notes` (team-only),
   `Target price`, `Dwg link`, `Rep URL`, `Addl. files`, plus `srNo` and
   `acceptedProduct`. Glide returns a Row ID per row.
2. Each open question becomes a row in the **Queries** table, carrying the Row ID
   of every line it covers in `Product id`, comma-separated — one question that
   applies to several lines is one row, not one per line. At most four queries go
   to a customer for a whole RFQ. `Query Description` is written the way the
   team would ask the customer — one question per row, options stated where there
   are options, a reason only where it is a recommendation. An RFQ-level question (`product_ref:
   null`) is linked to the RFQ only. `Query ID` is database-assigned and
   `Query Response` belongs to the customer — this service writes neither. `Query Photo`
   is off: the model has no reliable way to pick the attachment that shows an
   ambiguity (`GLIDE_COL_QUERY_PHOTO` re-enables it).

The three calls start together, but the job only waits for triage and costing before
writing the ZAI response — product extraction keeps running in the background and is
collected afterwards, so it adds no latency to the ZAI response while still overlapping
rather than running serially. `PRODUCT_EXTRACTION_TIMEOUT_SEC` (default 300) caps that
wait; giving up costs the product rows only.

### Prompt rules enforced in code

The prompt asks for several rules to be checked rather than trusted, because the
model broke them in testing. `_validate()` in `product_extraction.py` reports them
as `validation_warnings` — logged, and stored in the Sheets log — without ever
blocking a write: a query that asks the customer about a file we could not open, asks
for something for our own tracking, names a supplier or vendor, or asks for quantity
basis and the rest of the assume list; product name over 50 characters or not a name at all, provenance
given as a phrase instead of one token, bold sub-headings inside `RFQ Details`,
`placeholder_count` or `query_count` disagreeing with what was emitted, a `\--`
marker with no query row (or the reverse), duplicate query text, two questions in
one query row, an unknown `section`, and a query pointing at a line that was never
extracted.

### Model

All seven tasks share one model (`ANTHROPIC_MODEL`, default `claude-opus-5`), with
`ANTHROPIC_MODEL_FALLBACKS` as the retry chain. `generate_text` sends no
`temperature` — Opus 5, Opus 4.8/4.7 and Sonnet 5 reject it with a 400 — and uses
adaptive thinking instead (`ANTHROPIC_ADAPTIVE_THINKING=false` turns it off). When a
fallback answers, the model that replied is logged, so a quietly worse answer is not
mistaken for a good one.

### Configuration

Both table ids and all column ids ship as defaults in `config.py`, so the
deployment needs no environment variables; the `GLIDE_COL_*` overrides exist for
pointing at a scratch table or leaving a column alone. `Addl. files` is a
single-uri column, so only the first supporting file is written and extras are
logged. Two switches, both defaulting on: `ENABLE_PRODUCT_EXTRACTION=false` makes
the whole feature a no-op (no third LLM call, `/query/triage` behaves exactly as it
did before), and `ENABLE_PRODUCT_WRITEBACK=false` runs the extraction but writes no
rows — the useful shape for validating output before pointing at a live table.
`ENABLE_QUERY_WRITEBACK` gates the queries table alone.

Everything the reviewer needs but the supplier must not see — provenance,
validation warnings, the count reconciliation, unparseable rows and the raw model
output — is logged to the Google Sheet under mode `triage_products`, never into
`RFQ Details`.
