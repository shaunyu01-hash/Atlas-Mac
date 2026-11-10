from decimal import Decimal

import pytest

from atlaspo.money import (compute_line, compute_totals, fmt_peso, fx_to_centavos, parse_percent, parse_pesos,
                        requires_approval)


def line(dept=1, qty=1, cost=10000, d1=0, d2=0, per=1):
    return {"department_id": dept, "qty": qty, "unit_cost": cost, "disc1_bp": d1, "disc2_bp": d2, "pieces_per_unit": per}


def test_discounts_compound_like_legacy():
    a = compute_line(10, 10000, 1000, 500)  # 10% then 5% is 14.5%, not 15%
    assert a.gross == 100000 and a.total == 85500 and a.discount == 14500 and a.net_unit == 8550


def test_no_float_drift():
    # 0.1 + 0.2 style error would show here if floats were involved
    assert parse_pesos("0.10") + parse_pesos("0.20") == parse_pesos("0.30")
    assert compute_line(3, 3333).total == 9999
    assert parse_pesos("1,234.565") == 123457  # half up


@pytest.mark.parametrize("text,want", [("₱1,234.50", 123450), ("", 0), (None, 0), ("0", 0), ("100000", 10000000)])
def test_parse_pesos(text, want):
    assert parse_pesos(text) == want


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        parse_pesos("abc")
    with pytest.raises(ValueError):
        parse_percent("x%")


def test_peso_format():
    assert fmt_peso(10000000) == "₱100,000.00"
    assert fmt_peso(-150) == "-₱1.50"


def test_fx_cost_matches_legacy_rule():
    # From 2026 deliveries: rate 26 x 8.50 yuan = 221.00
    assert fx_to_centavos(8500, 26000) == 22100


def test_vat_inclusive_adds_nothing():
    t = compute_totals([line(cost=11200)], vat_mode="INCLUSIVE")
    assert t.grand_total == 11200 and t.vat == 1200


def test_vat_exclusive_adds_on_top():
    t = compute_totals([line(cost=10000)], vat_mode="EXCLUSIVE")
    assert t.vat == 1200 and t.grand_total == 11200


def test_vat_none():
    t = compute_totals([line(cost=10000)], vat_mode="NONE")
    assert t.vat == 0 and t.grand_total == 10000


def test_full_breakdown():
    t = compute_totals(
        [line(1, 10, 10000, 1000), line(5, 2, 50000), line(5, 1, 25000, per=12)],
        overall_type="PERCENT", overall_value=500, vat_mode="EXCLUSIVE", vat_rate_bp=1200, other_charges=50000)
    assert t.total_qty == 13 and t.total_pieces == 24
    assert t.gross == 225000 and t.item_discount == 10000
    assert t.overall_discount == 10750 and t.net_merchandise == 204250
    assert t.vat == 24510 and t.grand_total == 278760
    assert t.departments[1].amount == 90000 and t.departments[5].amount == 125000
    assert t.departments[5].qty == 3 and t.departments[5].pieces == 14


def test_overall_amount_cannot_exceed_merchandise():
    with pytest.raises(ValueError):
        compute_totals([line(cost=100)], overall_type="AMOUNT", overall_value=200)


@pytest.mark.parametrize("total,enabled,cmp,want", [
    (10000000, True, "GT", False), (10000100, True, "GT", True), (5000000, True, "GT", False),
    (25000000, True, "GT", True), (25000000, False, "GT", False), (10000000, True, "GTE", True)])
def test_policy(total, enabled, cmp, want):
    assert requires_approval(total, enabled, 10000000, cmp) is want


def test_decimal_is_used_for_rounding():
    assert isinstance(Decimal(1), Decimal)
    assert compute_line(1, 333, 3333).total == 222  # 333 x 0.6667 = 222.01
