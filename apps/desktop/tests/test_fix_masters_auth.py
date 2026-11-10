"""Audit 2026-11-07 fixes: supplier and product input limits, logins (AUD-060, 080, 081, 085, 088, 089, 113, 118, 122, 149, 191, 192)."""
from __future__ import annotations

import pytest

from conftest import NORTH, PASSWORD
from atlaspo import auth, masters, order_forms
from atlaspo.db import PermissionDenied, POError


def sup(world, code, **over):
    return masters.save_supplier(world.conn, world.admin, {"code": code, "name": over.pop("name", f"Supplier {code}"), **over}, confirm_duplicate=True)


# ---------- AUD-060: terms days have a safety cap
def test_terms_of_millions_of_days_are_refused_plainly(world):
    with pytest.raises(POError, match="up to 3,650 days"):
        masters.clean_terms("3000000")
    with pytest.raises(POError, match="up to 3,650 days"):
        masters.clean_terms("2900000 days")
    with pytest.raises(POError, match="up to 3,650 days"):
        sup(world, "BIG", payment_terms="3000000")
    assert world.conn.execute("SELECT 1 FROM suppliers WHERE code = 'BIG'").fetchone() is None
    assert masters.clean_terms("120D") == "120D"
    assert masters.clean_terms("3650 days") == "3650D"
    assert masters.clean_terms("0") == "0D"
    assert masters.clean_terms("PDC 30 days") == "PDC 30 days"


def test_term_discount_days_capped(world):
    with pytest.raises(POError, match="up to 3,650"):
        sup(world, "TD1", term_discount_days=100000)
    assert sup(world, "TD2", term_discount_days=" 7 ")


def test_order_with_huge_terms_is_refused(world):
    with pytest.raises(POError, match="up to 3,650 days"):
        world.draft(payment_terms="3000000")


# ---------- AUD-080: term discount and sticker rate limits
def test_term_discount_over_100_percent_refused(world):
    with pytest.raises(POError, match="more than 100%"):
        sup(world, "T15", term_discount_bp=15000)
    assert sup(world, "T10", term_discount_bp=10000)


def test_sticker_rate_over_1000_pesos_refused(world):
    with pytest.raises(POError, match="sticker deduction can be up to"):
        sup(world, "STK", sticker_rate_c=1_000_000)
    assert sup(world, "ST1", sticker_rate_c=100000)


# ---------- AUD-085: whole numbers of days
@pytest.mark.parametrize("value", ["1.5", "abc", "7 days", 1.5])
def test_term_discount_days_not_whole_refused_plainly(world, value):
    with pytest.raises(POError, match="Type a whole number of days, like 30."):
        sup(world, "W01", term_discount_days=value)


def test_whole_number_helper():
    assert masters.whole_number(" 30 ") == 30
    assert masters.whole_number("") == 0
    assert masters.whole_number(None) == 0
    assert masters.whole_number(30.0) == 30
    assert masters.whole_number("-3") == -3
    with pytest.raises(POError):
        masters.whole_number("١٢")


@pytest.mark.parametrize("data", [{"default_discount_bp": "abc"}, {"default_discount_bp": 10001}, {"default_discount_bp": -1},
                                  {"sticker_rate_c": "x"}, {"term_discount_bp": "2.5"}])
def test_other_supplier_numbers_refused_plainly(world, data):
    with pytest.raises(POError):
        sup(world, "N01", **data)


# ---------- AUD-081: product costs and selling prices
@pytest.mark.parametrize("data, message", [
    ({"last_cost_c": -1}, "can't be negative"), ({"selling_c": -1}, "can't be negative"),
    ({"last_cost_c": 10**19}, "too large"), ({"selling_c": 10**17}, "too large"),
    ({"last_cost_c": "abc"}, "as an amount"), ({"pieces_per_unit": -1}, "at least 1"), ({"pieces_per_unit": 0}, "at least 1"), ({"pieces_per_unit": "0"}, "at least 1"),
    ({"pieces_per_unit": "abc"}, "whole number"),
])
def test_product_numbers_refused_plainly(world, data, message):
    with pytest.raises(POError, match=message):
        masters.save_product(world.conn, world.buyer, {"item_code": "P-1", "description": "Product", "department_id": 1, **data})
    assert world.conn.execute("SELECT 1 FROM products WHERE item_code = 'P-1'").fetchone() is None


@pytest.mark.parametrize("data, message", [({"department_id": 999}, "Choose a department."), ({"supplier_id": 999999}, "Choose a supplier."),
                                           ({"active": 5}, "active or not")])
def test_product_unknown_choices_refused_plainly(world, data, message):
    with pytest.raises(POError, match=message):
        masters.save_product(world.conn, world.buyer, {"item_code": "P-3", "description": "Product", "department_id": 1, **data})


def test_supplier_status_and_branch_code_refused_plainly(world):
    with pytest.raises(POError, match="active"):
        sup(world, "DEL", status="DELETED")
    with pytest.raises(POError, match="too large"):
        masters.save_branch(world.conn, world.admin, {"code": 10**19, "name": "Huge"})
    with pytest.raises(POError, match="Choose the branch"):
        auth.create_user(world.conn, world.admin, "r999", "R 999", PASSWORD, ["RECEIVER"], branch_code=999)


def test_product_normal_amounts_saved(world):
    pid = masters.save_product(world.conn, world.buyer, {"item_code": "P-2", "description": "Product", "department_id": 1,
                                                          "last_cost_c": 12550, "selling_c": 19900, "pieces_per_unit": 12})
    row = world.conn.execute("SELECT last_cost_c, selling_c, pieces_per_unit FROM products WHERE id = ?", (pid,)).fetchone()
    assert tuple(row) == (12550, 19900, 12)


@pytest.mark.parametrize("ppu", [None, ""])
def test_product_pieces_per_unit_left_out_means_one(world, ppu):
    pid = masters.save_product(world.conn, world.buyer, {"item_code": "P-3", "description": "Product", "department_id": 1, "pieces_per_unit": ppu})
    assert world.conn.execute("SELECT pieces_per_unit FROM products WHERE id = ?", (pid,)).fetchone()[0] == 1


# ---------- AUD-089 (part): codes and names over 200 characters
def test_long_codes_and_names_refused(world):
    with pytest.raises(POError, match="200 characters"):
        sup(world, "LNG", name="Ж" * 10000)
    with pytest.raises(POError, match="200 characters"):
        masters.save_product(world.conn, world.buyer, {"item_code": "C" * 201, "description": "x", "department_id": 1})
    with pytest.raises(POError, match="200 characters"):
        masters.save_product(world.conn, world.buyer, {"item_code": "C-1", "description": "D" * 201, "department_id": 1})
    with pytest.raises(POError, match="200 characters"):
        auth.create_user(world.conn, world.admin, "u" * 201, "Long user", PASSWORD, ["BUYER"])
    assert sup(world, "OK2", name="N" * 200)
    assert masters.save_product(world.conn, world.buyer, {"item_code": "C" * 200, "description": "D" * 200, "department_id": 1})


# ---------- AUD-122: case folding beyond A to Z
def test_sku_owned_by_other_supplier_with_n_tilde_refused(world):
    other = sup(world, "OTH", name="Other Trading")
    masters.save_product(world.conn, world.admin, {"item_code": "ÑANDU-1", "description": "Unicode owned", "department_id": 1, "supplier_id": other})
    with pytest.raises(POError, match="belongs to OTH"):
        world.draft(items=[world.item(item_code="ñandu-1")])
    with pytest.raises(POError, match="belongs to OTH"):
        masters.check_skus(world.conn, world.supplier, [{"item_code": "ñandu-1"}])
    # The owner itself is fine, in any case, and ASCII still works as before.
    masters.check_skus(world.conn, other, [{"item_code": "ñandu-1"}])
    masters.save_product(world.conn, world.admin, {"item_code": "OWN-1", "description": "Owned", "department_id": 1, "supplier_id": other})
    with pytest.raises(POError, match="belongs to OTH"):
        masters.check_skus(world.conn, world.supplier, [{"item_code": "own-1"}])


def test_duplicate_product_code_with_n_tilde_refused(world):
    masters.save_product(world.conn, world.buyer, {"item_code": "ÑINO-2", "description": "One", "department_id": 1})
    with pytest.raises(POError, match="already in the catalog"):
        masters.save_product(world.conn, world.buyer, {"item_code": "ñino-2", "description": "Two", "department_id": 1})
    pid = world.conn.execute("SELECT id FROM products WHERE item_code = 'ÑINO-2'").fetchone()[0]
    # Saving the product itself again is not a clash.
    masters.save_product(world.conn, world.buyer, {"item_code": "ñino-2", "description": "One again", "department_id": 1}, pid)


def test_approval_links_existing_product_whatever_the_case(world):
    pid = masters.save_product(world.conn, world.buyer, {"item_code": "ÉTÉ-1", "description": "Summer", "department_id": 1, "supplier_id": world.supplier})
    of_id = world.approved(items=[world.item(item_code="été-1", alloc={NORTH: 2})])
    assert order_forms.get_of(world.conn, of_id)["items"][0]["product_id"] == pid
    assert world.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1


def test_usernames_differing_only_in_case_refused(world):
    auth.create_user(world.conn, world.admin, "Ñino", "Upper", PASSWORD, ["BUYER"])
    with pytest.raises(POError, match="already taken"):
        auth.create_user(world.conn, world.admin, "ñino", "Lower", PASSWORD, ["BUYER"])
    with pytest.raises(POError, match="already taken"):
        auth.create_user(world.conn, world.admin, "BUYER", "Again", PASSWORD, ["BUYER"])


# ---------- AUD-118: record_products needs someone who works on orders or the catalog
def test_record_products_refuses_viewer(world):
    viewer = auth.create_user(world.conn, world.admin, "vic", "Vic Viewer", PASSWORD, ["VIEWER"], must_change=False)
    items = [{"item_code": "NEW-9", "product_id": None, "department_id": 1, "description": "New", "brand": "", "size": "", "color": "", "uom": "PC",
              "pieces_per_unit": 1, "box_size": None, "supplier_item_code": "", "unit_cost_c": 100, "selling_c": None}]
    with pytest.raises(PermissionDenied):
        masters.record_products(world.conn, viewer, world.supplier, [dict(i) for i in items])
    assert world.conn.execute("SELECT 1 FROM products WHERE item_code = 'NEW-9'").fetchone() is None
    approver = auth.create_user(world.conn, world.admin, "amy", "Amy Approver", PASSWORD, ["APPROVER"], must_change=False)
    masters.record_products(world.conn, approver, world.supplier, [dict(i) for i in items])
    assert world.conn.execute("SELECT 1 FROM products WHERE item_code = 'NEW-9'").fetchone()


# ---------- AUD-088: update_user keeps a full name
@pytest.mark.parametrize("name", ["", "   ", None])
def test_update_user_refuses_blank_full_name(world, name):
    with pytest.raises(POError, match="full name"):
        auth.update_user(world.conn, world.admin, world.buyer.id, name, ["BUYER"], True)
    assert auth.get_user(world.conn, world.buyer.id).full_name == "Bea Buyer"


# ---------- AUD-113: a changed password must be new
def test_change_password_to_same_refused(world):
    with pytest.raises(POError, match="Choose a new password."):
        auth.change_own_password(world.conn, world.buyer, PASSWORD, PASSWORD)
    auth.change_own_password(world.conn, world.buyer, PASSWORD, "another-horse")
    assert auth.login(world.conn, "buyer", "another-horse").id == world.buyer.id


# ---------- AUD-191: unknown usernames still check a password
def test_unknown_username_runs_scrypt(world, monkeypatch):
    calls = []
    real = auth.hashlib.scrypt
    monkeypatch.setattr(auth.hashlib, "scrypt", lambda *a, **k: calls.append(1) or real(*a, **k))
    with pytest.raises(POError, match="Wrong username or password"):
        auth.login(world.conn, "nobody-here", PASSWORD)
    assert len(calls) == 1
    with pytest.raises(POError, match="Wrong username or password"):
        auth.login(world.conn, "buyer", "wrong-password")
    assert len(calls) == 2


# ---------- AUD-192: turned-off accounts give one message and are logged
def test_turned_off_account_same_message_and_logged(world):
    auth.update_user(world.conn, world.admin, world.buyer.id, "Bea Buyer", ["BUYER"], False)
    messages = []
    for password in (PASSWORD, "wrong-password"):
        with pytest.raises(POError) as err:
            auth.login(world.conn, "buyer", password)
        messages.append(str(err.value))
    with pytest.raises(POError) as err:
        auth.login(world.conn, "nobody-here", PASSWORD)
    messages.append(str(err.value))
    assert messages == [auth.LOGIN_REFUSED] * 3
    failed = world.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'LOGIN_FAILED' AND user_name = 'buyer'").fetchone()[0]
    assert failed == 2
    assert world.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'LOGIN' AND user_name = 'Bea Buyer'").fetchone()[0] == 0


def test_active_login_still_works(world):
    assert auth.login(world.conn, " buyer ", PASSWORD).id == world.buyer.id


# ---------- AUD-149: list_users without a query per person
def _old_list_users(conn):
    out = []
    everyone = auth.chips(conn)
    for u in conn.execute("SELECT u.*, c.initials, c.color FROM users u JOIN user_chips c ON c.user_id = u.id ORDER BY u.full_name"):
        roles = [r["role"] for r in conn.execute("SELECT role FROM user_roles WHERE user_id=?", (u["id"],))]
        branch = conn.execute("SELECT branch_code FROM user_branches WHERE user_id = ?", (u["id"],)).fetchone()
        out.append({"id": u["id"], "username": u["username"], "full_name": u["full_name"],
                    "active": bool(u["active"]), "roles": roles, "branch_code": branch["branch_code"] if branch else None,
                    "initials": u["initials"], "color": u["color"], "chip": everyone[u["id"]].initials, "title": auth.get_title(conn, u["id"])})
    return out


def _statements(conn, fn):
    seen = []
    conn.set_trace_callback(seen.append)
    try:
        result = fn()
    finally:
        conn.set_trace_callback(None)
    return result, len(seen)


def test_list_users_same_output_fixed_statement_count(world):
    conn = world.conn
    auth.create_user(conn, world.admin, "nor1", "Rita Receiver", PASSWORD, ["RECEIVER"], branch_code=NORTH, title="Head Receiver")
    auth.create_user(conn, world.admin, "acc1", "Ana Accounting", PASSWORD, ["ACCOUNTING"])
    first, few = _statements(conn, lambda: auth.list_users(conn))
    assert first == _old_list_users(conn)
    for i in range(10):
        auth.create_user(conn, world.admin, f"extra{i}", f"Extra {i}", PASSWORD, ["VIEWER"])
    second, many = _statements(conn, lambda: auth.list_users(conn))
    assert second == _old_list_users(conn)
    assert many == few <= 6
