"""
sheet_columns.py — which extracted columns reach a sheet, and what they are called.

The extraction hands columns over as keys (`part_number`, `key_dimensions`) and
sometimes carries bookkeeping along with them: a serial number, a row index, a
reference the model made up to keep variants apart. None of that belongs in a
sheet a person reads. What does belong is the part number and the description
or part name, when the customer gave them.

Supplier-facing sheets drop the target price as well — whatever it is called.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence

# Headers that only ever number the rows. "Item no" is NOT here: customers use it
# for real part codes as often as for a count, so it is judged by its values.
_SERIAL_HEADER = re.compile(
    r"^\s*(#|no\.?|sr\.?|s\.?\s*no\.?|sl\.?\s*no\.?|sr\.?\s*no\.?|serial(\s*(no\.?|number))?|"
    r"row(\s*(no\.?|id|index|ref))?|index|idx|line\s*(no\.?|index)|variant[\s_]*(ref|id|index|no))\s*$",
    re.IGNORECASE,
)
_TARGET_PRICE = re.compile(r"target[\s_]*(price|cost|rate)", re.IGNORECASE)

_LABELS = {
    "part_number": "Part number",
    "part_no": "Part number",
    "part_name": "Part name",
    "description": "Description",
    "key_dimensions": "Key dimensions",
    "drawing_ref": "Drawing ref",
    "drawing_no": "Drawing no.",
    "target_price": "Target price",
    "qty": "Qty",
}


def display_header(key: Any) -> str:
    """`key_dimensions` -> `Key dimensions`; a header the customer wrote stays as written."""
    k = re.sub(r"\s+", " ", str(key or "").replace("\n", " ")).strip()
    if k.lower() in _LABELS:
        return _LABELS[k.lower()]
    if "_" in k and " " not in k and k == k.lower():
        return k.replace("_", " ").capitalize()
    return k


def _is_running_count(values: Sequence[Any]) -> bool:
    """1, 2, 3 … N in order — a row count, not a code."""
    nums = []
    for v in values:
        s = str(v if v is not None else "").strip()
        if not s:
            continue
        if not re.fullmatch(r"\d+(\.0+)?", s):
            return False
        nums.append(int(float(s)))
    return len(nums) >= 2 and nums == list(range(nums[0], nums[0] + len(nums))) and nums[0] in (0, 1)


def visible_columns(columns: Sequence[Any], rows: Sequence[Dict[str, Any]] = (),
                    supplier_facing: bool = False) -> List[str]:
    """The columns worth showing, in their original order.

    Drops serial numbers and made-up row references by name, any column whose
    values are just 1..N, and — on anything a supplier will see — the target price.
    """
    out = []
    for c in columns:
        key = str(c)
        if _SERIAL_HEADER.match(key.replace("_", " ")):
            continue
        if supplier_facing and _TARGET_PRICE.search(key):
            continue
        if rows and _is_running_count([r.get(key) for r in rows]):
            continue
        out.append(key)
    return out
