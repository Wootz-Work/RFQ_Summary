"""
costing_rates.py — the starting INR/kg rates the costing workbook fills in.

These are Wootz's planning numbers, kept on the higher side, so a first total
appears on every row. Every one lands in red: a reviewer confirms or replaces
it before quoting. Edit the tables here when the team's numbers move — the
legend's Basis column says each rate came from this table.

Material rates are raw material per kg. Process rates are the conversion cost
per kg of finished weight for that step, the way fabricators quote them.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

BASIS = "Wootz default (higher side) — confirm"

# (pattern, name shown in the legend, INR / kg). First match wins, so the
# specific grades come before the broad families they belong to.
MATERIAL_RATES: List[Tuple[str, str, float]] = [
    (r"super\s*duplex|2507|s32750|s32760|1\.4410", "Super duplex SS", 750),
    (r"duplex|2205|s31803|s32205|1\.4462", "Duplex SS", 520),
    (r"inconel|incoloy|hastelloy|monel|nickel\s*alloy|alloy\s*(600|625|718|825)|\b(718|625)\b", "Nickel alloy", 4500),
    (r"titanium|\bti-?6al", "Titanium", 3500),
    (r"316|a4\b|a4-\d|1\.440[14]|1\.4571", "SS 316", 380),
    (r"304|a2\b|a2-\d|1\.430[17]|1\.4306|\bss\b|stainless|inox", "SS 304", 280),
    (r"\b4[123]0\b|\b17-4|\b630\b|1\.4021|1\.4016", "SS 400 series", 240),
    (r"brass|cw6\d\d|cuzn", "Brass", 650),
    (r"bronze|cusn|cual|phosphor", "Bronze", 850),
    (r"copper|\bcu\b|c110|etp", "Copper", 950),
    (r"alumin|\bal\b|6061|6082|6063|5083|7075|2024|lm\d", "Aluminium", 330),
    (r"\bsg\b|ductile|grey\s*iron|gray\s*iron|cast\s*iron|en-gjs|en-gjl|\bfg\s*\d", "Cast iron", 90),
    (r"42crmo4|4140|4340|en19|en24|a193|b7\b|b16\b|alloy\s*steel|cr-?mo|\b(8\.8|10\.9|12\.9)\b|scm", "Alloy steel", 110),
    (r"spring\s*steel|en42|en47|65mn|c75|ck67", "Spring steel", 120),
    (r"carbon\s*steel|mild\s*steel|\bms\b|\bcs\b|s235|s275|s355|is\s*2062|a36|a105|a106|a53|a516|"
     r"c45|en8|en9|1018|1020|1045|ss400|st37|st52|\b(4\.6|4\.8|5\.6|5\.8|6\.8)\b|steel",
     "Carbon steel", 75),
    (r"ptfe|teflon", "PTFE", 1200),
    (r"peek", "PEEK", 9000),
    (r"nylon|\bpa\s*6|\bpa66|polyamide", "Nylon", 400),
    (r"frp|grp|fibreglass|fiberglass|vinyl\s*ester", "FRP", 350),
    (r"pvc|cpvc", "PVC", 200),
    (r"\bpp\b|polypropylene|hdpe|\bpe\b|polyethylene", "PP / PE", 180),
    (r"epdm|nbr|viton|fkm|rubber|neoprene|silicone", "Rubber", 450),
    (r"graphite", "Graphite", 900),
]

# INR / kg of finished weight for each step. Names match the workbook's
# process list (and the extraction prompt's) case-insensitively.
PROCESS_RATES = {
    "Cutting": 8, "Laser cutting": 15, "Plasma cutting": 10, "Rolling": 10, "Bending": 8,
    "Pressing": 15, "Stamping": 15, "Welding": 35, "Machining": 60, "Turning": 50, "Milling": 60,
    "Drilling": 20, "Tapping": 15, "Threading": 20, "Grinding": 40, "Brushing": 10, "Polishing": 30,
    "Pickling & passivation": 15, "Passivation": 12, "Leak test": 10, "Hydro test": 10,
    "Powder coating": 30, "Painting": 25, "Galvanising (HDG)": 30, "Zinc plating": 20,
    "Zinc flake coating": 45, "Nickel plating": 80, "Electroless nickel plating": 150,
    "Chrome plating": 150, "Anodising": 60, "PVD coating": 300, "Thermal spray coating": 400,
    "Heat treatment": 25, "Induction hardening": 40, "Case hardening": 35, "Cold heading": 20,
    "Hot forging": 45, "Forging": 45, "Casting": 60, "Thread rolling": 15, "Assembly": 15,
    "Moulding": 60, "Winding": 40, "Lamination": 50,
}

_PROCESS_INDEX = {k.lower(): (k, v) for k, v in PROCESS_RATES.items()}


def material_rate(material: str) -> Optional[Tuple[str, float]]:
    """('SS 316', 380) for '316L', or None when the material is unknown."""
    text = (material or "").strip().lower()
    if not text:
        return None
    for pattern, name, rate in MATERIAL_RATES:
        if re.search(pattern, text):
            return name, float(rate)
    return None


def process_rate(process: str) -> Optional[float]:
    hit = _PROCESS_INDEX.get((process or "").strip().lower())
    return float(hit[1]) if hit else None
