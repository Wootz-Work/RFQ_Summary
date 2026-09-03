
RFQ_Summary

Worker/service that ingests RFQ JSON + attachments and produces a summary output.

## Product extraction (`/query/triage`)

When the generation endpoint is hit, the email body and its parsed attachments feed
three Claude calls in parallel:

| Prompt | Output | Written to |
|---|---|---|
| `prompts/query_triage.md` | triage response | ZAI Regenerate |
| `prompts/query_costing_estimate.md` | costing order of magnitude + reason | ALL RFQ |
| `prompts/rfq_product_extraction.md` | product line items (NDJSON) | ALL Product |

The product prompt returns NDJSON — an `rfq_header`, one `product` per line item in
the customer's own order, then an `rfq_summary`. `product_extraction.py` parses it and
each line item becomes one row in the "ALL Product" table, linked back to the RFQ row:
`Name`, `Qty`, `Details` (the four-section markdown package), `Target price`,
`Dwg link`, `Rep URL`, `Addl. files`.

Provenance, assumptions and queries stay out of the table — they are reviewer-facing
and are logged to the Google Sheet alongside the raw model output, the count
reconciliation and any rows that could not be parsed.

The three calls start together, but the job only waits for triage and costing before
writing the ZAI response — product extraction keeps running in the background and is
collected afterwards, so it adds no latency to the ZAI response while still overlapping
rather than running serially. `PRODUCT_EXTRACTION_TIMEOUT_SEC` (default 180) caps that
wait; giving up costs the product rows only.

`ENABLE_PRODUCT_EXTRACTION=false` makes the whole feature a no-op — no third LLM call,
no added cost — so `/query/triage` behaves exactly as it did before this existed.

Configure the target table with `GLIDE_ALL_PRODUCT_TABLE` and the `GLIDE_COL_PRODUCT_*`
column ids (see `.env.example`). Only configured columns are written, and product
writeback is best-effort: it can be turned off with `ENABLE_PRODUCT_WRITEBACK=false`, and
a failure there is logged without failing the triage response.
