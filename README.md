
RFQ_Summary

Worker/service that ingests RFQ JSON + attachments and produces a summary output.

## Optional Strike task reporting

`STRIKE_PROGRESS_ENABLED=false` by default. When enabled, triage requests may add
`_strike: { operationId, rfqRowId, taskCallbackUrl, attempt }` without changing the existing
request fields or acknowledgement. `taskCallbackUrl` must exactly match the HTTPS
`STRIKE_PROGRESS_CALLBACK_URL` configured on this service, with path
`/api/integrations/task-events`. Set `STRIKE_PROGRESS_SIGNING_SECRET` to the native
Strike service's callback signing key; keep it server-side.

The additive observer reports `zai_summary`, `products` and `queries` at existing
write/error points. Successful events require confirmed record IDs; query events
also check the intended RFQ/product links. Incomplete extraction, missing returned
IDs, disabled writes and ambiguous saves remain unconfirmed/skipped. A valid empty
extraction can explicitly report no products or no queries. Event sequences are
independent per task and bound to the particular operation, RFQ and provider run.

One bounded daemon sender signs the exact JSON bytes using HMAC-SHA256 over
`timestamp + "\n" + body`, with `X-Strike-Timestamp` and `X-Strike-Signature` headers.
It refuses redirects and retries the same notification in the background. It does
not retry RFQ work, change writer return values, block on callback HTTP in the
business path, or propagate reporting errors into existing processing. Legacy
requests and other endpoint modes do not report. No dispatcher, work queue,
prompts, model calls or business-write behavior is replaced.

### Retry attempts

When a person presses Retry in Strike, Strike sends a new triage request with `_strike.attempt` greater than 1 and a
new `operationId`. First attempts and legacy callers (no `attempt`) behave exactly as before. On a retry attempt only,
this service first looks for rows an earlier attempt already saved for the same RFQ, and never adds rows on top of them:

- **Summary:** reuses the initial (version `0`) ZAI Regenerate row if one exists, fills its response if it arrived empty,
  and adds a row only when none exists. The past-quotes step does not rewrite a reused row.
- **Products:** adds none if any product row exists for the RFQ, and reports `PRODUCT_ROWS_EXIST`.
- **Queries:** adds none if any query row exists for the RFQ, and reports `QUERY_ROWS_EXIST`.
- **Could not check:** if a lookup fails, nothing is written and the task reports `*_CHECK_FAILED`. "Could not check" is never
  treated as "nothing there". An `attempt` value that is present but unreadable is treated as a retry.

Rows found by a retry are left for the team to verify; Strike keeps that task unconfirmed. Queries cannot be retried alone,
because their product links come from the same extraction as the products.

Notifications queued in memory can be lost on process exit. Strike must retain
unconfirmed status when evidence is missing; this feature does not promise
durable/exactly-once provider processing or repair pre-existing backend issues.

Run the reporting tests without credentials or external calls:

```sh
python test_strike_progress.py
```

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
   `RFQ Details` (four-section markdown: Specification, Scope, Application,
   Additional note), `AI Internal notes` (team-only — sourcing, applicable
   standards, what to attach, assumptions, context), `Target price`, plus `srNo`
   and `acceptedProduct`. `Dwg link`, `Rep URL` and `Addl. files` are deliberately
   left empty: attaching files is the team's step in the app, and the model names
   what to attach under `Attachments:` in the internal notes instead. Glide returns a Row ID per row.
2. Each open question becomes a row in the **Queries** table, carrying the Row ID
   of every line it covers in `Product id`, comma-separated — one question that
   applies to several lines is one row, not one per line. Every query carries a
   `Type`: `Team` for anything the team can settle or safely assume (incoterm,
   PPAP, quantity basis, packaging), `Customer` for what genuinely cannot be
   assumed. At most four `Customer` queries per RFQ; `Team` is uncapped. `Query Description` is written the way the
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

## CI/CD Deployment (GitHub Actions to EC2)

This repository includes an automated CI/CD pipeline (`.github/workflows/deploy.yml`) that deploys the application directly to an AWS EC2 instance whenever changes are pushed or merged into the `main` branch.

### How it works:
1. Triggered on push / merge to `main` (or manually via GitHub Actions **Run workflow**).
2. GitHub Actions connects to the EC2 server over SSH.
3. Pulls latest changes from `origin/main` directly on EC2 (`git fetch origin main && git reset --hard origin/main`).
4. Builds the Docker container locally on EC2 using `docker compose up -d --build` (no Docker Hub account required).
5. Cleans up unused old images (`docker image prune -f`) and outputs container status.

### One-Time EC2 Setup:
1. **Clone the repository on your EC2 instance**:
   ```bash
   git clone git@github-wootzwork:Wootz-Work/RFQ_Summary.git ~/RFQ_Summary
   # or
   git clone https://github.com/Wootz-Work/RFQ_Summary.git ~/RFQ_Summary
   ```
2. **Setup your `.env` file on EC2**:
   ```bash
   cd ~/RFQ_Summary
   cp .env.example .env
   # Edit .env with production credentials:
   nano .env
   ```
3. **Ensure Docker & Docker Compose are installed and the user has docker permissions**:
   ```bash
   sudo usermod -aG docker $USER
   newgrp docker
   ```

### Required GitHub Secrets:
Add the following secrets under **Repository Settings > Secrets and variables > Actions > Repository secrets**:

| Secret Name | Description | Example |
|---|---|---|
| `EC2_HOST` | Public IP or DNS of the EC2 instance | `54.210.xx.xx` or `ec2-xx.compute-1.amazonaws.com` |
| `EC2_USER` | SSH username for the instance | `ubuntu` (Ubuntu) or `ec2-user` (Amazon Linux) |
| `EC2_SSH_KEY` | Full private SSH key (`.pem` content) | `-----BEGIN RSA PRIVATE KEY----- ... -----END RSA PRIVATE KEY-----` |
| `EC2_PORT` | *(Optional)* SSH Port (defaults to `22`) | `22` |
| `APP_DIR` | *(Optional)* App directory on EC2 (defaults to `~/RFQ_Summary`) | `/home/ubuntu/RFQ_Summary` |

