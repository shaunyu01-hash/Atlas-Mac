"""Safe restore and backup, and what the health check looks at (AUD-123 to AUD-128, AUD-133, AUD-135, AUD-138, AUD-159, AUD-193)."""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
import pytest

from atlaspo import backup, db, orders, schema
from atlaspo.db import POError


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _count(path, sql="SELECT COUNT(*) FROM order_forms"):
    c = sqlite3.connect(str(path))
    try:
        return c.execute(sql).fetchone()[0]
    finally:
        c.close()


# ---- AUD-123: the live database can't be restored over itself.

def test_restore_refuses_the_live_database_under_any_spelling(world, env):
    world.draft(10)
    live = db.db_path()
    other = db.connect(live)  # another window open on the same file
    other.execute("SELECT COUNT(*) FROM order_forms").fetchone()
    before = _sha(live)
    spellings = [live, str(live), env / "." / "po.sqlite", str(live).upper(), str(live).replace("\\", "/")]
    for spelling in spellings:
        with pytest.raises(POError, match="database in use"):
            backup.restore(world.conn, world.admin, spelling, live)
    assert _sha(live) == before
    assert world.conn.execute("SELECT COUNT(*) FROM order_forms").fetchone()[0] == 1  # the caller's connection still works
    assert not list((env / "backups").glob("po-before-restore-*"))
    other.close()


def test_restore_list_never_offers_the_live_database(world, env):
    db.set_settings(world.conn, world.admin, {"backup.folder": str(env)})
    backup.manual_backup(world.conn, world.admin)
    names = [b["name"] for b in backup.list_backups(world.conn)]
    assert names and "po.sqlite" not in names


# ---- AUD-124: nobody else may be saving.

def test_restore_refused_while_someone_else_is_saving(world, env):
    saved = backup.manual_backup(world.conn, world.admin)
    world.draft(10)
    other = db.connect(db.db_path())
    other.execute("BEGIN IMMEDIATE")
    other.execute("INSERT INTO system_settings(key, value, updated_at) VALUES ('other.pc', 'pending', '2026-11-07')")
    with pytest.raises(POError, match="Someone else is saving"):
        backup.restore(world.conn, world.admin, saved, db.db_path())
    other.execute("COMMIT")
    other.close()
    assert world.conn.execute("SELECT COUNT(*) FROM order_forms").fetchone()[0] == 1
    assert world.conn.execute("SELECT value FROM system_settings WHERE key = 'other.pc'").fetchone()[0] == "pending"
    assert not world.conn.execute("SELECT 1 FROM audit_logs WHERE action = 'RESTORE'").fetchone()


def _other_pc_saves(key):
    other = db.connect(db.db_path())
    try:
        with other:
            other.execute("INSERT INTO system_settings(key, value, updated_at) VALUES (?, 'saved', '2026-11-07')", (key,))
    finally:
        other.close()


def test_a_save_between_the_safety_copy_and_the_swap_is_never_lost_from_both(world, env, monkeypatch):
    saved = backup.manual_backup(world.conn, world.admin)
    world.draft(10)
    calls = []

    def save_once():
        calls.append(1)
        if len(calls) == 1:
            _other_pc_saves("other.pc.late")
    monkeypatch.setattr(backup, "_before_swap", save_once)
    safety = backup.restore(world.conn, world.admin, saved, db.db_path())
    assert len(calls) == 2  # the first safety copy missed the save, so it was taken again
    assert _count(safety, "SELECT COUNT(*) FROM system_settings WHERE key = 'other.pc.late'") == 1
    assert _count(safety) == 1
    assert len(list((env / "backups").glob("po-before-restore-*.sqlite"))) == 1


def test_restore_refused_when_other_saves_keep_landing(world, env, monkeypatch):
    saved = backup.manual_backup(world.conn, world.admin)
    world.draft(10)
    n = []

    def save_every_time():
        n.append(1)
        _other_pc_saves(f"other.pc.{len(n)}")
    monkeypatch.setattr(backup, "_before_swap", save_every_time)
    with pytest.raises(POError, match="Someone else is saving"):
        backup.restore(world.conn, world.admin, saved, db.db_path())
    assert world.conn.execute("SELECT COUNT(*) FROM order_forms").fetchone()[0] == 1
    assert world.conn.execute("SELECT COUNT(*) FROM system_settings WHERE key LIKE 'other.pc.%'").fetchone()[0] == len(n)
    assert not world.conn.execute("SELECT 1 FROM audit_logs WHERE action = 'RESTORE'").fetchone()
    assert not list((env / "backups").glob("po-before-restore-*"))
    assert not list(env.glob("po.restoring-*"))


def test_safety_copy_still_holds_the_attachments(world, env, tmp_path_factory):
    po_id = world.po(100)
    quote = tmp_path_factory.mktemp("outside") / "quote.txt"
    quote.write_text("supplier quotation", encoding="utf-8")
    orders.add_attachment(world.conn, world.buyer, po_id, quote)
    saved = env / "empty-backup.sqlite"
    shutil.copy(backup.manual_backup(world.conn, world.admin), saved)  # no attachments zip beside this one
    safety = backup.restore(world.conn, world.admin, saved, db.db_path())
    import zipfile
    with zipfile.ZipFile(backup.attachments_zip(safety)) as z:
        assert len(z.namelist()) == 1


def test_leftovers_from_a_cut_off_restore_are_cleared(world, env):
    import os, time
    saved = backup.manual_backup(world.conn, world.admin)
    old_temp = env / "po.restoring-0123.sqlite"
    old_part = env / "backups" / "po-manual-x.sqlite.abc.partial"
    new_temp = env / "po.restoring-4567.sqlite"
    for f in (old_temp, old_part, new_temp):
        f.write_bytes(b"x" * 10)
    hours_ago = time.time() - 2 * 3600
    for f in (old_temp, old_part):
        os.utime(f, (hours_ago, hours_ago))
    backup.restore(world.conn, world.admin, saved, db.db_path())
    assert not old_temp.exists() and not old_part.exists()
    assert new_temp.exists()  # recent: another computer may still be writing it


def test_restore_records_itself_in_the_restored_file(world, env):
    saved = backup.manual_backup(world.conn, world.admin)
    safety = backup.restore(world.conn, world.admin, saved, db.db_path())
    fresh = db.open_database()
    row = fresh.execute("SELECT summary FROM audit_logs WHERE action = 'RESTORE'").fetchone()
    assert row and safety.name in row["summary"]
    fresh.close()


# ---- AUD-125: a backup that can't be brought up to date changes nothing.

def test_a_backup_that_cannot_be_upgraded_leaves_the_live_database_alone(world, env):
    world.draft(10)
    bad = env / "bad.sqlite"
    copy = sqlite3.connect(str(bad))
    world.conn.backup(copy)
    copy.execute("DROP TABLE claim_request_items")
    copy.execute("DROP TABLE claim_requests")
    copy.execute("CREATE TABLE claim_requests(x)")  # clashes with what version 27 makes
    copy.execute("PRAGMA user_version = 26")
    copy.commit()
    copy.close()
    live = db.db_path()
    before = _sha(live)
    with pytest.raises(POError, match="brought up to date"):
        backup.restore(world.conn, world.admin, bad, live)
    assert _sha(live) == before
    assert world.conn.execute("SELECT COUNT(*) FROM order_forms").fetchone()[0] == 1
    again = db.open_database()
    assert again.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
    assert again.execute("SELECT COUNT(*) FROM order_forms").fetchone()[0] == 1
    again.close()
    assert not list(env.glob("po.restoring-*"))


# ---- AUD-126: the copy never writes over the live file directly, and a break part-way keeps the old data.

def test_a_restore_broken_part_way_keeps_the_live_data(world, env, monkeypatch):
    saved = backup.manual_backup(world.conn, world.admin)
    world.draft(10)
    live = db.db_path()

    def no_file_copy(*a, **k):
        raise AssertionError("restore must not copy files over the database")
    monkeypatch.setattr(shutil, "copy2", no_file_copy)
    monkeypatch.setattr(shutil, "copyfile", no_file_copy)
    monkeypatch.setattr(backup, "_SWAP_PAGES", 1)
    steps = []

    def power_cut(remaining, total):
        steps.append(remaining)
        if len(steps) == 3:
            raise OSError("power cut")
    monkeypatch.setattr(backup, "_swap_step", power_cut)
    with pytest.raises(OSError):
        backup.restore(world.conn, world.admin, saved, live)
    assert len(steps) == 3
    assert world.conn.execute("SELECT COUNT(*) FROM order_forms").fetchone()[0] == 1
    assert [r[0] for r in world.conn.execute("PRAGMA integrity_check")] == ["ok"]
    check = db.open_database()
    assert check.execute("SELECT COUNT(*) FROM order_forms").fetchone()[0] == 1
    check.close()
    # The same restore without the break works and doesn't need a file copy either.
    monkeypatch.setattr(backup, "_swap_step", lambda remaining, total: None)
    backup.restore(world.conn, world.admin, saved, live)
    assert _count(live) == 0


# ---- AUD-159: names never collide, and an existing backup is never replaced.

def test_two_restores_in_the_same_second_keep_both_safety_copies(world, env, clock):
    clock(2026, 11, 7, 9, 0)
    saved = backup.manual_backup(world.conn, world.admin)
    world.draft(10)
    first = backup.restore(world.conn, world.admin, saved, db.db_path())
    world.conn = db.open_database()
    world.draft(20)
    world.draft(30)
    second = backup.restore(world.conn, world.admin, saved, db.db_path())
    assert first != second and first.exists() and second.exists()
    assert _count(first) == 1 and _count(second) == 2
    again = backup.manual_backup(db.open_database(), world.admin)
    assert again != saved and saved.exists()


def test_copy_never_replaces_an_existing_backup(world, env):
    saved = backup.manual_backup(world.conn, world.admin)
    before = _sha(saved)
    world.draft(10)
    with pytest.raises(POError, match="already there"):
        backup.manual_backup(world.conn, world.admin, saved)
    assert _sha(saved) == before


# ---- AUD-127: the closing backup holds the day's work.

def test_exit_backup_holds_changes_made_after_start_up(world, env, clock):
    clock(2026, 11, 7, 8, 0)
    first = backup.auto_backup(world.conn)
    assert _count(first) == 0
    world.draft(10)
    clock(2026, 11, 7, 17, 30)
    assert backup.auto_backup(world.conn) == first
    assert _count(first) == 1
    assert not list(first.parent.glob("*.partial"))


# ---- AUD-128: attached files are backed up and come back with a restore.

def test_attachments_come_back_with_a_restore(world, env, tmp_path_factory):
    po_id = world.po(100)
    quote = tmp_path_factory.mktemp("outside") / "quote.txt"
    quote.write_text("supplier quotation", encoding="utf-8")
    orders.add_attachment(world.conn, world.buyer, po_id, quote)
    row = orders.list_attachments(world.conn, po_id)[0]
    saved = backup.manual_backup(world.conn, world.admin)
    assert backup.attachments_zip(saved).is_file()
    shutil.rmtree(env / "attachments")
    problems = backup.integrity_report(world.conn)
    assert any("missing" in p and "quote.txt" in p for p in problems)
    backup.restore(world.conn, world.admin, saved, db.db_path())
    assert orders.attachment_path(row).read_text(encoding="utf-8") == "supplier quotation"
    fresh = db.open_database()
    assert backup.integrity_report(fresh) == []
    fresh.close()


def test_health_check_lists_files_that_belong_to_no_po(world, env):
    (env / "attachments").mkdir(exist_ok=True)
    (env / "attachments" / "stray.pdf").write_bytes(b"x")
    assert any("belong to no PO" in p and "stray.pdf" in p for p in backup.integrity_report(world.conn))


# ---- AUD-133, AUD-135, AUD-138, AUD-193: what the health check looks at.

def _raw(path):
    """An outside tool on the file: none of the settings the program's own connection uses."""
    return sqlite3.connect(str(path), isolation_level=None)


def test_health_check_flags_a_leftover_write_guard(world, env):
    raw = _raw(db.db_path())
    raw.execute("INSERT INTO po_write_guard(token) VALUES ('left-behind')")
    raw.close()
    assert any("po_write_guard" in p for p in backup.integrity_report(world.conn))


def test_health_check_flags_a_missing_guard_trigger(world, env):
    raw = _raw(db.db_path())
    raw.execute("DROP TRIGGER issued_versions_no_delete")
    raw.close()
    world.conn.close()
    world.conn = db.connect()
    assert any("issued_versions_no_delete" in p for p in backup.integrity_report(world.conn))


def _issue_dm(world, extra_item=None):
    """A debit memo made straight in the tables the way the program stores one: header, lines, then its kept PDF."""
    po_id = world.po(100)
    item = world.conn.execute("SELECT id FROM purchase_order_items WHERE po_id = ?", (po_id,)).fetchone()[0]
    pdf = b"%PDF-1.4 debit memo"
    raw = _raw(db.db_path())
    raw.execute("INSERT INTO dm_number_sequences(year, last_number) VALUES (2026, 1)")
    dm = raw.execute("INSERT INTO debit_memos(dm_number, dm_year, dm_seq, dm_date, po_id, supplier_id, status, total_c, prepared_by, prepared_at)"
                     " VALUES ('2026-DM-00001', 2026, 1, '2026-11-07', ?, ?, 'ISSUED', 5000, ?, '2026-11-07')",
                     (po_id, world.supplier, world.admin.id)).lastrowid
    lines = [(item, 5000)] + ([(extra_item, 0)] if extra_item else [])
    for n, (po_item, amount) in enumerate(lines, 1):
        raw.execute("INSERT INTO debit_memo_items(dm_id, line_no, po_item_id, reason, description, pieces_m, net_unit_c, pieces_per_unit, amount_c)"
                    " VALUES (?, ?, ?, 'SHORT', 'Item', 1000, ?, 1, ?)", (dm, n, po_item, amount, amount))
    raw.execute("UPDATE debit_memos SET pdf = ?, sha256 = ? WHERE id = ?", (pdf, hashlib.sha256(pdf).hexdigest(), dm))
    raw.close()
    return po_id, item, dm


def test_health_check_is_quiet_for_a_proper_debit_memo(world, env):
    _issue_dm(world)
    assert backup.integrity_report(world.conn) == []


def test_health_check_flags_debit_memo_faults(world, env):
    po_id, item, dm = _issue_dm(world)
    raw = _raw(db.db_path())
    raw.execute("DROP TRIGGER debit_memo_fixed")
    raw.execute("DROP TRIGGER debit_memo_pdf_fixed")
    raw.execute("DROP TRIGGER dm_sequence_never_backward")
    raw.execute("UPDATE debit_memos SET total_c = 9999, pdf = x'00', sha256 = sha256 WHERE id = ?", (dm,))
    raw.close()
    problems = backup.integrity_report(world.conn)
    assert any("2026-DM-00001: the debit memo total differs from its lines" == p for p in problems)
    assert any("2026-DM-00001: the kept debit memo PDF has changed" in p for p in problems)
    assert any("guards that keep records permanent are gone" in p for p in problems)


def test_health_check_flags_a_counter_behind_and_a_memo_without_its_pdf(world, env):
    _issue_dm(world)
    raw = _raw(db.db_path())
    raw.execute("DROP TRIGGER dm_sequence_no_delete")
    raw.execute("DELETE FROM dm_number_sequences")
    raw.execute("DROP TRIGGER debit_memo_pdf_fixed")
    raw.execute("UPDATE debit_memos SET pdf = x'', sha256 = ''")
    raw.close()
    problems = backup.integrity_report(world.conn)
    assert "2026: the debit memo number counter is behind the highest debit memo number" in problems
    assert "2026-DM-00001: the debit memo has no kept PDF" in problems


def test_health_check_flags_a_changed_issued_po_pdf(world, env):
    po_id = world.po(100)
    orders.issue(world.conn, world.buyer, po_id)
    assert world.conn.execute("SELECT 1 FROM po_issued_versions").fetchone()
    assert backup.integrity_report(world.conn) == []
    raw = _raw(db.db_path())
    raw.execute("DROP TRIGGER issued_versions_no_update")
    raw.execute("UPDATE po_issued_versions SET pdf = x'00'")
    raw.close()
    assert any("issued PO PDF has changed" in p for p in backup.integrity_report(world.conn))


def test_health_check_flags_orphan_references(world, env):
    po_id, item, dm = _issue_dm(world, extra_item=999999)
    raw = _raw(db.db_path())
    raw.execute("INSERT INTO payment_signoffs(kind, ref_id, amount_c, requested_by, requested_at) VALUES ('CHECK', 424242, 100, ?, '2026-11-07')", (world.admin.id,))
    raw.execute("INSERT INTO goods_arrivals(kind, item_id, pieces_m, counted_by, counted_at) VALUES ('CS', 777777, 1000, ?, '2026-11-07')", (world.admin.id,))
    raw.close()
    problems = backup.integrity_report(world.conn)
    assert "1 debit memo lines point to a PO line that no longer exists" in problems
    assert "1 payment sign-offs point to an OF or check that no longer exists" in problems
    assert "1 warehouse counts of goods coming back point to a claim stub or item pass line that no longer exists" in problems


def test_health_check_flags_a_replaced_attachment(world, env, tmp_path_factory):
    po_id = world.po(100)
    quote = tmp_path_factory.mktemp("outside") / "quote.txt"
    quote.write_text("the real quotation", encoding="utf-8")
    orders.add_attachment(world.conn, world.buyer, po_id, quote)
    assert backup.integrity_report(world.conn) == []
    row = orders.list_attachments(world.conn, po_id)[0]
    orders.attachment_path(row).write_text("a swapped quotation", encoding="utf-8")
    assert any("quote.txt' has changed since it was attached" in p for p in backup.integrity_report(world.conn))


# ---- Settings > Restore: a refused restore keeps the screen working.

def test_settings_restore_of_the_live_file_is_refused_and_the_screen_keeps_working(world, env, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from atlaspo.ui import common, settings_page
    app = QApplication.instance() or QApplication([])  # noqa: F841  kept alive for the page
    monkeypatch.setattr(settings_page, "confirm", lambda *a, **k: True)
    shown = []
    monkeypatch.setattr(common, "error", lambda *a, **k: shown.append(a[1:]))
    page = settings_page.SettingsPage(world.conn, world.admin)
    emitted = []
    page.restored.connect(lambda: emitted.append(1))
    common.guarded(page, page._restore, str(db.db_path()))
    assert shown and "database in use" in str(shown[0]) and not emitted
    assert page.conn.execute("SELECT COUNT(*) FROM departments").fetchone()[0] == 9
    assert all(b["name"] != "po.sqlite" for b in backup.list_backups(page.conn))
