"""Second fix round, payments and passes: AUD-130 A6 (a recorded check's orders are fixed) and AUD-149 (payments lists without a query per row)."""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PO_SAMPLE_DIR", tempfile.mkdtemp(prefix="po-sample-"))

import pytest

from atlaspo import auth, db, payments, receiving, sample_data, schema
from atlaspo.db import POError

from conftest import PASSWORD
from test_fix_payments import Order

NEW_TRIGGERS = ("check_orders_once_recorded", "check_orders_paid_once")


def _blocked(conn, sql, args=()):
    """True when the database refuses the change. Nothing stays changed either way."""
    conn.execute("SAVEPOINT probe")
    try:
        conn.execute(sql, args)
        return False
    except sqlite3.DatabaseError:
        return True
    finally:
        conn.execute("ROLLBACK TO probe")
        conn.execute("RELEASE probe")


@pytest.fixture(scope="module")
def sample_file(tmp_path_factory):
    folder = tmp_path_factory.mktemp("fix2-payments-sample")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PO_DATA_DIR", str(folder))
        conn = db.open_database(folder / "sample.sqlite")
        sample_data.build(conn)
        conn.close()
    return folder / "sample.sqlite"


@pytest.fixture
def sample(sample_file, env):
    path = env / "po.sqlite"
    shutil.copy(sample_file, path)
    conn = db.open_database(path)
    yield conn
    conn.close()


def _recorded_check(conn):
    return conn.execute("SELECT c.id, c.supplier_id FROM payment_checks c WHERE c.status = 'ACTIVE' AND EXISTS (SELECT 1 FROM audit_logs a"
                        " WHERE a.action IN ('CHECK_RECORD', 'CHECK_CREDIT') AND a.entity_type = 'payment_check' AND a.entity_id = c.id) ORDER BY c.id").fetchone()


def _unpaid_of(conn):
    return conn.execute("SELECT id FROM order_forms WHERE of_number IS NOT NULL AND id NOT IN (SELECT o.of_id FROM payment_check_ofs o JOIN payment_checks c"
                        " ON c.id = o.check_id WHERE c.status = 'ACTIVE') ORDER BY id").fetchone()[0]


# ------------------------------------------------------------------ AUD-130 A6

def test_an_order_cannot_be_added_to_a_recorded_check(sample):
    check = _recorded_check(sample)
    other = _unpaid_of(sample)
    insert = "INSERT INTO payment_check_ofs(check_id, of_id, amount_c) VALUES (?, ?, 1)"
    assert _blocked(sample, insert, (check["id"], other))
    # Before this fix the same insert went in: without the guard it is accepted.
    sample.execute("DROP TRIGGER check_orders_once_recorded")
    assert not _blocked(sample, insert, (check["id"], other))


def test_a_claimed_or_cleared_check_takes_no_new_order(sample):
    """Even a check written without a log entry (the oldest records) is fixed once the supplier has it or the bank cleared it."""
    supplier = sample.execute("SELECT id FROM suppliers ORDER BY id").fetchone()[0]
    admin = sample.execute("SELECT id FROM users ORDER BY id").fetchone()[0]
    of_a, of_b = [r[0] for r in sample.execute("SELECT id FROM order_forms WHERE of_number IS NOT NULL AND id NOT IN (SELECT of_id FROM payment_check_ofs) ORDER BY id LIMIT 2")]
    for table, number in (("check_claims", "990001"), ("check_clears", "990002")):
        sample.execute("SAVEPOINT plant")
        check = sample.execute("INSERT INTO payment_checks(check_number, source, check_date, supplier_id, amount_c, status, recorded_by, recorded_at) VALUES (?, 'EXTERNAL', '2026-11-07', ?, 100, 'ACTIVE', ?, 't')",
                               (number, supplier, admin)).lastrowid
        sample.execute("INSERT INTO payment_check_ofs(check_id, of_id, amount_c) VALUES (?, ?, 100)", (check, of_a))  # still being written: allowed
        if table == "check_claims":
            sample.execute("INSERT INTO check_claims(check_id, claimed_on, claimed_by_name, recorded_by, recorded_at) VALUES (?, '2026-11-07', 'Juan', ?, 't')", (check, admin))
        else:
            sample.execute("INSERT INTO check_clears(check_id, cleared_on, recorded_by, recorded_at) VALUES (?, '2026-11-07', ?, 't')", (check, admin))
        assert _blocked(sample, "INSERT INTO payment_check_ofs(check_id, of_id, amount_c) VALUES (?, ?, 1)", (check, of_b))
        sample.execute("ROLLBACK TO plant")
        sample.execute("RELEASE plant")


def test_an_order_with_a_live_check_cannot_go_on_a_second_check(sample):
    paid = sample.execute("SELECT o.of_id, c.supplier_id FROM payment_check_ofs o JOIN payment_checks c ON c.id = o.check_id WHERE c.status = 'ACTIVE' ORDER BY o.of_id").fetchone()
    admin = sample.execute("SELECT id FROM users ORDER BY id").fetchone()[0]
    check = sample.execute("INSERT INTO payment_checks(check_number, source, check_date, supplier_id, amount_c, status, recorded_by, recorded_at) VALUES ('990003', 'EXTERNAL', '2026-11-07', ?, 100, 'ACTIVE', ?, 't')",
                           (paid["supplier_id"], admin)).lastrowid
    assert _blocked(sample, "INSERT INTO payment_check_ofs(check_id, of_id, amount_c) VALUES (?, ?, 1)", (check, paid["of_id"]))
    # The program refuses it too, in plain words, before the database has to.
    with pytest.raises(POError, match="already has check"):
        payments._link_orders(sample, check, [{"id": paid["of_id"], "of_number": "OF", "pay_c": 1}])


def test_link_orders_writes_a_checks_orders_only_once(sample):
    check = _recorded_check(sample)
    with pytest.raises(POError, match="already has its orders"):
        payments._link_orders(sample, check["id"], [{"id": _unpaid_of(sample), "of_number": "OF", "pay_c": 1}])


def _two_ready(world):
    world.accounting = auth.create_user(world.conn, world.admin, "lorna", "Lorna Tan", PASSWORD, ["ACCOUNTING"], must_change=False)
    first, second = Order(world).of_id, Order(world).of_id
    for of_id in (first, second):
        if payments.order_row(world.conn, of_id)["stage"] == payments.ENTER_DRS:
            payments.drs_match(world.conn, world.accounting, of_id)
    group = next(g for g in payments.checks_due(world.conn) if {first, second} <= {o["id"] for o in g["orders"]})
    return first, second, group


def test_one_check_for_several_orders_still_records(world):
    """The program writes all of a check's orders before it logs the check, so a check paying two orders still records, then is fixed."""
    first, second, group = _two_ready(world)
    check = payments.record_check(world.conn, world.accounting, [first, second], "123456", group["due"].isoformat(), f"{group['check_c'] / 100:.2f}")
    assert sorted(r[0] for r in world.conn.execute("SELECT of_id FROM payment_check_ofs WHERE check_id = ?", (check,))) == sorted([first, second])
    assert world.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'CHECK_RECORD' AND entity_id = ?", (check,)).fetchone()[0] == 2
    third = Order(world).of_id
    assert _blocked(world.conn, "INSERT INTO payment_check_ofs(check_id, of_id, amount_c) VALUES (?, ?, 1)", (check, third))
    # Cancelled, the orders come back and a new check pays them.
    payments.cancel_check(world.conn, world.accounting, check, "Misprinted")
    again = payments.record_check(world.conn, world.accounting, [first, second], "123457", group["due"].isoformat(), f"{group['check_c'] / 100:.2f}")
    assert world.conn.execute("SELECT COUNT(*) FROM payment_check_ofs WHERE check_id = ?", (again,)).fetchone()[0] == 2


def test_settle_by_credit_for_several_orders_still_records(world, monkeypatch):
    first, second, _ = _two_ready(world)
    monkeypatch.setattr(payments, "_expected", lambda conn, rows, groups=None: (sum(r["pay_c"] for r in rows), sum(r["pay_c"] for r in rows), 0))
    check = payments.settle_by_credit(world.conn, world.accounting, [first, second])
    assert world.conn.execute("SELECT COUNT(*) FROM payment_check_ofs WHERE check_id = ?", (check,)).fetchone()[0] == 2
    assert _blocked(world.conn, "INSERT INTO payment_check_ofs(check_id, of_id, amount_c) VALUES (?, ?, 1)", (check, Order(world).of_id))


# ------------------------------------------------------------------ the update from version 27

def test_v27_upgrade_keeps_every_row_and_adds_the_new_guards(sample_file, env):
    path = env / "po.sqlite"
    shutil.copy(sample_file, path)
    c = db.connect(path)
    c.executescript("BEGIN;" + "".join(f"DROP TRIGGER {name};" for name in schema.V28_TRIGGERS) + "PRAGMA user_version = 27;COMMIT;")
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    before = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    db.initialize(c)
    after = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    assert after.pop("audit_logs") == before.pop("audit_logs") + 1
    assert after == before
    assert set(NEW_TRIGGERS) <= {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert set(NEW_TRIGGERS) <= set(schema.V28_TRIGGERS)
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    c.close()


# ------------------------------------------------------------------ AUD-149

class Statements:
    def __init__(self, conn):
        self.conn, self.sql = conn, []

    def __enter__(self):
        self.conn.set_trace_callback(self.sql.append)
        return self

    def __exit__(self, *_):
        self.conn.set_trace_callback(None)


def test_checks_list_is_one_query_and_says_the_same(sample):
    rows = payments.checks(sample)
    assert len(rows) > 3
    assert [r["waiting_signoff"] for r in rows] == [payments.check_waiting(sample, r["id"]) for r in rows]
    assert any(r["waiting_signoff"] for r in rows) or not sample.execute("SELECT 1 FROM payment_signoffs WHERE kind = 'CHECK' AND signed_by IS NULL").fetchone()
    with Statements(sample) as s:
        payments.checks(sample)
    assert len(s.sql) == 1


def test_waiting_signoffs_needs_no_query_per_check(sample):
    expected = 0
    for r in sample.execute("SELECT s.kind, s.ref_id FROM payment_signoffs s WHERE s.signed_by IS NULL AND s.id = (SELECT MAX(t.id) FROM payment_signoffs t WHERE t.kind = s.kind AND t.ref_id = s.ref_id)"):
        if r["kind"] == "CHECK":
            expected += sample.execute("SELECT 1 FROM payment_checks WHERE id = ? AND status = 'ACTIVE'", (r["ref_id"],)).fetchone() is not None
        else:
            expected += not payments._active_check(sample, r["ref_id"]) and payments.order_row(sample, r["ref_id"])["stage"] == payments.SIGN_OFF
    assert payments.waiting_signoffs(sample) == expected
    with Statements(sample) as s:
        payments.waiting_signoffs(sample, rows=[])
    assert not [q for q in s.sql if "FROM payment_checks WHERE id" in q]


def test_orders_in_payment_reads_counts_once_and_lists_the_same(sample, monkeypatch):
    ids = [r[0] for r in sample.execute("SELECT f.id FROM order_forms f WHERE f.status = 'APPROVED' AND f.of_number IS NOT NULL AND NOT EXISTS"
                                        " (SELECT 1 FROM payment_check_ofs o JOIN payment_checks c ON c.id = o.check_id WHERE o.of_id = f.id AND c.status = 'ACTIVE') ORDER BY f.id")]
    expected = [i for i in ids if receiving.of_delivery_complete(sample, i) and payments.counted_by_branches(sample, i)]
    assert expected
    monkeypatch.setattr(payments, "counted_by_branches", lambda *a: pytest.fail("asked per order"))
    assert [r["id"] for r in payments.orders_in_payment(sample)] == expected
