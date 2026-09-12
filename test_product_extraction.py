"""
test_product_extraction.py
──────────────────────────
Tests the product-extraction pipeline (prompt v3) without hitting Claude or Glide:

  1. NDJSON parses into typed products and separate query objects
  2. lenient parsing survives fences, pretty-printed objects and junk lines
  3. rows with no name never reach the product table
  4. the prompt rules that are checked in code produce warnings, not silence
  5. the Glide payload carries the right column ids, types and defaults
  6. query rows link to the Row IDs Glide returns for their products

Run:
    python test_product_extraction.py
"""

import json
import sys

sys.path.insert(0, "src")

import httpx

from rfq_summary.config import Settings
from rfq_summary.glide_client import glide_add_product_rows, glide_add_query_rows
from rfq_summary.product_extraction import parse_product_extraction
from rfq_summary.schema import ExtractedProduct, ExtractedQuery

DETAILS = (
    "Specification:\n`M10 X 1.5 X 25MM HEX GR 8.8`\nHexagon head cap screw, fully threaded.\n"
    "Carbon or alloy steel, property class 8.8.\n<br>\nScope:\n"
    "Manufacture, heat treatment, coating, inspection, certification.\n<br>\n"
    "Application:\n\\--\n<br>\n"
    "Additional note:\nQuote each tier separately."
)

INTERNAL = (
    "**Sourcing:** Cold heading + thread rolling; **Cr(VI)-free** passivation line.\n\n"
    "**Applicable standards:** `ISO 4017:2022` — dimensions (attached).\n\n"
    "**Attachments:** ISO 4017 from the enquiry; the MTL5102 coating standard.\n\n"
    "**Assumptions:** MTL5102A treated as applicable at <mark>class 8.8, its upper limit</mark>.\n\n"
    "**Context:** Price-conscious, competing on volume."
)

NDJSON = "\n".join(
    [
        json.dumps(
            {
                "type": "rfq_header",
                "project": "Project Falcon",
                "rfq_title": "Fasteners and washers",
                "line_count_expected": 3,
                "line_count_extracted": 3,
                "reconciliation": "3 items in the email, all parsed",
                "common_conditions": "Certificates with every shipment, all lines.",
            }
        ),
        json.dumps(
            {
                "type": "product",
                "index": 1,
                "source_ref": "Item 1",
                "name": "Hex Cap Screw M10 x 25 — 8.8",
                "structure": "single",
                "variant_count": None,
                "quantity": "160,000 / 325,000 / 650,000 pcs",
                "quantity_basis": "price_breaks",
                "details": DETAILS,
                "internal_notes": INTERNAL,
                "target_price": None,
                "dwg_link": "https://example.com/dwg/iso4017.pdf",
                "rep_url": None,
                "addl_files": [],
                "annexure": None,
                "provenance": {"name": "derived", "application": "unknown"},
            }
        ),
        # Family line, with its own line-specific query following it.
        json.dumps(
            {
                "type": "product",
                "index": 2,
                "source_ref": "Items 2-15",
                "name": "Flat Washers — 14 sizes (family)",
                "structure": "family",
                "variant_count": 14,
                "quantity": "As per annexure",
                "quantity_basis": "annual",
                "details": DETAILS,
                "internal_notes": INTERNAL,
                "target_price": "$2.68 - FOB India",
                "dwg_link": None,
                "rep_url": "https://example.com/catalogue",
                "addl_files": ["https://example.com/a.jpg", "https://example.com/b.jpg"],
                "annexure": {"required": True, "by_reference": True, "suggested_filename": "sizes.xlsx"},
                "provenance": {"name": "verbatim", "target_price": "verbatim"},
            }
        ),
        json.dumps(
            {
                "type": "query",
                "query_ref": "Q1",
                "product_ref": [2],
                "query_type": "Customer",
                "section": "specification",
                "description": "DIN 125 offers 140 HV and 200 HV. We would suggest 140 HV against class 8.8 bolts — please confirm.",
                "photo": [],
            }
        ),
        # Pretty-printed product spanning several lines, quantity as a bare number.
        json.dumps(
            {
                "type": "product",
                "index": 3,
                "source_ref": "email body",
                "name": "Sodium Hypochlorite System",
                "structure": "system",
                "quantity": 4,
                "details": DETAILS,
                "internal_notes": "**Sourcing:** Process-skid fabricator.",
            },
            indent=2,
        ),
        # Scratch row: no name, must never be emitted.
        json.dumps({"type": "product", "index": 4, "name": "", "details": ""}),
        # RFQ-level query covering the Application placeholder on every line.
        json.dumps(
            {
                "type": "query",
                "query_ref": "Q2",
                "product_ref": None,
                "query_type": "Customer",
                "section": "application",
                "description": "What is the end application for these parts? It lets us propose equivalents where they would save cost.",
                "photo": [],
            }
        ),
        json.dumps(
            {
                "type": "rfq_summary",
                "placeholder_count": 3,
                "query_count": 2,
                "notes_for_reviewer": "Currency and incoterm still open.",
            }
        ),
    ]
)

FENCED = "Here is the output:\n```json\n" + NDJSON + "\n```\n"


def _check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"{'PASS' if condition else 'FAIL'}  {label}" + (f" — {detail}" if detail and not condition else ""))
    return bool(condition)


class _StubHttp:
    """Stands in for httpx.Client, capturing payloads and replaying Row IDs."""

    def __init__(self, row_ids=None):
        self.sent = []
        self._row_ids = row_ids or []
        self._cursor = 0

    def install(self):
        self._real = httpx.Client
        stub = self

        class _Resp:
            def __init__(self, body):
                self._body = body

            def raise_for_status(self):
                pass

            def json(self):
                return self._body

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, json=None):
                stub.sent.append(json)
                n = len(json.get("mutations", []))
                body = [{"Row ID": rid} for rid in stub._row_ids[stub._cursor : stub._cursor + n]]
                stub._cursor += n
                return _Resp(body)

        httpx.Client = _Client
        return self

    def restore(self):
        httpx.Client = self._real


def test_parse() -> bool:
    ok = True
    for label, text in (("plain NDJSON", NDJSON), ("fenced + prose", FENCED)):
        r = parse_product_extraction(text)

        ok &= _check(f"[{label}] no parse errors", not r.parse_errors, str(r.parse_errors))
        ok &= _check(f"[{label}] header uses project, not customer", r.header.project == "Project Falcon")
        ok &= _check(f"[{label}] common_conditions captured", "every shipment" in r.header.common_conditions)
        ok &= _check(f"[{label}] 3 products emitted", len(r.products) == 3, f"got {len(r.products)}")
        ok &= _check(f"[{label}] 2 queries emitted", len(r.queries) == 2, f"got {len(r.queries)}")
        ok &= _check(f"[{label}] unnamed row skipped", len(r.skipped_products) == 1)
        ok &= _check(f"[{label}] counts reconcile", r.reconciliation_note() == "", r.reconciliation_note())

        screw, family, system = r.products
        ok &= _check(f"[{label}] order preserved", [p.index for p in r.products] == [1, 2, 3])
        ok &= _check(f"[{label}] qty is a plain string", screw.quantity == "160,000 / 325,000 / 650,000 pcs")
        ok &= _check(f"[{label}] quantity_basis kept", screw.quantity_basis == "price_breaks")
        ok &= _check(f"[{label}] numeric qty coerced", system.quantity == "4", system.quantity)
        ok &= _check(f"[{label}] internal notes captured", "**Sourcing:**" in screw.internal_notes)
        ok &= _check(f"[{label}] target price verbatim", family.target_price == "$2.68 - FOB India")
        ok &= _check(f"[{label}] absent target price is None", screw.target_price is None)
        ok &= _check(f"[{label}] annexure by reference", bool(family.annexure and family.annexure.by_reference))
        ok &= _check(f"[{label}] placeholder counted", screw.placeholder_count() == 1)

        ok &= _check(f"[{label}] line query linked to its line", r.queries_for(2)[0].query_ref == "Q1")
        ok &= _check(f"[{label}] query reads as the team would ask it", r.queries_for(2)[0].description.startswith("DIN 125 offers"))
        ok &= _check(f"[{label}] rfq-level query has no product_ref", r.rfq_level_queries()[0].query_ref == "Q2")
        ok &= _check(f"[{label}] no validation warnings", not r.validation_warnings, str(r.validation_warnings))
    return ok


def test_validations() -> bool:
    """The prompt rules the maintainer notes ask to enforce in code, not prose."""
    bad = "\n".join(
        [
            json.dumps(
                {
                    "type": "product",
                    "index": 1,
                    "name": "M10 x 1.5 x 25mm Hex Head Cap Screw ISO 4017 — Grade 8.8 Steel, MTL5102A",
                    "quantity": "8,000 pcs",
                    "details": "Specification:\n**Summary:** a bold sub-heading\n<br>\nScope:\n\\--",
                    "provenance": {"specification": "verbatim + derived (cross-referenced)"},
                }
            ),
            json.dumps({"type": "query", "query_ref": "Q1", "product_ref": [1], "section": "scope",
                        "description": "Confirm the scope boundary."}),
            json.dumps({"type": "query", "query_ref": "Q2", "product_ref": [1], "section": "scope",
                        "description": "confirm the scope boundary."}),
            json.dumps({"type": "query", "query_ref": "Q3", "product_ref": [9], "section": "quantity",
                        "description": "Is this annual? And what incoterm applies?"}),
            json.dumps({"type": "query", "query_ref": "Q4", "product_ref": [1], "section": "pricing",
                        "description": "What currency?"}),
            json.dumps({"type": "rfq_summary", "placeholder_count": 5, "query_count": 2}),
        ]
    )
    r = parse_product_extraction(bad)
    w = " || ".join(r.validation_warnings)

    ok = _check("over-long name flagged", "max 50" in w, w)
    ok &= _check("provenance phrase flagged", "single token" in w, w)
    ok &= _check("bold sub-heading flagged", "bold sub-heading" in w, w)
    ok &= _check("placeholder count mismatch flagged", "placeholder_count says 5" in w, w)
    ok &= _check("query count mismatch flagged", "query_count says 2" in w, w)
    ok &= _check("duplicate query flagged", "duplicate query text" in w, w)
    ok &= _check("two-questions-in-one flagged", "more than one question" in w, w)
    ok &= _check("unknown section flagged", "unknown section" in w, w)
    ok &= _check("query pointing at a missing line flagged", "not extracted" in w, w)

    # A response is the customer's to give; never accept one from the model.
    q = ExtractedQuery.model_validate({"description": "x?", "Query Response": "B1", "response": "B1"})
    ok &= _check("model-supplied response discarded", not hasattr(q, "response"))
    return ok


def test_banned_queries() -> bool:
    """§1.2 — four kinds of question that must never reach a customer."""
    banned = [
        ("a file we could not open", "One of the attached files would not open at our end — could you resend the drawing?"),
        ("an unreadable file", "The PDF appears corrupt — please share the file again."),
        ("our own tracking", "Could you share your project reference number for our internal records?"),
        ("the supplier model", "Which hardness class applies, so our supplier can quote correctly?"),
        ("a project name", "No project name was provided. Could you confirm the project or programme name?"),
        ("a fetch failure", "Two of the attached files failed to fetch — could you check and resend them?"),
    ]
    allowed = [
        "MTL5102B has two sub-states: B1 (min 5 um, 480 h salt spray) and B2 (min 8 um, 720 h). Which applies?",
        "DIN 125 offers 140 HV and 200 HV. We would suggest 140 HV against class 8.8 bolts — please confirm.",
        "Is any of these parts used in a safety-critical assembly? It changes the inspection level we build in.",
        "Drawing MT-4471 rev C is referenced but we have rev B. Which revision should we quote against?",
        "The enquiry references ISO 4014 but it was not attached. Could you share a copy?",
        # Real technical questions from the live run — these must survive the filter.
        "The descriptor references VDA235-104 without the .20 suffix used elsewhere. We have treated it as "
        "VDA 235-104.20. Please confirm, or clarify if a different VDA code applies.",
        "What is the end application for these fasteners? Knowing the assembly helps us propose equivalents "
        "where they would save cost, and set the right inspection level if any are safety-critical.",
    ]

    def warnings_for(text):
        nd = "\n".join([
            json.dumps({"type": "product", "index": 1, "name": "Hex Bolt M10", "details": "Specification:\nx\n\\--"}),
            json.dumps({"type": "query", "query_ref": "Q1", "product_ref": [1],
                        "section": "specification", "description": text}),
        ])
        return [w for w in parse_product_extraction(nd).validation_warnings if "query Q1" in w]

    # A commercial section is itself the defect, whatever the wording.
    nd = "\n".join([
        json.dumps({"type": "product", "index": 1, "name": "Hex Bolt M10", "details": "Specification:\nx\n\\--"}),
        json.dumps({"type": "query", "query_ref": "Q1", "product_ref": [1],
                    "section": "commercial", "description": "What are the payment milestones?"}),
    ])
    ok = _check(
        "flags a commercial section",
        any("commercial query" in w for w in parse_product_extraction(nd).validation_warnings),
    )
    for label, text in banned:
        ok &= _check(f"flags {label}", bool(warnings_for(text)), text[:60])
    for text in allowed:
        ok &= _check(f"allows {text[:44]!r}", not warnings_for(text), str(warnings_for(text)))
    return ok


def test_query_typing() -> bool:
    """§1.2 — assumable questions are Team; only the unassumable reach a customer."""
    def one(text, qtype="Customer"):
        nd = "\n".join([
            json.dumps({"type": "product", "index": 1, "name": "Hex Bolt M10",
                        "details": "Specification:\nx\n\\--"}),
            json.dumps({"type": "query", "query_ref": "Q1", "product_ref": [1], "query_type": qtype,
                        "section": "scope", "description": text}),
        ])
        return parse_product_extraction(nd)

    assumable = [
        ("a commercial term", "What currency and incoterm should we quote against?"),
        ("PPAP", "Is PPAP required on these parts, and if so at which level?"),
        ("quantity basis", "Are these quantities annual usage or a one-time requirement?"),
        ("packaging", "What packaging requirement applies to these parts?"),
    ]
    ok = True
    for label, text in assumable:
        r = one(text, "Customer")
        ok &= _check(f"{label} as Customer is flagged",
                     any("typed Customer but asks" in w for w in r.validation_warnings), text[:50])
        r = one(text, "Team")
        ok &= _check(f"{label} as Team is accepted",
                     not any("typed Customer but asks" in w for w in r.validation_warnings),
                     str(r.validation_warnings))

    # Unassumable questions are Customer and pass clean.
    r = one("MTL5102B has two sub-states: B1 (5 um, 480 h) and B2 (8 um, 720 h). Which applies?", "Customer")
    ok &= _check("a real spec question stays Customer", not r.validation_warnings, str(r.validation_warnings))

    # Neither type is capped: the bar is the subject, not the count.
    objs = [{"type": "product", "index": 1, "name": "Part", "details": "Specification:\nx\n\\--"}]
    customer_texts = [
        "The drawing calls out 42CrMo4 but the enquiry line says EN8. Which material governs?",
        "Thread runout is untoleranced on the print. Is a 2 mm undercut acceptable?",
        "Surface finish is shown as Ra 1.6 on one view and Ra 3.2 on another. Which applies?",
        "Should the flange face be machined after heat treatment, or before?",
        "Is a hardness check on every batch required, or is a certificate of conformity enough?",
    ]
    team_texts = [
        "Confirm the incoterm to quote against for this account.",
        "Payment terms are not stated; use the standing account terms.",
        "Packaging assumed as standard export cartons on pallets.",
        "Delivery window taken as eight weeks from order.",
        "Quantities read as a one-time requirement, not annual usage.",
        "Freight assumed ex-works our works; buyer nominates the carrier.",
    ]
    objs += [{"type": "query", "query_ref": f"C{i}", "product_ref": [1], "query_type": "Customer",
              "section": "specification", "description": t} for i, t in enumerate(customer_texts)]
    objs += [{"type": "query", "query_ref": f"T{i}", "product_ref": [1], "query_type": "Team",
              "section": "scope", "description": t} for i, t in enumerate(team_texts)]
    r = parse_product_extraction("\n".join(json.dumps(o) for o in objs))
    ok &= _check("five technical Customer queries not flagged", not r.validation_warnings,
                 str(r.validation_warnings))
    ok &= _check("six Team queries not flagged", len(r.team_queries()) == 6)
    ok &= _check("split counted correctly", len(r.customer_queries()) == 5)
    return ok


def test_query_budget_and_merging() -> bool:
    """§1.2 — no query cap, and one question covering several lines is one row."""
    def build(queries):
        objs = [{"type": "product", "index": i, "name": f"Part {i}",
                 "details": "Specification:\nx\n\\--"} for i in (1, 2, 3, 4)]
        return "\n".join(json.dumps(o) for o in objs + queries)

    same_a = ("The descriptor references both MTL5102A and VDA 235-104.20. We have treated "
              "VDA 235-104.20 as the VDA code for the same coating defined in MTL5102A. Please confirm.")
    same_b = ("The descriptor references VDA235-104 without the .20 suffix. We have treated this "
              "as the same VDA 235-104.20 / MTL5102A coating. Please confirm.")
    other = "DIN 125 Part 1 offers two hardness classes: 140 HV and 200 HV. Should we quote 140 HV?"
    third = "MTL5102B has two sub-states: B1 (min 5 um, 480 h) and B2 (min 8 um, 720 h). Which applies?"

    # The same question split across two lines must be caught even though the wording differs.
    r = parse_product_extraction(build([
        {"type": "query", "query_ref": "Q1", "product_ref": 1, "section": "specification", "description": same_a},
        {"type": "query", "query_ref": "Q8", "product_ref": 4, "section": "specification", "description": same_b},
    ]))
    ok = _check("reworded duplicate caught", any("ask the same thing" in w for w in r.validation_warnings),
                str(r.validation_warnings))
    ok &= _check("merge hint names both lines", any("[1, 4]" in w for w in r.validation_warnings))

    # Genuinely different questions in the same section must not be merged.
    r = parse_product_extraction(build([
        {"type": "query", "query_ref": "Q3", "product_ref": 2, "section": "specification", "description": other},
        {"type": "query", "query_ref": "Q5", "product_ref": 3, "section": "specification", "description": third},
    ]))
    ok &= _check("distinct questions left alone", not any("ask the same thing" in w for w in r.validation_warnings),
                 str(r.validation_warnings))

    # Eight distinct technical questions are fine — there is no count to trip.
    distinct = [
        "The drawing calls out 42CrMo4 but the enquiry line says EN8. Which material governs?",
        "Thread runout is untoleranced on the print. Is a 2 mm undercut acceptable?",
        "Surface finish is shown as Ra 1.6 on one view and Ra 3.2 on another. Which applies?",
        "Should the flange face be machined after heat treatment, or before?",
        "Is a hardness check on every batch required, or is a certificate of conformity enough?",
        "Zinc plating is specified without a thickness class. Confirm 8 um to ISO 4042.",
        "Concentricity between the bore and the outer diameter is unstated. Is 0.05 mm TIR workable?",
        "Two revisions of print 4471 are attached, C and D. Which revision governs?",
    ]
    eight = [{"type": "query", "query_ref": f"Q{i}", "product_ref": [i % 4 + 1], "section": "standards",
              "description": t} for i, t in enumerate(distinct)]
    r = parse_product_extraction(build(eight))
    ok &= _check("eight technical questions not flagged", not r.validation_warnings, str(r.validation_warnings))

    # One row may carry several lines.
    r = parse_product_extraction(build([
        {"type": "query", "query_ref": "Q1", "product_ref": [1, 4], "section": "specification", "description": same_a},
        {"type": "query", "query_ref": "Q3", "product_ref": [2], "section": "specification", "description": other},
        {"type": "query", "query_ref": "Q5", "product_ref": [3], "section": "specification", "description": third},
    ]))
    ok &= _check("merged run is clean", not r.validation_warnings, str(r.validation_warnings))
    ok &= _check("merged query covers both lines", r.queries[0].product_refs == [1, 4])
    ok &= _check("queries_for finds a merged query", r.queries_for(4)[0].query_ref == "Q1")
    return ok


def test_empty_reply_is_explained() -> bool:
    """A 200 with thinking blocks and no text is not a silent zero.

    Thinking and output share one max_tokens budget. On a big extraction the
    model can spend the whole budget reasoning and stop before writing any
    answer. That cost a 214-second production run reported only as
    'empty model output'.
    """
    from rfq_summary.llm import describe_empty_reply

    class Resp:
        def __init__(self, content, meta=None):
            self.content = content
            self.response_metadata = meta or {}

    # The exact production shape.
    r = Resp([{"type": "thinking", "thinking": "working through the lines..."}],
             {"stop_reason": "max_tokens"})
    why = describe_empty_reply(r)
    ok = _check("budget exhaustion named", "whole max_tokens budget" in why, why)
    ok &= _check("stop_reason surfaced", "max_tokens" in why, why)
    ok &= _check("the remedy is stated", "thinking off" in why, why)

    # Thinking present but a different stop reason is a different story.
    why = describe_empty_reply(Resp([{"type": "thinking", "thinking": "x"}], {"stop_reason": "end_turn"}))
    ok &= _check("non-budget case distinguished", "whole max_tokens" not in why and "thinking block" in why, why)

    # Other block types are named rather than lumped in.
    why = describe_empty_reply(Resp([{"type": "tool_use", "name": "x"}], {"stop_reason": "tool_use"}))
    ok &= _check("tool_use named", "tool_use" in why, why)

    # Genuinely empty, and missing metadata, both stay readable.
    ok &= _check("empty content handled", "nothing at all" in describe_empty_reply(Resp([], {"stop_reason": "end_turn"})))
    ok &= _check("absent metadata handled", "unknown" in describe_empty_reply(Resp([])))
    ok &= _check("string content handled", bool(describe_empty_reply(Resp(""))))

    # Object-style blocks count too.
    class Block:
        def __init__(self, t): self.type = t
    why = describe_empty_reply(Resp([Block("thinking")], {"stop_reason": "max_tokens"}))
    ok &= _check("object blocks counted", "whole max_tokens budget" in why, why)
    return ok


def test_budget_fits_thinking_plus_answer() -> bool:
    """Thinking and output share max_tokens, so the budget has to hold both.

    A production run spent 214s thinking, hit the 16000 cap, and returned no
    answer at all. Thinking is worth keeping on this task, so the fix is a
    bigger budget plus streaming, not switching the reasoning off.
    """
    import inspect
    from pathlib import Path
    from rfq_summary import llm
    from rfq_summary.config import Settings

    s = Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="app")
    ok = _check("budget has real headroom", s.product_extraction_max_tokens >= 64000,
                str(s.product_extraction_max_tokens))
    ok &= _check("large budgets stream", s.product_extraction_max_tokens > llm.STREAM_ABOVE_TOKENS)
    ok &= _check("the wait outlasts a long generation", s.product_extraction_timeout_sec >= 420,
                 str(s.product_extraction_timeout_sec))
    ok &= _check("the job kill is looser than the wait",
                 s.job_timeout_sec > s.product_extraction_timeout_sec,
                 f"job={s.job_timeout_sec} wait={s.product_extraction_timeout_sec}")

    # Thinking stays on by default; the override exists for callers who need it.
    sig = inspect.signature(llm.generate_text)
    ok &= _check("generate_text takes a thinking override", "thinking" in sig.parameters)
    ok &= _check("it defaults to following the setting", sig.parameters["thinking"].default is None)
    ok &= _check("adaptive thinking is on by default", s.anthropic_adaptive_thinking is True)

    # Read task.py as text: importing it needs fitz, which is not installed here.
    src = Path("src/rfq_summary/task.py").read_text(encoding="utf-8")
    idx = src.find("settings.product_extraction_max_tokens")
    ok &= _check("product extraction call found", idx > 0)
    ok &= _check("product extraction does NOT disable thinking",
                 "False," not in src[idx:idx + 200], src[idx:idx + 200])
    return ok


def test_usage_is_reported() -> bool:
    """Log what the call actually spent, instead of inferring it from output length.

    output_tokens covers thinking AND the answer, so this is the line that says
    whether thinking crowded the answer out of the budget.
    """
    import io, contextlib
    from rfq_summary import llm
    from rfq_summary.config import Settings

    class Resp:
        def __init__(self, meta): self.response_metadata = meta

    def log(meta, budget=64000):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            llm._log_usage("claude-opus-4-6", Resp(meta), budget)
        return buf.getvalue()

    out = log({"usage": {"input_tokens": 18000, "output_tokens": 64000}, "stop_reason": "max_tokens"})
    ok = _check("cap hit is called out", "HIT THE CAP" in out, out.strip())
    ok &= _check("the split is shown", "64000/64000" in out, out.strip())
    ok &= _check("thinking is named as included", "thinking included" in out, out.strip())

    out = log({"usage": {"input_tokens": 18000, "output_tokens": 9500}, "stop_reason": "end_turn"})
    ok &= _check("a healthy call is not flagged", "HIT THE CAP" not in out, out.strip())
    ok &= _check("percentage of budget shown", "15%" in out, out.strip())

    ok &= _check("absent usage survives", "no usage reported" in log({"stop_reason": "end_turn"}))
    ok &= _check("absent metadata survives", bool(log(None)))

    st = Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="app")
    ok &= _check("effort defaults to the API default", st.anthropic_effort == "")
    ok &= _check("effort is configurable",
                 Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="app",
                          ANTHROPIC_EFFORT="medium").anthropic_effort == "medium")
    return ok


def test_complete_lines_survive_truncation() -> bool:
    """A cap hit partway through must not throw away the lines already finished."""
    def line(i):
        return json.dumps({"type": "product", "index": i, "name": f"Part {i}",
                           "details": "Specification:\nx", "quantity": "10 pcs"})

    nd = "\n".join([json.dumps({"type": "rfq_header", "line_count_expected": 5}),
                    line(1), line(2),
                    '{"type":"product","index":3,"name":"Part 3","details":"Spec'])
    r = parse_product_extraction(nd)
    ok = _check("finished lines are kept", [p.index for p in r.products] == [1, 2],
                str([p.index for p in r.products]))
    ok &= _check("the cut-off is reported", any("stops mid-object" in e for e in r.parse_errors))
    ok &= _check("the lost line is named", any("Part 3" in e for e in r.parse_errors))

    # When the FIRST product is the one cut off there is nothing to salvage.
    # That is the production case, and zero products is the correct answer.
    r2 = parse_product_extraction("\n".join([
        json.dumps({"type": "rfq_header", "line_count_expected": 3}),
        '{"type":"product","index":1,"name":"Drilled Tubesheet","addl_files":[],']))
    ok &= _check("nothing salvageable yields nothing", len(r2.products) == 0)
    ok &= _check("and points at the stop reason", any("stop=max_tokens" in e for e in r2.parse_errors),
                 str(r2.parse_errors))
    return ok


def test_response_text_handles_thinking_blocks() -> bool:
    """With thinking on, LangChain returns a LIST of blocks, not a string.

    Calling .strip() on that list raised AttributeError and killed every task
    in production — the model had answered fine, we just could not read it.
    """
    from rfq_summary.llm import response_text as f

    ndjson = '{"type":"product","index":1,"name":"Hex Bolt"}'
    # The exact shape that crashed: thinking block first, answer after.
    thinking_then_text = [
        {"type": "thinking", "thinking": "Let me work through the line items...",
         "signature": "sig"},
        {"type": "text", "text": ndjson},
    ]

    class _Block:
        def __init__(self, type_, text=None):
            self.type, self.text = type_, text

    cases = [
        ("plain string still works", "hello  ", "hello"),
        ("thinking + text", thinking_then_text, ndjson),
        ("text-only list", [{"type": "text", "text": "abc"}], "abc"),
        ("several text blocks join", [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], "ab"),
        ("thinking alone yields nothing", [{"type": "thinking", "thinking": "x"}], ""),
        ("list of bare strings", ["a", "b"], "ab"),
        ("None", None, ""),
        ("empty list", [], ""),
        ("tool_use ignored", [{"type": "tool_use", "name": "x"}, {"type": "text", "text": "ok"}], "ok"),
        ("object-style blocks", [_Block("thinking"), _Block("text", "ok")], "ok"),
        ("malformed block skipped", [{"type": "text"}, {"type": "text", "text": "ok"}], "ok"),
    ]
    ok = True
    for label, given, want in cases:
        got = f(given)
        ok &= _check(label, got == want, f"got {got!r}, want {want!r}")

    # Reasoning must never reach the NDJSON parser.
    ok &= _check("thinking text never leaks into the answer",
                 "work through" not in f(thinking_then_text))

    # And the result is parseable, which is the whole point.
    ok &= _check("the recovered text parses",
                 len(parse_product_extraction(f(thinking_then_text)).products) == 1)
    return ok


def test_adaptive_thinking_gating() -> bool:
    """Adaptive thinking goes only to models that accept it — the rest 400 on it."""
    from rfq_summary.llm import _supports_adaptive_thinking as ok_for

    accepts = ["claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
               "claude-sonnet-5", "claude-sonnet-4-6", "claude-fable-5-1"]
    rejects = ["claude-haiku-4-5", "claude-haiku-4-5-20251001", "claude-3-haiku-20240307",
               "claude-sonnet-4-5", "claude-3-5-sonnet-20241022", ""]

    ok = True
    for m in accepts:
        ok &= _check(f"{m} gets adaptive thinking", ok_for(m))
    for m in rejects:
        ok &= _check(f"{m or '<empty>'} does not", not ok_for(m))
    return ok


def test_internal_notes_formatting() -> bool:
    """§5.4 / hard rule 12f — bold block labels, one blank line between topics."""
    def one(notes):
        nd = json.dumps({"type": "product", "index": 1, "name": "Hex Bolt M10",
                         "details": "Specification:\nx", "internal_notes": notes})
        return parse_product_extraction(nd)

    formatted = ("**Sourcing:** Cold heading + thread rolling.\n\n"
                 "**Applicable standards:** `ISO 4017:2022` — dimensions (attached).\n\n"
                 "**Attachments:** the MTL5102 coating standard.\n\n"
                 "**Assumptions:** Class 8.8 treated as the limit.\n\n"
                 "**Context:** Priority lead.")
    r = one(formatted)
    ok = _check("well-formatted notes pass clean", not r.validation_warnings, str(r.validation_warnings))

    # Blocks run together: the second one has no blank line above it.
    r = one("**Sourcing:** Cold heading.\n**Assumptions:** Class 8.8 assumed.")
    ok &= _check("run-together block flagged",
                 any("runs into the block above" in w for w in r.validation_warnings),
                 str(r.validation_warnings))
    ok &= _check("the flagged block is named",
                 any("'Assumptions'" in w for w in r.validation_warnings), str(r.validation_warnings))
    ok &= _check("the block above is not flagged",
                 not any("'Sourcing'" in w for w in r.validation_warnings), str(r.validation_warnings))

    # Plain labels are the old format — every one is flagged, none silently rewritten.
    r = one("Sourcing: Cold heading.\n\nAssumptions: Class 8.8 assumed.")
    ok &= _check("unbolded labels flagged", len([w for w in r.validation_warnings
                                                 if "label is not bold" in w]) == 2,
                 str(r.validation_warnings))

    # A label word inside a sentence is not a block heading.
    r = one("**Sourcing:** We read the Attachments: list in the thread.\n\n**Context:** Priority.")
    ok &= _check("mid-sentence label ignored", not r.validation_warnings, str(r.validation_warnings))

    # A single block needs no separator, and empty notes are not a defect.
    ok &= _check("single block passes", not one("**Sourcing:** Cold heading.").validation_warnings)
    ok &= _check("empty notes pass", not one("").validation_warnings)

    # Warnings only — the note reaches Glide exactly as the model wrote it.
    bad = "**Sourcing:** Cold heading.\n**Assumptions:** Class 8.8 assumed."
    ok &= _check("notes never rewritten", one(bad).products[0].internal_notes == bad)
    return ok


def test_truncation_names_the_lost_line() -> bool:
    """A real 9-line RFQ truncated mid-way through the ninth product."""
    objs = [{"type": "rfq_header", "project": "P", "line_count_expected": 9}]
    objs += [{"type": "product", "index": i, "name": f"Line {i}", "details": "Specification:\nx"}
             for i in range(1, 9)]
    text = "\n".join(json.dumps(o) for o in objs)
    text += ('\n{"type":"product","index":9,"source_ref":"M080726 / Supports",'
             '"name":"Pipe Supports — Phase 2.5 BoP (family)","structure":"family",'
             '"variant_count":42,"quantity"')

    r = parse_product_extraction(text)
    truncation = [e for e in r.parse_errors if "stops mid-object" in e]

    ok = _check("eight complete lines still parse", len(r.products) == 8, str(len(r.products)))
    ok &= _check("the cut-off is reported", bool(truncation), str(r.parse_errors))
    ok &= _check("the lost line is named", "line 9" in (truncation[0] if truncation else ""))
    ok &= _check("its name is named", "Pipe Supports" in (truncation[0] if truncation else ""))
    msg = truncation[0] if truncation else ""
    ok &= _check("no unproven cause is asserted",
                 "Cause is not determined here" in msg and "truncated at the token cap" not in msg, msg[:120])
    ok &= _check("both causes are offered", "stop=max_tokens" in msg and "cut short in transit" in msg, msg[:160])
    ok &= _check("count mismatch surfaces too",
                 r.reconciliation_note() == "line_count_expected=9 but 8 product line(s) parsed")
    # A half-specified row is worse than a missing one: nothing partial is emitted.
    ok &= _check("no partial row emitted", all(p.index != 9 for p in r.products))

    # Ordinary junk is not mistaken for truncation.
    r2 = parse_product_extraction("I could not find any products in this email.")
    ok &= _check("prose is not called truncation", not [e for e in r2.parse_errors if "truncated" in e])
    return ok


def test_mismatch_is_reported() -> bool:
    text = "\n".join(
        [
            json.dumps({"type": "rfq_header", "line_count_expected": 5}),
            json.dumps({"type": "product", "index": 1, "name": "Hex Bolt M10 x 120", "details": DETAILS}),
        ]
    )
    r = parse_product_extraction(text)
    return _check(
        "count mismatch surfaced",
        r.reconciliation_note() == "line_count_expected=5 but 1 product line(s) parsed",
        r.reconciliation_note(),
    )


def test_garbage_is_not_fatal() -> bool:
    r = parse_product_extraction("I could not find any products in this email.")
    ok = _check("prose-only output yields no products", not r.products)
    ok &= _check("prose-only output records an error", bool(r.parse_errors))
    ok &= _check("empty output yields no products", not parse_product_extraction("").products)
    return ok


def test_glide_payload() -> bool:
    r = parse_product_extraction(NDJSON)
    stub = _StubHttp(row_ids=["ROW_A", "ROW_B", "ROW_C"]).install()
    try:
        settings = Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="app")
        row_ids = glide_add_product_rows(settings, "ALL_RFQ_ROW", r.products)

        ok = _check("row ids returned per product", row_ids == ["ROW_A", "ROW_B", "ROW_C"], str(row_ids))
        muts = stub.sent[0]["mutations"]
        ok &= _check(
            "targets the ALL Product table",
            muts[0]["tableName"] == "native-table-4c42a6c4-6b7c-476f-88a8-65c0e8d3c774",
        )
        first, second = muts[0]["columnValues"], muts[1]["columnValues"]
        ok &= _check("name -> Name", first["Name"] == "Hex Cap Screw M10 x 25 — 8.8")
        ok &= _check("qty -> KAbSp verbatim", first["KAbSp"] == "160,000 / 325,000 / 650,000 pcs", first["KAbSp"])
        ok &= _check("details -> K03pz", first["K03pz"] == DETAILS)
        ok &= _check("rfq id -> 3E2xY", first["3E2xY"] == "ALL_RFQ_ROW")
        ok &= _check("target price -> hgVgd", second["hgVgd"] == "$2.68 - FOB India")
        # Link fields are the team's to fill in the app — the model never writes them.
        for col, label in (("f4QCb", "dwg link"), ("LXcW2", "rep url"), ("JR0Lx", "addl files")):
            ok &= _check(f"{label} left for the team",
                         col not in first and col not in second, f"{col} was written")
        ok &= _check("accepted -> 117zS is JSON true", first["117zS"] is True)
        ok &= _check("srNo -> XbErc is a number", first["XbErc"] == 1 and second["XbErc"] == 2)
        ok &= _check("absent target price omitted", "hgVgd" not in first)
        ok &= _check("internal notes -> vizbU", first["vizbU"] == INTERNAL, first.get("vizbU", "(missing)"))
        ok &= _check("no stray columns", "" not in first)

        # An RFQ row id is mandatory: unlinked rows are orphans in a live table.
        try:
            glide_add_product_rows(settings, "   ", r.products[:1])
            ok &= _check("missing rfq id refused", False, "no error raised")
        except RuntimeError as e:
            ok &= _check("missing rfq id refused", "refusing to add unlinked" in str(e), str(e))

        # An explicitly emptied column id is left alone.
        stub.sent.clear()
        bare = Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="app", GLIDE_COL_PRODUCT_SR_NO="",
                        GLIDE_COL_PRODUCT_ACCEPTED="")
        glide_add_product_rows(bare, "ALL_RFQ_ROW", r.products[:1])
        cleared = stub.sent[0]["mutations"][0]["columnValues"]
        ok &= _check("emptied column ids are skipped", "XbErc" not in cleared and "117zS" not in cleared)
        return ok
    finally:
        stub.restore()


def test_query_rows_link_to_products() -> bool:
    r = parse_product_extraction(NDJSON)
    stub = _StubHttp(row_ids=["ROW_A", "ROW_B", "ROW_C"]).install()
    try:
        settings = Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="app")
        row_ids = glide_add_product_rows(settings, "ALL_RFQ_ROW", r.products)
        resolved = {p.index: rid for p, rid in zip(r.products, row_ids) if rid}

        stub.sent.clear()
        written = glide_add_query_rows(settings, "ALL_RFQ_ROW", r.queries, resolved)

        ok = _check("both queries written", written == 2, str(written))
        muts = stub.sent[0]["mutations"]
        ok &= _check(
            "targets the queries table",
            muts[0]["tableName"] == "native-table-19b47480-d912-462e-8721-584b5063f704",
        )
        line_q, rfq_q = muts[0]["columnValues"], muts[1]["columnValues"]
        ok &= _check("description -> Ucd5N", line_q["Ucd5N"].startswith("DIN 125 offers"))
        ok &= _check("rfq id -> Name", line_q["Name"] == "ALL_RFQ_ROW")
        # Line 2's product row came back as ROW_B, so its query must point there.
        ok &= _check("product id -> pfIJe from the returned row id", line_q["pfIJe"] == "ROW_B", str(line_q))
        ok &= _check("rfq-level query carries no product id", "pfIJe" not in rfq_q)
        ok &= _check("query id never written", "OMn91" not in line_q)
        ok &= _check("query response never written", "YoqlH" not in line_q)

        # When Glide returns no row ids, the question is still asked — against the RFQ.
        stub.sent.clear()
        glide_add_query_rows(settings, "ALL_RFQ_ROW", r.queries, {})
        degraded = stub.sent[0]["mutations"][0]["columnValues"]
        ok &= _check("unresolved product id degrades to RFQ-only", "pfIJe" not in degraded and degraded["Name"] == "ALL_RFQ_ROW")

        ok &= _check("query type -> W6l3l", line_q["W6l3l"] == "Customer", str(line_q.get("W6l3l")))

        # A Team query never reaches the customer but is still recorded.
        stub.sent.clear()
        team = ExtractedQuery.model_validate(
            {"description": "Packaging not specified; quoted as standard export packaging.",
             "product_ref": [1], "query_type": "Team"})
        glide_add_query_rows(settings, "ALL_RFQ_ROW", [team], {1: "ROW_A"})
        ok &= _check("team query typed Team", stub.sent[0]["mutations"][0]["columnValues"]["W6l3l"] == "Team")

        # Query Photo is off by default; a photo the model volunteers is not written.
        stub.sent.clear()
        q = ExtractedQuery.model_validate(
            {"description": "Which of these two revisions applies?", "product_ref": [1],
             "photo": ["https://example.com/rev-a.png"]}
        )
        glide_add_query_rows(settings, "ALL_RFQ_ROW", [q], {1: "ROW_A"})
        photo_row = stub.sent[0]["mutations"][0]["columnValues"]
        ok &= _check("photo column off by default", "KbO6i" not in photo_row, str(photo_row))

        # ...but still writeable if the column is switched on later.
        stub.sent.clear()
        with_photo = Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="app", GLIDE_COL_QUERY_PHOTO="KbO6i")
        glide_add_query_rows(with_photo, "ALL_RFQ_ROW", [q], {1: "ROW_A"})
        ok &= _check(
            "photo -> KbO6i when enabled",
            stub.sent[0]["mutations"][0]["columnValues"]["KbO6i"] == "https://example.com/rev-a.png",
        )
        return ok
    finally:
        stub.restore()


if __name__ == "__main__":
    passed = all(
        [
            test_parse(),
            test_validations(),
            test_banned_queries(),
            test_query_typing(),
            test_query_budget_and_merging(),
            test_internal_notes_formatting(),
            test_adaptive_thinking_gating(),
            test_response_text_handles_thinking_blocks(),
            test_empty_reply_is_explained(),
            test_budget_fits_thinking_plus_answer(),
            test_usage_is_reported(),
            test_complete_lines_survive_truncation(),
            test_mismatch_is_reported(),
            test_garbage_is_not_fatal(),
            test_truncation_names_the_lost_line(),
            test_glide_payload(),
            test_query_rows_link_to_products(),
        ]
    )
    print("\nALL PASSED" if passed else "\nFAILURES ABOVE")
    sys.exit(0 if passed else 1)
