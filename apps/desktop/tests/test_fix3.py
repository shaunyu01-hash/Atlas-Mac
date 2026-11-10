"""Third fix round: the items still open after round 2.

AUD-130 A7 (a sent damage report takes no more lines, even by editing the file), AUD-006 (Received columns at 1366), AUD-024 (one word for
the same thing), AUD-117 (item pass lines need a user), AUD-135 (late damage names a real check), AUD-149 (lists without a query per row)"""
from __future__ import annotations

import inspect
import os
import shutil
import sqlite3
import tempfile
from io import BytesIO
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PO_SAMPLE_DIR", tempfile.mkdtemp(prefix="po-sample-"))

import pytest
from pypdf import PdfReader
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from atlaspo import auth, backup, claim_stubs as cs, db, item_passes, order_forms, orders, pdf, receiving, sample_data, schema
from atlaspo.db import PermissionDenied, today, tx
from atlaspo.ui import item_pass_page, payments_page, receiving_page, theme

from conftest import NORTH, PASSWORD, SOUTH
from test_payments import Order

ROOT = Path(__file__).resolve().parents[1]
NEW_TRIGGERS = ("claim_request_items_once_sent", "late_damage_paid_before_known")


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


@pytest.fixture
def app(tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: settings)
    yield QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def sample_file(tmp_path_factory):
    folder = tmp_path_factory.mktemp("fix3-sample")
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


def _receiver(world, branch=NORTH):
    return auth.get_user(world.conn, world.conn.execute("SELECT id FROM users WHERE username = ?", (f"r{branch}x1",)).fetchone()[0])


def _text(data: bytes) -> str:
    return " ".join(" ".join(p.extract_text() for p in PdfReader(BytesIO(data)).pages).split()).lower()


# ------------------------------------------------------------------ AUD-130 A7: a sent damage report takes no more lines

def test_a_sent_damage_report_takes_no_more_lines_even_by_editing_the_file(world):
    o = Order(world)
    po = o.pos[NORTH]
    item = world.conn.execute("SELECT id FROM purchase_order_items WHERE po_id = ?", (po["id"],)).fetchone()[0]
    other = world.conn.execute("SELECT id FROM purchase_order_items WHERE po_id = ?", (o.pos[SOUTH]["id"],)).fetchone()[0]
    request_id = cs.send_report(world.conn, _receiver(world), po["po_number"], {item: "1"}, "Torn seams")[0]
    # The log line names the report itself, and still the PO, so the PO's history shows it as before.
    log = world.conn.execute("SELECT entity_type, entity_id, po_id FROM audit_logs WHERE action = 'CS_REQUEST'").fetchone()
    assert tuple(log) == ("claim_request", request_id, po["id"])
    # Still waiting for approval, but sent: the file refuses another line.
    assert _blocked(world.conn, "INSERT INTO claim_request_items(request_id, po_item_id, pieces_m) VALUES (?, ?, 1000)", (request_id, other))
    assert world.conn.execute("SELECT COUNT(*) FROM claim_request_items WHERE request_id = ?", (request_id,)).fetchone()[0] == 1
    # A report being written takes all its lines at once, as send_report writes them before it logs the report.
    c = world.conn
    c.execute("SAVEPOINT writing")
    try:
        fresh = c.execute("INSERT INTO claim_requests(po_id, reason, status, requested_by, requested_at) VALUES (?, 'x', 'PENDING', ?, '2026-11-07 10:00:00')",
                          (po["id"], _receiver(world).id)).lastrowid
        c.executemany("INSERT INTO claim_request_items(request_id, po_item_id, pieces_m) VALUES (?, ?, 1000)", [(fresh, item), (fresh, other)])
        assert c.execute("SELECT COUNT(*) FROM claim_request_items WHERE request_id = ?", (fresh,)).fetchone()[0] == 2
    finally:
        c.execute("ROLLBACK TO writing")
        c.execute("RELEASE writing")
    # Deciding it still works, and a decided report stays refused too.
    stub = cs.approve_report(world.conn, world.admin, request_id)
    assert stub and _blocked(world.conn, "INSERT INTO claim_request_items(request_id, po_item_id, pieces_m) VALUES (?, ?, 1000)", (request_id, other))
    # A second report on the same PO goes through as normal.
    o2 = Order(world)
    item2 = world.conn.execute("SELECT id FROM purchase_order_items WHERE po_id = ?", (o2.pos[NORTH]["id"],)).fetchone()[0]
    assert cs.send_report(world.conn, _receiver(world), o2.pos[NORTH]["po_number"], {item2: "2"}, "Wet box")


def test_v27_upgrade_keeps_every_row_and_adds_the_round_3_guards(sample_file, env):
    path = env / "po.sqlite"
    shutil.copy(sample_file, path)
    c = db.connect(path)
    c.executescript("BEGIN;" + "".join(f"DROP TRIGGER {name};" for name in schema.V28_TRIGGERS) + "PRAGMA user_version = 27;COMMIT;")
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    # 8 Nov: V28 also fills two new columns from the record (what the program worked out for each check, and the check a memo cancelled
    # after payment is paid back after). They only ever go from empty to a value; every other value stays exactly as it was.
    filled = {"payment_checks": "expected_c", "debit_memos": "paid_back_check_id"}

    def rows(t):
        out = [dict(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY rowid")]
        if t in filled:
            for r in out:
                r.pop(filled[t])
        return out
    before = {t: rows(t) for t in tables if t != "audit_logs"}
    empty = {t: c.execute(f"SELECT COUNT(*) FROM {t} WHERE {col} IS NULL").fetchone()[0] for t, col in filled.items()}
    logs = c.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
    db.initialize(c)
    assert c.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
    assert {t: rows(t) for t in tables if t != "audit_logs"} == before
    assert all(c.execute(f"SELECT COUNT(*) FROM {t} WHERE {col} IS NULL").fetchone()[0] <= empty[t] for t, col in filled.items())
    assert c.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0] == logs + 2  # the MIGRATE_V28 note, and V29's (inventory)
    assert set(NEW_TRIGGERS) <= set(schema.V28_TRIGGERS) <= {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert c.execute("PRAGMA foreign_key_check").fetchall() == [] and c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    c.close()


# ------------------------------------------------------------------ AUD-135: damage found later names a check that is on record

def test_late_damage_must_name_a_check_on_record(sample):
    stub = sample.execute("SELECT id FROM claim_stubs WHERE id NOT IN (SELECT cs_id FROM late_damage) ORDER BY id").fetchone()[0]
    check = sample.execute("SELECT id FROM payment_checks ORDER BY id").fetchone()[0]
    who = sample.execute("SELECT id FROM users ORDER BY id").fetchone()[0]
    sql = "INSERT INTO late_damage(cs_id, reason, found_by, found_at, paid_before) VALUES (?, 'torn', ?, '2026-11-07 10:00:00', ?)"
    missing = sample.execute("SELECT COALESCE(MAX(id), 0) + 1000 FROM payment_checks").fetchone()[0]
    assert _blocked(sample, sql, (stub, who, missing))
    assert not _blocked(sample, sql, (stub, who, check))
    assert not _blocked(sample, sql, (stub, who, None))  # damage found before any check was made
    # The health check never has one to report.
    assert not any("late" in p.lower() for p in backup.integrity_report(sample))


def test_the_fake_old_database_test_still_starts_from_a_version_16_file():
    source = inspect.getsource(__import__("test_payments").test_a_database_from_before_payments_gains_them)
    assert "DROP TRIGGER late_damage_paid_before_known" in source and "PRAGMA user_version = 16" in source


# ------------------------------------------------------------------ AUD-006: Received at 1366

MEASURE = r'''
"""Real Windows platform with its fonts (offscreen has none), off screen, temp folders only."""
import json, os, sys, tempfile
from pathlib import Path
os.environ["PO_DATA_DIR"] = tempfile.mkdtemp(prefix="fix3-data-")
os.environ["PO_SAMPLE_DIR"] = tempfile.mkdtemp(prefix="fix3-sample-")
sys.path.insert(0, sys.argv[1])
from PySide6.QtCore import QRect, QSettings, Qt
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QApplication, QMessageBox
app = QApplication([])
from atlaspo.ui import theme
theme._settings = lambda: QSettings(str(Path(os.environ["PO_DATA_DIR"]) / "ui.ini"), QSettings.Format.IniFormat)
for n in ("information", "warning", "critical", "question"):
    setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
from atlaspo import auth, db, sample_data
from atlaspo.ui import receiving_page
from atlaspo.ui.main_window import MainWindow
assert os.environ["PO_DATA_DIR"] in str(db.data_dir())
conn = db.open_database(Path(os.environ["PO_DATA_DIR"]) / "m.sqlite")
sample_data.build(conn)
theme.apply("light")
pump = lambda: [app.processEvents() for _ in range(4)]
win = MainWindow(conn, auth.login(conn, "owner", "123"))
win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
win.show()
win.resize(1366, 768)
pump()
page = win.received
win.nav.setCurrentRow(next(r for r, p in win.nav_rows.items() if p is page))
pump()
t = page.table
bold = QFont(t.font())
bold.setBold(True)
metrics = QFontMetrics(bold)
head = QFont(t.horizontalHeader().font())
head.setBold(True)
head_metrics = QFontMetrics(head)
# A row with every document kind, the longest the column gets in practice.
t.item(0, 8).setText("2026-DM-00001, 2026-CS-00002, 2026-IP-00003")
receiving_page._fit_rows(t, receiving_page._WRAPPED)
pump()
cut = []
texts = set()
for r in range(t.rowCount()):
    for c in (8, 9):
        text = t.item(r, c).text()
        texts.add((c, text))
        room = t.columnWidth(c) - 16  # 6 px padding a side, and the style's text margin
        need = metrics.boundingRect(QRect(0, 0, room, 10000), int(Qt.TextFlag.TextWordWrap), text)
        if need.width() > room or need.height() + 6 > t.rowHeight(r):
            cut.append([r, c, text, t.columnWidth(c), t.rowHeight(r)])
heads = [[line, head_metrics.horizontalAdvance(line) + 12, t.columnWidth(c)] for c in (8, 9) for line in t.horizontalHeaderItem(c).text().splitlines()]
shown = sum(t.columnWidth(c) for c in range(t.columnCount()) if not t.isColumnHidden(c))
out = {"cut": cut, "heads": heads, "shown": shown, "viewport": t.viewport().width(), "hscroll": t.horizontalScrollBar().maximum(),
       "texts": sorted(x[1] for x in texts if x[0] == 9), "supplier": t.columnWidth(3)}
print("RESULT " + json.dumps(out))
'''


@pytest.fixture(scope="module")
def received_measured(tmp_path_factory):
    import json
    import subprocess
    import sys
    if sys.platform != "win32":
        pytest.skip("measures with the Windows fonts")
    script = tmp_path_factory.mktemp("measure3") / "measure.py"
    script.write_text(MEASURE, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("QT_QPA_PLATFORM", "PO_DATA_DIR", "PO_SAMPLE_DIR")}
    env.update(QT_SCALE_FACTOR="1", QT_ENABLE_HIGHDPI_SCALING="0")
    done = subprocess.run([sys.executable, "-E", str(script), str(ROOT)], capture_output=True, text=True, env=env, timeout=600)
    line = next((l for l in done.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, done.stdout[-2000:] + done.stderr[-2000:]
    return json.loads(line[7:])


def test_received_counting_and_documents_show_whole_at_1366(received_measured):
    m = received_measured
    assert m["cut"] == [], m
    assert any("Counting stopped" in t for t in m["texts"]), m["texts"]  # the longest Counting text is among those checked
    # Each line of the two-line heading fits, and nothing is pushed past the right edge.
    assert all(need <= width for _, need, width in m["heads"]), m["heads"]
    assert m["shown"] <= m["viewport"] and m["hscroll"] == 0, m


def test_the_two_line_heading_keeps_its_column_name_and_choice(app, sample, tmp_path):
    from atlaspo.ui.receiving_page import ReceivedPage
    owner_user = auth.login(sample, "owner", "123")
    # Someone who hid Counting before this version keeps the documents column, which is saved by its one-line name.
    theme._settings().setValue("columns/received-owner", "of-number branch supplier ordered received difference damaged dm-/-cs-/-item-pass status")
    page = ReceivedPage(sample, owner_user)
    chooser = page.table_columns
    assert chooser.columns["dm-/-cs-/-item-pass"] == 8
    assert chooser.checks["dm-/-cs-/-item-pass"].text() == "DM / CS / item pass"
    assert not page.table.isColumnHidden(8) and page.table.isColumnHidden(9)
    assert page.table.horizontalHeaderItem(8).toolTip() == "DM / CS / item pass"
    # Supplier takes the spare room alone; with Supplier hidden, the last column does.
    assert not page.table.horizontalHeader().stretchLastSection()
    chooser.checks["supplier"].setChecked(False)
    assert page.table.horizontalHeader().stretchLastSection()
    page.close()


def test_only_rows_with_long_text_grow_and_they_stay_right_after_sorting(app, sample):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont, QFontMetrics
    from PySide6.QtTest import QTest
    from atlaspo.ui.receiving_page import ReceivedPage, _WRAPPED
    page = ReceivedPage(sample, auth.login(sample, "owner", "123"))
    page.resize(1366, 768)
    page.show()
    QApplication.processEvents()
    t = page.table
    font = QFont(t.font())
    font.setBold(True)
    metrics = QFontMetrics(font)

    def heights_follow_the_text():
        for r in range(t.rowCount()):
            long = any(metrics.horizontalAdvance(t.item(r, c).text()) > t.columnWidth(c) - 16 for c in _WRAPPED)
            want = max(30, t.sizeHintForRow(r)) if long else 30
            assert t.rowHeight(r) == want, (r, [t.item(r, c).text() for c in _WRAPPED], t.rowHeight(r), want)

    heights_follow_the_text()
    t.sortByColumn(9, Qt.SortOrder.DescendingOrder)
    QTest.qWait(20)
    heights_follow_the_text()
    t.setColumnWidth(9, 400)  # wide enough: the rows go back to one line
    heights_follow_the_text()
    page.close()


# ------------------------------------------------------------------ AUD-024: one word for one thing

def test_print_again_collected_and_go_back_are_the_words(app, sample):
    from atlaspo.ui.item_pass_page import ItemPassesPage
    from atlaspo.ui.of_page import OFPage
    from atlaspo.ui.pages import PrintPage
    from atlaspo.ui.po_page import POPage
    owner_user = auth.login(sample, "owner", "123")
    page = PrintPage(sample, owner_user)
    page.tabs.setCurrentIndex(next(i for i in range(page.tabs.count()) if page.tabs.tabData(i) == page.PRINTED))
    assert page.print_btn.text() == "Print again"
    page.close()
    # Payments says it the same way: Print ... again.
    source = inspect.getsource(payments_page)
    assert '"Print Final OF again"' in source and '"Reprint' not in source
    # Item passes use the claim stubs' word.
    passes = ItemPassesPage(sample, owner_user)
    assert passes.collected_btn.text() == "Collected"
    passes.close()
    assert item_passes.status_labels(sample)[item_passes.COLLECTED] == cs.status_labels(sample)[cs.COLLECTED] == "Collected by supplier"
    for module in (item_passes, item_pass_page):
        text = inspect.getsource(module).lower()
        assert "picked up" not in text and "pick up" not in text and "picks them up" not in text, module.__name__
    # One wording for the buttons that go back to the list.
    nor1 = auth.login(sample, "nor1", "123")
    for widget, button in ((OFPage(sample, owner_user), "back"), (POPage(sample, owner_user), "back"), (receiving_page.ReceivingPage(sample, nor1), "back_btn")):
        assert getattr(widget, button).text() == "Go back", type(widget).__name__
        widget.close()


def test_the_passes_on_paper_say_collect(sample):
    ip = sample.execute("SELECT id FROM item_passes WHERE pdf IS NOT NULL ORDER BY id").fetchone()[0]
    stub = sample.execute("SELECT id FROM claim_stubs ORDER BY id").fetchone()[0]
    item_text = _text(pdf.render_item_pass(sample, ip))
    stub_text = _text(pdf.render_claim_stub_pass(sample, stub, "Ana Receiver"))
    assert "for the supplier to collect" in item_text and "not collected within 90 days" in item_text and "pick" not in item_text
    assert "the supplier collects them there" in stub_text and "pick" not in stub_text


# ------------------------------------------------------------------ AUD-117: reading a pass's lines always needs a user

def test_item_pass_lines_always_need_a_user(app, sample):
    params = list(inspect.signature(item_passes.items).parameters)
    assert params == ["conn", "user", "ip_id"]
    ip = sample.execute("SELECT i.id FROM item_passes i JOIN purchase_orders p ON p.id = i.po_id WHERE p.branch_code = ? ORDER BY i.id", (NORTH,)).fetchone()[0]
    with pytest.raises(TypeError):
        item_passes.items(sample, ip)  # no user
    viewer = auth.login(sample, "viewer", "123")
    assert item_passes.items(sample, viewer, ip)
    stranger = sample.execute("SELECT u.username FROM users u JOIN user_branches b ON b.user_id = u.id JOIN user_roles r ON r.user_id = u.id"
                              " WHERE r.role = 'RECEIVER' AND b.branch_code <> ? ORDER BY u.id LIMIT 1", (NORTH,)).fetchone()[0]
    with pytest.raises(PermissionDenied):
        item_passes.items(sample, auth.login(sample, stranger, "123"), ip)
    # The home office page passes its user and shows the lines.
    from atlaspo.ui.item_pass_page import ItemPassesPage
    page = ItemPassesPage(sample, auth.login(sample, "owner", "123"))
    page.tabs.setCurrentIndex(page.tabs.count() - 1)  # All
    page.table.selectRow(0)
    assert page.lines.rowCount() == len(item_passes.items(sample, page.user, page._row()["id"])) > 0
    page.close()


# ------------------------------------------------------------------ AUD-149: lists read in a few statements, with the same results

class Statements:
    def __init__(self, conn):
        self.conn, self.sql = conn, []

    def __enter__(self):
        self.conn.set_trace_callback(self.sql.append)
        return self

    def __exit__(self, *_):
        self.conn.set_trace_callback(None)


def _same_as_one_at_a_time(conn, rows):
    """Each row worked out again one PO at a time, the way it was done before."""
    ids = [r["id"] for r in rows]
    lines_by, extra_by = receiving._lines_many(conn, ids), receiving._extra_lines_many(conn, ids)
    charged_by = receiving._charged_many(conn, ids)
    for r in rows:
        po_id = r["id"]
        assert r["progress"] == receiving.progress(conn, po_id), po_id
        lines = receiving.lines_of(conn, po_id)
        assert lines_by.get(po_id, []) == lines and extra_by.get(po_id, []) == receiving._extra_lines(conn, po_id), po_id
        assert charged_by.get(po_id, {}) == receiving.dm_pieces(conn, po_id), po_id
        excess = receiving.excess_lines(conn, po_id, lines)
        assert (r["excess_m"], r["excess_left_m"]) == (sum(x["excess_m"] for x in excess), sum(x["left_m"] for x in excess)), po_id
        assert r["tab"] == (receiving.COMPLETED if not r["progress"] and receiving._settled(conn, po_id) else receiving.TO_CHECK), po_id


def test_received_list_is_the_same_and_reads_in_a_few_statements(sample):
    owner_user = auth.login(sample, "owner", "123")
    reopenable = next((r["id"] for r in receiving.po_results(sample, owner_user) if receiving.reopen_label(sample, r["id"]) == "Reopen for recount"), None)
    if reopenable:
        receiving.reopen(sample, owner_user, reopenable, "4 pieces found in another box")
    with Statements(sample) as s:
        rows = receiving.po_results(sample, owner_user)
    assert len(rows) > 20
    _same_as_one_at_a_time(sample, rows)
    words = {r["progress"] for r in rows}
    assert receiving.COUNT_LABELS[receiving.WAITING] in words and any(w.endswith("Counting stopped") for w in words)
    assert not reopenable or "Reopened for recount" in words
    assert {r["tab"] for r in rows} == {receiving.TO_CHECK, receiving.COMPLETED}
    assert len(s.sql) <= 15, len(s.sql)  # was about 7 per PO


def test_received_list_matches_for_every_counting_state(world):
    w = world
    make = lambda name: auth.create_user(w.conn, w.admin, name, name.title(), PASSWORD, ["RECEIVER"], must_change=False, branch_code=NORTH)
    ana, ben = make("ana"), make("ben")

    def new_po():
        of_id = w.approved(items=[w.item(cost=5000, alloc={NORTH: 10}, description="Cap", item_code="CAP-1")])
        po = order_forms.generate_pos(w.conn, w.buyer, of_id)[0]
        orders.issue(w.conn, w.buyer, po["id"])
        return po["id"], orders.get_po(w.conn, po["id"])["items"][0]["id"]

    waiting, item = new_po()
    receiving.save_count(w.conn, ana, waiting, {item: "10"})
    again, item = new_po()
    receiving.save_count(w.conn, ana, again, {item: "10"})
    receiving.save_count(w.conn, ben, again, {item: "9"})
    held, item = new_po()
    for _ in range(2):
        receiving.save_count(w.conn, ana, held, {item: "10"})
        receiving.save_count(w.conn, ben, held, {item: "9"})
    one_more, item = new_po()
    for _ in range(2):
        receiving.save_count(w.conn, ana, one_more, {item: "10"})
        receiving.save_count(w.conn, ben, one_more, {item: "9"})
    receiving.reopen(w.conn, w.admin, one_more, "Called the branch")
    recount, item = new_po()
    receiving.save_count(w.conn, ana, recount, {item: "12"})
    receiving.save_count(w.conn, ben, recount, {item: "12"})
    receiving.reopen(w.conn, w.admin, recount, "Box counted twice")
    done, item = new_po()
    receiving.save_count(w.conn, ana, done, {item: "10"})
    receiving.save_count(w.conn, ben, done, {item: "10"})
    rows = receiving.po_results(w.conn, w.admin)
    by_id = {r["id"]: r["progress"] for r in rows}
    assert by_id[waiting] == "Waiting for second count" and by_id[again] == "Being recounted" and by_id[one_more] == "Being recounted"
    assert by_id[held] == "Check: 2 counts didn't match. Counting stopped" and by_id[recount] == "Reopened for recount" and by_id[done] == ""
    _same_as_one_at_a_time(w.conn, rows)
    assert receiving._lines_many(w.conn, []) == {} and receiving._returned_many(w.conn, []) == {}


def _old_integrity_report(conn):
    """backup.integrity_report as it was before round 3, one lookup per PO and per year."""
    from atlaspo.orders import get_po
    problems = [r[0] for r in conn.execute("PRAGMA integrity_check") if r[0] != "ok"]
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        problems.append(f"{len(fk)} broken links between records")
    if conn.execute("SELECT COUNT(*) FROM po_write_guard").fetchone()[0]:
        problems.append("The locks on issued OFs and POs are switched off: a leftover entry in po_write_guard lets anyone change them")
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    missing = sorted(backup._fresh_triggers() - present)
    if missing:
        problems.append(f"{len(missing)} of the guards that keep records permanent are gone: " + ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else ""))
    for row in conn.execute("SELECT id, po_number, grand_total_c FROM purchase_orders").fetchall():
        if get_po(conn, row["id"])["totals"].grand_total != row["grand_total_c"]:
            problems.append(f"{row['po_number'] or row['id']}: stored total differs from its rows")
    for n in conn.execute("SELECT po_year, branch_code, MAX(po_seq) AS top FROM purchase_orders WHERE of_id IS NOT NULL GROUP BY po_year, branch_code").fetchall():
        seq = conn.execute("SELECT last_number FROM po_number_sequences WHERE year=? AND branch_code=?", (n["po_year"], n["branch_code"])).fetchone()
        if seq is None or seq["last_number"] < n["top"]:
            problems.append(f"{n['po_year']} branch {n['branch_code']}: the PO number counter is behind the highest PO number")
    for n in conn.execute("SELECT of_year, MAX(of_seq) AS top FROM order_forms WHERE of_year IS NOT NULL GROUP BY of_year").fetchall():
        seq = conn.execute("SELECT last_number FROM of_number_sequences WHERE year=?", (n["of_year"],)).fetchone()
        if seq is None or seq["last_number"] < n["top"]:
            problems.append(f"{n['of_year']}: the OF number counter is behind the highest OF number")
    for row in conn.execute("SELECT i.id, f.of_number, f.id AS of_id, i.description FROM order_form_items i JOIN order_forms f ON f.id = i.of_id"
                            " WHERE i.total_qty_m <> COALESCE((SELECT SUM(a.qty_m) FROM order_form_allocations a WHERE a.of_item_id = i.id), 0)").fetchall():
        problems.append(f"{row['of_number'] or row['of_id']}: '{row['description']}' total differs from its branch allocations")
    for row in conn.execute("SELECT f.id, f.of_number, f.grand_total_c, (SELECT COALESCE(SUM(p.grand_total_c),0) FROM purchase_orders p"
                            " WHERE p.of_id = f.id AND p.status <> 'CANCELLED') AS pos FROM order_forms f WHERE f.pos_generated_at IS NOT NULL AND f.status <> 'CANCELLED'").fetchall():
        if row["grand_total_c"] != row["pos"]:
            problems.append(f"{row['of_number']}: the OF total differs from the sum of its branch POs")
    for what, table, p, empty in backup._DOCUMENTS:
        for n in conn.execute(f"SELECT {p}_year AS year, MAX({p}_seq) AS top FROM {table} GROUP BY {p}_year").fetchall():
            seq = conn.execute(f"SELECT last_number FROM {p}_number_sequences WHERE year = ?", (n["year"],)).fetchone()
            if seq is None or seq["last_number"] < n["top"]:
                problems.append(f"{n['year']}: the {what} number counter is behind the highest {what} number")
        for row in conn.execute(f"SELECT {p}_number AS number, ({empty}) AS empty, pdf, sha256 FROM {table} ORDER BY id").fetchall():
            if row["empty"]:
                problems.append(f"{row['number']}: the {what} has no kept PDF")
            elif backup._sha(row["pdf"]) != (row["sha256"] or ""):
                problems.append(f"{row['number']}: the kept {what} PDF has changed since it was made")
    for what, table, items, key in (("debit memo", "debit_memos", "debit_memo_items", "dm_id"), ("claim stub", "claim_stubs", "claim_stub_items", "cs_id")):
        number = "dm_number" if table == "debit_memos" else "cs_number"
        for row in conn.execute(f"SELECT d.{number} AS number FROM {table} d WHERE d.total_c <> COALESCE((SELECT SUM(i.amount_c) FROM {items} i WHERE i.{key} = d.id), 0)").fetchall():
            problems.append(f"{row['number']}: the {what} total differs from its lines")
    for row in conn.execute("SELECT v.revision, v.pdf, v.sha256, p.po_number, p.id FROM po_issued_versions v JOIN purchase_orders p ON p.id = v.po_id ORDER BY v.id").fetchall():
        if backup._sha(row["pdf"]) != row["sha256"]:
            problems.append(f"{row['po_number'] or row['id']} revision {row['revision']}: the issued PO PDF has changed since it was issued")
    return problems + backup._orphans(conn) + backup._attachments(conn)


def test_health_check_is_the_same_on_a_healthy_and_a_broken_file(sample):
    with Statements(sample) as s:
        healthy = backup.integrity_report(sample)
    assert healthy == _old_integrity_report(sample)  # this copy has no attachments folder, so that file shows as missing in both
    pos = sample.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0]
    assert len(s.sql) < pos, (len(s.sql), pos)  # no longer a full PO read per PO
    # Break it in the ways these checks look for (the guards go first so the file can be edited), and both versions say the same.
    for (name,) in sample.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall():
        sample.execute(f"DROP TRIGGER {name}")
    sample.execute("UPDATE purchase_orders SET grand_total_c = grand_total_c + 1 WHERE id = (SELECT MIN(id) FROM purchase_orders WHERE of_id IS NOT NULL)")
    sample.execute("DELETE FROM dm_number_sequences")
    sample.execute("UPDATE of_number_sequences SET last_number = 0")
    sample.execute("DELETE FROM po_number_sequences WHERE rowid = (SELECT MIN(rowid) FROM po_number_sequences)")
    sample.execute("UPDATE cs_number_sequences SET last_number = 1")
    sample.commit()
    broken = backup.integrity_report(sample)
    assert broken == _old_integrity_report(sample)
    text = " | ".join(broken)
    for expected in ("stored total differs", "debit memo number counter is behind", "OF number counter is behind", "PO number counter is behind"):
        assert expected in text, expected


def test_claim_stub_list_works_out_paid_dates_in_a_few_statements(sample):
    owner_user = auth.login(sample, "owner", "123")
    # The sample has one stub at the warehouse. In this copy every stub is put there, so paid and unpaid ones are both worked out.
    for (name,) in sample.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'claim_stubs'").fetchall():
        sample.execute(f"DROP TRIGGER {name}")
    sample.execute("UPDATE claim_stubs SET status = 'AT_WAREHOUSE'")
    sample.commit()
    with Statements(sample) as s:
        rows = cs.search(sample, owner_user)
    at_warehouse = [r for r in rows if r["status"] == cs.AT_WAREHOUSE]
    assert len(at_warehouse) > 3 and any(r["paid_on"] for r in at_warehouse) and not all(r["paid_on"] for r in at_warehouse)
    for r in rows:
        expected = cs.paid_on(sample, r["po_id"], r["id"]) if r["status"] == cs.AT_WAREHOUSE else None
        assert r["paid_on"] == expected, r["cs_number"]
        assert r["days_left"] == (cs.days_left(expected) if expected else None)
    assert len(s.sql) <= 4, s.sql
    # One stub at a time reads the same.
    assert all(cs.get(sample, owner_user, r["id"])["paid_on"] == r["paid_on"] for r in rows)


def test_claim_stub_list_paid_dates_for_late_damage(world):
    """Damage found after the check: the date comes from the supplier's next check (none yet, then its claim date), in the list as for one stub."""
    from atlaspo import payments
    from test_fix_payments import Order as PaidOrder, _record
    world.accounting = auth.create_user(world.conn, world.admin, "lorna", "Lorna Tan", PASSWORD, ["ACCOUNTING"], must_change=False)
    first = PaidOrder(world)
    check = _record(world, first.of_id, "600001")
    payments.claim_check(world.conn, world.accounting, check, "2026-11-02")
    number = first.pos[NORTH]["po_number"]
    lines = cs.late_lines(world.conn, world.buyer, number)
    stub = cs.issue_late(world.conn, world.buyer, number, {lines[0]["po_item_id"]: "3"}, "Found torn when unpacked")[0]
    assert world.conn.execute("SELECT paid_before FROM late_damage WHERE cs_id = ?", (stub["id"],)).fetchone()[0] == check
    cs.arrived(world.conn, world.warehouse, stub["id"], cs.as_on_paper(world.conn, stub["id"]))

    def same():
        rows = {r["id"]: r for r in cs.search(world.conn, world.admin)}
        assert rows[stub["id"]]["status"] == cs.AT_WAREHOUSE
        for r in rows.values():
            assert r["paid_on"] == (cs.paid_on(world.conn, r["po_id"], r["id"]) if r["status"] == cs.AT_WAREHOUSE else None), r["cs_number"]
        return rows[stub["id"]]["paid_on"]

    assert same() is None  # the supplier's next check isn't made yet
    second = PaidOrder(world)
    later = _record(world, second.of_id, "600002")
    payments.claim_check(world.conn, world.accounting, later, today().isoformat())
    assert same() == today().isoformat()
