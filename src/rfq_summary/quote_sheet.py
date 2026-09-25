"""
The supplier quotation sheet for a product family.

One family, one workbook: every SKU a row, the attributes that vary as
columns, and an empty block the supplier prices into.

Two things here are worth knowing before changing anything.

**The variance rule is enforced in code, not asked for in a prompt.** A spec
column whose value is identical on every row is not a column — it is one line
in the conditions block. Three hundred rows repeating "DIN 976" teaches a
reader to stop reading, and the rule that prevents it cannot be left to a
model's judgement if it is to hold every time.

**Rows come from the customer's own workbook when there is one.** Asking a
model to retype 250 SKUs costs 40k+ output tokens, sits on the extraction
token ceiling, and silently mistypes part numbers. Reading their sheet and
mapping its columns costs nothing and cannot mistype. `read_variant_table`
is deliberately NOT bounded by MAX_EXCEL_ROWS: that cap exists to bound what
reaches the prompt, and nothing here reaches a prompt.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

# Palette shared with the change-notification mail, so the two artefacts a
# customer-facing team sees read as one system.
NAVY = "4A6178"
NAVY_DK = "33485C"
AMBER_DK = "8A6400"
AMBER_LT = "FFF3B0"
RULE = "D5DAE1"
BAND = "F4F6F8"
FONT = "Arial"

# Never collapsed into the conditions block even when every row agrees: these
# name the item, and a sheet whose rows cannot be told apart is not a sheet.
DEFAULT_PROTECTED = ("Item code", "Description")

# OneDrive/SharePoint reject these outright, and a family name routinely
# carries several of them — "Studs — M56 x 2000 / DIN 976".
_ILLEGAL_FILENAME = re.compile(r'[":<>?/\\|*\x00-\x1f]')


def annexure_filename(index: int, family_name: str, extension: str = "xlsx") -> str:
    """
    The name a reader refers to the file by: `Annexure 2 - Hex Bolts.xlsx`.

    `index` counts the annexures of one RFQ, not the product lines — a family
    sitting on line 5 of an RFQ whose only annexure it is should be Annexure
    1, because that is how someone will cite it.
    """
    name = _ILLEGAL_FILENAME.sub(" ", family_name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    # Long names break some mail clients and every path limit; the annexure
    # number carries the identity when the name has to give.
    if len(name) > 80:
        name = name[:80].rstrip(" .")
    stem = f"Annexure {index} - {name}" if name else f"Annexure {index}"
    return f"{stem}.{extension}"


@dataclass(frozen=True)
class QuoteColumn:
    """A column the supplier fills in, not us."""
    name: str
    width: int = 12
    kind: str = "number"           # number | text | choice
    number_format: Optional[str] = None
    choices: Tuple[str, ...] = ()


DEFAULT_QUOTE_COLUMNS: Tuple[QuoteColumn, ...] = (
    QuoteColumn("Price basis", 13, "choice", choices=("per pc", "per kg", "per 1000 pcs")),
    QuoteColumn("Weight / pc\n(kg)", 11, "number", "0.000"),
    QuoteColumn("Unit price\n(USD)", 11, "number", "$#,##0.000"),
    QuoteColumn("Lead time\n(weeks)", 10, "number", "0"),
    QuoteColumn("MOQ\n(pcs)", 10, "number", "#,##0"),
)


@dataclass
class VariantTable:
    """A table lifted out of the customer's own workbook, untruncated."""
    sheet: str
    header: List[str]
    rows: List[List[str]] = field(default_factory=list)
    header_row: int = 1

    def as_dicts(self) -> List[Dict[str, str]]:
        return [
            {h: (row[i] if i < len(row) else "") for i, h in enumerate(self.header) if h}
            for row in self.rows
        ]


# ---------------------------------------------------------------------------
# Reading the customer's workbook
# ---------------------------------------------------------------------------

def _cell_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _looks_like_header(row: Sequence[str]) -> bool:
    filled = [c for c in row if c]
    if len(filled) < 2:
        return False
    short = sum(1 for c in filled if len(c) <= 30)
    return short >= max(2, int(0.6 * len(filled)))


def read_variant_table(
    data: bytes,
    sheet: Optional[str] = None,
    header_row: Optional[int] = None,
) -> Optional[VariantTable]:
    """
    Pull the largest table out of a customer workbook, in full.

    No row cap: a 300-line BOM is read as 300 lines. Returns None when the
    workbook holds nothing table-shaped, which is a normal answer — the
    caller falls back to model-extracted rows.
    """
    wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    try:
        best: Optional[VariantTable] = None
        for ws in wb.worksheets:
            if sheet and ws.title != sheet:
                continue
            matrix = [[_cell_text(c) for c in row] for row in ws.iter_rows(values_only=True)]
            if not matrix:
                continue

            start = (header_row - 1) if header_row else None
            if start is None:
                for i, row in enumerate(matrix):
                    if _looks_like_header(row):
                        start = i
                        break
            if start is None or start >= len(matrix):
                continue

            header = [h for h in matrix[start]]
            while header and not header[-1]:
                header.pop()
            if not header:
                continue

            body: List[List[str]] = []
            for row in matrix[start + 1:]:
                if not any(row):
                    # A single blank line ends a table; the rest of the sheet
                    # is usually notes or a second unrelated block.
                    break
                trimmed = list(row[: len(header)])
                body.append(trimmed + [""] * (len(header) - len(trimmed)))

            if body and (best is None or len(body) > len(best.rows)):
                best = VariantTable(sheet=ws.title, header=header, rows=body, header_row=start + 1)
        return best
    finally:
        try:
            wb.close()
        except Exception:
            pass


def apply_mapping(table: VariantTable, mapping: Dict[str, str]) -> List[Dict[str, str]]:
    """
    Turn the customer's columns into ours, in code.

    `mapping` is {our column: their column}. A target whose source is missing
    from their header comes through empty rather than raising — a column they
    did not supply is a gap to show the reviewer, not a crash.
    """
    index = {h: i for i, h in enumerate(table.header) if h}
    out: List[Dict[str, str]] = []
    for row in table.rows:
        rec: Dict[str, str] = {}
        for ours, theirs in mapping.items():
            i = index.get(theirs)
            rec[ours] = row[i].strip() if i is not None and i < len(row) else ""
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# The variance rule
# ---------------------------------------------------------------------------

def split_constant_columns(
    rows: Sequence[Dict[str, Any]],
    columns: Sequence[str],
    protected: Sequence[str] = DEFAULT_PROTECTED,
) -> Tuple[List[str], List[str]]:
    """
    Split spec columns into those that vary (stay columns) and those that do
    not (become one line each in the conditions block).

    Returns (varying_columns, constant_facts). A column with no value on any
    row is dropped from both — an empty column is worse than a missing one.
    """
    varying: List[str] = []
    constants: List[str] = []
    for col in columns:
        values = {str(r.get(col, "")).strip() for r in rows}
        values.discard("")
        if col in protected or len(values) > 1:
            varying.append(col)
        elif len(values) == 1:
            constants.append(f"{_clean_header(col)}: {values.pop()}")
        # len(values) == 0 -> dropped entirely
    return varying, constants


def _clean_header(h: str) -> str:
    return re.sub(r"\s+", " ", h.replace("\n", " ")).strip()


_NUM_RE = re.compile(r"^-?[\d,]*\.?\d+$")


def _as_number(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = str(v or "").strip()
    if not s or not _NUM_RE.match(s):
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _numeric_columns(rows: Sequence[Dict[str, Any]], columns: Sequence[str]) -> set:
    """A column is numeric when every value it carries is a number."""
    out = set()
    for col in columns:
        seen = False
        for r in rows:
            raw = r.get(col, "")
            if str(raw or "").strip() == "":
                continue
            if _as_number(raw) is None:
                break
            seen = True
        else:
            if seen:
                out.add(col)
    return out


# ---------------------------------------------------------------------------
# Building the workbook
# ---------------------------------------------------------------------------

def build_quote_sheet(
    *,
    rfq_ref: str,
    title: str,
    rows: Sequence[Dict[str, Any]],
    spec_columns: Sequence[str],
    quote_columns: Sequence[QuoteColumn] = DEFAULT_QUOTE_COLUMNS,
    conditions: Sequence[str] = (),
    currency: str = "USD",
    incoterm: str = "Ex-works",
    issued: Optional[date] = None,
    protected: Sequence[str] = DEFAULT_PROTECTED,
) -> bytes:
    """
    Render the family as a supplier quotation workbook and return the bytes.

    Carries nothing identifying the customer and no target price: this file
    goes to a supplier. What the caller passes in is what a supplier sees.
    """
    varying, derived = split_constant_columns(rows, spec_columns, protected)
    all_conditions = list(conditions) + derived
    numeric = _numeric_columns(rows, varying)

    thin = Side(style="thin", color=RULE)
    box = Border(left=thin, right=thin, top=thin, bottom=thin)

    wb = Workbook()
    ws = wb.active
    ws.title = "Quote Sheet"

    headers = ["Sr"] + [_clean_header(c) for c in varying] + [q.name for q in quote_columns]
    n_ask = 1 + len(varying)
    ncol = len(headers)
    last_letter = get_column_letter(ncol)

    # --- title band -------------------------------------------------------
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    c = ws.cell(row=1, column=1, value=f"REQUEST FOR QUOTATION  ·  {title}")
    c.font = Font(name=FONT, size=14, bold=True, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor=NAVY)
    c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[1].height = 26

    issued = issued or date.today()
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncol)
    c = ws.cell(row=2, column=1, value=(
        f"RFQ ref: {rfq_ref}   ·   Issued: {issued:%d %b %Y}   ·   Currency: {currency}"
        f"   ·   Incoterm: {incoterm}   ·   {len(rows)} line items"
    ))
    c.font = Font(name=FONT, size=9, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor=NAVY_DK)
    c.alignment = Alignment(horizontal="left", vertical="center", indent=1)

    first_quote = get_column_letter(n_ask + 1)
    ws.merge_cells(start_row=4, start_column=1, end_row=4, end_column=ncol)
    c = ws.cell(row=4, column=1, value=(
        f"Complete the amber columns ({first_quote}–{last_letter}). "
        f"Columns A–{get_column_letter(n_ask)} describe the requirement and must not be altered."
    ))
    c.font = Font(name=FONT, size=9, italic=True, color="5A5A66")
    c.alignment = Alignment(horizontal="left", vertical="center", indent=1)

    # --- header row -------------------------------------------------------
    HDR = 6
    for i, name in enumerate(headers, start=1):
        c = ws.cell(row=HDR, column=i, value=name)
        c.font = Font(name=FONT, size=9, bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=NAVY if i <= n_ask else AMBER_DK)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = box
    ws.row_dimensions[HDR].height = 30

    ws.column_dimensions["A"].width = 5
    for i, col in enumerate(varying, start=2):
        longest = max([len(_clean_header(col))] + [len(str(r.get(col, ""))) for r in rows] or [10])
        ws.column_dimensions[get_column_letter(i)].width = max(9, min(32, longest + 2))
    for i, q in enumerate(quote_columns, start=n_ask + 1):
        ws.column_dimensions[get_column_letter(i)].width = q.width

    # --- grid -------------------------------------------------------------
    first_data = HDR + 1
    for r_off, rec in enumerate(rows):
        r = first_data + r_off
        banded = r_off % 2 == 1

        c = ws.cell(row=r, column=1, value=r_off + 1)
        c.font, c.border = Font(name=FONT, size=9), box
        c.alignment = Alignment(horizontal="center", vertical="top")
        if banded:
            c.fill = PatternFill("solid", fgColor=BAND)

        for i, col in enumerate(varying, start=2):
            raw = rec.get(col, "")
            num = _as_number(raw) if col in numeric else None
            c = ws.cell(row=r, column=i, value=num if num is not None else (str(raw or "") or None))
            c.font, c.border = Font(name=FONT, size=9), box
            if num is not None:
                c.number_format = "#,##0.###"
                c.alignment = Alignment(horizontal="right", vertical="top")
            else:
                c.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
            if banded:
                c.fill = PatternFill("solid", fgColor=BAND)

        for i, q in enumerate(quote_columns, start=n_ask + 1):
            c = ws.cell(row=r, column=i)
            c.font, c.border = Font(name=FONT, size=9), box
            c.fill = PatternFill("solid", fgColor=AMBER_LT)
            if q.number_format:
                c.number_format = q.number_format
            c.alignment = Alignment(
                horizontal="center" if q.kind == "choice" else
                ("right" if q.kind == "number" else "left"),
                vertical="top",
            )

    last_data = HDR + len(rows)

    # --- conditions, stated once ------------------------------------------
    if all_conditions:
        r = last_data + 3
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncol)
        c = ws.cell(row=r, column=1, value="STATED BY THE CUSTOMER — applies to every line")
        c.font = Font(name=FONT, size=10, bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=NAVY)
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        for line in all_conditions:
            r += 1
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncol)
            c = ws.cell(row=r, column=1, value=f"•   {line}")
            c.font = Font(name=FONT, size=9)
            c.alignment = Alignment(horizontal="left", vertical="center", indent=1)

    # --- what makes 300 rows workable -------------------------------------
    if rows:
        ws.freeze_panes = f"{get_column_letter(min(4, n_ask + 1))}{first_data}"
        ws.auto_filter.ref = f"A{HDR}:{last_letter}{last_data}"

        for i, q in enumerate(quote_columns, start=n_ask + 1):
            letter = get_column_letter(i)
            rng = f"{letter}{first_data}:{letter}{last_data}"
            if q.kind == "choice" and q.choices:
                dv = DataValidation(
                    type="list", formula1='"' + ",".join(q.choices) + '"',
                    allow_blank=True, showDropDown=False,
                )
                dv.error = "Choose one of: " + ", ".join(q.choices)
            elif q.kind == "number":
                dv = DataValidation(
                    type="decimal", operator="greaterThanOrEqual",
                    formula1="0", allow_blank=True,
                )
                dv.error = "Enter a number"
            else:
                continue
            ws.add_data_validation(dv)
            dv.add(rng)

    ws.print_title_rows = f"{HDR}:{HDR}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.oddFooter.left.text = f"{title} — {rfq_ref}"
    ws.oddFooter.right.text = "Page &P of &N"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _annexure_rows_as_dicts(annexure: Any) -> List[Dict[str, str]]:
    """Variant rows reach us as lists (positional) or dicts, same as the log writer sees."""
    columns = [str(c) for c in (getattr(annexure, "columns", None) or [])]
    out: List[Dict[str, str]] = []
    for row in getattr(annexure, "rows", None) or []:
        if isinstance(row, dict):
            out.append({c: str(row.get(c, "") or "") for c in columns})
        elif isinstance(row, (list, tuple)):
            out.append({c: (str(row[i]) if i < len(row) and row[i] is not None else "")
                        for i, c in enumerate(columns)})
    return out


def build_family_annexures(
    *,
    rfq_ref: str,
    products: Sequence[Any],
    conditions: Sequence[str] = (),
    quote_columns: Sequence[QuoteColumn] = DEFAULT_QUOTE_COLUMNS,
    issued: Optional[date] = None,
) -> List[Tuple[str, bytes]]:
    """
    Build one workbook per family line, returning [(filename, bytes), ...].

    Scope is deliberately narrow, and matches where the data actually lives:
    `annexure` hangs off a single product, so a sheet belongs to one family
    and never to the whole RFQ. Consolidating two lines into one workbook
    would also cross process families — studs are thread-rolled, nuts are
    forged and tapped — which the extraction rules forbid outright.

    A line that is not a family, or carries no variant rows, produces no
    file; an RFQ of four single lines produces none at all.
    """
    out: List[Tuple[str, bytes]] = []
    for product in products or []:
        if str(getattr(product, "structure", "") or "").strip().lower() != "family":
            continue
        annexure = getattr(product, "annexure", None)
        if not annexure or getattr(annexure, "by_reference", False):
            # by_reference means the customer's own workbook travels with the
            # RFQ; we do not replace it with one of ours.
            continue
        rows = _annexure_rows_as_dicts(annexure)
        if not rows:
            continue

        name = str(getattr(product, "name", "") or "").strip()
        filename = annexure_filename(len(out) + 1, name)
        data = build_quote_sheet(
            rfq_ref=rfq_ref,
            title=name or f"Annexure {len(out) + 1}",
            rows=rows,
            spec_columns=[str(c) for c in (getattr(annexure, "columns", None) or [])],
            quote_columns=quote_columns,
            conditions=conditions,
            issued=issued,
        )
        out.append((filename, data))
    return out
