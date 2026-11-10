"""Owner decisions of 8 November 2026, package P1: the V28 columns and guards (Q1, Q2, Q4), AUD-130 for damage reports waiting at the
upgrade, and several PCs sharing one SQLite file on the office network (Q32)."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PO_SAMPLE_DIR", tempfile.mkdtemp(prefix="po-sample-"))

import pytest

from atlaspo import auth, backup, claim_stubs as cs, db, schema

from conftest import NORTH, SOUTH, World
from test_payments import Order

NEW_TRIGGERS = ("check_expected_fixed", "debit_memo_paid_back_fixed", "debit_memo_paid_back_new")
NEW_COLUMNS = {"payment_checks": "expected_c", "payment_differences": "dr_lower_c", "debit_memos": "paid_back_check_id"}


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


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _triggers(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}


def _v27(path: Path):
    """A real version-27 file: made by this program with version 28 left out, so it has no V28 guards, no new columns and no MIGRATE_V28 note."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(schema, "MIGRATE_V28", "PRAGMA user_version = 27;")
        mp.setattr(schema, "V28_COLUMNS", {})
        conn = db.connect(path)
        db.initialize(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 27
    assert not conn.execute("SELECT 1 FROM audit_logs WHERE action = 'MIGRATE_V28'").fetchone()
    assert "expected_c" not in _columns(conn, "payment_checks")
    return conn


# ------------------------------------------------------------------ A: the columns and guards

def test_a_new_file_has_the_three_columns_and_the_new_guards(conn):
    for table, column in NEW_COLUMNS.items():
        assert column in _columns(conn, table)
    assert set(NEW_TRIGGERS) <= set(schema.V28_TRIGGERS) <= _triggers(conn)
    assert len(schema.V28_TRIGGERS) == schema.V28_TRIGGER_SQL.count("CREATE TRIGGER IF NOT EXISTS ")
    assert db._shape_problem(conn) == ""
    note = conn.execute("SELECT summary, new_value FROM audit_logs WHERE action = 'MIGRATE_V28'").fetchone()
    assert json.loads(note["new_value"]) == {"checks_expected": 0, "memos_paid_back": 0, "last_claim_request": 0}


def _old_style_report(world, monkeypatch, order):
    """A damage report sent under version 27, whose log line named the PO, not the report."""
    real = cs.audit

    def old_audit(conn, user, action, entity_type, entity_id=None, **kw):
        if action == "CS_REQUEST":
            entity_type, entity_id = "purchase_order", kw.get("po_id")
        return real(conn, user, action, entity_type, entity_id, **kw)
    monkeypatch.setattr(cs, "audit", old_audit)
    po = order.pos[NORTH]
    item = world.conn.execute("SELECT id FROM purchase_order_items WHERE po_id = ?", (po["id"],)).fetchone()[0]
    receiver = auth.get_user(world.conn, world.conn.execute("SELECT id FROM users WHERE username = ?", (f"r{NORTH}x1",)).fetchone()[0])
    request_id = cs.send_report(world.conn, receiver, po["po_number"], {item: "1"}, "Torn seams")[0]
    monkeypatch.setattr(cs, "audit", real)
    return request_id


def _check(conn, admin, supplier, of_id, number, at, status="ACTIVE", expected=None):
    check_id = conn.execute(
        "INSERT INTO payment_checks(check_number, source, check_date, supplier_id, amount_c, status, recorded_by, recorded_at, cancelled_at)"
        " VALUES (?, 'EXTERNAL', ?, ?, 100000, ?, ?, ?, ?)",
        (number, at[:10], supplier, status, admin.id, at, at if status == "CANCELLED" else None)).lastrowid
    conn.execute("INSERT INTO payment_check_ofs(check_id, of_id, amount_c) VALUES (?, ?, 100000)", (check_id, of_id))
    if expected is not None:
        db.audit(conn, admin, "CHECK_RECORD", "payment_check", check_id, of_id=of_id, summary=f"Check {number}",
                 new={"amount_c": 100000, "expected_c": expected, "credit_c": 0, "reason": ""})
    return check_id


def _memo(conn, admin, po_id, supplier, n, cancelled_at=None):
    return conn.execute(
        "INSERT INTO debit_memos(dm_number, dm_year, dm_seq, dm_date, po_id, supplier_id, status, total_c, prepared_by, prepared_at,"
        " cancelled_by, cancelled_at, cancel_reason) VALUES (?, 2026, ?, '2026-11-01', ?, ?, ?, 5000, ?, '2026-11-01 09:00:00', ?, ?, ?)",
        (f"DMT-{n}", n, po_id, supplier, "CANCELLED" if cancelled_at else "ISSUED", admin.id,
         admin.id if cancelled_at else None, cancelled_at, "Goods came after all" if cancelled_at else "")).lastrowid


@pytest.fixture
def upgraded(env, monkeypatch):
    """A version-27 file with checks, memos and a waiting damage report, then upgraded to 28 by the program."""
    path = env / "po.sqlite"
    conn = _v27(path)
    world = World(conn)
    first, second = Order(world), Order(world)
    request_id = _old_style_report(world, monkeypatch, first)
    po1 = first.pos[NORTH]["id"]
    w = world
    with db.tx(conn):
        paid = _check(conn, w.admin, w.supplier, first.of_id, "1001", "2026-11-03 10:00:00", expected=199000)
        _check(conn, w.admin, w.supplier, first.of_id, "1000", "2026-11-01 10:00:00", status="CANCELLED", expected=150000)
        unlogged = _check(conn, w.admin, w.supplier, second.of_id, "1002", "2026-11-03 11:00:00")
        after = _memo(conn, w.admin, po1, w.supplier, 1, cancelled_at="2026-11-04 09:00:00")
        before = _memo(conn, w.admin, po1, w.supplier, 2, cancelled_at="2026-11-02 09:00:00")
        issued = _memo(conn, w.admin, po1, w.supplier, 3)
        other = _memo(conn, w.admin, second.pos[NORTH]["id"], w.supplier, 4, cancelled_at="2026-11-02 09:00:00")
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    rows = {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables}
    conn.close()
    conn = db.open_database(path)
    yield {"conn": conn, "world": world, "rows": rows, "request_id": request_id, "first": first, "second": second,
           "paid": paid, "unlogged": unlogged, "after": after, "before": before, "issued": issued, "other": other}
    conn.close()


def test_a_v27_file_upgrades_keeping_every_row_and_fills_the_new_columns_from_the_record(upgraded):
    c, u = upgraded["conn"], upgraded
    assert c.execute("PRAGMA user_version").fetchone()[0] == 28
    for table, column in NEW_COLUMNS.items():
        assert column in _columns(c, table)
    assert set(schema.V28_TRIGGERS) <= _triggers(c) and db._shape_problem(c) == ""
    # Every row is still there with every value it had. Only the MIGRATE_V28 note is new in the log.
    for table, before in u["rows"].items():
        after = [dict(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        if table == "audit_logs":
            assert after[:len(before)] == before and [r["action"] for r in after[len(before):]] == ["MIGRATE_V28"]
            continue
        for r in after:
            r.pop(NEW_COLUMNS.get(table, ""), None)
        assert after == before, table
    # What the program worked out comes from each check's own record line. A check with no line stays empty.
    expected = dict(c.execute("SELECT check_number, expected_c FROM payment_checks").fetchall())
    assert expected == {"1001": 199000, "1000": 150000, "1002": None}
    # Only the memo cancelled after a live check paid its order is paid back, after that check.
    paid_back = dict(c.execute("SELECT id, paid_back_check_id FROM debit_memos").fetchall())
    assert paid_back == {u["after"]: u["paid"], u["before"]: None, u["issued"]: None, u["other"]: None}
    assert c.execute("SELECT COUNT(*) FROM payment_differences WHERE dr_lower_c IS NOT NULL").fetchone()[0] == 0
    note = c.execute("SELECT summary, new_value FROM audit_logs WHERE action = 'MIGRATE_V28'").fetchone()
    assert "2 checks keep what the program worked out" in note["summary"] and "1 debit memos cancelled after payment" in note["summary"]
    assert json.loads(note["new_value"]) == {"checks_expected": 2, "memos_paid_back": 1, "last_claim_request": u["request_id"]}
    assert c.execute("PRAGMA foreign_key_check").fetchall() == [] and c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # A file already at 28 is not touched again.
    db.initialize(c)
    assert c.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'MIGRATE_V28'").fetchone()[0] == 1


def test_the_new_columns_cannot_change_once_set(upgraded):
    c, u = upgraded["conn"], upgraded
    assert _blocked(c, "UPDATE payment_checks SET expected_c = expected_c + 1 WHERE id = ?", (u["paid"],))
    assert _blocked(c, "UPDATE payment_checks SET expected_c = NULL WHERE id = ?", (u["paid"],))
    # An empty one may be filled once (the program fills it when it records the check), then it is fixed too.
    c.execute("SAVEPOINT fill")
    c.execute("UPDATE payment_checks SET expected_c = 5 WHERE id = ?", (u["unlogged"],))
    assert _blocked(c, "UPDATE payment_checks SET expected_c = 6 WHERE id = ?", (u["unlogged"],))
    c.execute("ROLLBACK TO fill")
    c.execute("RELEASE fill")
    # The paid-back check of a cancelled memo stays as it is.
    assert _blocked(c, "UPDATE debit_memos SET paid_back_check_id = NULL WHERE id = ?", (u["after"],))
    assert _blocked(c, "UPDATE debit_memos SET paid_back_check_id = ? WHERE id = ?", (u["paid"], u["before"]))
    # It can only be set in the same change that cancels an issued memo.
    assert _blocked(c, "UPDATE debit_memos SET paid_back_check_id = ? WHERE id = ?", (u["paid"], u["issued"]))
    cancel = ("UPDATE debit_memos SET status = 'CANCELLED', cancelled_by = 1, cancelled_at = '2026-11-08 10:00:00', cancel_reason = 'x',"
              " paid_back_check_id = ? WHERE id = ?")
    assert not _blocked(c, cancel, (u["paid"], u["issued"]))
    assert _blocked(c, "INSERT INTO debit_memos(dm_number, dm_year, dm_seq, dm_date, po_id, supplier_id, status, total_c, prepared_by, prepared_at,"
                       " paid_back_check_id) SELECT 'DMT-9', 2026, 9, dm_date, po_id, supplier_id, 'ISSUED', 0, prepared_by, prepared_at, ? FROM debit_memos"
                       " WHERE id = ?", (u["paid"], u["issued"]))


def test_a_damage_report_waiting_at_the_upgrade_takes_no_new_line_but_can_still_be_approved(upgraded):
    c, u, world = upgraded["conn"], upgraded, upgraded["world"]
    request_id = u["request_id"]
    other = c.execute("SELECT id FROM purchase_order_items WHERE po_id = ?", (u["first"].pos[SOUTH]["id"],)).fetchone()[0]
    # Its log line names the PO, the old way, so only the upgrade note protects it.
    assert not c.execute("SELECT 1 FROM audit_logs WHERE action = 'CS_REQUEST' AND entity_type = 'claim_request'").fetchone()
    assert _blocked(c, "INSERT INTO claim_request_items(request_id, po_item_id, pieces_m) VALUES (?, ?, 1000)", (request_id, other))
    admin = auth.get_user(c, world.admin.id)
    stub = cs.approve_report(c, admin, request_id)
    assert stub and c.execute("SELECT COUNT(*) FROM claim_stub_items WHERE cs_id = ?", (stub["id"],)).fetchone()[0] == 1
    # A report made after the upgrade is written normally, and its own log line then fixes it.
    item2 = c.execute("SELECT id FROM purchase_order_items WHERE po_id = ?", (u["second"].pos[NORTH]["id"],)).fetchone()[0]
    receiver = auth.get_user(c, c.execute("SELECT id FROM users WHERE username = ?", (f"r{NORTH}x1",)).fetchone()[0])
    new = cs.send_report(c, receiver, u["second"].pos[NORTH]["po_number"], {item2: "1"}, "Wet box")[0]
    assert new > request_id
    other2 = c.execute("SELECT id FROM purchase_order_items WHERE po_id = ?", (u["second"].pos[SOUTH]["id"],)).fetchone()[0]
    assert _blocked(c, "INSERT INTO claim_request_items(request_id, po_item_id, pieces_m) VALUES (?, ?, 1000)", (new, other2))


def test_a_report_made_after_the_upgrade_still_takes_its_lines_while_it_is_written(upgraded):
    c, u = upgraded["conn"], upgraded
    receiver_id = c.execute("SELECT id FROM users WHERE username = ?", (f"r{NORTH}x1",)).fetchone()[0]
    po = u["second"].pos[NORTH]["id"]
    item = c.execute("SELECT id FROM purchase_order_items WHERE po_id = ?", (po,)).fetchone()[0]
    c.execute("SAVEPOINT w")
    try:
        # A clock set months back (as the sample data does) makes no difference: the report number decides, not the time.
        rid = c.execute("INSERT INTO claim_requests(po_id, reason, status, requested_by, requested_at) VALUES (?, 'x', 'PENDING', ?, '2020-01-01 10:00:00')",
                        (po, receiver_id)).lastrowid
        c.execute("INSERT INTO claim_request_items(request_id, po_item_id, pieces_m) VALUES (?, ?, 1000)", (rid, item))
    finally:
        c.execute("ROLLBACK TO w")
        c.execute("RELEASE w")


# ------------------------------------------------------------------ B1: several PCs starting the new version at once

def test_two_pcs_upgrading_at_once_make_one_upgrade(env, monkeypatch):
    path = env / "po.sqlite"
    _v27(path).close()
    real = db._backup_before_migration
    both_backed_up = threading.Barrier(2, timeout=60)

    def backup_then_wait(conn, version):
        made = real(conn, version)
        both_backed_up.wait()  # both PCs read version 27 and made their copy before either updates
        return made
    monkeypatch.setattr(db, "_backup_before_migration", backup_then_wait)
    errors, copies = [], []

    def start():
        try:
            conn = db.connect(path)
            try:
                db.initialize(conn)
            finally:
                conn.close()
        except BaseException as exc:  # noqa: BLE001 (reported by the test)
            errors.append(exc)
    threads = [threading.Thread(target=start) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert errors == []
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 28
    assert conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'MIGRATE_V28'").fetchone()[0] == 1
    assert set(NEW_TRIGGERS) <= _triggers(conn) and db._shape_problem(conn) == ""
    conn.close()
    copies = list((env / "backups").glob("po-before-migration-v27-*.sqlite"))
    assert len(copies) == 2  # two copies made in the same moment never share a name


def test_the_copy_before_an_update_has_a_name_no_other_pc_uses(env, clock):
    clock(2026, 11, 8, 9, 0)
    folder = env / "b"
    folder.mkdir()
    first = db._unique_backup_name(folder, "po-before-migration-v27")
    first.write_bytes(b"")
    second = db._unique_backup_name(folder, "po-before-migration-v27")
    assert first != second and db._computer() in first.name and "-000-" in first.name


def test_statements_run_one_by_one_keep_triggers_whole():
    parts = db._statements(schema.MIGRATE_V28)
    assert all(sqlite3.complete_statement(p) for p in parts)
    assert sum(p.strip().startswith(("CREATE TRIGGER", "-- ")) and "CREATE TRIGGER" in p for p in parts) == len(schema.V28_TRIGGERS)
    assert db._statements("SELECT 'a;b'; SELECT 2") == ["SELECT 'a;b';", " SELECT 2"]


# ------------------------------------------------------------------ B2, B3: another PC saving at start-up, and never WAL

def test_another_pc_saving_at_start_up_gives_the_plain_message(env, monkeypatch):
    path = env / "po.sqlite"
    db.open_database(path).close()
    monkeypatch.setattr(db, "BUSY_TIMEOUT_MS", 200)
    holder = sqlite3.connect(str(path), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(db.DatabaseBusy) as found:
            db.open_database(path)
        assert str(found.value) == db.BUSY_MESSAGE == "Another computer is saving right now. Try again in a moment."
        with pytest.raises(db.DatabaseBusy):
            db.connect(path)
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_the_start_up_window_shows_the_busy_message(env, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox
    from atlaspo import __main__ as main
    app = QApplication.instance() or QApplication([])
    db.open_database().close()
    shown = []
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda parent, title, text: shown.append((title, text))))

    def busy(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(db, "open_database", busy)
    assert main.Session(app).start() is False
    assert shown == [("Can't open the data", db.BUSY_MESSAGE)]
    shown.clear()

    def busy_rule(*a, **k):
        raise db.DatabaseBusy(db.BUSY_MESSAGE)
    monkeypatch.setattr(db, "open_database", busy_rule)
    assert main.Session(app).start() is False
    assert shown == [("Can't open the data", db.BUSY_MESSAGE)]
    assert not (env / "errors.log").exists()


def test_a_busy_speed_up_is_skipped_without_waiting(env):
    path = env / "po.sqlite"
    conn = db.open_database(path)
    holder = sqlite3.connect(str(path), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        db._optimize(conn)
        assert time.monotonic() - started < 3  # not the 15 s wait
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS == 15000
    assert not conn.in_transaction
    conn.close()


def test_a_file_that_will_not_leave_wal_is_refused(env, monkeypatch):
    real = sqlite3.connect

    class StuckInWal(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.strip().upper().startswith("PRAGMA JOURNAL_MODE"):
                return super().execute("SELECT 'wal'")
            return super().execute(sql, *args)
    monkeypatch.setattr(db.sqlite3, "connect", lambda *a, **k: real(*a, factory=StuckInWal, **k))
    with pytest.raises(db.POError) as found:
        db.connect(env / "po.sqlite")
    assert str(found.value) == db.JOURNAL_MESSAGE and "shared folder" in str(found.value)


def test_the_journal_stays_delete_and_full(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 15000


# ------------------------------------------------------------------ B4: data-folder.txt beside the built program

@pytest.fixture
def frozen(tmp_path, monkeypatch):
    program = tmp_path / "program"
    program.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(program / "ATLASPO.exe"))
    monkeypatch.delenv("PO_DATA_DIR", raising=False)
    return program


def test_the_pointer_file_is_read_only_by_the_built_program(tmp_path, monkeypatch, frozen):
    shared = tmp_path / "shared"
    shared.mkdir()
    (frozen / "data-folder.txt").write_text(f"# the office server\n\"{shared}\"\n", encoding="utf-8")
    assert db.pointer_folder() == shared and db.data_dir() == shared
    # PO_DATA_DIR still comes first.
    monkeypatch.setenv("PO_DATA_DIR", str(tmp_path / "env"))
    assert db.data_dir() == tmp_path / "env"
    # Run from the source (tests, development), the file is never read.
    monkeypatch.setattr(sys, "frozen", False)
    assert db.pointer_folder() is None


def test_an_unreachable_shared_folder_is_refused_and_never_made(tmp_path, frozen):
    missing = tmp_path / "server" / "ATLASPO"
    (frozen / "data-folder.txt").write_text(str(missing), encoding="utf-8")
    with pytest.raises(db.DataFolderUnreachable) as found:
        db.data_dir()
    assert str(found.value) == f"The shared data folder {missing} can't be reached. Check the network and try again."
    assert isinstance(found.value, OSError) and isinstance(found.value, db.POError)
    with pytest.raises(db.POError):
        db.open_database()
    assert not missing.exists() and not (tmp_path / "server").exists()


def test_a_reachable_shared_folder_without_data_is_not_given_an_empty_database(tmp_path, frozen):
    shared = tmp_path / "shared"
    shared.mkdir()
    (frozen / "data-folder.txt").write_text(str(shared), encoding="utf-8")
    with pytest.raises(db.POError) as found:
        db.open_database()
    assert "no PO data in the shared folder" in str(found.value)
    assert list(shared.iterdir()) == []
    # With the data there, it opens.
    db.open_database(shared / "po.sqlite").close()
    db.open_database().close()


# ------------------------------------------------------------------ B5: backups while other PCs save

def test_a_backup_copies_in_steps_and_another_pc_can_save_during_it(env, monkeypatch):
    conn = db.open_database()
    world = World(conn)
    for n in range(40):
        world.draft()
    path = db.db_path()
    monkeypatch.setattr(backup, "_COPY_PAGES", 2)
    monkeypatch.setattr(backup, "_COPY_SLEEP", 0)
    saved, steps = [], []

    def other_pc_saves(remaining, total):
        steps.append(remaining)
        if len(steps) == 3:
            other = sqlite3.connect(str(path), timeout=0, isolation_level=None)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("INSERT INTO system_settings(key, value, updated_at) VALUES ('test.saved_during_backup', '1', 'now')")
                other.execute("COMMIT")
                saved.append(True)
            finally:
                other.close()
    monkeypatch.setattr(backup, "_copy_step", other_pc_saves)
    copy = backup.manual_backup(conn, world.admin)
    assert saved == [True] and len(steps) > 3
    check = sqlite3.connect(str(copy))
    try:
        assert check.execute("SELECT value FROM system_settings WHERE key = 'test.saved_during_backup'").fetchone() == ("1",)
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()
    conn.close()


def test_saves_that_keep_landing_still_end_in_a_whole_copy(env, monkeypatch):
    conn = db.open_database()
    world = World(conn)
    for n in range(20):
        world.draft()
    path = db.db_path()
    monkeypatch.setattr(backup, "_COPY_PAGES", 2)
    monkeypatch.setattr(backup, "_COPY_SLEEP", 0)
    count = []

    def always_saving(remaining, total):
        count.append(1)
        other = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        try:
            other.execute("INSERT OR REPLACE INTO system_settings(key, value, updated_at) VALUES ('test.n', ?, 'now')", (str(len(count)),))
        finally:
            other.close()
    monkeypatch.setattr(backup, "_copy_step", always_saving)
    copy = backup.manual_backup(conn, world.admin)
    assert backup._integrity(copy) == []
    conn.close()


def test_the_start_up_copy_is_not_made_again_minutes_after_another_pc_made_it(env, monkeypatch):
    conn = db.open_database()
    target = backup.auto_backup(conn)
    made = target.stat().st_mtime
    monkeypatch.setattr(backup, "_copy", lambda *a, **k: pytest.fail("copied again"))
    assert backup.auto_backup(conn, at_start=True) == target and target.stat().st_mtime == made
    # Logout and exit still bring it up to date, and an older copy is refreshed at start-up too.
    monkeypatch.undo()
    monkeypatch.setenv("PO_DATA_DIR", str(env))
    calls = []
    real = backup._copy
    monkeypatch.setattr(backup, "_copy", lambda *a, **k: calls.append(1) or real(*a, **k))
    backup.auto_backup(conn)
    old = time.time() - 3600
    os.utime(target, (old, old))
    backup.auto_backup(conn, at_start=True)
    assert len(calls) == 2
    conn.close()


# ------------------------------------------------------------------ B6: print files in a shared folder

def test_print_file_names_never_collide(env, monkeypatch):
    from atlaspo.ui import po_page
    from atlaspo.ui.common import computer_name
    a = po_page.write_print_file("PO-0001", b"one")
    b = po_page.write_print_file("PO-0001", b"two")
    assert a != b and a.read_bytes() == b"one" and b.read_bytes() == b"two"
    assert a.parent == env / "print" and computer_name() in a.name and a.name.startswith("PO-0001 ")
    # A file another PC has open in its PDF viewer can't be written: the next free name is used.
    real_open = open
    refused = []

    def open_in_viewer(path, mode="r", *args, **kw):
        if "x" in mode and not refused:
            refused.append(path)
            raise PermissionError(13, "in use")
        return real_open(path, mode, *args, **kw)
    monkeypatch.setattr(po_page, "open", open_in_viewer, raising=False)
    c = po_page.write_print_file("PO-0001", b"three")
    assert refused and c != Path(refused[0]) and c.read_bytes() == b"three"
    assert (env / "print").is_dir()
