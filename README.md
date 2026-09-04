
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
   of the line it blocks in `Product id`. An RFQ-level question (`product_ref:
   null`) is linked to the RFQ only. `Query ID` is database-assigned and
   `Query Response` belongs to the customer — this service writes neither.

The three calls start together, but the job only waits for triage and costing before
writing the ZAI response — product extraction keeps running in the background and is
collected afterwards, so it adds no latency to the ZAI response while still overlapping
rather than running serially. `PRODUCT_EXTRACTION_TIMEOUT_SEC` (default 300) caps that
wait; giving up costs the product rows only.

### Prompt rules enforced in code

The prompt asks for several rules to be checked rather than trusted, because the
model broke them in testing. `_validate()` in `product_extraction.py` reports them
as `validation_warnings` — logged, and stored in the Sheets log — without ever
blocking a write: product name over 50 characters or not a name at all, provenance
given as a phrase instead of one token, bold sub-headings inside `RFQ Details`,
`placeholder_count` or `query_count` disagreeing with what was emitted, a `\--`
marker with no query row (or the reverse), duplicate query text, two questions in
one query row, an unknown `section`, and a query pointing at a line that was never
extracted.

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
