"""Backup, restore and integrity checks. Uses SQLite's backup API, so a copy is consistent even mid-work."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import time
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path

from .auth import User, require
from .db import POError, audit, data_dir, get_setting, now, tx
from . import db, schema

AUTO_PATTERN = re.compile(r"po-auto-(\d{4}-\d{2}-\d{2})\.sqlite$")
BUSY = "Someone else is saving. Close the program on the other computers and try again."
# How many pages each step of a restore copies. The whole copy is still one change: SQLite journals it, so a crash part-way leaves the old data.
_SWAP_PAGES = 1024
# A backup copies the live file a few pages at a time and rests between steps, so other PCs sharing the file can save meanwhile.
# If one does, SQLite starts the copy again so it stays consistent; after a few such starts the rest is copied in one go.
_COPY_PAGES = 256
_COPY_SLEEP = 0.02
_COPY_RESTARTS = 5
# An automatic copy written this recently (by another PC, at its own login) is not made again at start-up.
_AUTO_FRESH_SECONDS = 600


def backup_folder(conn) -> Path:
    custom = get_setting(conn, "backup.folder")
    path = Path(custom) if custom else data_dir() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _attachments_folder() -> Path:
    return data_dir() / "attachments"


def attachments_zip(path: Path) -> Path:
    """The attached files saved with a backup sit beside it: po-manual-....sqlite and po-manual-...-attachments.zip."""
    path = Path(path)
    return path.with_name(path.stem + "-attachments.zip")


def _unique(folder: Path, stem: str) -> Path:
    """A name no other backup has: to the millisecond, with a counter if two land in the same one."""
    t = now()
    base = f"{stem}-{t:%Y-%m-%d-%H%M%S}-{t.microsecond // 1000:03d}"
    path, n = folder / f"{base}.sqlite", 2
    while True:
        if not attachments_zip(path).exists():
            try:  # claim the name: two backups started in the same millisecond can't both get it
                os.close(os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return path
            except FileExistsError:
                pass
        path, n = folder / f"{base}-{n}.sqlite", n + 1


class _TooManyRestarts(Exception):
    pass


def _copy_step(remaining: int, total: int) -> None:
    """Called after each step of a backup copy. Tests replace it to save from another connection part-way."""


def _copy_in_steps(conn, temp: Path) -> None:
    """Copies the live database to temp a few pages at a time, without holding the file between steps."""
    seen = {"left": None, "restarts": 0}

    def progress(status, remaining, total):
        if seen["left"] is not None and remaining >= seen["left"]:
            # No progress: another computer saved, so SQLite started the copy again from the beginning.
            seen["restarts"] += 1
            if seen["restarts"] > _COPY_RESTARTS:
                raise _TooManyRestarts()
        seen["left"] = remaining
        _copy_step(remaining, total)

    dest = sqlite3.connect(str(temp))
    try:
        try:
            conn.backup(dest, pages=_COPY_PAGES, progress=progress, sleep=_COPY_SLEEP)
            return
        except _TooManyRestarts:
            pass
        # Saves keep landing: the rest is copied in one step, which holds the file only for as long as the copy takes.
        conn.backup(dest)
    finally:
        dest.close()


def _copy(conn, target: Path, replace: bool = False, attachments: bool = True, steps: bool = True) -> Path:
    """Writes a checked copy to target. An existing backup is never replaced, except today's automatic copy (replace=True).
    steps=False copies in one go, as a restore's safety copy does while nobody else may save."""
    target.parent.mkdir(parents=True, exist_ok=True)
    claimed = _is_claimed_name(target)  # an empty file _unique made to hold the name for this copy
    if target.exists() and not replace and not claimed:
        raise POError(f"A backup named {target.name} is already there, so nothing was replaced.")
    temp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.partial")
    done = False
    try:
        if steps:
            _copy_in_steps(conn, temp)
        else:
            dest = sqlite3.connect(str(temp))
            try:
                conn.backup(dest)
            finally:
                dest.close()
        problems = _integrity(temp)
        if problems:
            raise POError("The backup failed its integrity check, so it was not kept: " + "; ".join(problems))
        os.replace(temp, target)
        done = True
    finally:
        temp.unlink(missing_ok=True)
        if not done and claimed and _is_claimed_name(target):
            target.unlink(missing_ok=True)  # no copy was made: give the name back
    if attachments:
        _save_attachments(target)
    return target


def _is_claimed_name(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size == 0
    except OSError:
        return False


def _save_attachments(backup_path: Path) -> Path | None:
    """Zips the attachments folder beside the backup. Attached files are never deleted by the program, so a refreshed zip keeps every file it already had."""
    folder = _attachments_folder()
    files = sorted(p for p in folder.iterdir() if p.is_file()) if folder.is_dir() else []
    target = attachments_zip(backup_path)
    kept: dict[str, int] = {}
    if target.is_file():
        try:
            with zipfile.ZipFile(target) as z:
                kept = {i.filename: i.file_size for i in z.infolist()}
        except (OSError, zipfile.BadZipFile):
            kept = {}
    if not files:
        return target if kept else None
    if all(kept.get(p.name) == p.stat().st_size for p in files):
        return target
    names = {p.name for p in files}
    partial = target.with_name(f"{target.name}.{uuid.uuid4().hex}.partial")
    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_STORED, allowZip64=True) as out:
            for p in files:
                out.write(p, p.name)
            if kept:
                with zipfile.ZipFile(target) as old:
                    for name in kept:
                        if name not in names:
                            out.writestr(old.getinfo(name), old.read(name))
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def _restore_attachments(source: Path) -> int:
    """Puts back any attached file the backup's zip holds that the attachments folder is missing. Files already there are left alone."""
    zipped = attachments_zip(source)
    if not zipped.is_file():
        return 0
    folder = _attachments_folder()
    folder.mkdir(parents=True, exist_ok=True)
    added = 0
    try:
        with zipfile.ZipFile(zipped) as z:
            for info in z.infolist():
                name = info.filename
                if info.is_dir() or not name or name in (".", "..") or name != Path(name).name or "/" in name or "\\" in name:
                    continue
                dest = folder / name
                if dest.exists():
                    continue
                part = folder.parent / f"attachment-{uuid.uuid4().hex}.partial"
                try:
                    with z.open(info) as src, open(part, "wb") as out:
                        shutil.copyfileobj(src, out)
                    os.replace(part, dest)
                    added += 1
                finally:
                    part.unlink(missing_ok=True)
    except (OSError, zipfile.BadZipFile):
        pass  # The database is already restored. The health check lists any attached file still missing.
    return added


def _integrity(path: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        result = [r[0] for r in conn.execute("PRAGMA integrity_check")]
        problems = [] if result == ["ok"] else result
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            problems.append(f"{len(fk)} broken links between records")
        return problems
    except sqlite3.DatabaseError as exc:
        return [str(exc)]
    finally:
        conn.close()


_expected_triggers: dict[int, set[str]] = {}


def _fresh_triggers() -> set[str]:
    """The guards a brand-new database of this version has, made in memory."""
    if schema.SCHEMA_VERSION not in _expected_triggers:
        mem = db.connect(":memory:")
        try:
            db.initialize(mem)
            _expected_triggers[schema.SCHEMA_VERSION] = {r[0] for r in mem.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        finally:
            mem.close()
    return _expected_triggers[schema.SCHEMA_VERSION]


def _sha(data) -> str:
    return hashlib.sha256(bytes(data or b"")).hexdigest()


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# Lines that point at a PO line by number, without a database link, because they keep their own copy of it. A minus number is a line the branch added.
# Lines on a cancelled memo, stub or pass may point at a PO line removed after the cancel, and a count line with nothing counted proves nothing, so those are skipped.
_PO_LINE_REFS = (
    ("branch_count_items", "pieces_m > 0", "branch count lines"),
    ("branch_count_damage", "1", "damage lines on branch counts"),
    ("branch_received", "pieces_m > 0 OR damaged_m > 0", "received lines"),
    ("excess_returns", "1", "returned excess lines"),
    ("debit_memo_items", "dm_id IN (SELECT id FROM debit_memos WHERE status <> 'CANCELLED')", "debit memo lines"),
    ("claim_stub_items", "cs_id IN (SELECT id FROM claim_stubs WHERE status <> 'CANCELLED')", "claim stub lines"),
    ("item_pass_items", "ip_id IN (SELECT id FROM item_passes WHERE status <> 'CANCELLED')", "item pass lines"),
)


def _orphans(conn) -> list[str]:
    problems = []
    for table, when, label in _PO_LINE_REFS:
        n = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE ({when}) AND ("
            " (po_item_id > 0 AND po_item_id NOT IN (SELECT id FROM purchase_order_items))"
            " OR (po_item_id < 0 AND -po_item_id NOT IN (SELECT id FROM branch_extra_items)) OR po_item_id = 0)").fetchone()[0]
        if n:
            problems.append(f"{n} {label} point to a PO line that no longer exists")
    for table, label in (("forfeit_prices", "selling prices for forfeited goods"), ("goods_arrivals", "warehouse counts of goods coming back")):
        n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE (kind = 'CS' AND item_id NOT IN (SELECT id FROM claim_stub_items))"
                         " OR (kind = 'IP' AND item_id NOT IN (SELECT id FROM item_pass_items))").fetchone()[0]
        if n:
            problems.append(f"{n} {label} point to a claim stub or item pass line that no longer exists")
    n = conn.execute("SELECT COUNT(*) FROM payment_signoffs WHERE (kind = 'DIFFERENCE' AND ref_id NOT IN (SELECT id FROM order_forms))"
                     " OR (kind = 'CHECK' AND ref_id NOT IN (SELECT id FROM payment_checks))").fetchone()[0]
    if n:
        problems.append(f"{n} payment sign-offs point to an OF or check that no longer exists")
    n = conn.execute("SELECT COUNT(*) FROM late_damage WHERE paid_before IS NOT NULL AND paid_before NOT IN (SELECT id FROM payment_checks)").fetchone()[0]
    if n:
        problems.append(f"{n} claim stubs for damage found later point to a check that no longer exists")
    return problems


# (what it's called, table, column prefix, the empty-PDF test)
_DOCUMENTS = (("debit memo", "debit_memos", "dm", "length(pdf) = 0"),
              ("claim stub", "claim_stubs", "cs", "length(pdf) = 0"),
              ("item pass", "item_passes", "ip", "pdf IS NULL OR length(pdf) = 0"))


def _documents(conn) -> list[str]:
    problems = []
    for what, table, p, empty in _DOCUMENTS:
        # Each year's counter is read with its year, not one lookup per year (AUD-149).
        for n in conn.execute(f"SELECT d.year, d.top, s.last_number FROM (SELECT {p}_year AS year, MAX({p}_seq) AS top FROM {table} GROUP BY {p}_year) d"
                              f" LEFT JOIN {p}_number_sequences s ON s.year = d.year ORDER BY d.year").fetchall():
            if n["last_number"] is None or n["last_number"] < n["top"]:
                problems.append(f"{n['year']}: the {what} number counter is behind the highest {what} number")
        for row in conn.execute(f"SELECT {p}_number AS number, ({empty}) AS empty, pdf, sha256 FROM {table} ORDER BY id").fetchall():
            if row["empty"]:
                problems.append(f"{row['number']}: the {what} has no kept PDF")
            elif _sha(row["pdf"]) != (row["sha256"] or ""):
                problems.append(f"{row['number']}: the kept {what} PDF has changed since it was made")
    for what, table, items, key in (("debit memo", "debit_memos", "debit_memo_items", "dm_id"), ("claim stub", "claim_stubs", "claim_stub_items", "cs_id")):
        number = "dm_number" if table == "debit_memos" else "cs_number"
        for row in conn.execute(f"SELECT d.{number} AS number FROM {table} d"
                                f" WHERE d.total_c <> COALESCE((SELECT SUM(i.amount_c) FROM {items} i WHERE i.{key} = d.id), 0)").fetchall():
            problems.append(f"{row['number']}: the {what} total differs from its lines")
    for row in conn.execute("SELECT v.revision, v.pdf, v.sha256, p.po_number, p.id FROM po_issued_versions v JOIN purchase_orders p ON p.id = v.po_id ORDER BY v.id").fetchall():
        if _sha(row["pdf"]) != row["sha256"]:
            problems.append(f"{row['po_number'] or row['id']} revision {row['revision']}: the issued PO PDF has changed since it was issued")
    return problems


def _attachments(conn) -> list[str]:
    problems, known = [], set()
    folder = _attachments_folder()
    for row in conn.execute("SELECT a.stored_name, a.file_name, a.sha256, p.po_number, p.id AS po_id FROM po_attachments a"
                            " LEFT JOIN purchase_orders p ON p.id = a.po_id ORDER BY a.id").fetchall():
        known.add(row["stored_name"])
        path = folder / row["stored_name"]
        where = row["po_number"] or f"PO {row['po_id']}"
        if not path.is_file():
            problems.append(f"{where}: the attached file '{row['file_name']}' is missing from the attachments folder")
        elif _sha_file(path) != row["sha256"]:
            problems.append(f"{where}: the attached file '{row['file_name']}' has changed since it was attached")
    if folder.is_dir():
        extra = sorted(p.name for p in folder.iterdir() if p.is_file() and p.name not in known)
        if extra:
            problems.append(f"{len(extra)} files in the attachments folder belong to no PO: " + ", ".join(extra[:5]) + (" ..." if len(extra) > 5 else ""))
    return problems


def integrity_report(conn) -> list[str]:
    """Empty list means healthy. Also checks every PO's stored total against its own rows."""
    problems = [r[0] for r in conn.execute("PRAGMA integrity_check") if r[0] != "ok"]
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        problems.append(f"{len(fk)} broken links between records")
    if conn.execute("SELECT COUNT(*) FROM po_write_guard").fetchone()[0]:
        problems.append("The locks on issued OFs and POs are switched off: a leftover entry in po_write_guard lets anyone change them")
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    missing = sorted(_fresh_triggers() - present)
    if missing:
        problems.append(f"{len(missing)} of the guards that keep records permanent are gone: " + ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else ""))
    # Every PO's total worked out again from its rows, the way orders.get_po does it, with all the rows read in one statement (AUD-149).
    from .orders import _totals
    lines: dict[int, list[dict]] = {}
    for item in conn.execute("SELECT i.* FROM purchase_order_items i JOIN departments d ON d.id = i.department_id ORDER BY i.po_id, i.line_no"):
        lines.setdefault(item["po_id"], []).append(dict(item))
    for row in conn.execute("SELECT * FROM purchase_orders").fetchall():
        calc = _totals(dict(row), lines.get(row["id"], [])).grand_total
        if calc != row["grand_total_c"]:
            problems.append(f"{row['po_number'] or row['id']}: stored total differs from its rows")
    numbers = conn.execute("SELECT d.po_year, d.branch_code, d.top, s.last_number FROM (SELECT po_year, branch_code, MAX(po_seq) AS top FROM purchase_orders"
                           " WHERE of_id IS NOT NULL GROUP BY po_year, branch_code) d"
                           " LEFT JOIN po_number_sequences s ON s.year = d.po_year AND s.branch_code = d.branch_code ORDER BY d.po_year, d.branch_code").fetchall()
    for n in numbers:
        if n["last_number"] is None or n["last_number"] < n["top"]:
            problems.append(f"{n['po_year']} branch {n['branch_code']}: the PO number counter is behind the highest PO number")
    for n in conn.execute("SELECT d.of_year, d.top, s.last_number FROM (SELECT of_year, MAX(of_seq) AS top FROM order_forms WHERE of_year IS NOT NULL GROUP BY of_year) d"
                          " LEFT JOIN of_number_sequences s ON s.year = d.of_year ORDER BY d.of_year").fetchall():
        if n["last_number"] is None or n["last_number"] < n["top"]:
            problems.append(f"{n['of_year']}: the OF number counter is behind the highest OF number")
    for row in conn.execute(
            "SELECT i.id, f.of_number, f.id AS of_id, i.description FROM order_form_items i JOIN order_forms f ON f.id = i.of_id"
            " WHERE i.total_qty_m <> COALESCE((SELECT SUM(a.qty_m) FROM order_form_allocations a WHERE a.of_item_id = i.id), 0)").fetchall():
        problems.append(f"{row['of_number'] or row['of_id']}: '{row['description']}' total differs from its branch allocations")
    for row in conn.execute(
            "SELECT f.id, f.of_number, f.grand_total_c, (SELECT COALESCE(SUM(p.grand_total_c),0) FROM purchase_orders p"
            " WHERE p.of_id = f.id AND p.status <> 'CANCELLED') AS pos FROM order_forms f"
            " WHERE f.pos_generated_at IS NOT NULL AND f.status <> 'CANCELLED'").fetchall():
        if row["grand_total_c"] != row["pos"]:
            problems.append(f"{row['of_number']}: the OF total differs from the sum of its branch POs")
    problems += _documents(conn)
    problems += _orphans(conn)
    problems += _attachments(conn)
    return problems


# Files a copy or restore leaves behind if the program is closed part-way. Older than this, nothing is still writing them.
_LEFTOVER_AGE = 3600


def _sweep_leftovers(*folders: Path) -> None:
    """Removes half-written copies left by a backup or restore that was cut off. Recent ones are left: another computer may be writing them."""
    cutoff = time.time() - _LEFTOVER_AGE
    for folder in folders:
        try:
            found = [p for pattern in ("*.partial", "*.restoring-*.sqlite", "*.restoring-*.sqlite-journal") for p in folder.glob(pattern)]
        except OSError:
            continue
        for p in found:
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass


def manual_backup(conn, user: User, target: str | Path | None = None) -> Path:
    require(user, "backup.manage")
    path = Path(target) if target else _unique(backup_folder(conn), "po-manual")
    _copy(conn, path)
    with tx(conn):
        audit(conn, user, "BACKUP", "system", summary=f"Manual backup to {path}")
    return path


def _written_lately(path: Path) -> bool:
    try:
        return abs(time.time() - path.stat().st_mtime) < _AUTO_FRESH_SECONDS
    except OSError:
        return False


def auto_backup(conn, at_start: bool = False) -> Path | None:
    """One a day, made at start-up and brought up to date at logout and exit, so it holds the day's work.
    Older automatic copies are removed after the keep period; manual ones never are.
    at_start: with several PCs logging in one after another, a copy another PC made in the last few minutes is not made again."""
    folder = backup_folder(conn)
    _sweep_leftovers(folder)
    day = now().date()
    target = folder / f"po-auto-{day.isoformat()}.sqlite"
    if at_start and target.is_file() and _written_lately(target):
        return target
    _copy(conn, target, replace=True)
    keep = int(get_setting(conn, "backup.keep_days") or 30)
    cutoff = day - timedelta(days=keep)
    for old in folder.glob("po-auto-*.sqlite"):
        m = AUTO_PATTERN.search(old.name)
        if m and m.group(1) < cutoff.isoformat():
            old.unlink(missing_ok=True)
            attachments_zip(old).unlink(missing_ok=True)
    return target


def _is_live(path: Path) -> bool:
    live = db.db_path()
    try:
        return live.exists() and path.exists() and os.path.samefile(path, live)
    except OSError:
        return False


def list_backups(conn) -> list[dict]:
    out = []
    for path in sorted((p for p in backup_folder(conn).glob("po-*.sqlite") if not _is_claimed_name(p)), key=lambda p: p.stat().st_mtime, reverse=True):
        if _is_live(path):
            continue
        out.append({"path": path, "name": path.name, "size": path.stat().st_size, "modified": path.stat().st_mtime})
    return out


class _Busy(Exception):
    pass


def _swap_step(remaining: int, total: int) -> None:
    """Called after each step of the copy into the live database. Tests replace it to break a restore part-way."""


def _before_swap() -> None:
    """Called after the safety copy, just before the swap. Tests replace it to save from another connection at that moment."""


def _swap_in(conn, temp: Path) -> None:
    """Copies the checked file into the live database through SQLite. The copy is journalled, so it either all lands or the old data stays."""
    def progress(status, remaining, total):
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise _Busy()
        _swap_step(remaining, total)
    src = sqlite3.connect(str(temp))
    try:
        src.backup(conn, pages=_SWAP_PAGES, progress=progress)
    except _Busy:
        raise POError(BUSY) from None
    finally:
        src.close()


def restore(conn, user: User, source: str | Path, live_path: str | Path) -> Path:
    """Checks the file, brings a copy of it up to date beside the live database, saves the current database, then swaps the copy in.

    On success `conn` is closed: the caller reopens. On any failure nothing is changed and `conn` stays open. Returns the safety copy's path.
    """
    require(user, "backup.manage")
    source, live_path = Path(source), Path(live_path)
    if not source.is_file():
        raise POError("That backup file can't be found.")
    try:
        same = live_path.exists() and os.path.samefile(source, live_path)
    except OSError:
        same = False
    if same:
        raise POError("That is the database in use. Choose a backup file.")
    problems = _integrity(source)
    if problems:
        raise POError("That file failed its integrity check: " + "; ".join(problems))
    probe = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        version = probe.execute("PRAGMA user_version").fetchone()[0]
        has_po = probe.execute("SELECT 1 FROM sqlite_master WHERE name='purchase_orders'").fetchone()
        from_sample = probe.execute("SELECT 1 FROM system_settings WHERE key = 'sample.data' AND value = '1'").fetchone() if has_po else None
    finally:
        probe.close()
    if from_sample and get_setting(conn, "sample.data") != "1":
        raise POError("That file is practice data from the sample program. It can't replace real records.")
    if not has_po or not 1 <= version <= schema.SCHEMA_VERSION:
        raise POError("That file isn't a backup from this version of the PO system.")
    who, name = user.display, source.name
    _sweep_leftovers(live_path.parent, backup_folder(conn))
    temp = live_path.with_name(f"{live_path.stem}.restoring-{uuid.uuid4().hex}.sqlite")
    safety = None
    try:
        # 1. A copy of the backup, brought up to date, recorded and checked, before anything live is touched.
        probe = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        dest = sqlite3.connect(str(temp))
        try:
            probe.backup(dest)
        finally:
            dest.close()
            probe.close()
        try:
            upgrading = db.connect(temp)
            try:
                db.initialize(upgrading)
            finally:
                upgrading.close()
        except Exception as exc:
            why = f" ({exc})" if isinstance(exc, POError) else ""
            raise POError(f"That backup can't be brought up to date for this version of the program, so nothing was changed.{why}") from None
        # The restore is recorded in the restored file's own audit log, so it arrives with the data in one step.
        safety_path = _unique(backup_folder(conn), "po-before-restore")
        log = db.connect(temp)
        try:
            with tx(log):
                log.execute(
                    "INSERT INTO audit_logs(occurred_at, user_id, user_name, action, entity_type, summary) VALUES (?,?,?,?,?,?)",
                    (now().isoformat(sep=" ", timespec="seconds"), None, who, "RESTORE", "system",
                     f"Restored from {name}. Safety copy: {safety_path.name}"))
        finally:
            log.close()
        problems = _integrity(temp)
        if problems:
            raise POError("That backup failed its check after being brought up to date, so nothing was changed: " + "; ".join(problems))
        # 2. Nobody else may be saving: someone part-way through a save would hold the lock. Then the safety copy.
        #    PRAGMA data_version changes when another connection saves, so a save that lands while the safety copy
        #    is made is caught and the copy taken again: no save is ever missing from both the live file and the copy.
        conn.execute("PRAGMA busy_timeout = 2000")
        try:
            for attempt in range(3):
                try:
                    conn.execute("BEGIN EXCLUSIVE")
                except sqlite3.OperationalError:
                    raise POError(BUSY) from None
                conn.execute("COMMIT")
                seen = conn.execute("PRAGMA data_version").fetchone()[0]
                safety = _copy(conn, safety_path, attachments=False, steps=False)
                _before_swap()
                if conn.execute("PRAGMA data_version").fetchone()[0] == seen:
                    break
                safety_path.unlink(missing_ok=True)
                safety = None
            if safety is None:
                raise POError(BUSY)
            # 3. The swap.
            _swap_in(conn, temp)
        finally:
            try:
                conn.execute(f"PRAGMA busy_timeout = {int(db.BUSY_TIMEOUT_MS)}")
            except sqlite3.Error:
                pass
    finally:
        for leftover in (temp, temp.with_name(temp.name + "-journal")):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass
    # The attached files on disk are not touched by the swap, so the safety copy's zip is made now, before any come back from the backup.
    try:
        _save_attachments(safety)
    except Exception:
        pass  # The safety copy of the database is kept either way; the files themselves are still in the attachments folder.
    _restore_attachments(source)
    conn.close()
    return safety
