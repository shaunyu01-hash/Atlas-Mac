"""All money and PO arithmetic. Pure functions, Decimal only.

Amounts are integer centavos everywhere outside this module's parsing helpers.
Percentages are integer basis points (350 = 3.50%). Foreign amounts are integer
thousandths (8.500 = 8500).

Cost rule:
    net = unit cost x (1 - disc1) x (1 - disc2)    compound, never added together
Pack rule: a pack is bought and priced by the pack.
Bigger "boxes" (24, 36, 48...) are a label; quantity and cost stay per piece.

Quantities are stored as integer thousandths (_m) so a metre or kilo can be fractional without floats.
An order form's total is the exact sum of its branch POs, so the two can never differ by a centavo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

PESO = "₱"
HUNDRED = Decimal(100)
BP = Decimal(10000)
MILLI = Decimal(1000)

VAT_INCLUSIVE, VAT_EXCLUSIVE, VAT_NONE = "INCLUSIVE", "EXCLUSIVE", "NONE"
VAT_MODES = (VAT_INCLUSIVE, VAT_EXCLUSIVE, VAT_NONE)
DISC_NONE, DISC_PERCENT, DISC_AMOUNT = "NONE", "PERCENT", "AMOUNT"

# Unit: (pieces per unit, fractional quantities allowed). Anything not listed is whole-number, one piece per unit.
UNITS = {"PC": (1, False), "3 IN 1": (3, False), "4 IN 1": (4, False), "6 IN 1": (6, False), "1 DOZEN": (12, False),
         # Older documents and records can still hold these, but they're no longer offered.
         "PAIR": (1, False), "SET": (1, False), "DOZ": (12, False), "PK4": (4, False), "M": (1, True), "YD": (1, True), "KG": (1, True)}
OFFERED_UNITS = ("PC", "3 IN 1", "4 IN 1", "6 IN 1", "1 DOZEN")
SELLING_COST_SHARE, ROUND_SELLING_C = 70, 1000  # a 30% gross margin leaves cost at 70% of the price


def round_centavos(value: Decimal) -> int:
    """Pesos and fractions of a centavo in, whole centavos out, half up."""
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def parse_pesos(text: str | int | float | Decimal | None) -> int:
    """'1,234.50' or '₱1,234.50' -> 123450. Blank is 0. Raises ValueError on anything else."""
    if text is None:
        return 0
    if isinstance(text, float):
        text = repr(text)
    s = str(text).replace(",", "").replace(PESO, "").replace("PHP", "").strip()
    if not s:
        return 0
    try:
        return round_centavos(Decimal(s) * HUNDRED)
    except InvalidOperation:
        raise ValueError(f"'{text}' is not a valid amount") from None


def parse_percent(text: str | int | float | Decimal | None) -> int:
    """'3.5' or '3.5%' -> 350 basis points."""
    if text is None:
        return 0
    s = str(text).replace("%", "").strip()
    if not s:
        return 0
    try:
        return int((Decimal(s) * HUNDRED).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        raise ValueError(f"'{text}' is not a valid percentage") from None


def parse_milli(text: str | None) -> int:
    s = str(text or "").replace(",", "").strip()
    if not s:
        return 0
    try:
        return int((Decimal(s) * MILLI).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        raise ValueError(f"'{text}' is not a valid number") from None


def fmt_plain(centavos: int | None) -> str:
    return f"{Decimal(centavos or 0) / HUNDRED:,.2f}"


def fmt_peso(centavos: int | None) -> str:
    value = centavos or 0
    sign = "-" if value < 0 else ""
    return f"{sign}{PESO}{Decimal(abs(value)) / HUNDRED:,.2f}"


def fmt_percent(bp: int | None) -> str:
    if not bp:
        return ""
    return f"{(Decimal(bp) / HUNDRED).normalize():f}%"


def fmt_milli(value: int | None) -> str:
    if not value:
        return ""
    return f"{(Decimal(value) / MILLI).normalize():f}"


def pieces_per_unit(uom: str | None) -> int:
    return UNITS.get((uom or "PC").strip().upper(), (1, False))[0]


def parse_pieces(text: str | None, uom: str | None = "PC") -> int:
    """People type pieces. Records hold units, because the cost is per unit. '30' of 3 IN 1 is 10 units; '10' isn't a whole number of packs."""
    pieces_m = parse_qty(text, uom)
    per_unit = pieces_per_unit(uom)
    if pieces_m % (per_unit * 1000) and per_unit > 1:
        below = pieces_m // (per_unit * 1000) * per_unit
        options = f"{below:,} or {below + per_unit:,}" if below else f"{per_unit:,}"
        raise ValueError(f"{(uom or '').upper()} comes in {per_unit}s, so {pieces_m // 1000:,} isn't a full pack. Use {options}, a multiple of {per_unit}")
    return pieces_m // per_unit


def per_piece(unit_cost_c: int, pieces: int) -> int:
    """What one piece costs, from the cost of its pack or dozen."""
    return round_centavos(Decimal(unit_cost_c) / (pieces or 1))


def selling_from_cost(cost_c: int) -> int | None:
    """What a piece sells for: a 30% gross margin on its cost, then up to a multiple of 10 pesos, so nobody counts change.
    39.00 becomes 40.00, 42.00 and 45.00 become 50.00, and 40.00 stays. Whole-centavo arithmetic, so nothing rounds early."""
    if cost_c <= 0:
        return None
    return -(-cost_c * 100 // (SELLING_COST_SHARE * ROUND_SELLING_C)) * ROUND_SELLING_C


def sticker_deduction(pieces, rate_c: int) -> int:
    """What stickers cost a supplier: every piece, whatever its pack, at a rate in centavos."""
    return round_centavos(Decimal(str(pieces)) * rate_c)


def allows_decimal(uom: str | None) -> bool:
    return UNITS.get((uom or "PC").strip().upper(), (1, False))[1]


def parse_qty(text: str | int | float | Decimal | None, uom: str | None = "PC") -> int:
    """'1,250' -> 1250000 thousandths. Blank is 0. Only some units may be fractional, and none may be negative."""
    if text is None:
        return 0
    if isinstance(text, float):
        text = repr(text)
    s = str(text).replace(",", "").strip()
    if not s:
        return 0
    try:
        value = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"'{text}' is not a valid quantity") from None
    if not value.is_finite():
        raise ValueError(f"'{text}' is not a valid quantity")
    if value < 0:
        raise ValueError("Quantity can't be negative")
    if value != value.to_integral_value() and not allows_decimal(uom):
        raise ValueError(f"Quantity in {(uom or 'PC').upper()} must be a whole number")
    thousandths = value * MILLI
    if thousandths != thousandths.to_integral_value():
        raise ValueError("Quantity can have at most 3 decimal places")
    return int(thousandths)


def m_to_qty(qty_m: int | None) -> Decimal:
    return Decimal(qty_m or 0) / MILLI


def qty_to_m(qty) -> int:
    return int((Decimal(qty) * MILLI).to_integral_value(rounding=ROUND_HALF_UP))


def fmt_qty(qty_m: int | None) -> str:
    return f"{m_to_qty(qty_m):,f}" if (qty_m or 0) % 1000 else f"{(qty_m or 0) // 1000:,}"


def fx_to_centavos(fx_cost_m: int, fx_rate_m: int) -> int:
    """Peso cost from a foreign cost and rate. Legacy: pack_cost = fx_rate x fx_cost."""
    return round_centavos(Decimal(fx_cost_m) / MILLI * (Decimal(fx_rate_m) / MILLI) * HUNDRED)


def discount_factor(disc1_bp: int = 0, disc2_bp: int = 0) -> Decimal:
    return (1 - Decimal(disc1_bp) / BP) * (1 - Decimal(disc2_bp) / BP)


@dataclass(frozen=True)
class LineAmounts:
    gross: int
    discount: int
    total: int
    net_unit: int


def compute_line(qty: int | Decimal, unit_cost: int, disc1_bp: int = 0, disc2_bp: int = 0) -> LineAmounts:
    if qty <= 0:
        raise ValueError("Quantity must be more than zero")
    if unit_cost < 0:
        raise ValueError("Unit cost cannot be negative")
    for bp in (disc1_bp, disc2_bp):
        if not 0 <= bp <= 10000:
            raise ValueError("Discount must be between 0% and 100%")
    factor = discount_factor(disc1_bp, disc2_bp)
    gross = round_centavos(Decimal(qty) * unit_cost)
    total = round_centavos(Decimal(qty) * unit_cost * factor)
    return LineAmounts(gross, gross - total, total, round_centavos(Decimal(unit_cost) * factor))


@dataclass
class DeptTotal:
    department_id: int
    lines: int = 0
    qty: int = 0
    pieces: int = 0
    amount: int = 0


@dataclass
class POTotals:
    total_qty: int = 0
    total_pieces: int = 0
    gross: int = 0
    item_discount: int = 0
    overall_discount: int = 0
    net_merchandise: int = 0
    vat: int = 0
    other_charges: int = 0
    grand_total: int = 0
    departments: dict[int, DeptTotal] = field(default_factory=dict)


def compute_totals(
    lines: list[dict],
    overall_type: str = DISC_NONE,
    overall_value: int = 0,
    vat_mode: str = VAT_INCLUSIVE,
    vat_rate_bp: int = 1200,
    other_charges: int = 0,
) -> POTotals:
    """lines need department_id, qty, pieces_per_unit, unit_cost, disc1_bp, disc2_bp.

    overall_value is basis points for PERCENT and centavos for AMOUNT.
    VAT INCLUSIVE shows the VAT inside the amount and adds nothing; EXCLUSIVE adds it on top.
    Other charges are added last and carry no VAT.
    """
    if vat_mode not in VAT_MODES:
        raise ValueError(f"Unknown VAT setting: {vat_mode}")
    if other_charges < 0:
        raise ValueError("Other charges cannot be negative")
    t = POTotals(other_charges=other_charges)
    for ln in lines:
        a = compute_line(ln["qty"], ln["unit_cost"], ln.get("disc1_bp", 0), ln.get("disc2_bp", 0))
        pieces = ln["qty"] * (ln.get("pieces_per_unit") or 1)
        t.total_qty += ln["qty"]
        t.total_pieces += pieces
        t.gross += a.gross
        t.item_discount += a.discount
        d = t.departments.setdefault(ln["department_id"], DeptTotal(ln["department_id"]))
        d.lines += 1
        d.qty += ln["qty"]
        d.pieces += pieces
        d.amount += a.total

    after_items = t.gross - t.item_discount
    if overall_type == DISC_PERCENT:
        if not 0 <= overall_value <= 10000:
            raise ValueError("Overall discount must be between 0% and 100%")
        t.overall_discount = round_centavos(Decimal(after_items) * Decimal(overall_value) / BP)
    elif overall_type == DISC_AMOUNT:
        if not 0 <= overall_value <= after_items:
            raise ValueError("Overall discount cannot be more than the merchandise amount")
        t.overall_discount = overall_value
    elif overall_type != DISC_NONE:
        raise ValueError(f"Unknown discount type: {overall_type}")

    t.net_merchandise = after_items - t.overall_discount
    rate = Decimal(vat_rate_bp) / BP
    if vat_mode == VAT_INCLUSIVE:
        t.vat = round_centavos(Decimal(t.net_merchandise) * rate / (1 + rate))
        t.grand_total = t.net_merchandise + other_charges
    elif vat_mode == VAT_EXCLUSIVE:
        t.vat = round_centavos(Decimal(t.net_merchandise) * rate)
        t.grand_total = t.net_merchandise + t.vat + other_charges
    else:
        t.grand_total = t.net_merchandise + other_charges
    return t


def split_by_weight(total: int, weights: dict[int, int]) -> dict[int, int]:
    """Split whole centavos in proportion, largest remainder first, so the shares always add back to total."""
    if not weights:
        return {}
    base = sum(weights.values())
    if base <= 0:
        first = min(weights)
        return {k: (total if k == first else 0) for k in weights}
    shares, remainders = {}, []
    for key in sorted(weights):
        whole, rest = divmod(total * weights[key], base)
        shares[key] = whole
        remainders.append((-rest, key))
    for _, key in sorted(remainders)[:total - sum(shares.values())]:
        shares[key] += 1
    return shares


@dataclass
class OrderTotals(POTotals):
    """One supplier order across branches. Every figure is the sum of branches, each a complete PO calculation."""
    branches: dict[int, POTotals] = field(default_factory=dict)
    overall_shares: dict[int, int] = field(default_factory=dict)
    charge_shares: dict[int, int] = field(default_factory=dict)


def compute_order(
    lines: list[dict],
    overall_type: str = DISC_NONE,
    overall_value: int = 0,
    vat_mode: str = VAT_INCLUSIVE,
    vat_rate_bp: int = 1200,
    other_charges: int = 0,
) -> OrderTotals:
    """lines are as compute_totals, with allocations {branch: qty} in place of qty. Zero allocations are skipped.

    Item discounts are per line. The overall discount and other charges are one amount for the whole order,
    shared across branches by their merchandise value. VAT is worked out per branch, on that branch's PO.
    """
    if other_charges < 0:
        raise ValueError("Other charges cannot be negative")
    per_branch: dict[int, list[dict]] = {}
    merchandise: dict[int, int] = {}
    for ln in lines:
        for branch, qty in ln["allocations"].items():
            if qty <= 0:
                continue
            per_branch.setdefault(branch, []).append({**ln, "qty": qty})
            merchandise[branch] = merchandise.get(branch, 0) + compute_line(
                qty, ln["unit_cost"], ln.get("disc1_bp", 0), ln.get("disc2_bp", 0)).total
    after_items = sum(merchandise.values())
    if overall_type == DISC_PERCENT:
        if not 0 <= overall_value <= 10000:
            raise ValueError("Overall discount must be between 0% and 100%")
        overall = round_centavos(Decimal(after_items) * Decimal(overall_value) / BP)
    elif overall_type == DISC_AMOUNT:
        if not 0 <= overall_value <= after_items:
            raise ValueError("Overall discount cannot be more than the merchandise amount")
        overall = overall_value
    elif overall_type == DISC_NONE:
        overall = 0
    else:
        raise ValueError(f"Unknown discount type: {overall_type}")
    t = OrderTotals(overall_shares=split_by_weight(overall, merchandise), charge_shares=split_by_weight(other_charges, merchandise))
    for branch in sorted(per_branch):
        b = compute_totals(per_branch[branch], DISC_AMOUNT, t.overall_shares[branch], vat_mode, vat_rate_bp, t.charge_shares[branch])
        t.branches[branch] = b
        t.total_qty += b.total_qty
        t.total_pieces += b.total_pieces
        t.gross += b.gross
        t.item_discount += b.item_discount
        t.overall_discount += b.overall_discount
        t.net_merchandise += b.net_merchandise
        t.vat += b.vat
        t.other_charges += b.other_charges
        t.grand_total += b.grand_total
        for did, d in b.departments.items():
            into = t.departments.setdefault(did, DeptTotal(did))
            into.lines += d.lines
            into.qty += d.qty
            into.pieces += d.pieces
            into.amount += d.amount
    return t


def requires_approval(total: int, enabled: bool, threshold: int, comparison: str = "GT") -> bool:
    """Policy decision. GT = strictly greater than the threshold, GTE = at or above it."""
    if not enabled:
        return False
    return total >= threshold if comparison == "GTE" else total > threshold
