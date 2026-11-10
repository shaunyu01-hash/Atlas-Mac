"""Database guards and migration V28 (AUD-129, 130, 132, 134, 136, 137, 144, 150, 151, 046, 053)."""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PO_SAMPLE_DIR", tempfile.mkdtemp(prefix="po-sample-"))

import pytest

from atlaspo import auth, db, orders, sample_data, schema
from atlaspo.db import POError

PO_STATUSES = ("DRAFT", "PENDING_APPROVAL", "APPROVED", "ISSUED", "PARTIALLY_DELIVERED", "FULLY_DELIVERED", "CLOSED", "CANCELLED")
OF_STATUSES = ("DRAFT", "PENDING_APPROVAL", "APPROVED", "CANCELLED")
# Every status change the program makes without the write guard (order_forms.submit/approve/reject/cancel, orders.issue/cancel/close/
# receive_outside). The amendment that sends an issued PO back to Approved, and receiving's delivered status, hold the write guard.
PO_ALLOWED = {("DRAFT", "CANCELLED"), ("PENDING_APPROVAL", "CANCELLED"),
              ("APPROVED", "ISSUED"), ("APPROVED", "PARTIALLY_DELIVERED"), ("APPROVED", "CANCELLED"),
              ("ISSUED", "CLOSED"), ("ISSUED", "CANCELLED"),
              ("PARTIALLY_DELIVERED", "CLOSED"), ("FULLY_DELIVERED", "CLOSED")}
OF_ALLOWED = {("DRAFT", "PENDING_APPROVAL"), ("DRAFT", "APPROVED"), ("DRAFT", "CANCELLED"),
              ("PENDING_APPROVAL", "DRAFT"), ("PENDING_APPROVAL", "APPROVED"), ("PENDING_APPROVAL", "CANCELLED"),
              ("APPROVED", "CANCELLED")}
NEW_INDEXES = {"idx_claim_requests_status", "idx_claim_requests_po", "idx_claim_request_items_item", "idx_payment_checks_status", "idx_audit_user",
               "idx_warehouse_delivery_items_item", "idx_late_damage_check", "idx_branch_counts_recount", "idx_claim_stub_items_item", "idx_item_pass_items_item"}
DROPPED = {"idx_items_po": "purchase_order_items(po_id)", "idx_of_items_of": "order_form_items(of_id)", "idx_amendments_po": "po_amendments(po_id)",
           "idx_of_amendments_of": "of_amendments(of_id)", "idx_dr_po": "delivery_receipts(po_id)", "idx_po_of": "purchase_orders(of_id)"}


def _objects(conn):
    return sorted(tuple(r) for r in conn.execute("SELECT type, name, tbl_name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))


def _counts(conn):
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


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


def _undo_v28(conn):
    """Puts a version 28 database back exactly as version 27 had it: the new guards and indexes gone, the dropped indexes back."""
    script = "".join(f"DROP TRIGGER {name};" for name in schema.V28_TRIGGERS)
    script += "".join(f"DROP INDEX {name};" for name in NEW_INDEXES)
    script += "".join(f"CREATE INDEX {name} ON {on};" for name, on in DROPPED.items())
    conn.executescript("BEGIN;" + script + "PRAGMA user_version = 27;COMMIT;")


@pytest.fixture(scope="module")
def sample_file(tmp_path_factory):
    """A full practice database, built once. Tests work on copies."""
    folder = tmp_path_factory.mktemp("schema-sample")
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


# ------------------------------------------------------------------ the migration itself

def test_v27_database_upgrades_keeping_every_row(sample_file, env):
    path = env / "po.sqlite"
    shutil.copy(sample_file, path)
    fresh = db.open_database(env / "fresh.sqlite")
    expected = _objects(fresh)
    fresh.close()
    c = db.connect(path)
    _undo_v28(c)
    c.execute("DROP TABLE IF EXISTS sqlite_stat1")
    before = _counts(c)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 27
    db.initialize(c)
    assert c.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
    after = _counts(c)
    assert after.pop("audit_logs") == before.pop("audit_logs") + 2  # the MIGRATE_V28 and MIGRATE_V29 notes, nothing else
    assert after == before
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert _objects(c) == expected
    # AUD-144: the planner gets its figures as soon as the update is done.
    assert c.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat1'").fetchone()
    assert c.execute("SELECT COUNT(*) FROM sqlite_stat1").fetchone()[0] > 0
    assert list((env / "backups").glob("po-before-migration-v27-*.sqlite"))
    c.close()
    again = db.open_database(path)
    assert again.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
    again.close()


def test_older_migrations_still_rerun_under_the_new_guards(env):
    """V11 rebuilds branch_counts, which the new count guards read. Going forward from version 8 still works, and every guard comes back."""
    path = env / "po.sqlite"
    c = db.open_database(path)
    auth.create_first_admin(c, "boss", "Boss", "correct-horse")
    expected = _objects(c)
    c.execute("PRAGMA user_version = 8")
    c.close()
    c = db.open_database(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
    assert _objects(c) == expected
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    c.close()


def test_a_later_table_rebuild_keeps_every_v28_guard(sample):
    """A future migration that rebuilds a table a guard reads (here branch_counts) runs on a file already at 28. V28 never runs
    there again, so the rebuild itself must leave every V28 guard in place, still working, with every row kept."""
    assert len(schema.V28_TRIGGERS) == schema.V28_TRIGGER_SQL.count("CREATE TRIGGER IF NOT EXISTS ") > 30
    before_objects = _objects(sample)
    before_rows = _counts(sample)
    sql = sample.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'branch_counts'").fetchone()[0]
    columns = ", ".join(f'"{r[1]}"' for r in sample.execute("PRAGMA table_xinfo(branch_counts)"))
    indexes = "".join(r[0] + ";" for r in sample.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'branch_counts' AND sql IS NOT NULL"))
    own_triggers = "".join(r[0] + ";\n" for r in sample.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'branch_counts'"
        f" AND name NOT IN ({','.join('?' * len(schema.V28_TRIGGERS))})", schema.V28_TRIGGERS))
    rebuild = (re.sub(r'^CREATE TABLE "?branch_counts"?', "CREATE TABLE branch_counts_new", sql, count=1) + ";"
               + f"INSERT INTO branch_counts_new({columns}) SELECT {columns} FROM branch_counts;"
               + "DROP TABLE branch_counts; ALTER TABLE branch_counts_new RENAME TO branch_counts;"
               + indexes + own_triggers + "PRAGMA user_version = 28;")
    assert "CREATE TABLE branch_counts_new" in rebuild
    db._migrate(sample, rebuild)
    names = {r[0] for r in sample.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert set(schema.V28_TRIGGERS) <= names
    assert _objects(sample) == before_objects
    assert _counts(sample) == before_rows
    assert sample.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert sample.execute("PRAGMA foreign_key_check").fetchall() == []
    # The guards still refuse: an agreed or mismatched count takes no new line.
    done = sample.execute("SELECT id FROM branch_counts WHERE status = 'MISMATCH' LIMIT 1").fetchone()
    if done:
        line = sample.execute("SELECT * FROM branch_count_items LIMIT 1").fetchone()
        if line:
            cols = [k for k in line.keys() if k != "id"]
            values = [done["id"] if k == "count_id" else line[k] for k in cols]
            assert _blocked(sample, f"INSERT INTO branch_count_items({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", values)
    assert _blocked(sample, "UPDATE purchase_orders SET status = 'DRAFT' WHERE status = 'ISSUED'") or not sample.execute(
        "SELECT 1 FROM purchase_orders WHERE status = 'ISSUED'").fetchone()


def test_open_runs_the_planner_figures_and_never_fails_for_them(env, monkeypatch):
    conn = db.open_database()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
    calls = []
    real = db._optimize
    monkeypatch.setattr(db, "_optimize", lambda c: calls.append(1) or real(c))
    db.open_database().close()
    assert calls == [1]
    conn.close()


# ------------------------------------------------------------------ AUD-129: status moves only the way the program moves it

def _po_row(w, status):
    seq = w.conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] + 900
    return w.conn.execute("INSERT INTO purchase_orders(po_number, po_year, po_seq, po_date, supplier_id, branch_code, branch_name, status, prepared_by,"
                          " created_at, updated_at) VALUES (?, 2026, ?, '2026-11-07', ?, 2, 'North', ?, ?, 't', 't')",
                          (f"T-{seq}", seq, w.supplier, status, w.admin.id)).lastrowid


def _of_row(w, status):
    seq = w.conn.execute("SELECT COUNT(*) FROM order_forms").fetchone()[0] + 900
    return w.conn.execute("INSERT INTO order_forms(of_number, of_year, of_seq, of_date, supplier_id, status, prepared_by, created_at, updated_at)"
                          " VALUES (?, 2026, ?, '2026-11-07', ?, ?, ?, 't', 't')", (f"T-OF-{seq}", seq, w.supplier, status, w.admin.id)).lastrowid


def test_po_status_moves_only_along_the_program_paths(world):
    rows = {s: _po_row(world, s) for s in PO_STATUSES}
    for old in PO_STATUSES:
        for new in PO_STATUSES:
            if old == new:
                continue
            refused = _blocked(world.conn, "UPDATE purchase_orders SET status = ? WHERE id = ?", (new, rows[old]))
            assert refused == ((old, new) not in PO_ALLOWED), (old, new)
    # A recorded operation holding the write guard (an amendment, receiving) may still move it.
    with db.tx(world.conn), orders.write_guard(world.conn):
        assert not _blocked(world.conn, "UPDATE purchase_orders SET status = 'APPROVED' WHERE id = ?", (rows["ISSUED"],))
        assert not _blocked(world.conn, "UPDATE purchase_orders SET status = 'ISSUED' WHERE id = ?", (rows["FULLY_DELIVERED"],))


def test_of_status_moves_only_along_the_program_paths(world):
    rows = {s: _of_row(world, s) for s in OF_STATUSES}
    for old in OF_STATUSES:
        for new in OF_STATUSES:
            if old == new:
                continue
            refused = _blocked(world.conn, "UPDATE order_forms SET status = ? WHERE id = ?", (new, rows[old]))
            assert refused == ((old, new) not in OF_ALLOWED), (old, new)


def test_tampering_an_issued_po_through_draft_is_refused(sample):
    """trigger_probes S1-S3: revive a cancelled PO, send an issued PO or approved OF back to Draft."""
    cancelled = sample.execute("SELECT id FROM purchase_orders WHERE status = 'CANCELLED' AND po_number IS NOT NULL").fetchone()[0]
    issued = sample.execute("SELECT id FROM purchase_orders WHERE status = 'ISSUED'").fetchone()[0]
    of = sample.execute("SELECT id FROM order_forms WHERE status = 'APPROVED'").fetchone()[0]
    closed = sample.execute("SELECT id FROM purchase_orders WHERE status = 'CLOSED'").fetchone()
    assert _blocked(sample, "UPDATE purchase_orders SET status = 'ISSUED' WHERE id = ?", (cancelled,))
    assert _blocked(sample, "UPDATE purchase_orders SET status = 'DRAFT' WHERE id = ?", (issued,))
    assert _blocked(sample, "UPDATE order_forms SET status = 'DRAFT' WHERE id = ?", (of,))
    if closed:
        assert _blocked(sample, "UPDATE purchase_orders SET status = 'ISSUED' WHERE id = ?", (closed[0],))


# ------------------------------------------------------------------ AUD-130: finished documents take no new lines

def _clone(conn, table, where, args, **over):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_xinfo({table})") if r[6] == 0 and r[1] != "id"]
    row = conn.execute(f"SELECT * FROM {table} WHERE {where} LIMIT 1", args).fetchone()
    assert row is not None, table
    return f"INSERT INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", [over.get(c, row[c]) for c in cols]


def test_no_new_lines_on_finished_documents(sample):
    dm = sample.execute("SELECT id FROM debit_memos WHERE status = 'ISSUED'").fetchone()[0]
    assert _blocked(sample, *_clone(sample, "debit_memo_items", "dm_id = ?", (dm,), line_no=999))
    cs = sample.execute("SELECT id FROM claim_stubs WHERE status <> 'CANCELLED'").fetchone()[0]
    assert _blocked(sample, *_clone(sample, "claim_stub_items", "cs_id = ?", (cs,), line_no=999))
    ip = sample.execute("SELECT id FROM item_passes").fetchone()[0]
    assert _blocked(sample, *_clone(sample, "item_pass_items", "ip_id = ?", (ip,), line_no=999))
    count = sample.execute("SELECT id FROM branch_counts WHERE status = 'APPROVED'").fetchone()[0]
    assert _blocked(sample, *_clone(sample, "branch_count_items", "count_id = ?", (count,), po_item_id=-99999))
    assert _blocked(sample, "INSERT INTO branch_count_damage(count_id, po_item_id, damaged_m) VALUES (?, -99999, 1000)", (count,))
    request = sample.execute("SELECT id FROM claim_requests WHERE status = 'PENDING'").fetchone()
    if request:
        sample.execute("SAVEPOINT decided")
        sample.execute("UPDATE claim_requests SET status = 'REJECTED', decided_by = 1, decided_at = 't', decision_reason = 'no' WHERE id = ?", (request[0],))
        assert _blocked(sample, "INSERT INTO claim_request_items(request_id, po_item_id, pieces_m) VALUES (?, (SELECT MAX(id) FROM purchase_order_items), 1000)", (request[0],))
        sample.execute("ROLLBACK TO decided")
        sample.execute("RELEASE decided")
    check = sample.execute("SELECT id FROM payment_checks WHERE status = 'ACTIVE'").fetchone()[0]
    other = sample.execute("SELECT id FROM order_forms WHERE id NOT IN (SELECT of_id FROM payment_check_ofs)").fetchone()[0]
    sample.execute("SAVEPOINT cancelled")
    sample.execute("UPDATE payment_checks SET status = 'CANCELLED' WHERE id = ?", (check,))
    assert _blocked(sample, "INSERT INTO payment_check_ofs(check_id, of_id, amount_c) VALUES (?, ?, 1)", (check, other))
    sample.execute("ROLLBACK TO cancelled")
    sample.execute("RELEASE cancelled")


def test_lines_still_go_on_while_a_document_is_being_made(world):
    """The services add lines in the same transaction, before the pdf is stored: that stays possible."""
    c = world.conn
    po = _po_row(world, "ISSUED")
    dm = c.execute("INSERT INTO debit_memos(dm_number, dm_year, dm_seq, dm_date, po_id, supplier_id, status, total_c, prepared_by, prepared_at)"
                   " VALUES ('T-DM', 2026, 1, 'd', ?, ?, 'ISSUED', 0, ?, 't')", (po, world.supplier, world.admin.id)).lastrowid
    line = ("INSERT INTO debit_memo_items(dm_id, line_no, po_item_id, reason, description, pieces_m, net_unit_c, pieces_per_unit, amount_c)"
            " VALUES (?, ?, 1, 'SHORT', 'x', 1000, 1, 1, 1)")
    c.execute(line, (dm, 1))
    c.execute("UPDATE debit_memos SET pdf = x'01', sha256 = 's' WHERE id = ?", (dm,))
    assert _blocked(c, line, (dm, 2))
    count = c.execute("INSERT INTO branch_counts(po_id, delivery_no, status, counted_by, counted_at) VALUES (?, 1, 'WAITING', ?, 't')", (po, world.admin.id)).lastrowid
    c.execute("INSERT INTO branch_count_items(count_id, po_item_id, description, pieces_m) VALUES (?, 1, 'x', 1000)", (count,))
    c.execute("INSERT INTO branch_count_damage(count_id, po_item_id, damaged_m) VALUES (?, 1, 1000)", (count,))


# ------------------------------------------------------------------ AUD-132: decided amendments and attachments stay

def test_decided_amendments_and_attachments_stay(sample):
    po_am = sample.execute("SELECT id FROM po_amendments WHERE status <> 'PENDING_APPROVAL'").fetchone()[0]
    assert _blocked(sample, "UPDATE po_amendments SET after_json = '{}', reason = 'edited' WHERE id = ?", (po_am,))
    assert _blocked(sample, "UPDATE po_amendments SET status = 'REJECTED' WHERE id = ?", (po_am,))
    assert _blocked(sample, "DELETE FROM po_amendments WHERE id = ?", (po_am,))
    of_am = sample.execute("SELECT id FROM of_amendments WHERE status <> 'PENDING_APPROVAL'").fetchone()
    if of_am:
        assert _blocked(sample, "UPDATE of_amendments SET status = 'REJECTED', after_json = '{}' WHERE id = ?", (of_am[0],))
        assert _blocked(sample, "DELETE FROM of_amendments WHERE id = ?", (of_am[0],))
    att = sample.execute("SELECT id FROM po_attachments").fetchone()
    if att:
        assert _blocked(sample, "DELETE FROM po_attachments WHERE id = ?", (att[0],))
        assert _blocked(sample, "UPDATE po_attachments SET sha256 = 'x' WHERE id = ?", (att[0],))


def test_a_pending_amendment_can_still_be_decided(world):
    c = world.conn
    po = _po_row(world, "ISSUED")
    am = c.execute("INSERT INTO po_amendments(po_id, status, reason, before_json, after_json, requested_by, requested_at) VALUES (?, 'PENDING_APPROVAL', 'r', '{}', '{}', ?, 't')",
                   (po, world.admin.id)).lastrowid
    assert _blocked(c, "UPDATE po_amendments SET after_json = '{\"x\": 1}' WHERE id = ?", (am,))
    c.execute("UPDATE po_amendments SET status = 'REJECTED', decided_by = ?, decided_at = 't' WHERE id = ?", (world.admin.id, am))


# ------------------------------------------------------------------ AUD-134: one probe per new lock

def test_records_keep_who_and_why(sample):
    po = sample.execute("SELECT id FROM purchase_orders WHERE status = 'ISSUED'").fetchone()[0]
    of = sample.execute("SELECT id FROM order_forms WHERE status = 'APPROVED' AND pos_generated_at IS NOT NULL").fetchone()[0]
    assert _blocked(sample, "UPDATE purchase_orders SET approved_by = prepared_by, approval_date = '2000-01-01' WHERE id = ?", (po,))  # S4
    assert _blocked(sample, "UPDATE order_forms SET approved_by = prepared_by WHERE id = ?", (of,))
    assert _blocked(sample, "UPDATE purchase_orders SET total_pieces_m = 1 WHERE id = ?", (po,))  # S6
    assert _blocked(sample, "UPDATE order_forms SET total_pieces_m = 1 WHERE id = ?", (of,))
    assert _blocked(sample, "UPDATE purchase_orders SET payment_status = 'WHATEVER' WHERE id = ?", (po,))  # U18
    assert _blocked(sample, "UPDATE order_forms SET pos_generated_at = NULL, pos_generated_by = NULL WHERE id = ?", (of,))  # U14
    assert _blocked(sample, "UPDATE payment_checks SET difference_reason = 'x', recorded_by = 1 WHERE id = (SELECT MIN(id) FROM payment_checks)")  # U13
    dm = sample.execute("SELECT id FROM debit_memos WHERE status = 'CANCELLED'").fetchone()
    if dm:
        assert _blocked(sample, "UPDATE debit_memos SET cancel_reason = 'rewritten', cancelled_by = 1, cancelled_at = 't' WHERE id = ?", (dm[0],))  # U11
    cs = sample.execute("SELECT id FROM claim_stubs WHERE status = 'AT_WAREHOUSE'").fetchone()
    if cs:
        assert _blocked(sample, "UPDATE claim_stubs SET status = 'ISSUED' WHERE id = ?", (cs[0],))  # U12
    count = sample.execute("SELECT * FROM branch_counts WHERE status = 'APPROVED'").fetchone()
    assert _blocked(sample, "UPDATE branch_counts SET approved_by = counted_by + 1000 WHERE id = ?", (count["id"],))
    assert _blocked(sample, *_clone(sample, "branch_counts", "id = ?", (count["id"],), status="APPROVED", approved_by=None, attempt_no=99))  # A9
    request = sample.execute("SELECT po_id FROM claim_requests").fetchone()
    po_id = request[0] if request else po
    assert _blocked(sample, "INSERT INTO claim_requests(po_id, reason, status, requested_by, requested_at, decided_by, decided_at, cs_id)"
                            " VALUES (?, 'r', 'APPROVED', 1, 't', 1, 't', NULL)", (po_id,))  # A8


def test_dm_cancel_reason_is_written_once(world):
    c = world.conn
    po = _po_row(world, "ISSUED")
    dm = c.execute("INSERT INTO debit_memos(dm_number, dm_year, dm_seq, dm_date, po_id, supplier_id, status, total_c, prepared_by, prepared_at)"
                   " VALUES ('T-DM2', 2026, 2, 'd', ?, ?, 'ISSUED', 0, ?, 't')", (po, world.supplier, world.admin.id)).lastrowid
    c.execute("UPDATE debit_memos SET status = 'CANCELLED', cancelled_by = ?, cancelled_at = 't', cancel_reason = 'first' WHERE id = ?", (world.admin.id, dm))
    assert _blocked(c, "UPDATE debit_memos SET cancel_reason = 'second' WHERE id = ?", (dm,))


# ------------------------------------------------------------------ AUD-136 and AUD-137: files that are not what they should be

def test_a_file_from_another_program_is_refused_cleanly(env):
    path = env / "other.sqlite"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE users(x)")
    raw.commit()
    raw.close()
    c = db.connect(path)
    with pytest.raises(POError, match="This file is not a PO database."):
        db.initialize(c)
    assert not c.in_transaction
    c.close()
    raw = sqlite3.connect(path)
    assert [r[0] for r in raw.execute("SELECT name FROM sqlite_master")] == ["users"]
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
    raw.close()


def test_a_clashing_table_before_v27_is_refused_and_nothing_changes(env):
    path = env / "po.sqlite"
    c = db.open_database(path)
    c.executescript("BEGIN;" + "".join(f"DROP TRIGGER {n};" for n in schema.V28_TRIGGERS)
                    + "DROP TABLE claim_request_items; DROP TABLE claim_requests;"
                    "CREATE TABLE claim_requests(id INTEGER PRIMARY KEY, note TEXT); INSERT INTO claim_requests(note) VALUES ('kept');"
                    "PRAGMA user_version = 26; COMMIT;")
    before = _objects(c)
    with pytest.raises(POError, match="claim_requests"):
        db.initialize(c)
    assert not c.in_transaction
    assert c.execute("PRAGMA user_version").fetchone()[0] == 26
    assert _objects(c) == before
    assert c.execute("SELECT note FROM claim_requests").fetchall()[0][0] == "kept"
    c.close()


def test_a_table_missing_a_column_after_updating_puts_everything_back(env):
    path = env / "po.sqlite"
    c = db.open_database(path)
    _undo_v28(c)
    c.executescript("BEGIN; ALTER TABLE suppliers DROP COLUMN term_discount_days; COMMIT;")
    before = _objects(c)
    audits = c.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
    with pytest.raises(POError, match="suppliers"):
        db.initialize(c)
    assert not c.in_transaction
    assert c.execute("PRAGMA user_version").fetchone()[0] == 27
    assert _objects(c) == before
    assert c.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0] == audits
    c.close()


# ------------------------------------------------------------------ AUD-150: indexes

def test_lookups_use_indexes_and_repeated_ones_are_gone(env):
    c = db.open_database(env / "po.sqlite")
    c.execute("DROP TABLE IF EXISTS sqlite_stat1")
    c.close()
    c = db.connect(env / "po.sqlite")  # no figures yet, as on a large file before they're gathered
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert NEW_INDEXES <= names and not set(DROPPED) & names
    assert "idx_po_year" in names
    for sql, index in (("SELECT * FROM claim_requests WHERE status = 'PENDING'", "idx_claim_requests_status"),
                       ("SELECT * FROM claim_requests WHERE po_id = 1", "idx_claim_requests_po"),
                       ("SELECT * FROM claim_request_items WHERE po_item_id = 1", "idx_claim_request_items_item"),
                       ("SELECT * FROM payment_checks WHERE status = 'ACTIVE'", "idx_payment_checks_status"),
                       ("SELECT * FROM audit_logs WHERE user_id = 1", "idx_audit_user"),
                       ("SELECT * FROM warehouse_delivery_items WHERE po_item_id = 1", "idx_warehouse_delivery_items_item"),
                       ("SELECT * FROM branch_counts WHERE recount_of = 1", "idx_branch_counts_recount"),
                       ("SELECT * FROM claim_stub_items WHERE po_item_id = 1", "idx_claim_stub_items_item"),
                       ("SELECT * FROM item_pass_items WHERE po_item_id = 1", "idx_item_pass_items_item"),
                       ("SELECT * FROM late_damage WHERE paid_before = 1", "idx_late_damage_check"),
                       # The dropped ones were only the start of these.
                       ("SELECT * FROM purchase_order_items WHERE po_id = 1", "sqlite_autoindex_purchase_order_items_1"),
                       ("SELECT * FROM order_form_items WHERE of_id = 1", "sqlite_autoindex_order_form_items_1"),
                       ("SELECT * FROM purchase_orders WHERE of_id = 1", "ux_po_of_branch"),
                       ("SELECT * FROM delivery_receipts WHERE po_id = 1", "sqlite_autoindex_delivery_receipts_2")):
        plan = " ".join(r[3] for r in c.execute("EXPLAIN QUERY PLAN " + sql))
        assert index in plan, (sql, plan)
    c.close()


# ------------------------------------------------------------------ AUD-151: another PC holding the file

def test_a_held_file_gives_a_plain_message(env):
    path = env / "po.sqlite"
    db.open_database(path).close()
    mine, other = db.connect(path), db.connect(path)
    mine.execute("PRAGMA busy_timeout = 100")
    other.execute("BEGIN IMMEDIATE")
    with pytest.raises(POError, match="Another computer is saving right now. Try again in a moment.") as caught:
        with db.tx(mine):
            mine.execute("UPDATE system_settings SET value = value")
    assert isinstance(caught.value, sqlite3.OperationalError)  # still the database error it was, for code that waits and retries
    assert not mine.in_transaction
    other.execute("ROLLBACK")
    # At the commit: someone still reading keeps the change from being written. It is undone, not left open.
    other.execute("BEGIN")
    other.execute("SELECT COUNT(*) FROM system_settings").fetchone()
    with pytest.raises(POError, match="Another computer is saving"):
        with db.tx(mine):
            mine.execute("UPDATE system_settings SET updated_by = NULL WHERE key = 'approval.enabled'")
    assert not mine.in_transaction
    other.execute("ROLLBACK")
    with db.tx(mine):
        mine.execute("UPDATE system_settings SET value = value")
    mine.close()
    other.close()


def test_other_database_errors_are_left_as_they_are(env):
    c = db.open_database(env / "po.sqlite")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        with db.tx(c):
            c.execute("SELECT * FROM nothing_here")
    assert not c.in_transaction
    c.close()
