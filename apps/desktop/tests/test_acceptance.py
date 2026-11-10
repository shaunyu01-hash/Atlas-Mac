"""The OF-first spec's acceptance tests 1-19, then the rules kept from the first phase."""
from __future__ import annotations

import json
import multiprocessing
import sqlite3
from pathlib import Path

import pytest

from atlaspo import auth, backup, db, masters, order_forms as of, orders, pdf, reports, schema
from atlaspo.db import POError
from atlaspo.money import parse_pesos, selling_from_cost
from atlaspo.orders import APPROVED, CANCELLED, ISSUED, PENDING

from conftest import NORTH, PASSWORD, SOUTH, WEST, EAST

ALL_FOUR = {NORTH: 120, SOUTH: 50, EAST: 40, WEST: 40}


def set_policy(world, **kw):
    db.set_settings(world.conn, world.admin, {f"approval.{k}": str(v) for k, v in kw.items()})


def texts_of(monkeypatch):
    """Every string a PDF renders goes through Paragraph, so collect them there."""
    printed = []
    real = pdf.Paragraph

    def capture(text, *args, **kwargs):
        printed.append(str(text))
        return real(text, *args, **kwargs)

    monkeypatch.setattr(pdf, "Paragraph", capture)
    return printed


# ------------------------------------------------------------------ the 19 acceptance tests

def test_01_of_with_multiple_products_one_supplier(world):
    items = [world.item(dept=5, cost=100000, alloc=ALL_FOUR, description="Hardware A"),
             world.item(dept=5, cost=90000, alloc={NORTH: 60, SOUTH: 30, EAST: 20, WEST: 10}, description="Hardware B"),
             world.item(dept=9, cost=5000, alloc={NORTH: 10}, description="Belt")]
    o = of.get_of(world.conn, world.draft(items=items))
    assert [i["description"] for i in o["items"]] == ["Hardware A", "Hardware B", "Belt"]
    assert o["supplier_id"] == world.supplier and o["of_number"] is None
    assert {i["department_id"] for i in o["items"]} == {5, 9}


def test_02_allocating_to_all_four_active_branches(world):
    o = of.get_of(world.conn, world.draft(items=[world.item(alloc=ALL_FOUR)]))
    assert [b["name"] for b in o["branch_columns"]] == ["North", "South", "East", "West"]
    assert o["items"][0]["allocations"] == ALL_FOUR


def test_03_total_quantity_and_amount(world):
    items = [world.item(cost=10000, alloc=ALL_FOUR), world.item(cost=20000, alloc={NORTH: 60, SOUTH: 30, EAST: 20, WEST: 10})]
    o = of.get_of(world.conn, world.draft(items=items))
    assert [i["total_qty"] for i in o["items"]] == [250, 120]
    assert o["total_qty"] == 370 and o["grand_total_c"] == 250 * 10000 + 120 * 20000


def test_03b_order_quantity_must_equal_branch_sum_and_no_negatives(world):
    with pytest.raises(POError, match="add up to"):
        world.draft(items=[world.item(alloc={NORTH: 100, SOUTH: 50}, ordered_qty="151")])
    assert world.draft(items=[world.item(alloc={NORTH: 100, SOUTH: 50}, ordered_qty="150")])
    with pytest.raises(POError, match="negative"):
        world.draft(items=[world.item(alloc={NORTH: -1})])


def test_03c_decimals_only_where_the_unit_allows(world):
    with pytest.raises(POError, match="whole number"):
        world.draft(items=[world.item(alloc={NORTH: "1.5"}, uom="PC")])
    o = of.get_of(world.conn, world.draft(items=[world.item(cost=10000, alloc={NORTH: "1.5", SOUTH: "0.25"}, uom="M")]))
    assert o["items"][0]["total_qty"] == 1.75 and o["grand_total_c"] == 17500


def test_04_inactive_branches_stay_out_of_new_allocations(world):
    assert [b["code"] for b in of.active_branches(world.conn)] == [NORTH, SOUTH, EAST, WEST]
    masters.save_branch(world.conn, world.admin, {"code": 20, "name": "Old Branch", "active": False})
    assert [b["code"] for b in of.active_branches(world.conn)] == [NORTH, SOUTH, EAST, WEST]
    with pytest.raises(POError, match="inactive"):
        world.draft(items=[world.item(alloc={20: 5})])
    for removed in (1, 3, 5):
        with pytest.raises(POError):
            world.draft(items=[world.item(alloc={removed: 5})])
    with pytest.raises(auth.PermissionDenied):
        masters.save_branch(world.conn, world.buyer, {"name": "Old Branch", "active": True}, 20)
    assert masters.save_branch(world.conn, world.admin, {"name": "Old Branch", "active": True}, 20) == 20
    assert 20 in [b["code"] for b in of.active_branches(world.conn)]
    assert world.draft(items=[world.item(alloc={20: 5})])
    assert world.conn.execute("SELECT 1 FROM audit_logs WHERE action='BRANCH_REACTIVATE'").fetchone()


def test_04b_branch_deactivated_after_approval_blocks_generation(world):
    of_id = world.approved(items=[world.item(alloc={NORTH: 5, SOUTH: 5})])
    masters.save_branch(world.conn, world.admin, {"name": "South", "active": False}, SOUTH)
    with pytest.raises(POError, match="inactive"):
        of.generate_pos(world.conn, world.buyer, of_id)
    assert world.conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 0
    assert world.conn.execute("SELECT COUNT(*) FROM po_number_sequences").fetchone()[0] == 0  # whole transaction undone


def test_05_code_8_is_west_and_codes_never_change(conn):
    branches = {r["code"]: r["name"] for r in conn.execute("SELECT code, name FROM branches")}
    assert branches == {2: "North", 4: "South", 6: "East", 8: "West"}
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE branches SET code = 9 WHERE code = 8")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM branches WHERE code = 2")


def test_06_one_po_per_branch_with_positive_allocation(world):
    items = [world.item(cost=10000, alloc=ALL_FOUR, description="Shoes A"), world.item(cost=20000, alloc={NORTH: 60, SOUTH: 0, EAST: "", WEST: 10}, description="Shoes B")]
    of_id = world.approved(items=items)
    pos = of.generate_pos(world.conn, world.buyer, of_id)
    assert [p["branch_code"] for p in pos] == [NORTH, SOUTH, EAST, WEST]
    ros = orders.get_po(world.conn, pos[1]["id"])
    assert [(i["description"], i["qty"]) for i in ros["items"]] == [("Shoes A", 50)]
    ave = orders.get_po(world.conn, pos[0]["id"])
    assert [(i["description"], i["qty"]) for i in ave["items"]] == [("Shoes A", 120), ("Shoes B", 60)]
    assert ave["of_number"].endswith("-OF-00001") and ave["status"] == APPROVED
    # A branch with nothing gets no PO.
    only_two = world.approved(items=[world.item(alloc={NORTH: 5, EAST: 0, WEST: 3})])
    assert [p["branch_code"] for p in of.generate_pos(world.conn, world.buyer, only_two)] == [NORTH, WEST]
    # Every PO line traces to its OF item and allocation.
    line = ros["items"][0]
    alloc = world.conn.execute("SELECT * FROM order_form_allocations WHERE id=?", (line["of_allocation_id"],)).fetchone()
    assert alloc["branch_code"] == SOUTH and alloc["of_item_id"] == line["of_item_id"]


def test_06b_branch_pos_add_up_to_the_of_to_the_centavo(world):
    """Rounding per line, a shared overall discount, shared charges, and VAT per branch must still add up exactly."""
    items = [world.item(cost=3333, alloc={NORTH: 7, SOUTH: 11, EAST: 13, WEST: 17}, disc1_bp=333, disc2_bp=125),
             world.item(cost=1999, alloc={NORTH: 3, SOUTH: 5, WEST: 1}, disc1_bp=750)]
    of_id = world.approved(items=items, overall_discount_type="PERCENT", overall_discount_value=250, vat_mode="EXCLUSIVE",
                           vat_rate_bp=1200, other_charges_c=100001, other_charges_note="Freight")
    before = of.get_of(world.conn, of_id)["grand_total_c"]
    pos = of.generate_pos(world.conn, world.buyer, of_id)
    assert sum(p["grand_total_c"] for p in pos) == before
    assert sum(orders.get_po(world.conn, p["id"])["other_charges_c"] for p in pos) == 100001
    assert backup.integrity_report(world.conn) == []


def test_07_branch_specific_numbering_and_year_rollover(world, clock):
    clock(2026, 12, 31, 23, 50)
    first = of.generate_pos(world.conn, world.buyer, world.approved(items=[world.item(alloc=ALL_FOUR)]))
    assert [p["po_number"] for p in first] == ["2026-2-00001", "2026-4-00001", "2026-6-00001", "2026-8-00001"]
    second = of.generate_pos(world.conn, world.buyer, world.approved(items=[world.item(alloc={NORTH: 1, WEST: 1})]))
    assert [p["po_number"] for p in second] == ["2026-2-00002", "2026-8-00002"]
    third = of.generate_pos(world.conn, world.buyer, world.approved(items=[world.item(alloc={SOUTH: 1})]))
    assert [p["po_number"] for p in third] == ["2026-4-00002"]
    numbers = [r["of_number"] for r in world.conn.execute("SELECT of_number FROM order_forms ORDER BY id")]
    assert numbers == ["2026-OF-00001", "2026-OF-00002", "2026-OF-00003"]
    clock(2027, 1, 1, 0, 5)
    fourth = of.generate_pos(world.conn, world.buyer, world.approved(items=[world.item(alloc={NORTH: 1})]))
    assert fourth[0]["po_number"] == "2027-2-00001"
    assert world.conn.execute("SELECT of_number FROM order_forms ORDER BY id DESC").fetchone()[0] == "2027-OF-00001"


def test_07b_drafts_consume_no_numbers(world, clock):
    clock(2026, 3, 1, 9, 0)
    for _ in range(3):
        world.draft(10)
    assert world.conn.execute("SELECT COUNT(*) FROM of_number_sequences").fetchone()[0] == 0
    of_id = world.draft(10)
    with pytest.raises(POError):
        of.generate_pos(world.conn, world.buyer, of_id)  # a draft can't make official POs
    of.submit(world.conn, world.buyer, of_id)
    assert of.get_of(world.conn, of_id)["of_number"] == "2026-OF-00001"


def _number_worker(path, count, out):
    conn = db.connect(path)
    user = auth.get_user(conn, 2)
    made = []
    for _ in range(count):
        of_id = of.create_draft(conn, user, {"supplier_id": 1, "of_date": "2026-11-02"},
                                [{"department_id": 1, "description": "x", "unit_cost_c": 1000, "allocations": {2: 1, 8: 1}}])
        of.submit(conn, user, of_id)
        made += [p["po_number"] for p in of.generate_pos(conn, user, of_id)]
    out.put(made)


def test_07c_numbers_safe_across_processes(world, env):
    ctx = multiprocessing.get_context("spawn")
    out = ctx.Queue()
    workers = [ctx.Process(target=_number_worker, args=(str(db.db_path()), 10, out)) for _ in range(4)]
    for w in workers:
        w.start()
    numbers = sum((out.get(timeout=120) for _ in workers), [])
    for w in workers:
        w.join()
    assert len(numbers) == 80 and len(set(numbers)) == 80
    for code in ("2", "8"):
        seqs = sorted(int(n[-5:]) for n in numbers if n.split("-")[1] == code)
        assert seqs == list(range(1, 41))


def test_08_generating_twice_makes_no_duplicates(world):
    of_id = world.approved(items=[world.item(alloc=ALL_FOUR)])
    first = of.generate_pos(world.conn, world.buyer, of_id)
    again = of.generate_pos(world.conn, world.buyer, of_id)
    assert [p["id"] for p in again] == [p["id"] for p in first]
    assert world.conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 4
    assert world.conn.execute("SELECT SUM(last_number) FROM po_number_sequences").fetchone()[0] == 4
    assert sum(1 for r in of.audit_trail(world.conn, of_id) if r["action"] == "OF_GENERATE_POS") == 1
    # The database itself refuses a second PO for the same OF and branch.
    p = first[0]
    with pytest.raises(sqlite3.IntegrityError):
        world.conn.execute("INSERT INTO purchase_orders(po_number, po_year, po_seq, po_date, of_id, supplier_id, branch_code, branch_name, status, prepared_by, created_at, updated_at)"
                           " VALUES ('X', 2026, 99, '2026-11-02', ?, ?, ?, 'A', 'APPROVED', 1, 'n', 'n')", (of_id, world.supplier, p["branch_code"]))


def test_09_exactly_100k_needs_no_approval(world):
    # Four branches, one OF: exactly the limit in total.
    of_id = world.draft(items=[world.item(cost=100000, alloc={NORTH: 40, SOUTH: 30, EAST: 20, WEST: 10})])
    assert of.submit(world.conn, world.buyer, of_id) == APPROVED
    o = of.get_of(world.conn, of_id)
    assert o["grand_total_c"] == 10000000 and o["auto_approved"] == 1 and o["of_number"]


def test_10_100001_needs_approval_on_the_whole_of(world):
    # No single branch is near the limit. The whole OF is over it, so it still needs approval.
    of_id = world.draft(items=[world.item(cost=100000, alloc={NORTH: 40, SOUTH: 30, EAST: 20, WEST: 10}), world.item(cost=100, alloc={NORTH: 1})])
    assert of.get_of(world.conn, of_id)["grand_total_c"] == 10000100
    assert of.submit(world.conn, world.buyer, of_id) == PENDING
    assert of.get_of(world.conn, of_id)["of_number"] is None
    with pytest.raises(POError):
        of.generate_pos(world.conn, world.buyer, of_id)
    number = of.approve(world.conn, world.admin, of_id, remarks="ok")
    assert number.endswith("-OF-00001")
    pos = of.generate_pos(world.conn, world.buyer, of_id)
    assert all(orders.get_po(world.conn, p["id"])["approved_by_name"] == "Boss Approver" for p in pos)


def test_10b_approval_basis_is_configurable(world):
    set_policy(world, basis="LARGEST_BRANCH")
    of_id = world.draft(items=[world.item(cost=100000, alloc={NORTH: 60, SOUTH: 60})])  # 120k in all, 60k per branch
    assert of.submit(world.conn, world.buyer, of_id) == APPROVED
    set_policy(world, basis="OF_TOTAL")
    assert of.submit(world.conn, world.buyer, world.draft(items=[world.item(cost=100000, alloc={NORTH: 60, SOUTH: 60})])) == PENDING


def test_11_disabling_and_reenabling_the_policy(world):
    pending = world.draft(250000)
    of.submit(world.conn, world.buyer, pending)
    of.approve(world.conn, world.admin, pending)
    set_policy(world, enabled=0)
    assert of.submit(world.conn, world.buyer, world.draft(250000)) == APPROVED
    set_policy(world, enabled=1)
    assert of.submit(world.conn, world.buyer, world.draft(100001)) == PENDING
    assert of.submit(world.conn, world.buyer, world.draft(100000)) == APPROVED
    assert [r["decision"] for r in of.approval_history(world.conn, pending)] == ["SUBMITTED", "APPROVED"]
    log = world.conn.execute("SELECT summary FROM audit_logs WHERE action='APPROVAL_POLICY_CHANGE'").fetchall()
    assert any("approval.enabled" in r["summary"] for r in log)


def test_11b_threshold_and_approver_are_configurable(world):
    set_policy(world, threshold_c=5000000)
    assert of.submit(world.conn, world.buyer, world.draft(50001)) == PENDING
    other = auth.create_user(world.conn, world.admin, "ana", "Ana Approver", PASSWORD, ["APPROVER"], must_change=False)
    set_policy(world, approver_user_ids=other.id)
    of_id = world.conn.execute("SELECT id FROM order_forms WHERE status=?", (PENDING,)).fetchone()["id"]
    with pytest.raises(POError):
        of.approve(world.conn, world.admin, of_id)
    assert of.approve(world.conn, other, of_id)


def test_12_warehouse_of_printout(world, env, monkeypatch):
    items = [world.item(dept=5, cost=100000, alloc=ALL_FOUR, description="Hardware A", item_code="SKU-1", brand="Acme", size="40-44", color="Black", selling_c=250000)]
    of_id = world.draft(items=items, warehouse_notes="Dock B only", internal_notes="SECRET-NOTE", remarks="Check seals")
    with pytest.raises(POError):
        pdf.render_warehouse_of(world.conn, of_id)
    of.submit(world.conn, world.buyer, of_id)
    of.approve(world.conn, world.admin, of_id, remarks="APPROVER-COMMENT")
    of.generate_pos(world.conn, world.buyer, of_id)
    printed = texts_of(monkeypatch)
    out = pdf.warehouse_of(world.conn, world.buyer, of_id, env / "of.pdf")
    assert out.read_bytes().startswith(b"%PDF")
    text = "\n".join(printed)
    for needed in ("2026-OF-00001", "North", "South", "East", "West", "2026-2-00001", "2026-4-00001", "2026-6-00001", "2026-8-00001",
                   "5 Hardware", "SKU-1", "Acme", "40-44", "Black", "Received", "Short", "Excess", "Damaged", "Warehouse remarks", "Checked by", "Boxes received", "Date received",
                   "Dock B only", "Check seals", "250"):
        assert needed in text, needed
    assert "1,000.00" in text and "OF total" in text and "Supplier agrees to costs" in text and "Not for the supplier" not in text  # the supplier signs its costs
    for secret in ("SECRET-NOTE", "APPROVER-COMMENT", "2,500.00"):  # never the selling price or anything internal
        assert secret not in text, secret
    assert any(r["action"] == "OF_PRINT_WAREHOUSE" for r in of.audit_trail(world.conn, of_id))


def test_13_14_supplier_po_printout(world, env, monkeypatch):
    item_a = world.item(dept=1, cost=50000, alloc={NORTH: 3, SOUTH: 777}, selling_c=99900, fx_currency="Y", fx_rate_m=26000, fx_cost_m=8500,
                        item_code="SKU-1", brand="Acme", size="M", color="Red")
    other = world.item(dept=6, cost=1000, alloc={SOUTH: 5}, description="Only at South")
    of_id = world.draft(items=[item_a, world.item(dept=6, alloc={NORTH: 1}), other], internal_notes="SECRET-NOTE", warehouse_notes="WAREHOUSE-NOTE",
                        remarks="Rush please", delivery_instructions="Back gate", expected_delivery_date="2026-12-01")
    of.submit(world.conn, world.buyer, of_id)
    of.approve(world.conn, world.admin, of_id, remarks="APPROVER-COMMENT")
    pos = of.generate_pos(world.conn, world.buyer, of_id)
    printed = texts_of(monkeypatch)
    out = pdf.generate(world.conn, pos[0]["id"], env / "po.pdf", preview=False)
    assert out.stat().st_size > 2000 and out.read_bytes().startswith(b"%PDF")
    text = "\n".join(printed)
    for needed in ("ATLAS PO ERP", "2026-2-00001", "2026-OF-00001", "North", "PURCHASE ORDER", "Delivery Receipt (DR)", "2026-2-DR-00001",
                   "SKU-1", "Acme", "Net unit", "Prepared by", "Approved by", "Rush please", "Back gate", "December 1, 2026", "Subtotal", "GRAND TOTAL"):
        assert needed in text, needed
    for secret in ("SECRET-NOTE", "WAREHOUSE-NOTE", "APPROVER-COMMENT", "999.00", "26.00", "8.50", "777", "Only at South", "South"):
        assert secret not in text, secret
    assert "PREVIEW" not in text
    printed.clear()
    pdf.render_supplier_po(world.conn, pos[0]["id"])  # not issued yet, so it says so
    assert "PREVIEW" in "\n".join(printed)


def test_15_issued_documents_keep_their_original_version(world):
    po_id = world.po(items=[world.item(cost=10000, alloc={NORTH: 10}, description="Polo")], remarks="Original")
    assert orders.issued_pdf(world.conn, None, po_id) is None
    orders.issue(world.conn, world.buyer, po_id)
    po = orders.get_po(world.conn, po_id)
    assert po["status"] == ISSUED and po["issued_at"] and po["issued_by"] == world.buyer.id
    original = orders.issued_pdf(world.conn, None, po_id)
    assert orders.issued_pdf(world.conn, world.buyer, po_id) != original  # a person printing again gets a marked copy
    assert original.startswith(b"%PDF")
    # A later supplier rename changes neither the stored document nor the PO's own copy of the supplier.
    masters.save_supplier(world.conn, world.admin, {"code": "ABC", "name": "Renamed Company"}, world.supplier, confirm_duplicate=True)
    assert orders.get_po(world.conn, po_id)["supplier"]["name"] == "ABC Trading"
    # Amending after issue makes a controlled revision, waiting to be issued, and the first version stays.
    items = _existing(po)
    items[0]["qty_m"] = 8000
    result = orders.amend(world.conn, world.buyer, po_id, {"remarks": "Cut"}, items, "Supplier short")
    assert result["status"] == "APPLIED" and result["revision"] == 1
    after = orders.get_po(world.conn, po_id)
    assert after["status"] == APPROVED and after["revision"] == 1 and after["po_number"] == po["po_number"]
    # It goes back on the Open tab until the new revision is issued.
    assert po_id in [r["id"] for r in orders.search(world.conn, tab=orders.OPEN)]
    assert orders.issued_pdf(world.conn, None, po_id, 0) == original
    orders.issue(world.conn, world.buyer, po_id)
    versions = orders.issued_versions(world.conn, po_id)
    assert [v["revision"] for v in versions] == [0, 1]
    assert orders.issued_pdf(world.conn, None, po_id, 1) != original and orders.issued_pdf(world.conn, None, po_id, 0) == original
    assert json.loads(world.conn.execute("SELECT snapshot_json FROM po_issued_versions WHERE revision=0").fetchone()[0])["remarks"] == "Original"
    with pytest.raises(sqlite3.IntegrityError):
        world.conn.execute("UPDATE po_issued_versions SET pdf = x'00'")
    with pytest.raises(sqlite3.IntegrityError):
        world.conn.execute("DELETE FROM po_issued_versions")
    assert any(r["action"] == "PO_REPRINT" for r in orders.audit_trail(world.conn, po_id))


def test_15b_only_approved_can_be_issued(world):
    po_id = world.po(10)
    orders.issue(world.conn, world.buyer, po_id)
    with pytest.raises(POError):
        orders.issue(world.conn, world.buyer, po_id)
    orders.cancel(world.conn, world.buyer, po_id, "test")
    with pytest.raises(POError):
        orders.issue(world.conn, world.buyer, po_id)


def test_16_cancelled_numbers_are_never_reused(world):
    first = world.approved(items=[world.item(alloc=ALL_FOUR)])
    pos = of.generate_pos(world.conn, world.buyer, first)
    of_number = of.get_of(world.conn, first)["of_number"]
    of.cancel(world.conn, world.buyer, first, "Supplier closed")
    assert of.get_of(world.conn, first)["status"] == CANCELLED and of.get_of(world.conn, first)["of_number"] == of_number
    assert all(orders.get_po(world.conn, p["id"])["status"] == CANCELLED for p in pos)
    second = world.approved(items=[world.item(alloc={NORTH: 1})])
    assert of.get_of(world.conn, second)["of_number"] != of_number
    assert of.generate_pos(world.conn, world.buyer, second)[0]["po_number"].endswith("-00002")
    with pytest.raises(sqlite3.IntegrityError):
        world.conn.execute("UPDATE po_number_sequences SET last_number = 0")
    with pytest.raises(sqlite3.IntegrityError):
        world.conn.execute("UPDATE of_number_sequences SET last_number = 0")
    with pytest.raises(sqlite3.IntegrityError):
        world.conn.execute("DELETE FROM order_forms WHERE id = ?", (first,))
    with pytest.raises(sqlite3.IntegrityError):
        world.conn.execute("DELETE FROM purchase_orders WHERE id = ?", (pos[0]["id"],))


def test_16b_cancelling_one_branch_po_updates_the_of(world):
    of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 10, SOUTH: 5})])
    pos = of.generate_pos(world.conn, world.buyer, of_id)
    orders.cancel(world.conn, world.buyer, pos[1]["id"], "Branch doesn't need it")
    o = of.get_of(world.conn, of_id)
    assert o["status"] == APPROVED and o["total_qty"] == 10 and o["grand_total_c"] == 100000
    assert o["items"][0]["allocations"] == {NORTH: 10}
    assert backup.integrity_report(world.conn) == []


def test_17_audit_history(world):
    of_id = world.draft(150000)
    of.update_draft(world.conn, world.buyer, of_id, world.header(remarks="changed"), [world.item(cost=15000000, alloc={NORTH: 1, SOUTH: 1})])
    of.submit(world.conn, world.buyer, of_id)
    of.reject(world.conn, world.admin, of_id, "Too much stock")
    of.submit(world.conn, world.buyer, of_id)
    of.approve(world.conn, world.admin, of_id, remarks="Reduced elsewhere")
    pos = of.generate_pos(world.conn, world.buyer, of_id)
    orders.issue(world.conn, world.buyer, pos[0]["id"])
    trail = of.audit_trail(world.conn, of_id)
    actions = [r["action"] for r in trail]
    assert actions == ["OF_CREATE", "OF_MODIFY", "OF_ALLOCATION_CHANGE", "OF_SUBMIT", "OF_REJECT", "OF_SUBMIT", "OF_APPROVE",
                       "PO_GENERATE", "PO_GENERATE", "OF_GENERATE_POS", "PO_ISSUE", "DR_CREATE"]
    assert all(r["occurred_at"] and r["user_name"] for r in trail)
    modify = next(r for r in trail if r["action"] == "OF_MODIFY")
    assert json.loads(modify["previous_value"])["header"]["remarks"] == "" and json.loads(modify["new_value"])["header"]["remarks"] == "changed"
    assert next(r for r in trail if r["action"] == "PO_ISSUE")["document_number"] == pos[0]["po_number"]
    assert next(r for r in trail if r["action"] == "OF_GENERATE_POS")["document_number"] == "2026-OF-00001"
    history = of.approval_history(world.conn, of_id)
    assert [r["decision"] for r in history] == ["SUBMITTED", "REJECTED", "SUBMITTED", "APPROVED"]
    assert history[1]["reason"] == "Too much stock" and history[3]["decided_name"] == "Boss Approver" and history[0]["basis"] == "OF_TOTAL"
    for sql in ("DELETE FROM audit_logs", "UPDATE audit_logs SET action='x'", "DELETE FROM of_approvals", "DELETE FROM po_approvals"):
        with pytest.raises(sqlite3.IntegrityError):
            world.conn.execute(sql)
    masters.save_supplier(world.conn, world.admin, {"code": "ABC", "name": "ABC Trading Inc"}, world.supplier, confirm_duplicate=True)
    assert world.conn.execute("SELECT 1 FROM audit_logs WHERE action='SUPPLIER_UPDATE'").fetchone()
    pid = masters.save_product(world.conn, world.admin, {"item_code": "P1", "department_id": 1, "description": "P", "last_cost_c": 100})
    masters.save_product(world.conn, world.admin, {"item_code": "P1", "department_id": 1, "description": "P", "last_cost_c": 200}, pid)
    assert world.conn.execute("SELECT 1 FROM audit_logs WHERE action='PRODUCT_COST_CHANGE'").fetchone()


def test_18_backup_and_restore(world, env):
    keep = world.approved(items=[world.item(alloc=ALL_FOUR)])
    of.generate_pos(world.conn, world.buyer, keep)
    saved = backup.manual_backup(world.conn, world.admin)
    assert saved.exists()
    world.draft(10)
    assert world.conn.execute("SELECT COUNT(*) FROM order_forms").fetchone()[0] == 2
    safety = backup.restore(world.conn, world.admin, saved, db.db_path())
    assert safety.exists()
    fresh = db.open_database()
    assert fresh.execute("SELECT COUNT(*) FROM order_forms").fetchone()[0] == 1
    assert fresh.execute("SELECT COUNT(*) FROM purchase_orders WHERE of_id=?", (keep,)).fetchone()[0] == 4
    assert fresh.execute("SELECT 1 FROM audit_logs WHERE action='RESTORE'").fetchone()
    assert backup.integrity_report(fresh) == []
    assert sqlite3.connect(safety).execute("SELECT COUNT(*) FROM order_forms").fetchone()[0] == 2
    # Numbering carries on from the restored counters.
    user = auth.get_user(fresh, world.buyer.id)
    next_of = of.create_draft(fresh, user, world.header(), [world.item(alloc={NORTH: 1})])
    of.submit(fresh, user, next_of)
    assert of.generate_pos(fresh, user, next_of)[0]["po_number"].endswith("-00002")
    fresh.close()


def test_19_historical_branch_records_stay_intact_through_migration(env):
    """An old database with POs at old branches comes through with every number, amount and name as it was."""
    path = env / "po.sqlite"
    old = _version_1_database(path)
    old.close()
    conn = db.open_database(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
    backups = list((env / "backups").glob("po-before-migration-v1-*.sqlite"))
    assert len(backups) == 1
    assert sqlite3.connect(backups[0]).execute("PRAGMA user_version").fetchone()[0] == 1
    pos = {r["po_number"]: r for r in conn.execute("SELECT * FROM purchase_orders WHERE po_number IS NOT NULL")}
    assert set(pos) == {"2026PO-00001", "2026PO-00002"}
    one = orders.get_po(conn, pos["2026PO-00001"]["id"])
    assert one["branch"] == {"code": 8, "name": "West", "address": "Old address"}  # as printed then
    assert one["items"][0]["qty"] == 5 and one["grand_total_c"] == 50000 and one["total_qty"] == 5 and one["status"] == "ISSUED"
    assert conn.execute("SELECT name FROM branches WHERE code = 8").fetchone()[0] == "West"  # as named now
    assert conn.execute("SELECT active FROM branches WHERE code = 1").fetchone()[0] == 0
    two = orders.get_po(conn, pos["2026PO-00002"]["id"])
    assert two["branch"]["code"] == 1 and two["branch"]["name"] == "Old Town Old"  # an inactive branch's history is kept
    assert conn.execute("SELECT last_number FROM legacy_po_number_sequences WHERE year = 2026").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='PO_ISSUE'").fetchone()[0] == 1
    assert backup.integrity_report(conn) == []
    # Old POs keep working: amended, re-issued, and their numbers don't collide with the new format.
    user = auth.get_user(conn, 1)
    db.set_settings(conn, user, {"approval.allow_self_approval": "1"})
    items = [{k: one["items"][0][k] for k in (*orders.ITEM_FIELDS, "id")}]
    items[0]["qty_m"] = 4000
    assert orders.amend(conn, user, one["id"], {}, items, "Short")["status"] == "APPLIED"
    orders.issue(conn, user, one["id"])
    assert orders.get_po(conn, one["id"])["revision"] == 1
    sup = conn.execute("SELECT id FROM suppliers").fetchone()[0]
    of_id = of.create_draft(conn, user, {"supplier_id": sup}, [{"department_id": 1, "description": "n", "unit_cost_c": 1000, "allocations": {NORTH: 1, WEST: 1}}])
    of.submit(conn, user, of_id)
    numbers = [p["po_number"] for p in of.generate_pos(conn, user, of_id)]
    assert all(n.endswith("-00001") and "PO" not in n for n in numbers)
    conn.close()
    # Opening again doesn't migrate again.
    again = db.open_database(path)
    assert len(list((env / "backups").glob("po-before-migration-*"))) == 1
    again.close()


def _version_1_database(path) -> sqlite3.Connection:
    stamp = "2026-10-01 09:00:00"
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.executescript(schema.DDL)
    c.executemany("INSERT INTO departments(id, name) VALUES (?, ?)", schema.DEPARTMENTS)
    c.executemany("INSERT INTO destinations(code, name, address, legacy_outlet) VALUES (?, ?, ?, ?)",
                  [("WST", "West", "Old address", 8), ("NTH", "North", "", 2), ("OLD", "Old Town Old", "", 1), ("WH", "Main Warehouse", "", None)])
    c.executemany("INSERT INTO system_settings(key, value, updated_at) VALUES (?, ?, ?)",
                  [(k, v, stamp) for k, v in schema.DEFAULT_SETTINGS.items() if k.split(".")[0] != "x"])
    c.execute("INSERT INTO users(id, username, full_name, password_hash, created_at) VALUES (1, 'old', 'Old User', 'x', ?)", (stamp,))
    c.execute("INSERT INTO user_roles VALUES (1, 'ADMIN')")
    c.execute("INSERT INTO user_roles VALUES (1, 'APPROVER')")
    c.execute("INSERT INTO suppliers(id, code, name, created_at, updated_at) VALUES (1, 'SUP', 'Old Supplier', ?, ?)", (stamp, stamp))
    c.execute("INSERT INTO po_number_sequences VALUES (2026, 2)")
    for pid, number, seq, dest, status, qty in ((1, "2026PO-00001", 1, 1, "DRAFT", 5), (2, "2026PO-00002", 2, 3, "DRAFT", 2), (3, None, None, 2, "DRAFT", 1)):
        total = qty * 10000
        c.execute("INSERT INTO purchase_orders(id, po_date, supplier_id, destination_id, status, total_qty, total_pieces, gross_c, grand_total_c, prepared_by, created_at, updated_at)"
                  " VALUES (?, '2026-10-01', 1, ?, 'DRAFT', ?, ?, ?, ?, 1, ?, ?)", (pid, dest, qty, qty, total, total, stamp, stamp))
        c.execute("INSERT INTO purchase_order_items(po_id, line_no, department_id, description, qty, unit_cost_c, net_unit_c, line_gross_c, line_discount_c, line_total_c)"
                  " VALUES (?, 1, 1, 'Old item', ?, 10000, 10000, ?, 0, ?)", (pid, qty, total, total))
        if number:
            final = "ISSUED" if pid == 1 else "APPROVED"
            c.execute("UPDATE purchase_orders SET status=?, po_number=?, po_year=2026, po_seq=?, issued_at=? WHERE id=?",
                      (final, number, seq, stamp if pid == 1 else None, pid))
    c.execute("INSERT INTO po_audit_logs(occurred_at, user_id, user_name, action, entity_type, po_id) VALUES (?, 1, 'Old User', 'PO_ISSUE', 'purchase_order', 1)", (stamp,))
    c.execute("PRAGMA user_version = 1")
    c.commit()
    return c


def test_19b_failed_migration_leaves_the_database_as_it_was(env, monkeypatch):
    path = env / "po.sqlite"
    _version_1_database(path).close()
    monkeypatch.setattr(schema, "MIGRATE_V2", schema.MIGRATE_V2.replace("PRAGMA user_version = 2;", "THIS IS NOT SQL;"))
    with pytest.raises(sqlite3.Error):
        db.open_database(path)
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 1
    assert raw.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 3
    assert raw.execute("SELECT 1 FROM sqlite_master WHERE name='destinations'").fetchone()
    assert not raw.execute("SELECT 1 FROM sqlite_master WHERE name='order_forms'").fetchone()
    raw.close()


def test_19d_version_3_removes_only_unused_inactive_mb_branches(env):
    path = env / "po.sqlite"
    v2 = _version_1_database(path)
    v2.execute("PRAGMA foreign_keys = OFF")
    v2.executescript("BEGIN;" + schema.MIGRATE_V2 + "COMMIT;")
    assert {r[0] for r in v2.execute("SELECT code FROM branches WHERE code IN (1, 3, 5)")} == {1, 3, 5}
    v2.execute("UPDATE branches SET active = 1 WHERE code = 5")  # someone switched it back on
    v2.commit()
    v2.close()
    conn = db.open_database(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
    assert list((env / "backups").glob("po-before-migration-v2-*.sqlite"))
    remaining = {r[0] for r in conn.execute("SELECT code FROM branches")}
    assert 1 in remaining  # has a PO
    assert 5 in remaining  # active
    assert 3 not in remaining  # unused and inactive
    assert conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM branches WHERE code = 2")
    assert conn.execute("SELECT 1 FROM audit_logs WHERE action='MIGRATE_V3'").fetchone()
    assert backup.integrity_report(conn) == []
    conn.close()


def test_19e_version_4_standardizes_suppliers_and_keeps_the_approver(env):
    path = env / "po.sqlite"
    v3 = _version_1_database(path)
    v3.execute("PRAGMA foreign_keys = OFF")
    v3.executescript("BEGIN;" + schema.MIGRATE_V2 + "COMMIT;BEGIN;" + schema.MIGRATE_V3 + "COMMIT;")
    stamp = "2026-10-01 09:00:00"
    rows = [("-ML", "Dash", "60 D"), ("-MM", "Dash clash", "90 D"), ("MM", "Clean", "30D"), ("HALF", "Discount", "5.0% + 14 D / 60 D"), ("BLNK", "Blank", ""),
            ("WEEK", "Week", "7 d"), ("LONG", "Long", "120 D"), ("CASH", "Cash", "COD")]
    v3.executemany("INSERT INTO suppliers(code, name, payment_terms, created_at, updated_at) VALUES (?,?,?,?,?)", [(*r, stamp, stamp) for r in rows])
    v3.execute("DELETE FROM system_settings WHERE key = 'approval.approver_user_ids'")
    v3.execute("INSERT INTO system_settings(key, value, updated_at) VALUES ('approval.approver_user_id', '1', ?)", (stamp,))
    v3.commit()
    v3.close()
    conn = db.open_database(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
    found = {r["name"]: r for r in conn.execute("SELECT * FROM suppliers")}
    assert (found["Dash"]["code"], found["Dash"]["payment_terms"]) == ("ML", "60D")
    assert "Old code -ML" in found["Dash"]["remarks"]
    assert (found["Dash clash"]["code"], found["Dash clash"]["payment_terms"]) == ("-MM", "90D")  # left for a person to recode
    assert (found["Clean"]["code"], found["Clean"]["payment_terms"]) == ("MM", "30D")
    assert found["Discount"]["payment_terms"] == "5.0% + 14 D / 60 D" and found["Cash"]["payment_terms"] == "COD"  # custom terms stay
    assert found["Week"]["payment_terms"] == "7D" and found["Long"]["payment_terms"] == "120D"
    assert found["Blank"]["payment_terms"] == "None" and found["Blank"]["remarks"] == ""
    assert orders.policy(conn)["approver_user_ids"] == [1]
    assert not conn.execute("SELECT 1 FROM system_settings WHERE key = 'approval.approver_user_id'").fetchone()
    conn.close()


def test_19c_restoring_a_version_1_backup_brings_it_up_to_date(world, env):
    old_file = env / "old-backup.sqlite"
    _version_1_database(old_file).close()
    backup.restore(world.conn, world.admin, old_file, db.db_path())
    fresh = db.open_database()
    assert fresh.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
    assert fresh.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 3
    assert fresh.execute("SELECT 1 FROM audit_logs WHERE action='RESTORE'").fetchone()
    fresh.close()


# ------------------------------------------------------------------ rules beyond the numbered tests

@pytest.mark.parametrize("code", ["ABCDE", "A-B", "-ML", "A B", "Ñ1"])
def test_supplier_code_is_up_to_4_letters_or_numbers(world, code):
    with pytest.raises(POError, match="up to 4"):
        masters.save_supplier(world.conn, world.admin, {"code": code, "name": "New Supplier Ltd"})


def test_supplier_code_accepts_short_mixed_and_lowercase(world):
    made = masters.save_supplier(world.conn, world.admin, {"code": "a1b2", "name": "New Supplier Ltd"})
    assert world.conn.execute("SELECT code FROM suppliers WHERE id=?", (made,)).fetchone()[0] == "A1B2"


@pytest.mark.parametrize("typed, stored", [("7D", "7D"), ("30D", "30D"), ("45 days", "45D"), ("60 d", "60D"), ("90", "90D"), ("120 Days", "120D"),
                                           ("", "None"), ("none", "None"), ("COD", "COD"), ("  TT  50% down ", "TT 50% down"), ("3% 30D", "3% 30D")])
def test_terms_are_written_one_way_and_custom_ones_stay_as_typed(world, typed, stored):
    made = masters.save_supplier(world.conn, world.admin, {"code": "NEW", "name": "New Supplier Ltd", "payment_terms": typed})
    assert world.conn.execute("SELECT payment_terms FROM suppliers WHERE id=?", (made,)).fetchone()[0] == stored
    assert of.get_of(world.conn, world.draft(payment_terms=typed or "None"))["payment_terms"] == stored


def test_very_long_terms_are_refused(world):
    with pytest.raises(POError, match="60 characters"):
        masters.save_supplier(world.conn, world.admin, {"code": "NEW", "name": "New Supplier Ltd", "payment_terms": "x" * 61})
    with pytest.raises(POError, match="60 characters"):
        world.draft(payment_terms="x" * 61)


def test_order_form_takes_supplier_terms_or_its_own_standard_choice(world):
    assert of.get_of(world.conn, world.draft())["payment_terms"] == "30D"
    assert of.get_of(world.conn, world.draft(payment_terms="None"))["payment_terms"] == "None"


def test_older_po_terms_stay_until_someone_changes_them(world):
    po_id = world.po()
    po = orders.get_po(world.conn, po_id)
    items = [{k: po["items"][0][k] for k in (*orders.ITEM_FIELDS, "id")}]
    with pytest.raises(POError, match="Nothing changed"):
        orders.amend(world.conn, world.buyer, po_id, {"payment_terms": po["payment_terms"]}, items, "No change")
    assert orders.get_po(world.conn, po_id)["payment_terms"] == po["payment_terms"]
    # New terms move the check date, so they wait for approval while the policy is on. Once approved, they're stored the standard way.
    result = orders.amend(world.conn, world.buyer, po_id, {"payment_terms": "45 days"}, items, "New terms")
    assert result["status"] == "PENDING_APPROVAL" and orders.get_po(world.conn, po_id)["payment_terms"] == "30D"
    orders.decide_amendment(world.conn, world.admin, result["amendment_id"], True)
    assert orders.get_po(world.conn, po_id)["payment_terms"] == "45D"


def test_any_listed_approver_can_approve_and_others_are_told_who_can(world):
    second = auth.create_user(world.conn, world.admin, "ana", "Ana Approver", PASSWORD, ["APPROVER"], must_change=False)
    third = auth.create_user(world.conn, world.admin, "ben", "Ben Approver", PASSWORD, ["APPROVER"], must_change=False)
    set_policy(world, approver_user_ids=f"{world.admin.id},{second.id}")
    first_of, second_of, third_of = world.draft(200000), world.draft(200000), world.draft(200000)
    for of_id in (first_of, second_of, third_of):
        of.submit(world.conn, world.buyer, of_id)
    assert of.approve(world.conn, world.admin, first_of)
    assert of.approve(world.conn, second, second_of)
    with pytest.raises(POError) as stopped:
        of.approve(world.conn, third, third_of)
    assert "Ana Approver or Boss Approver" in str(stopped.value) and "Settings, Approval policy" in str(stopped.value)
    assert orders.approver_names(world.conn) == "Ana Approver or Boss Approver"


def test_prepared_by_a_listed_approver_goes_to_another_unless_allowed(world):
    second = auth.create_user(world.conn, world.admin, "ana", "Ana Approver", PASSWORD, ["APPROVER"], must_change=False)
    set_policy(world, approver_user_ids=f"{world.admin.id},{second.id}")
    of_id = of.create_draft(world.conn, world.admin, world.header(), [world.item(cost=20000000)])
    of.submit(world.conn, world.admin, of_id)
    with pytest.raises(POError, match="someone else"):
        of.approve(world.conn, world.admin, of_id)
    assert of.approve(world.conn, second, of_id)


def test_documents_print_a_header_for_each_department_with_subtotals(world, env, monkeypatch):
    items = [world.item(dept=5, cost=10000, alloc={NORTH: 2}, description="Shoe"), world.item(dept=1, cost=5000, alloc={NORTH: 3, SOUTH: 4}, description="Shirt A"),
             world.item(dept=1, cost=5000, alloc={NORTH: 1}, description="Shirt B")]
    of_id = world.approved(items=items)
    pos = of.generate_pos(world.conn, world.buyer, of_id)
    printed = []
    real = pdf.Paragraph
    monkeypatch.setattr(pdf, "Paragraph", lambda text, *a, **k: printed.append(text) or real(text, *a, **k))
    pdf.render_warehouse_of(world.conn, of_id)
    names = [t.split("<br/>")[0] for t in printed]  # A product's details print on a line under its name.
    order = [t for t in names if t in ("1 Men&#x27;s Wear", "1 Grocery", "Shirt A", "Shirt B", "5 Hardware", "Shoe")]
    assert order == ["1 Grocery", "Shirt A", "Shirt B", "5 Hardware", "Shoe"]
    printed.clear()
    pdf.render_supplier_po(world.conn, next(p["id"] for p in pos if p["branch_code"] == NORTH), preview=False)
    # Header first, then its rows, with the amount for that department: 4 shirts at 50.00, 2 shoes at 100.00.
    at = printed.index("1 Grocery")
    assert "4" in printed[at + 1] and printed[at + 2] == "200.00"
    assert printed.index("1 Grocery") < printed.index("Shirt A") < printed.index("Shirt B") < printed.index("5 Hardware") < printed.index("Shoe")


def _printed_text(monkeypatch):
    printed = []
    real = pdf.Paragraph
    monkeypatch.setattr(pdf, "Paragraph", lambda text, *a, **k: printed.append(text) or real(text, *a, **k))
    return printed


def test_the_signed_and_office_copies_show_stickers_and_the_po_doesnt(world, monkeypatch):
    masters.save_supplier(world.conn, world.admin, {"code": "ABC", "name": "ABC Trading", "sticker_rate_c": 50}, world.supplier, confirm_duplicate=True)
    of_id = world.approved(items=[world.item(cost=9000, uom="3 IN 1", alloc={NORTH: 10}, description="Socks", disc1_bp=1000)], internal_notes="OFFICE-NOTE")
    po = of.generate_pos(world.conn, world.buyer, of_id)[0]
    printed = _printed_text(monkeypatch)
    pdf.render_office_of(world.conn, of_id)
    text = " ".join(printed)
    # 30 pieces, 10 packs at 90.00 less 10%: 81.00 a pack, 27.00 a piece, 810.00 in all. Stickers are 15.00.
    assert "FINAL COPY" in text and "90.00" in text and "27.00" in text and "810.00" in text and "OFFICE-NOTE" in text
    assert "30 pcs x 0.50" in text and "15.00" in text and "795.00" in text and "To pay after stickers" in text
    assert "Received:" in text and "Final deduction" in text and "Final to pay" in text
    printed.clear()
    pdf.render_warehouse_of(world.conn, of_id)
    text = " ".join(printed)
    assert "30" in printed[next(i for i, t in enumerate(printed) if t.startswith("Socks")):]  # the branch quantity prints in pieces
    # What the supplier signs shows what they'll be paid. The blanks for settling it are accounting's.
    assert "90.00" in text and "810.00" in text and "30 pcs x 0.50" in text and "795.00" in text and "To pay after stickers" in text
    for hidden in ("Final deduction", "OFFICE-NOTE"):
        assert hidden not in text
    printed.clear()
    pdf.render_supplier_po(world.conn, po["id"], preview=False)
    assert "Sticker" not in " ".join(printed) and "OFFICE-NOTE" not in " ".join(printed) and "30" in printed
    # No charge, no sticker lines on the office copy either.
    masters.save_supplier(world.conn, world.admin, {"code": "ABC", "name": "ABC Trading", "sticker_rate_c": 0}, world.supplier, confirm_duplicate=True)
    plain = world.approved(items=[world.item()])
    printed.clear()
    pdf.render_office_of(world.conn, plain)
    assert "OF total" in " ".join(printed) and "Sticker" not in " ".join(printed)


def test_blind_sheet_lists_only_what_to_look_for(world, monkeypatch):
    of_id = world.approved(items=[world.item(dept=5, cost=7777, uom="3 IN 1", alloc={NORTH: 41, SOUTH: 53}, description="Socks", item_code="SOCK-1", size="M-XL", color="Red", selling_c=88800),
                                  world.item(dept=1, cost=6666, alloc={NORTH: 37}, description="Shirt", item_code="SHIRT-1")], internal_notes="SECRET-NOTE", warehouse_notes="WH-NOTE")
    north = next(p for p in of.generate_pos(world.conn, world.buyer, of_id) if p["branch_code"] == NORTH)
    printed = _printed_text(monkeypatch)
    pdf.render_blind_sheet(world.conn, north["id"])
    text = " ".join(printed)
    for shown in ("BLIND RECEIVING SHEET", north["po_number"], "North", "SOCK-1", "Socks", "M-XL", "Red", "3 IN 1", "SHIRT-1", "1 Grocery", "5 Hardware", "Pieces received", "Counted by", "Backup form", "Nothing is recorded until then"):
        assert shown in text, shown
    # No quantity in any form, no money, no other branch, no notes.
    for hidden in ("123", "41", "37", "160", "77.77", "66.66", "888.00", "South", "SECRET-NOTE", "WH-NOTE", "Total"):
        assert hidden not in text, hidden
    assert printed.index("1 Grocery") < printed.index("Shirt") < printed.index("5 Hardware") < printed.index("Socks")


def test_blind_sheet_keeps_out_a_pos_quantity_even_in_the_paragraph_count(world):
    from io import BytesIO
    from pypdf import PdfReader
    of_id = world.approved(items=[world.item(cost=7777, alloc={NORTH: 4321}, description="Socks", item_code="SOCK-1")])
    po = of.generate_pos(world.conn, world.buyer, of_id)[0]
    words = PdfReader(BytesIO(pdf.render_blind_sheet(world.conn, po["id"]))).pages[0].extract_text()
    assert "SOCK-1" in words and "4321" not in words and "4,321" not in words and "77.77" not in words


@pytest.mark.parametrize("cost, price", [("27.30", "40.00"), ("29.40", "50.00"), ("31.50", "50.00"), ("28.00", "40.00"), ("28.01", "50.00"), ("0.70", "10.00"), ("100.01", "150.00")])
def test_selling_price_is_a_30_percent_margin_up_to_a_multiple_of_10_pesos(cost, price):
    # Dividing by 0.7, 27.30 is 39, so 40. 29.40 is 42, so 50. 31.50 is 45, so 50. 28.00 is exactly 40, so it stays.
    assert selling_from_cost(parse_pesos(cost)) == parse_pesos(price)


def test_selling_price_never_gives_less_than_the_margin():
    for cost in range(1, 200000, 37):
        price = selling_from_cost(cost)
        assert price % 1000 == 0 and (price - cost) * 100 >= price * 30 and price - cost * 100 / 70 < 1000
    assert selling_from_cost(0) is None


def test_cut_slip_tells_a_branch_its_po_number_and_nothing_about_the_goods(world):
    from io import BytesIO
    from pypdf import PdfReader
    of_id = world.approved(items=[world.item(cost=7777, alloc={NORTH: 4321, SOUTH: 1234}, description="Socks", item_code="SOCK-1")])
    pos = of.generate_pos(world.conn, world.buyer, of_id)
    ids = [p["id"] for p in pos]
    words = PdfReader(BytesIO(pdf.render_cut_slips(world.conn, ids))).pages[0].extract_text()
    for wanted in (pos[0]["po_number"], pos[1]["po_number"], "North (2)", "South (4)", "ABC Trading", "To be confirmed"):
        assert wanted in words, wanted
    for hidden in ("SOCK-1", "Socks", "4321", "4,321", "1234", "1,234", "77.77"):
        assert hidden not in words, hidden
    # Four fit on a page, and more run on to the next.
    assert len(PdfReader(BytesIO(pdf.render_cut_slips(world.conn, ids * 2))).pages) == 1
    assert len(PdfReader(BytesIO(pdf.render_cut_slips(world.conn, ids * 3))).pages) == 2


def test_po_goes_to_the_main_warehouse_for_its_branch(world, monkeypatch):
    po = world.po(items=[world.item(alloc={NORTH: 1})])
    printed = _printed_text(monkeypatch)
    pdf.render_supplier_po(world.conn, po, preview=False)
    text = " ".join(printed)
    assert "DELIVER TO" in text and "Central Warehouse" in text and "FOR BRANCH" in text and "North (2)" in text
    db.set_settings(world.conn, world.admin, {"company.warehouse_name": "Valenzuela Warehouse", "company.warehouse_address": "12 Depot Road"})
    other = world.po(items=[world.item(alloc={NORTH: 1})])
    printed.clear()
    pdf.render_supplier_po(world.conn, other, preview=False)
    assert "Valenzuela Warehouse" in printed and "12 Depot Road" in printed and "Central Warehouse" not in printed


def test_cost_per_piece_comes_from_the_pack_or_dozen():
    from atlaspo.money import per_piece
    assert per_piece(9000, 3) == 3000 and per_piece(180000, 12) == 15000 and per_piece(25000, 1) == 25000
    assert per_piece(10000, 3) == 3333  # rounded to the centavo
    assert per_piece(100, 0) == 100


def test_parse_pieces_turns_pieces_into_whole_packs():
    from atlaspo.money import parse_pieces
    assert parse_pieces("30", "3 IN 1") == 10000 and parse_pieces("24", "1 DOZEN") == 2000 and parse_pieces("12", "4 IN 1") == 3000 and parse_pieces("7", "PC") == 7000
    assert parse_pieces("", "3 IN 1") == 0 and parse_pieces("1.5", "M") == 1500
    for text, uom in (("10", "3 IN 1"), ("7", "6 IN 1"), ("13", "1 DOZEN")):
        with pytest.raises(ValueError, match="multiple of"):
            parse_pieces(text, uom)


def test_batch_issues_what_it_can_and_combines_in_order(world, env):
    of_id = world.approved(items=[world.item(alloc=ALL_FOUR)])
    pos = [p["id"] for p in of.generate_pos(world.conn, world.buyer, of_id)]
    orders.cancel(world.conn, world.buyer, pos[1], "Branch doesn't need it")
    done, problems = orders.issue_batch(world.conn, world.buyer, pos)
    assert done == [pos[0], pos[2], pos[3]]
    assert len(problems) == 1 and orders.get_po(world.conn, pos[1])["po_number"] in problems[0]
    documents = [orders.issued_pdf(world.conn, None, p) for p in done]
    from pypdf import PdfReader
    from io import BytesIO
    combined = PdfReader(BytesIO(pdf.combine(documents)))
    assert len(combined.pages) == sum(len(PdfReader(BytesIO(d)).pages) for d in documents)
    numbers = [orders.get_po(world.conn, p)["po_number"] for p in done]
    texts = [page.extract_text() for page in combined.pages]
    assert [i for n in numbers for i, t in enumerate(texts) if n in t] == sorted(i for n in numbers for i, t in enumerate(texts) if n in t)


def test_restore_refuses_a_bad_file(world, env):
    bad = env / "bad.sqlite"
    bad.write_bytes(b"not a database at all")
    with pytest.raises(POError):
        backup.restore(world.conn, world.admin, bad, db.db_path())
    assert world.conn.execute("SELECT COUNT(*) FROM departments").fetchone()[0] == 9


def test_auto_backup_once_a_day_and_keeps_manual(world, clock):
    clock(2026, 11, 2, 8, 0)
    first = backup.auto_backup(world.conn)
    assert backup.auto_backup(world.conn) == first
    clock(2027, 1, 15, 8, 0)
    backup.auto_backup(world.conn)
    assert not first.exists()


def test_catalog_items(world):
    pid = masters.save_product(world.conn, world.admin, {"item_code": "1ABC 100", "department_id": 1, "description": "Polo Shirt", "uom": "PC", "last_cost_c": 25000})
    found = masters.search_products(world.conn, "polo")[0]
    assert found["id"] == pid and found["department"] == "Grocery"
    assert masters.last_purchase_cost(world.conn, pid) == 25000
    po_id = world.po(items=[{"department_id": 1, "product_id": pid, "item_code": found["item_code"], "description": found["description"], "unit_cost_c": 26000, "allocations": {NORTH: 4}}])
    assert orders.get_po(world.conn, po_id)["items"][0]["product_id"] == pid
    assert masters.last_purchase_cost(world.conn, pid) == 26000


def test_items_without_a_sku_do_not_create_products(world):
    before = world.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    o = of.get_of(world.conn, world.draft(items=[world.item(size="M", color="Red")]))
    assert o["items"][0]["product_id"] is None and o["items"][0]["size"] == "M"
    assert world.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == before


def test_version_5_moves_the_catalog_to_the_new_units_and_adds_stickers(env):
    path = env / "po.sqlite"
    c = db.open_database(path)
    stamp = "2026-10-01 09:00:00"
    for code, uom in (("A", "DOZ"), ("B", "PK4"), ("C", "PAIR"), ("D", "SET"), ("E", "M")):
        c.execute("INSERT INTO products(item_code, department_id, description, uom, created_at, updated_at) VALUES (?, 1, 'x', ?, ?, ?)", (code, uom, stamp, stamp))
    c.executescript("BEGIN;ALTER TABLE suppliers DROP COLUMN sticker_rate_c;PRAGMA user_version = 4;COMMIT;")
    c.close()
    conn = db.open_database(path)
    assert {r["item_code"]: r["uom"] for r in conn.execute("SELECT item_code, uom FROM products")} == {"A": "1 DOZEN", "B": "4 IN 1", "C": "PC", "D": "PC", "E": "M"}
    assert "sticker_rate_c" in [r["name"] for r in conn.execute("PRAGMA table_info(suppliers)")]
    assert list((env / "backups").glob("po-before-migration-v4-*.sqlite"))
    conn.close()


def test_version_6_makes_no_vat_the_default_and_leaves_registered_suppliers(env):
    path = env / "po.sqlite"
    c = db.open_database(path)
    stamp = "2026-10-01 09:00:00"
    for code, registered, mode in (("OLD", 0, "INCLUSIVE"), ("REG", 1, "INCLUSIVE"), ("TOP", 0, "EXCLUSIVE")):
        c.execute("INSERT INTO suppliers(code, name, vat_registered, vat_mode, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)", (code, code, registered, mode, stamp, stamp))
    c.execute("UPDATE system_settings SET value = 'INCLUSIVE' WHERE key = 'tax.default_vat_mode'")
    c.execute("PRAGMA user_version = 5")
    c.close()
    conn = db.open_database(path)
    assert {r["code"]: r["vat_mode"] for r in conn.execute("SELECT code, vat_mode FROM suppliers")} == {"OLD": "NONE", "REG": "INCLUSIVE", "TOP": "EXCLUSIVE"}
    assert db.get_setting(conn, "tax.default_vat_mode") == "NONE"
    conn.close()


def test_the_warehouse_takes_its_name_unless_someone_already_typed_one(env):
    for before, place, after, town in (("Main Warehouse", "", "Central Warehouse", ""), ("Depot 2", "12 Depot Road", "Depot 2", "12 Depot Road")):
        path = env / f"{after}.sqlite"
        c = db.open_database(path)
        c.execute("UPDATE system_settings SET value = ? WHERE key = 'company.warehouse_name'", (before,))
        c.execute("UPDATE system_settings SET value = ? WHERE key = 'company.warehouse_address'", (place,))
        c.execute("PRAGMA user_version = 6")
        c.close()
        conn = db.open_database(path)
        assert db.get_setting(conn, "company.warehouse_name") == after and db.get_setting(conn, "company.warehouse_address") == town
        assert conn.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
        conn.close()


def _mixed_order(world, **header):
    return world.draft(items=[world.item(dept=5, cost=10000, alloc={NORTH: 2, SOUTH: 1}, description="Shoe"),
                              world.item(dept=1, cost=5000, alloc={NORTH: 3}, description="Shirt A"),
                              world.item(dept=1, cost=5000, alloc={SOUTH: 1}, description="Shirt B")], **header)


def test_submitting_makes_one_of_per_department_each_with_its_own_pos(world):
    draft = _mixed_order(world)
    before = world.conn.execute("SELECT last_number FROM of_number_sequences").fetchone()
    assert before is None  # no number until it's submitted
    results = of.submit_order(world.conn, world.buyer, draft)
    assert len(results) == 2 and results[0][0] == draft and all(status == APPROVED for _, status in results)
    men, shoes = (of.get_of(world.conn, i) for i, _ in results)
    assert [i["description"] for i in men["items"]] == ["Shirt A", "Shirt B"] and [i["description"] for i in shoes["items"]] == ["Shoe"]
    assert men["of_number"] != shoes["of_number"] and men["grand_total_c"] == 20000 and shoes["grand_total_c"] == 30000
    assert shoes["supplier_id"] == men["supplier_id"] == world.supplier
    men_pos = of.generate_pos(world.conn, world.buyer, men["id"])
    shoe_pos = of.generate_pos(world.conn, world.buyer, shoes["id"])
    assert [p["branch_code"] for p in men_pos] == [NORTH, SOUTH] and [p["branch_code"] for p in shoe_pos] == [NORTH, SOUTH]
    assert len({p["po_number"] for p in men_pos + shoe_pos}) == 4
    assert world.conn.execute("SELECT 1 FROM audit_logs WHERE action='OF_SPLIT'").fetchone()
    assert backup.integrity_report(world.conn) == []


def test_one_department_stays_one_of(world):
    draft = world.draft(items=[world.item(dept=1), world.item(dept=1)])
    assert of.submit_order(world.conn, world.buyer, draft) == [(draft, APPROVED)]
    assert world.conn.execute("SELECT COUNT(*) FROM order_forms").fetchone()[0] == 1


def test_a_split_order_cant_slip_under_the_approval_limit(world):
    # 60,000 + 60,000: each is within the 100,000 limit, but the order isn't.
    draft = world.draft(items=[world.item(dept=1, cost=6000000), world.item(dept=5, cost=6000000)])
    results = of.submit_order(world.conn, world.buyer, draft)
    assert [status for _, status in results] == [PENDING, PENDING]
    assert all(of.get_of(world.conn, i)["of_number"] is None for i, _ in results)
    # Approved one at a time, each getting its own number.
    assert of.approve(world.conn, world.admin, results[0][0]) != of.approve(world.conn, world.admin, results[1][0])


def test_a_problem_on_any_department_stops_the_whole_split(world):
    draft = world.draft(items=[world.item(dept=1), world.item(dept=5, alloc={})])
    with pytest.raises(POError, match="no branch quantities"):
        of.submit_order(world.conn, world.buyer, draft)
    assert world.conn.execute("SELECT COUNT(*) FROM order_forms").fetchone()[0] == 1
    assert len(of.get_of(world.conn, draft)["items"]) == 2 and of.get_of(world.conn, draft)["status"] == "DRAFT"


def test_discounts_and_charges_share_out_exactly_across_departments(world):
    draft = _mixed_order(world, overall_discount_type="AMOUNT", overall_discount_value=1000, other_charges_c=10001, other_charges_note="Freight", vat_mode="NONE")
    whole = of.get_of(world.conn, draft)["grand_total_c"]
    ids = [i for i, _ in of.submit_order(world.conn, world.buyer, draft)]
    made = [of.get_of(world.conn, i) for i in ids]
    assert sum(o["overall_discount_c"] for o in made) == 1000 and sum(o["other_charges_c"] for o in made) == 10001
    assert sum(o["grand_total_c"] for o in made) == whole
    assert all(o["other_charges_note"] == "Freight" for o in made)
    # A percent discount carries to each.
    percent = _mixed_order(world, overall_discount_type="PERCENT", overall_discount_value=1000, vat_mode="NONE")
    whole = of.get_of(world.conn, percent)["grand_total_c"]
    made = [of.get_of(world.conn, i) for i, _ in of.submit_order(world.conn, world.buyer, percent)]
    assert [o["overall_discount_value"] for o in made] == [1000, 1000] and sum(o["grand_total_c"] for o in made) == whole


def test_an_approved_of_cant_take_on_another_department(world):
    of_id = world.approved(items=[world.item(dept=1, description="Shirt")])
    row = of.get_of(world.conn, of_id)["items"][0]
    keep = {k: row[k] for k in ("department_id", "description", "unit_cost_c", "allocations_m")}
    with pytest.raises(POError, match="another department"):
        of.amend(world.conn, world.buyer, of_id, {}, [keep, keep | {"department_id": 5, "description": "Shoe"}], "Add shoes")
    assert of.amend(world.conn, world.buyer, of_id, {}, [keep | {"unit_cost_c": 9000}], "New price")["status"] == "APPLIED"


def test_pack_units_total_in_pieces_and_stickers_count_pieces(world):
    masters.save_supplier(world.conn, world.admin, {"code": "ABC", "name": "ABC Trading", "sticker_rate_c": 50}, world.supplier, confirm_duplicate=True)
    items = [world.item(cost=3000, uom="3 IN 1", alloc={NORTH: 10}), world.item(cost=6000, uom="6 IN 1", alloc={NORTH: 2}), world.item(cost=12000, uom="1 DOZEN", alloc={SOUTH: 1})]
    o = of.get_of(world.conn, world.draft(items=items))
    assert o["total_qty_m"] == 13000 and o["total_pieces_m"] == 54000  # 30 + 12 + 12 pieces
    assert o["grand_total_c"] == 30000 + 12000 + 12000  # the money stays per pack
    assert o["sticker_deduction_c"] == 2700 and o["net_payable_c"] == 54000 - 2700
    # The order quantity is checked in pieces.
    assert world.draft(items=[world.item(cost=3000, uom="3 IN 1", alloc={NORTH: 10}, ordered_qty="30")])
    with pytest.raises(POError, match="30 pieces"):
        world.draft(items=[world.item(cost=3000, uom="3 IN 1", alloc={NORTH: 10}, ordered_qty="10")])
    # A rate agreed at approval stays on that OF even if the supplier's rate changes later.
    approved = world.approved(items=[world.item(cost=3000, uom="3 IN 1", alloc={NORTH: 10})])
    masters.save_supplier(world.conn, world.admin, {"code": "ABC", "name": "ABC Trading", "sticker_rate_c": 100}, world.supplier, confirm_duplicate=True)
    assert of.get_of(world.conn, approved)["sticker_rate_c"] == 50
    assert of.get_of(world.conn, world.draft())["sticker_rate_c"] == 100


def test_department_and_grand_totals(world):
    items = [world.item(1, 10000, {NORTH: 10}, disc1_bp=1000), world.item(1, 20000, {NORTH: 5}), world.item(5, 50000, {NORTH: 2}, disc1_bp=1000, disc2_bp=500)]
    po = orders.get_po(world.conn, world.po(items=items, overall_discount_type="AMOUNT", overall_discount_value=1000, other_charges_c=5000))
    t = po["totals"]
    assert t.departments[1].amount == 190000 and t.departments[5].amount == 85500
    assert t.gross == 300000 and t.item_discount == 24500 and t.grand_total == 279500
    assert po["grand_total_c"] == t.grand_total


def test_po_keeps_department_and_delivery_details_from_the_of(world):
    po = orders.get_po(world.conn, world.po(items=[world.item(dept=7)], delivery_arrangement="PARTIAL", expected_delivery_date="2026-12-01", delivery_instructions="Gate 2"))
    assert po["items"][0]["department_id"] == 7 and po["delivery_arrangement"] == "PARTIAL"
    assert po["expected_delivery_date"] == "2026-12-01" and po["delivery_instructions"] == "Gate 2"
    assert po["delivery_count"] == 0
    assert po["items"][0]["received_qty"] == 0 and po["items"][0]["remaining_qty"] == po["items"][0]["qty"]


def test_creator_cannot_self_approve_unless_allowed(world):
    of_id = of.create_draft(world.conn, world.admin, world.header(), [world.item(cost=25000000)])
    of.submit(world.conn, world.admin, of_id)
    with pytest.raises(POError):
        of.approve(world.conn, world.admin, of_id)
    set_policy(world, allow_self_approval=1)
    assert of.approve(world.conn, world.admin, of_id)


def test_buyer_cannot_approve(world):
    of_id = world.draft(250000)
    of.submit(world.conn, world.buyer, of_id)
    with pytest.raises(auth.PermissionDenied):
        of.approve(world.conn, world.buyer, of_id)


def test_rejection_needs_reason_and_returns_to_draft(world):
    of_id = world.draft(250000)
    of.submit(world.conn, world.buyer, of_id)
    with pytest.raises(POError):
        of.reject(world.conn, world.admin, of_id, "  ")
    of.reject(world.conn, world.admin, of_id, "No budget")
    assert of.get_of(world.conn, of_id)["status"] == "DRAFT"


def test_submit_needs_quantities(world):
    of_id = world.draft(items=[world.item(alloc={})])
    with pytest.raises(POError, match="no branch quantities"):
        of.submit(world.conn, world.buyer, of_id)


def test_approved_documents_cannot_be_silently_changed(world):
    of_id = world.approved(10)
    with pytest.raises(POError):
        of.update_draft(world.conn, world.buyer, of_id, world.header(), [world.item()])
    item_id = world.conn.execute("SELECT id FROM order_form_items WHERE of_id=?", (of_id,)).fetchone()[0]
    for sql in (f"UPDATE order_form_allocations SET qty_m = 99000 WHERE of_item_id = {item_id}", f"DELETE FROM order_form_allocations WHERE of_item_id = {item_id}",
                f"UPDATE order_form_items SET unit_cost_c = 1 WHERE id = {item_id}", f"DELETE FROM order_form_items WHERE id = {item_id}",
                f"UPDATE order_forms SET grand_total_c = 1 WHERE id = {of_id}", f"UPDATE order_forms SET of_number = 'X' WHERE id = {of_id}"):
        with pytest.raises(sqlite3.IntegrityError):
            world.conn.execute(sql)
    po_id = of.generate_pos(world.conn, world.buyer, of_id)[0]["id"]
    for sql in (f"UPDATE purchase_order_items SET qty_m = 999 WHERE po_id = {po_id}", f"DELETE FROM purchase_order_items WHERE po_id = {po_id}",
                f"UPDATE purchase_orders SET grand_total_c = 1 WHERE id = {po_id}", f"UPDATE purchase_orders SET po_number = 'X' WHERE id = {po_id}",
                f"UPDATE purchase_orders SET branch_code = 4 WHERE id = {po_id}", f"UPDATE purchase_orders SET of_id = NULL WHERE id = {po_id}"):
        with pytest.raises(sqlite3.IntegrityError):
            world.conn.execute(sql)


def _existing(po):
    keys = list(orders.ITEM_FIELDS) + ["id"]
    return [{k: i[k] for k in keys} for i in po["items"]]


def test_amendment_decrease_applies_keeps_number_and_updates_of(world):
    of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 10, SOUTH: 5})])
    po_id = of.generate_pos(world.conn, world.buyer, of_id)[0]["id"]
    po = orders.get_po(world.conn, po_id)
    items = _existing(po)
    items[0]["qty_m"] = 8000
    item_id = items[0]["id"]
    result = orders.amend(world.conn, world.buyer, po_id, {"remarks": "cut"}, items, "Supplier short")
    assert result["status"] == "APPLIED" and result["revision"] == 1
    after = orders.get_po(world.conn, po_id)
    assert after["po_number"] == po["po_number"] and after["revision"] == 1
    assert after["items"][0]["qty"] == 8 and after["items"][0]["id"] == item_id and after["grand_total_c"] == 80000
    assert json.loads(orders.amendments(world.conn, po_id)[0]["before_json"])["items"][0]["qty_m"] == 10000
    o = of.get_of(world.conn, of_id)
    assert o["items"][0]["allocations"] == {NORTH: 8, SOUTH: 5} and o["total_qty"] == 13 and o["grand_total_c"] == 130000
    assert backup.integrity_report(world.conn) == []


def test_quantity_increase_needs_approval(world):
    po_id = world.po(items=[world.item(cost=10000, alloc={NORTH: 10})])
    items = _existing(orders.get_po(world.conn, po_id))
    items[0]["qty_m"] = 12000
    result = orders.amend(world.conn, world.buyer, po_id, {}, items, "Sold well")
    assert result["status"] == PENDING
    assert orders.get_po(world.conn, po_id)["items"][0]["qty"] == 10  # unchanged until approved
    with pytest.raises(POError):
        orders.issue(world.conn, world.buyer, po_id)
    orders.decide_amendment(world.conn, world.admin, result["amendment_id"], True, "ok")
    after = orders.get_po(world.conn, po_id)
    assert after["items"][0]["qty"] == 12 and after["revision"] == 1 and after["grand_total_c"] == 120000


def test_rejected_amendment_changes_nothing(world):
    po_id = world.po(items=[world.item(cost=10000, alloc={NORTH: 10})])
    items = _existing(orders.get_po(world.conn, po_id))
    items[0]["qty_m"] = 12000
    result = orders.amend(world.conn, world.buyer, po_id, {}, items, "More")
    orders.decide_amendment(world.conn, world.admin, result["amendment_id"], False, "No")
    after = orders.get_po(world.conn, po_id)
    assert after["items"][0]["qty"] == 10 and after["revision"] == 0


def test_po_from_of_only_changes_quantity_and_terms(world):
    po_id = world.po(items=[world.item(cost=10000, alloc={NORTH: 10})])
    existing = _existing(orders.get_po(world.conn, po_id))
    with pytest.raises(POError):
        orders.amend(world.conn, world.buyer, po_id, {"supplier_id": 1}, existing, "x")
    with pytest.raises(POError, match="whole OF"):
        orders.amend(world.conn, world.buyer, po_id, {"vat_mode": "NONE"}, existing, "x")
    dearer = [dict(existing[0], unit_cost_c=20000)]
    with pytest.raises(POError, match="new OF"):
        orders.amend(world.conn, world.buyer, po_id, {}, dearer, "x")
    with pytest.raises(POError, match="new OF"):
        orders.amend(world.conn, world.buyer, po_id, {}, existing + [{"department_id": 1, "description": "New", "qty": 1, "unit_cost_c": 100}], "x")
    assert orders.amend(world.conn, world.buyer, po_id, {"expected_delivery_date": "2027-01-15"}, existing, "Later")["status"] == "APPLIED"


def test_of_amendment_before_generation(world):
    of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 10}, description="Polo")])
    first_number = of.get_of(world.conn, of_id)["of_number"]
    smaller = [world.item(cost=10000, alloc={NORTH: 8}, description="Polo")]
    assert of.amend(world.conn, world.buyer, of_id, {"remarks": "cut"}, smaller, "Budget")["status"] == "APPLIED"
    o = of.get_of(world.conn, of_id)
    assert o["revision"] == 1 and o["of_number"] == first_number and o["grand_total_c"] == 80000 and o["remarks"] == "cut"
    bigger = [world.item(cost=10000, alloc={NORTH: 8, SOUTH: 3}, description="Polo")]
    result = of.amend(world.conn, world.buyer, of_id, {}, bigger, "South too")
    assert result["status"] == PENDING
    with pytest.raises(POError, match="waiting"):
        of.generate_pos(world.conn, world.buyer, of_id)
    assert of.get_of(world.conn, of_id)["total_qty"] == 8
    of.decide_amendment(world.conn, world.admin, result["amendment_id"], True, "ok")
    assert of.get_of(world.conn, of_id)["items"][0]["allocations"] == {NORTH: 8, SOUTH: 3}
    assert len(of.generate_pos(world.conn, world.buyer, of_id)) == 2
    with pytest.raises(POError, match="branch POs already exist"):
        of.amend(world.conn, world.buyer, of_id, {}, bigger, "again")


def test_duplicate_supplier_warns(world):
    with pytest.raises(masters.DuplicateSupplier):
        masters.save_supplier(world.conn, world.admin, {"code": "ABD", "name": "ABC  Trading"})
    assert masters.save_supplier(world.conn, world.admin, {"code": "ABD", "name": "ABC  Trading"}, confirm_duplicate=True)
    with pytest.raises(POError):
        masters.save_supplier(world.conn, world.admin, {"code": "abc", "name": "Different"}, confirm_duplicate=True)


def test_new_branch_can_be_added_and_used(world):
    assert masters.save_branch(world.conn, world.admin, {"code": "10", "name": "Riverside"}) == 10
    with pytest.raises(POError, match="already used"):
        masters.save_branch(world.conn, world.admin, {"code": "8", "name": "Another"})
    pos = of.generate_pos(world.conn, world.buyer, world.approved(items=[world.item(alloc={10: 5})]))
    assert pos[0]["po_number"].split("-")[1] == "10" and pos[0]["branch_name"] == "Riverside"


def test_department_ids_are_fixed(conn):
    assert [tuple(r) for r in masters.list_departments(conn)][2] == (3, "Household")
    assert len(masters.list_departments(conn)) == 9
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE departments SET id = 77 WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM departments WHERE id = 1")


def test_login_rules(world):
    assert auth.login(world.conn, "BUYER", PASSWORD).id == world.buyer.id
    with pytest.raises(POError):
        auth.login(world.conn, "buyer", "wrong-password")
    assert "password" not in world.conn.execute("SELECT password_hash FROM users WHERE username='buyer'").fetchone()[0]
    with pytest.raises(POError):
        auth.create_user(world.conn, world.admin, "x", "X", "short", ["BUYER"])
    with pytest.raises(POError):
        auth.update_user(world.conn, world.admin, world.admin.id, "Boss", ["BUYER"], True)  # last admin


def test_all_reports_run_and_export(world, env):
    a = world.approved(items=[world.item(item_code="A1", dept=1, cost=10000, alloc={NORTH: 2, SOUTH: 1}), world.item(item_code="A1", dept=2, cost=12000, alloc={NORTH: 1})])
    of.generate_pos(world.conn, world.buyer, a)
    of.generate_pos(world.conn, world.buyer, world.approved(items=[world.item(item_code="A1", cost=9000)]))
    gone = world.po(10)
    orders.cancel(world.conn, world.buyer, gone, "test")
    of.cancel(world.conn, world.buyer, world.draft(10), "test")
    of.submit(world.conn, world.buyer, world.draft(250000))
    assert len(reports.REPORTS) == 13
    for d in reports.REPORTS:
        rep = d.fn(world.conn, date_from="2026-01-01", date_to="2099-12-31")
        assert rep.title == d.title
        assert reports.to_excel(rep, env / f"{d.key}.xlsx", "2026").exists()
    assert reports.by_department(world.conn).rows[0][5] == 39000  # department 1 across branches and OFs
    assert reports.po_register(world.conn).rows[0][0].endswith("-00001") and reports.po_register(world.conn).rows[0][1].endswith("-OF-00001")
    assert {r[1]: r[3] for r in reports.by_branch(world.conn).rows} == {"North": 4000, "South": 1000}
    assert len(reports.pending_approval(world.conn).rows) == 1
    assert len(reports.outstanding(world.conn).rows) == 3
    kinds = sorted(r[0] for r in reports.cancelled(world.conn).rows)
    assert kinds == ["OF", "PO"]
    assert len(reports.of_register(world.conn).rows) == 3
    assert reports.cost_comparison(world.conn).rows[0][2] == 2  # one per OF, however many branch POs it made (AUD-175)
    assert reports.po_register(world.conn, date_from="2100-01-01").rows == []


def test_registers_filter(world):
    a = world.approved(items=[world.item(dept=3, alloc={NORTH: 1, SOUTH: 2})])
    pos = of.generate_pos(world.conn, world.buyer, a)
    world.draft(items=[world.item(dept=4)])
    c = orders.dashboard_counts(world.conn)
    assert c["total"] == 2 and c["by_status"]["APPROVED"] == 2 and c["purchasing_amount_c"] == 30000
    assert len(orders.search(world.conn, department_id=3)) == 2
    assert len(orders.search(world.conn, department_id=4)) == 0
    assert [r["branch_code"] for r in orders.search(world.conn, branch_code=SOUTH)] == [SOUTH]
    assert len(orders.search(world.conn, text="ABC Trading")) == 2
    assert len(orders.search(world.conn, status="APPROVED", year=str(pos[0]["po_number"][:4]))) == 2
    assert len(orders.search(world.conn, date_from="2100-01-01")) == 0
    number = of.get_of(world.conn, a)["of_number"]
    assert len(orders.search(world.conn, of_number=number)) == 2
    assert len(orders.search(world.conn, po_number=pos[0]["po_number"])) == 1
    assert len(orders.search(world.conn, approval="AUTO")) == 2 and len(orders.search(world.conn, approval="APPROVED")) == 0
    rows = of.search(world.conn)
    row = next(r for r in rows if r["id"] == a)
    assert (row["product_count"], row["branch_count"], row["po_count"], row["delivery"]) == (1, 2, 2, "Not yet issued")
    assert next(r for r in rows if r["id"] != a)["delivery"] == "POs not generated"
    assert len(of.search(world.conn, status="DRAFT")) == 1 and len(of.search(world.conn, department_id=4)) == 1
    assert len(of.search(world.conn, text=pos[1]["po_number"])) == 1
    assert of.counts(world.conn)["by_status"]["APPROVED"] == 1


def test_recording_nothing_never_marks_delivered(world):
    of_id = world.approved(items=[world.item(alloc={NORTH: 1})])
    po_id = of.generate_pos(world.conn, world.buyer, of_id)[0]["id"]
    orders.issue(world.conn, world.buyer, po_id)
    assert next(r for r in of.search(world.conn) if r["id"] == of_id)["delivery"] == "Awaiting delivery"


def test_attachment_kept_with_po(world, env):
    src = env / "quote.txt"
    src.write_text("supplier quote")
    po_id = world.po(10)
    orders.add_attachment(world.conn, world.buyer, po_id, src)
    row = orders.list_attachments(world.conn, po_id)[0]
    assert row["file_name"] == "quote.txt" and orders.attachment_path(row).read_text() == "supplier quote"


def test_integrity_report_clean(world):
    world.po(10)
    assert backup.integrity_report(world.conn) == []


def test_external_po_number_can_be_added_after_generation(world):
    po_id = world.po(10)
    orders.set_legacy_number(world.conn, world.buyer, po_id, "2612345")
    assert orders.get_po(world.conn, po_id)["legacy_po_number"] == "2612345"
    assert orders.search(world.conn, text="2612345")[0]["id"] == po_id
    assert any(r["action"] == "PO_LEGACY_NUMBER" for r in orders.audit_trail(world.conn, po_id))
    other = world.po(10)
    with pytest.raises(POError):
        orders.set_legacy_number(world.conn, world.buyer, other, "2612345")
    assert reports.po_register_excel(orders.search(world.conn), Path(db.data_dir()) / "d.xlsx").exists()
