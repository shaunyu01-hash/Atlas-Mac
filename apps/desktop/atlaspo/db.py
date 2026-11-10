"""Connection, transactions, data folder, and the helpers every service uses."""
from __future__ import annotations

import json
import os
import platform
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from . import schema


class POError(Exception):
    """A rule stopped the operation. The message is written for the person using the app."""


class PermissionDenied(POError):
    pass


class DatabaseBusy(POError, sqlite3.OperationalError):
    """Another computer held the file for longer than the wait. A rule-style message, and still the database error it was."""


class DataFolderUnreachable(POError, OSError):
    """The data folder (a shared network folder, for several PCs) can't be reached. Still an OSError, so code that skips a folder it
    can't write to, such as the error log, keeps doing so."""


# How long a save waits for another computer to finish its own, in milliseconds. Tests make it short.
BUSY_TIMEOUT_MS = 15000
# A staff PC can be pointed at the shared data folder by a text file beside the built program holding the folder, such as \\server\ATLASPO.
POINTER_FILE = "data-folder.txt"


def now() -> datetime:
    """The clock. Tests replace this to check year rollover."""
    return datetime.now()


def timestamp() -> str:
    return now().isoformat(sep=" ", timespec="seconds")


def today() -> date:
    return now().date()


def pointer_folder() -> Path | None:
    """The folder named in data-folder.txt beside the built program, or None. Only the built program (.exe) reads it, never a test or
    a run from the source, so nothing but PO_DATA_DIR can point those anywhere."""
    if not getattr(sys, "frozen", False):
        return None
    try:
        text = (Path(sys.executable).resolve().parent / POINTER_FILE).read_text(encoding="utf-8-sig")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip().strip('"').strip()
        if line and not line.startswith("#"):
            return Path(os.path.expandvars(line))
    return None


def _unreachable(path: Path, shared: bool) -> DataFolderUnreachable:
    what = "The shared data folder" if shared else "The data folder"
    return DataFolderUnreachable(f"{what} {path} can't be reached. Check the network and try again.")


def data_dir() -> Path:
    """PO_DATA_DIR if set, then the folder in data-folder.txt beside the built program, otherwise Documents\\Atlas PO ERP.
    A shared folder that can't be reached is refused, never made, and never swapped for this PC's own folder."""
    env = os.environ.get("PO_DATA_DIR")
    pointer = None if env else pointer_folder()
    if pointer is not None:
        if not pointer.is_dir():
            raise _unreachable(pointer, True)
        return pointer
    path = Path(env) if env else Path.home() / "Documents" / "Atlas PO ERP"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise _unreachable(path, str(path).startswith(("\\\\", "//"))) from None
    return path


def db_path() -> Path:
    return data_dir() / "po.sqlite"


BUSY_MESSAGE = "Another computer is saving right now. Try again in a moment."
JOURNAL_MESSAGE = ("The data file is open in a way that isn't safe in a shared folder, perhaps by another program. "
                   "Close the program on every computer, then open it again.")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    target = str(path or db_path())
    conn = sqlite3.connect(target, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_MS)}")
        # Rollback journal, never WAL: WAL is not safe on a network share, and the file stays one file for backup.
        # SQLite answers with the mode it is really in. Anything else (a WAL file held open elsewhere) is refused, not used.
        mode = str(conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0]).lower()
        if mode != "delete" and not (mode == "memory" and target == ":memory:"):
            raise POError(JOURNAL_MESSAGE)
        conn.execute("PRAGMA synchronous = FULL")
        # REPLACE deletes the row it replaces, and only fires the "stays on record" delete triggers with this on, so nothing permanent can be swapped out that way.
        conn.execute("PRAGMA recursive_triggers = ON")
    except sqlite3.OperationalError as e:
        conn.close()
        if _is_busy(e):
            raise DatabaseBusy(BUSY_MESSAGE) from e
        raise
    except BaseException:
        conn.close()
        raise
    return conn


def _is_busy(error: sqlite3.OperationalError) -> bool:
    text = str(error).lower()
    return "locked" in text or "busy" in text


def _begin(conn: sqlite3.Connection) -> None:
    """Takes the write lock. Another PC holding it longer than the wait is the plain busy message."""
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as e:
        if _is_busy(e):
            raise DatabaseBusy(BUSY_MESSAGE) from e
        raise


@contextmanager
def tx(conn: sqlite3.Connection):
    """Write transaction. Takes the write lock first, so two users can never read the same next PO number."""
    if conn.in_transaction:
        yield conn
        return
    # Another PC holding the file for longer than the wait stops here. Nothing was started, so nothing needs undoing.
    _begin(conn)
    try:
        yield conn
        # Inside the try: if the commit itself fails, for example because another PC holds the file, everything rolls back.
        # Otherwise the transaction stays open, every later save quietly joins it, and none of them is ever written.
        try:
            conn.execute("COMMIT")
        except sqlite3.OperationalError as e:
            if _is_busy(e):
                raise DatabaseBusy(BUSY_MESSAGE) from e
            raise
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def initialize(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > schema.SCHEMA_VERSION:
        raise POError("This database was made by a newer version of the program. Update the program first.")
    if version == schema.SCHEMA_VERSION:
        return
    if version == 0 and conn.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1").fetchone():
        # A file with tables but no version is some other program's database. Starting a new one inside it would fail halfway.
        raise POError("This file is not a PO database.")
    backup = None if version == 0 else _backup_before_migration(conn, version)
    if backup is not None:
        _refuse_clashing_tables(conn, version)
    _upgrade(conn, version)
    if backup is not None:
        clash = _shape_problem(conn)
        if clash:
            # Put the whole file back as it was before the update started, from the copy taken a moment ago.
            _restore(conn, backup)
            raise POError(f"This database could not be updated: the {clash} table is not what this program expects. Nothing was changed.")
    _optimize(conn)


def _statements(script: str) -> list[str]:
    """A script as single statements, in order. SQLite itself says where each one ends, so a trigger's inner ';' or one in a quote doesn't split it."""
    out, buf = [], ""
    parts = script.split(";")
    for part in parts[:-1]:
        buf += part + ";"
        if sqlite3.complete_statement(buf):
            if buf.strip(" \t\r\n;"):
                out.append(buf)
            buf = ""
    if (buf + parts[-1]).strip():
        out.append(buf + parts[-1])
    return out


def _run(conn: sqlite3.Connection, script: str) -> None:
    """Runs a script inside the transaction already open. executescript would commit it first and let another PC in between."""
    for statement in _statements(script):
        conn.execute(statement)


def _create(conn: sqlite3.Connection) -> None:
    """A brand-new database, in one locked transaction. Another PC that made it a moment earlier is left alone."""
    _begin(conn)
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] != 0 or conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1").fetchone():
            conn.execute("ROLLBACK")
            return
        _run(conn, schema.DDL)
        stamp = timestamp()
        conn.executemany("INSERT INTO departments(id, name) VALUES (?, ?)", schema.DEPARTMENTS)
        conn.executemany(
            "INSERT INTO destinations(code, name, legacy_outlet) VALUES (?, ?, ?)", schema.DESTINATIONS
        )
        conn.executemany(
            "INSERT INTO system_settings(key, value, updated_at) VALUES (?, ?, ?)",
            [(k, v, stamp) for k, v in schema.DEFAULT_SETTINGS.items()],
        )
        conn.execute("PRAGMA user_version = 1")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _missing_columns(conn: sqlite3.Connection, columns: dict) -> str:
    """The ADD COLUMN lines for the columns a table doesn't have yet. Keys are (table, column), or just the column for suppliers."""
    have: dict[str, set[str]] = {}
    out = ""
    for key, sql in columns.items():
        table, column = key if isinstance(key, tuple) else ("suppliers", key)
        if table not in have:
            have[table] = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        if column not in have[table]:
            out += sql
    return out


def _upgrade(conn: sqlite3.Connection, version: int) -> None:
    """Brings a database at `version` up to schema.SCHEMA_VERSION, one migration at a time.
    Each step reads the version again once it holds the write lock, so when several PCs start the new program at once, one updates
    the file and the others find it done."""
    if version == 0:
        _create(conn)
    if version < 2:
        _migrate(conn, schema.MIGRATE_V2)
    if version < 3:
        _migrate(conn, schema.MIGRATE_V3)
    if version < 4:
        _migrate(conn, schema.MIGRATE_V4)
    if version < 5:
        _migrate(conn, schema.MIGRATE_V5)
    if version < 6:
        _migrate(conn, schema.MIGRATE_V6)
    if version < 7:
        _migrate(conn, schema.MIGRATE_V7)
    if version < 8:
        _migrate(conn, schema.MIGRATE_V8)
    if version < 9:
        _migrate(conn, schema.MIGRATE_V9)
    if version < 10:
        _migrate(conn, schema.MIGRATE_V10)
    if version < 11:
        _migrate(conn, schema.MIGRATE_V11)
    if version < 12:
        _migrate(conn, schema.MIGRATE_V12)
    if version < 13:
        _migrate(conn, schema.MIGRATE_V13)
    if version < 14:
        _migrate(conn, schema.MIGRATE_V14)
    if version < 15:
        _migrate(conn, schema.MIGRATE_V15)
    if version < 16:
        _migrate(conn, schema.MIGRATE_V16)
    if version < 17:
        _migrate(conn, schema.MIGRATE_V17, lambda c: _missing_columns(c, schema.SUPPLIER_TERM_COLUMNS))
    if version < 18:
        _migrate(conn, schema.MIGRATE_V18)
    if version < 19:
        _migrate(conn, schema.MIGRATE_V19)
    if version < 20:
        _migrate(conn, schema.MIGRATE_V20)
    if version < 21:
        _migrate(conn, schema.MIGRATE_V21)
    if version < 22:
        _migrate(conn, schema.MIGRATE_V22)
    if version < 23:
        _migrate(conn, schema.MIGRATE_V23)
    if version < 24:
        _migrate(conn, schema.MIGRATE_V24)
    if version < 25:
        _migrate(conn, schema.MIGRATE_V25)
    if version < 26:
        _migrate(conn, schema.MIGRATE_V26)
    if version < 27:
        _migrate(conn, schema.MIGRATE_V27)
    if version < 28:
        _migrate(conn, schema.MIGRATE_V28, lambda c: _missing_columns(c, schema.V28_COLUMNS))


_fresh_columns: dict[int, dict[str, list[str]]] = {}


def _expected_columns() -> dict[str, list[str]]:
    """Each table's columns in a brand-new database of this version, made in memory."""
    if schema.SCHEMA_VERSION not in _fresh_columns:
        mem = connect(":memory:")
        try:
            _upgrade(mem, 0)
            _fresh_columns[schema.SCHEMA_VERSION] = _columns(mem)
        finally:
            mem.close()
    return _fresh_columns[schema.SCHEMA_VERSION]


def _columns(conn: sqlite3.Connection) -> dict[str, list[str]]:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    return {t: [r[1] for r in conn.execute(f'PRAGMA table_xinfo("{t}")')] for t in tables}


def _shape_problem(conn: sqlite3.Connection) -> str:
    """The first table that lacks a column a new database has, or nothing. Extra tables and columns are fine."""
    have = _columns(conn)
    for table, columns in _expected_columns().items():
        if table not in have or not set(columns) <= set(have[table]):
            return table
    return ""


def _refuse_clashing_tables(conn: sqlite3.Connection, version: int) -> None:
    """A migration makes its new tables only if they don't exist yet. One already there with other columns, from a hand-edited file,
    would be kept silently, so it is refused before anything changes."""
    have = _columns(conn)
    mem = sqlite3.connect(":memory:")
    try:
        for n in range(version + 1, schema.SCHEMA_VERSION + 1):
            script = getattr(schema, f"MIGRATE_V{n}", "")
            for found in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \(.*?\n\);", script, re.S):
                name = found.group(1)
                if name not in have:
                    continue
                mem.execute(f"DROP TABLE IF EXISTS {name}")
                mem.execute(found.group(0))
                wanted = {r[1] for r in mem.execute(f"PRAGMA table_xinfo({name})")}
                if not wanted <= set(have[name]):
                    raise POError(f"This database could not be updated: the {name} table is not what this program expects. Nothing was changed.")
    finally:
        mem.close()


def _restore(conn: sqlite3.Connection, backup: Path) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")
    source = sqlite3.connect(str(backup))
    try:
        source.backup(conn)
    finally:
        source.close()


def _optimize(conn: sqlite3.Connection) -> None:
    """Lets SQLite keep the figures it uses to pick the fastest way through a query. Never stops anything if it can't run,
    and never waits: if another computer is saving, it is simply skipped this time."""
    try:
        conn.execute("PRAGMA busy_timeout = 0")
        conn.execute("PRAGMA analysis_limit = 1000")
        conn.execute("PRAGMA optimize = 0x10002")
    except sqlite3.DatabaseError:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
    finally:
        try:
            conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_MS)}")
        except sqlite3.DatabaseError:
            pass


def _backup_before_migration(conn: sqlite3.Connection, version: int) -> Path:
    """A database with real data never changes shape without a copy of how it was. No copy, no migration."""
    custom = conn.execute("SELECT value FROM system_settings WHERE key = 'backup.folder'").fetchone()
    target = None
    for folder in ([Path(custom["value"])] if custom and custom["value"] else []) + [data_dir() / "backups"]:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target = _unique_backup_name(folder, f"po-before-migration-v{version}")
            break
        except OSError:
            continue
    if target is None:
        raise POError("The database needs updating, but no backup folder could be written, so nothing was changed.")
    copy = sqlite3.connect(str(target))
    try:
        conn.backup(copy)
    finally:
        copy.close()
    return target


def _computer() -> str:
    name = os.environ.get("COMPUTERNAME") or platform.node() or "pc"
    return re.sub(r'[<>:"/\\|?*\x00-\x1f\s]', "-", name).strip(" .-") or "pc"


def _unique_backup_name(folder: Path, stem: str) -> Path:
    """A name no other copy has, even when two PCs make one in the same second: the time to the millisecond, the computer, and a counter."""
    t = now()
    base = f"{stem}-{t:%Y-%m-%d-%H%M%S}-{t.microsecond // 1000:03d}-{_computer()}"
    path, n = folder / f"{base}.sqlite", 2
    while True:
        try:  # claim the name: two copies started in the same millisecond can't both get it
            os.close(os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return path
        except FileExistsError:
            path, n = folder / f"{base}-{n}.sqlite", n + 1


def _target_version(script: str) -> int | None:
    """The version a migration script brings the file to, or None if it doesn't say (then it always runs)."""
    found = re.findall(r"PRAGMA user_version = (\d+)", script)
    return int(found[-1]) if found else None


def _migrate(conn: sqlite3.Connection, script: str, extra=None) -> None:
    """One transaction: it either all applies, or the database stays exactly as it was. Foreign keys are off
    while tables are rebuilt, as SQLite requires, and every link is checked again before committing.
    The version is read again once the write lock is held: if another PC already made this step, it is skipped.
    extra(conn), if given, adds statements worked out inside the lock, such as columns only some files lack."""
    target = _target_version(script)
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        _begin(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if target is not None and version >= target:
            conn.execute("ROLLBACK")
            return
        if extra is not None:
            script = extra(conn) + script
        if "RENAME TO" in script:
            # A table rebuild. SQLite refuses it while a guard elsewhere reads the table, and V28's guards read rebuilt tables such as
            # branch_counts, so they are removed first. Below version 28, V28 makes them again afterwards. A file already at 28 or
            # newer never runs V28 again, so the same transaction puts them back after the rebuild.
            script = "".join(f"DROP TRIGGER IF EXISTS {name};" for name in schema.V28_TRIGGERS) + script
            if version >= 28:
                script += schema.V28_TRIGGER_SQL
        _run(conn, script)
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise POError(f"The database update found {len(broken)} broken links, so nothing was changed.")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def open_database(path: Path | str | None = None) -> sqlite3.Connection:
    """Opens (and if needed updates) the database. Another computer saving at that moment is the plain busy message, never a raw error."""
    if path is None and not os.environ.get("PO_DATA_DIR") and pointer_folder() is not None and not db_path().exists():
        # A PC pointed at the shared folder never starts an empty database of its own there.
        raise POError(f"There is no PO data in the shared folder {data_dir()}. Check data-folder.txt beside the program.")
    conn = connect(path)
    try:
        initialize(conn)
        _optimize(conn)
    except sqlite3.OperationalError as e:
        conn.close()
        if _is_busy(e) and not isinstance(e, DatabaseBusy):
            raise DatabaseBusy(BUSY_MESSAGE) from e
        raise
    except BaseException:
        conn.close()
        raise
    return conn


def audit(conn, user, action: str, entity_type: str, entity_id: int | None = None, *,
          po_id: int | None = None, of_id: int | None = None, summary: str = "", previous=None, new=None) -> None:
    def encode(v):
        if v is None or isinstance(v, str):
            return v
        return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)

    document = None
    if po_id:
        row = conn.execute("SELECT po_number FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
        document = row["po_number"] if row else None
    elif of_id:
        row = conn.execute("SELECT of_number FROM order_forms WHERE id = ?", (of_id,)).fetchone()
        document = row["of_number"] if row else None
    conn.execute(
        "INSERT INTO audit_logs(occurred_at, user_id, user_name, action, entity_type, entity_id, po_id, of_id,"
        " document_number, summary, previous_value, new_value) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (timestamp(), user.id if user else None, user.display if user else "system",
         action, entity_type, entity_id, po_id, of_id, document, summary, encode(previous), encode(new)),
    )


def get_setting(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM system_settings WHERE key = ?", (key,)).fetchone()
    if row is not None:
        return row["value"]
    return schema.DEFAULT_SETTINGS.get(key, "")


def set_settings(conn, user, values: dict[str, str]) -> None:
    """Change settings. Every real change goes in the audit log, including the approval policy. Only an administrator may, whatever the screen shows."""
    if not user.can("settings.edit"):
        raise PermissionDenied("Only an administrator can change settings.")
    with tx(conn):
        if "approval.approver_user_ids" in values:
            for uid in filter(None, str(values["approval.approver_user_ids"]).split(",")):
                ok = conn.execute("SELECT 1 FROM users u JOIN user_roles r ON r.user_id = u.id WHERE u.id = ? AND u.active = 1 AND r.role IN ('APPROVER', 'ADMIN')",
                                  (int(uid),)).fetchone()
                if not ok:
                    raise POError("An approver must be an active user with the Approver or Administrator role.")
        for key, value in values.items():
            old = get_setting(conn, key)
            value = str(value)
            if old == value:
                continue
            conn.execute(
                "INSERT INTO system_settings(key, value, updated_at, updated_by) VALUES (?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at,"
                " updated_by=excluded.updated_by",
                (key, value, timestamp(), user.id),
            )
            action = "APPROVAL_POLICY_CHANGE" if key.startswith("approval.") else "SETTING_CHANGE"
            audit(conn, user, action, "setting", summary=f"{key}: {old!r} to {value!r}",
                  previous={key: old}, new={key: value})
