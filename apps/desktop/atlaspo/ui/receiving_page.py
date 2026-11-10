"""Branch receiving: the two blind counts receivers do, and the results only the home office sees."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics
from PySide6.QtWidgets import QAbstractItemView, QButtonGroup, QComboBox, QCompleter, QHBoxLayout, QLineEdit, QPushButton, QStackedWidget, QTableWidgetItem, QVBoxLayout, QWidget

from .. import auth
from .. import claim_stubs as cs_service
from .. import debit_memos as dm_service
from .. import item_passes as ip_service
from ..db import POError
from .. import masters, orders, receiving
from ..money import fmt_qty
from .common import Pager, keep_place, ask_text, show_if, choose_printer, print_once, smart_columns, button, confirm, fill_table, fmt_date, fmt_datetime, guarded, label, make_table, open_for_printing, register_tabs, selected_key, set_widths, show_people, show_tab_counts
from .claim_stub_page import ClaimStubDialog
from .debit_memo_page import DebitMemoDialog
from .pages import page_layout
from . import theme

C_PIECES, C_DAMAGED, C_REMARKS, C_BEFORE = 5, 6, 7, 8


def _damaged(row: dict) -> str:
    return fmt_qty(row["damaged_m"]) if row["damaged_m"] else ""


def _status(row: dict) -> str:
    """How a completed delivery got there."""
    parts = [text for text, there in (("debit memo", row["debit_memos"]), ("claim stub", row["claim_stubs"]), ("item pass", row["item_passes"]), ("excess returned", row["excess_m"] and not row["item_passes"])) if there]
    if not parts:
        return "Completed delivery"
    return f"Completed with {parts[0]}" if len(parts) == 1 and parts[0] != "excess returned" else f"Completed: {', '.join(parts)}"


def _before(previous) -> str:
    if not previous:
        return ""
    pieces, damaged = previous
    return fmt_qty(pieces) + (f", {fmt_qty(damaged)} damaged" if damaged else "")


def _kind(row: dict) -> str:
    return "Recount" if row["recount_of"] or row["round_no"] > 1 or row["attempt_no"] > 1 else "Count"


def _tips(table, columns) -> None:
    """Each cell in these columns shows its whole text on hover, for when the column is too narrow to show it."""
    for r in range(table.rowCount()):
        for c in columns:
            cell = table.item(r, c)
            if cell is not None:
                cell.setToolTip(cell.text())


_WRAPPED = (8, 9, 10)  # Received: DM / CS / item pass, Counting, Status


def _fit_rows(table, columns) -> None:
    """A row is made taller only where one of these columns has more than fits on one line (bold, as when the row is chosen), so a long list
    stays quick and most rows keep their usual height."""
    font = QFont(table.font())
    font.setBold(True)
    metrics = QFontMetrics(font)
    usual = table.verticalHeader().defaultSectionSize()
    room = {c: table.columnWidth(c) - 16 for c in columns}
    for r in range(table.rowCount()):
        long = any((cell := table.item(r, c)) is not None and metrics.horizontalAdvance(cell.text()) > room[c] for c in columns if not table.isColumnHidden(c))
        if long:
            table.resizeRowToContents(r)
            if table.rowHeight(r) < usual:
                table.setRowHeight(r, usual)
        elif table.rowHeight(r) != usual:
            table.setRowHeight(r, usual)


class _Room:
    """Keeps a table's main column readable on a small window. When the table narrows, the columns named here give up width, in the order named
    and down to their least, before the main column (the one that stretches) drops below the room it needs. On a big window nothing changes.
    The page calls fit whenever it's resized or shown, so no event filter is left on the table to outlive it."""

    def __init__(self, table, stretch: int, room: int, shrink: dict[int, tuple[int, int]]):
        self.table, self.stretch, self.room, self.shrink = table, stretch, room, shrink

    def fit(self) -> None:
        t = self.table
        if t.isColumnHidden(self.stretch):
            return
        kept = getattr(t, "kept_widths", None) or {}  # widths the person dragged themselves are their full width here
        sizes = {c: (min(least, kept.get(c, full)), kept.get(c, full)) for c, (least, full) in self.shrink.items()}
        shown = [c for c in self.shrink if not t.isColumnHidden(c)]
        fixed = sum(t.columnWidth(c) for c in range(t.columnCount()) if c != self.stretch and c not in self.shrink and not t.isColumnHidden(c))
        cut = sum(sizes[c][1] for c in shown) - (t.viewport().width() - fixed - self.room)
        for c in shown:
            least, full = sizes[c]
            width = full - max(0, min(cut, full - least))
            cut -= full - width
            if t.columnWidth(c) != width:
                t.setColumnWidth(c, width)


class _FitsRooms:
    """For a page with _Room tables: shares the room out again once the page has been laid out at its new size."""
    rooms: tuple = ()

    def _fit_rooms(self) -> None:
        for room in self.rooms:
            room.fit()

    def _fit_soon(self) -> None:
        QTimer.singleShot(0, self, self._fit_rooms)

    def resizeEvent(self, event):  # noqa: N802 (Qt's name)
        super().resizeEvent(event)
        self._fit_soon()

    def showEvent(self, event):  # noqa: N802 (Qt's name)
        super().showEvent(event)
        self._fit_soon()


class ReceivingPage(_FitsRooms, QWidget):
    """Press a PO or type its number, count what's listed, save. A second person does the same on their own, and the two are compared."""
    changed = Signal()
    COUNT, COUNTS, PASSES, DAMAGE = "COUNT", "COUNTS", "PASSES", "DAMAGE"
    TABS = {COUNT: "Count a delivery", COUNTS: "My branch's counts", PASSES: "Item passes", DAMAGE: "Report damage"}

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.po_id = self.recount_of = None
        self.items, self.rows = [], []
        root, top = page_layout(self, "Receiving")
        names = {b["code"]: b["name"] for b in masters.list_branches(conn)}
        top.addWidget(label(names.get(user.branch_code, "All branches"), "PanelTitle"))
        self.tabs = register_tabs(self.TABS, self._set_tab)
        root.addWidget(self.tabs)
        self.views = QStackedWidget()
        self.views.addWidget(self._count_view())
        self.views.addWidget(self._counts_view())
        self.views.addWidget(self._passes_view())
        self.views.addWidget(self._damage_view())
        root.addWidget(self.views, 1)
        self.rooms = (self.grid_room, self.lines_room, self.pass_done_room)
        self.refresh()

    def _count_view(self) -> QWidget:
        view = QWidget()
        col = QVBoxLayout(view)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(10)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.back_btn = button("Go back", self._back_pressed, lock=False)
        bar.addWidget(self.back_btn)
        self.number = QLineEdit()
        self.number.setPlaceholderText("Type the OF number or PO number")
        self.number.setMinimumWidth(300)
        self.number.returnPressed.connect(lambda: guarded(self, self.open_number))
        bar.addWidget(self.number)
        bar.addWidget(button("Open", self.open_number))
        self.picker = QComboBox()
        self.picker.activated.connect(lambda _: guarded(self, self._load, self.picker.currentData()))
        self.picker.hide()
        bar.addWidget(self.picker)
        bar.addStretch(1)
        col.addLayout(bar)
        self.heading = label("", "PanelTitle")
        col.addWidget(self.heading)
        self.prompt = label("", "Muted")
        self.prompt.setWordWrap(True)
        col.addWidget(self.prompt)
        self.waiting_rows = []
        self.po_list = make_table(["PO number", "OF number", "Branch", "Supplier", "Expected delivery", "What's needed"], stretch=3)
        set_widths(self.po_list, {0: 120, 1: 120, 2: 90, 4: 120, 5: 240})
        smart_columns(self.po_list, f"to-count-{self.user.username}")
        self.po_list.cellClicked.connect(self._po_pressed)
        col.addWidget(self.po_list, 1)
        self.none_waiting = label("Nothing is waiting for your count. For another delivery of a PO, type its number above.", "Empty")
        self.none_waiting.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(self.none_waiting)
        self.grid = make_table(["SKU", "Product", "Size", "Color", "UOM", "Pieces counted", "Damaged", "Remarks", "You counted before"], numeric=(C_PIECES, C_DAMAGED), stretch=1)
        set_widths(self.grid, {0: 120, 2: 70, 3: 90, 4: 70, C_PIECES: 110, C_DAMAGED: 80, C_REMARKS: 220, C_BEFORE: 150})
        # On a small window Remarks and the earlier count give way first, so the product name stays readable.
        self.grid_room = _Room(self.grid, 1, 160, {C_REMARKS: (80, 220), C_BEFORE: (110, 150)})
        self.grid.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
        col.addWidget(self.grid, 1)
        row = QHBoxLayout()
        self.add_btn = button("Add product not on the PO", self.add_extra_row)
        row.addWidget(self.add_btn)
        row.addStretch(1)
        # The boxes that reached the branch, so a box lost between the warehouse and here shows. Only the home office sees what the warehouse sent.
        self.boxes_label = label("Boxes received")
        self.boxes = QLineEdit()
        self.boxes.setPlaceholderText("0")
        self.boxes.setMaximumWidth(80)
        row.addWidget(self.boxes_label)
        row.addWidget(self.boxes)
        self.cancel_btn = button("Clear what I typed", self._clear_pressed)
        self.save_btn = button("Save count", self._save_pressed, "Primary")
        row.addWidget(self.cancel_btn)
        row.addWidget(self.save_btn)
        col.addLayout(row)
        self._clear()
        return view

    def _counts_view(self) -> QWidget:
        view = QWidget()
        col = QVBoxLayout(view)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(10)
        self.counts = make_table(["PO number", "Branch", "Supplier", "Delivery", "Status", "Counted by", "Counted at", "Matched with", "Matched at", "Type"], numeric=(3,), stretch=2)
        set_widths(self.counts, {0: 110, 1: 76, 3: 60, 4: 170, 5: 84, 6: 134, 7: 96, 8: 134, 9: 66})
        smart_columns(self.counts, f"my-counts-{self.user.username}")
        self.counts.itemSelectionChanged.connect(self._count_selected)
        col.addWidget(self.counts, 3)
        row = QHBoxLayout()
        self.selected_note = label("Select one of your counts to see it.", "Muted")
        row.addWidget(self.selected_note)
        row.addStretch(1)
        self.recount_btn = button("Count mine again", self.recount)
        row.addWidget(self.recount_btn)
        col.addLayout(row)
        self.lines = make_table(["SKU", "Product", "Size", "Color", "UOM", "Pieces counted", "Damaged", "Remarks"], numeric=(C_PIECES, C_DAMAGED), stretch=1)
        set_widths(self.lines, {0: 120, 2: 70, 3: 90, 4: 70, C_PIECES: 110, C_DAMAGED: 80, C_REMARKS: 220})
        self.lines_room = _Room(self.lines, 1, 160, {C_REMARKS: (80, 220)})
        col.addWidget(self.lines, 2)
        return view

    def _damage_view(self) -> QWidget:
        """Damage found after the count was agreed, say when unpacking. One receiver reports it, in pieces only, and Accounting or management approves it.
        Once approved, its damaged goods pass prints under Item passes."""
        view = QWidget()
        col = QVBoxLayout(view)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        intro = label("Found damage after the delivery was counted? Type part of the PO number and pick it, type the damaged pieces and how you found them, then send it. "
                      "Accounting or management approves it, and the pass to send the goods to the warehouse then prints under Item passes.", "Muted")
        intro.setWordWrap(True)
        col.addWidget(intro)
        find = QHBoxLayout()
        self.damage_number = QLineEdit()
        self.damage_number.setPlaceholderText("PO or OF number, or part of it")
        self.damage_number.setClearButtonEnabled(True)
        self.damage_completer = QCompleter([], self)
        self.damage_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.damage_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.damage_completer.activated.connect(lambda text: guarded(self, self.find_damage, text))
        self.damage_number.setCompleter(self.damage_completer)
        self.damage_number.returnPressed.connect(lambda: guarded(self, self.find_damage))
        find.addWidget(self.damage_number, 1)
        find.addWidget(button("Find", lambda: guarded(self, self.find_damage)))
        self.damage_narrow = QLineEdit()
        self.damage_narrow.setPlaceholderText("Narrow the products: type part of the SKU or name")
        self.damage_narrow.setClearButtonEnabled(True)
        self.damage_narrow.textChanged.connect(self._narrow_damage)
        find.addWidget(self.damage_narrow, 1)
        col.addLayout(find)
        self.damage_grid = make_table(["SKU", "Product", "Size", "Color", "Can report", "Damaged pieces"], numeric=(4, 5), stretch=1)
        set_widths(self.damage_grid, {0: 110, 2: 70, 3: 90, 4: 90, 5: 120})
        self.damage_grid.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
        self.damage_grid.setProperty("no_sort", True)
        col.addWidget(self.damage_grid, 2)
        send = QHBoxLayout()
        self.damage_reason = QLineEdit()
        self.damage_reason.setPlaceholderText("How did you find it? For example: torn seams found when unpacking.")
        send.addWidget(self.damage_reason, 1)
        send.addWidget(button("Send for approval", lambda: guarded(self, self.send_damage), "Primary"))
        col.addLayout(send)
        col.addWidget(label("Sent", "PanelTitle"))
        self.damage_sent = make_table(["Sent", "PO number", "Products", "Pieces", "Status", "Note"], numeric=(3,), stretch=5)
        set_widths(self.damage_sent, {0: 130, 1: 120, 2: 220, 3: 60, 4: 170})
        col.addWidget(self.damage_sent, 1)
        self.damage_lines = []
        return view

    def _show_reports(self):
        if not self.user.can("receiving.count") or self.user.branch_code is None:
            return
        self.damage_completer.model().setStringList(cs_service.report_numbers(self.conn, self.user))
        sent = cs_service.reports(self.conn, self.user)
        fill_table(self.damage_sent, [[fmt_datetime(r["requested_at"]), r["po_number"], ", ".join(l["description"] for l in r["lines"]), fmt_qty(r["pieces_m"]), cs_service.REQUEST_LABELS[r["status"]],
                                       r["decision_reason"] or (r["cs_number"] or "")] for r in sent])
        self.tabs.setTabText(3, self.TABS[self.DAMAGE])

    def find_damage(self, text=None):
        number = (text or self.damage_number.text()).strip()
        if text:
            self.damage_number.setText(text)
        self.damage_lines = cs_service.report_lines(self.conn, self.user, number)
        self.damage_found = number
        fill_table(self.damage_grid, [[l["item_code"], l["description"], l["size"], l["color"], fmt_qty(l["room_m"]), ""] for l in self.damage_lines],
                   keys=[l["po_item_id"] for l in self.damage_lines])
        for r in range(self.damage_grid.rowCount()):
            for c in range(self.damage_grid.columnCount()):
                if c != 5:
                    cell = self.damage_grid.item(r, c)
                    cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._narrow_damage()

    def _narrow_damage(self, *_):
        words = self.damage_narrow.text().lower().split()
        for r, line in enumerate(self.damage_lines):
            text = f"{line['item_code']} {line['description']} {line['size']} {line['color']}".lower()
            self.damage_grid.setRowHidden(r, not all(w in text for w in words))

    def send_damage(self):
        if not self.damage_lines:
            raise POError("Find the PO first.")
        pieces = {self.damage_grid.item(r, 0).data(Qt.ItemDataRole.UserRole): self.damage_grid.item(r, 5).text() for r in range(self.damage_grid.rowCount())}
        cs_service.send_report(self.conn, self.user, self.damage_found, pieces, self.damage_reason.text())
        self.damage_lines = []
        self.damage_grid.setRowCount(0)
        for w in (self.damage_number, self.damage_reason, self.damage_narrow):
            w.clear()
        self.refresh()
        self.changed.emit()

    def _passes_view(self) -> QWidget:
        """Excess pieces leave the branch on an item pass, which prints once."""
        view = QWidget()
        col = QVBoxLayout(view)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(10)
        row = QHBoxLayout()
        self.pass_note = label("", "PanelTitle")
        self.pass_note.setWordWrap(True)
        row.addWidget(self.pass_note, 1)
        self.print_pass_btn = button("Print item pass", self.print_pass, "Primary")
        row.addWidget(self.print_pass_btn)
        col.addLayout(row)
        self.pass_waiting = make_table(["PO number", "OF number", "Supplier", "Excess pieces", "Products"], numeric=(3,), stretch=4)
        set_widths(self.pass_waiting, {0: 120, 1: 120, 2: 220, 3: 100})
        smart_columns(self.pass_waiting, f"passes-to-print-{self.user.username}")
        self.pass_waiting.itemSelectionChanged.connect(self._pass_selected)
        col.addWidget(self.pass_waiting, 1)
        # Damaged goods on a claim stub leave the branch on a pass too, printed here and sent with the goods to the warehouse.
        damaged_row = QHBoxLayout()
        damaged_row.addWidget(label("Damaged goods (claim stubs)", "PanelTitle"), 1)
        self.print_cs_btn = button("Print damaged goods pass", self.print_cs_pass, "Primary")
        damaged_row.addWidget(self.print_cs_btn)
        col.addLayout(damaged_row)
        self.cs_waiting = make_table(["Claim stub", "PO number", "Supplier", "Pieces", "Products", "Pass printed"], numeric=(3,), stretch=4)
        set_widths(self.cs_waiting, {0: 120, 1: 120, 2: 160, 3: 70, 5: 140})
        col.addWidget(self.cs_waiting, 1)
        col.addWidget(label("Already printed", "PanelTitle"))
        self.pass_done = make_table(["Item pass", "Date", "PO number", "Supplier", "Pieces", "Printed by", "Where the goods are"], numeric=(4,), stretch=6)
        set_widths(self.pass_done, {0: 120, 1: 96, 2: 120, 3: 220, 4: 70, 5: 150})
        # Where the goods are is what the branch looks for here, so the supplier and who printed give way first.
        self.pass_done_room = _Room(self.pass_done, 6, 200, {5: (100, 150), 3: (110, 220)})
        smart_columns(self.pass_done, f"passes-printed-{self.user.username}")
        col.addWidget(self.pass_done, 2)
        return view

    def _set_tab(self, tab):
        self.views.setCurrentIndex({self.COUNT: 0, self.COUNTS: 1, self.PASSES: 2, self.DAMAGE: 3}[tab])
        self.refresh()
        self._fit_soon()

    def _show_passes(self):
        self.pass_rows = ip_service.to_print(self.conn, self.user)
        fill_table(self.pass_waiting, [[r["po_number"], r["of_number"] or "", r["supplier"], fmt_qty(r["pieces_m"]), ", ".join(line["description"] for line in r["lines"])] for r in self.pass_rows],
                   keys=[r["id"] for r in self.pass_rows])
        labels = ip_service.status_labels(self.conn)
        done = ip_service.for_branch(self.conn, self.user)
        fill_table(self.pass_done, [[r["ip_number"], fmt_date(r["ip_date"]), r["po_number"], r["supplier"], fmt_qty(r["pieces_m"]), r["printed_name"], labels[r["status"]]] for r in done])
        _tips(self.pass_done, (3, 5, 6))
        self.cs_rows = cs_service.passes_for_branch(self.conn, self.user)
        fill_table(self.cs_waiting, [[r["cs_number"], r["po_number"], r["supplier"], fmt_qty(r["pieces_m"] or 0), ", ".join(l["description"] for l in r["lines"]), fmt_datetime(r["printed_at"]) if r["printed_at"] else "Not yet"]
                                     for r in self.cs_rows], keys=[r["id"] for r in self.cs_rows])
        if self.cs_rows:
            self.cs_waiting.selectRow(0)
        show_if(self.print_cs_btn, bool(self.cs_rows))
        to_print = len(self.pass_rows) + sum(not r["printed_at"] for r in self.cs_rows)
        self.tabs.setTabText(2, f"Item passes  ({to_print})" if to_print else "Item passes")
        if self.pass_rows:
            self.pass_waiting.selectRow(0)
        self._pass_selected()

    def _pass_selected(self):
        key = selected_key(self.pass_waiting)
        show_if(self.print_pass_btn, any(r["id"] == key for r in self.pass_rows))
        self.pass_note.setText("Print the item pass, then sign the goods out. Send the original with the goods and keep the duplicate. It prints only once." if self.pass_rows else "No excess pieces to send back.")

    def print_pass(self):
        row = next((r for r in self.pass_rows if r["id"] == selected_key(self.pass_waiting)), None)
        if not row or not confirm(self, "Print item pass", f"Print the item pass for {fmt_qty(row['pieces_m'])} excess pieces on {row['po_number']}? It prints once, an original and a duplicate. Make sure the printer is ready. Your name is recorded.", "Print"):
            return
        printer = choose_printer(self)
        if printer is None:
            return
        made = ip_service.issue(self.conn, self.user, row["id"])
        try:
            print_once(printer, made["pdf"])
        except Exception as exc:  # noqa: BLE001 (the printer's own error goes on the record)
            ip_service.print_failed(self.conn, self.user, made["id"], str(exc) or type(exc).__name__)
            raise POError(f"{made['ip_number']} didn't print ({exc}), so it was cancelled. Fix the printer, then print the item pass again.") from exc
        finally:
            self.refresh()
            self.changed.emit()

    def print_cs_pass(self):
        key = selected_key(self.cs_waiting)
        row = next((r for r in self.cs_rows if r["id"] == key), None)
        if not row:
            return
        again = " It was printed before, so this one is marked as a copy." if row["printed_at"] else ""
        if not confirm(self, "Print damaged goods pass", f"Print the pass for the {fmt_qty(row['pieces_m'] or 0)} damaged pieces on {row['cs_number']}? Send the original with the goods to the warehouse and keep the duplicate.{again}", "Print"):
            return
        try:
            # The print is recorded only once the file has opened, so a pass that never opened isn't shown as printed.
            cs_service.print_pass(self.conn, self.user, row["id"], deliver=lambda data: open_for_printing([data], f"Damaged goods pass {row['cs_number']}"))
        finally:
            # The other pages hear of it either way, so they show what's really on record.
            self.refresh()
            self.changed.emit()

    def _show_waiting(self):
        """The list of POs to press, whenever no PO is open for counting."""
        self.waiting_rows = receiving.to_count(self.conn, self.user)
        fill_table(self.po_list, [[r["po_number"], r["of_number"] or "", r["branch_name"], r["supplier"], fmt_date(r["expected_delivery_date"]) or "To be confirmed", r["need"]] for r in self.waiting_rows],
                   keys=[r["id"] for r in self.waiting_rows])
        counting = self.po_id is not None
        self.back_btn.setVisible(counting)
        self.po_list.setVisible(not counting and bool(self.waiting_rows))
        self.none_waiting.setVisible(not counting and not self.waiting_rows)
        self.grid.setVisible(counting)
        self.add_btn.setVisible(counting)
        self.cancel_btn.setVisible(counting)
        self.save_btn.setVisible(counting)
        self.tabs.setTabText(0, f"Count a delivery  ({len(self.waiting_rows)})" if self.waiting_rows else "Count a delivery")

    def _po_pressed(self, row: int, _column: int):
        self.number.setText(self.waiting_rows[row]["po_number"])
        guarded(self, self.open_number)

    def refresh(self, *_):
        self._show_waiting()
        self._show_passes()
        self._show_reports()
        selected = selected_key(self.counts)
        self.rows = receiving.branch_counts(self.conn, self.user)
        fill_table(self.counts, [[r["po_number"], r["branch_name"], r["supplier"], r["delivery_no"], receiving.COUNT_LABELS[r["status"]], r["counted_name"], fmt_datetime(r["counted_at"]),
                                  r["approved_name"] or "", fmt_datetime(r["approved_at"]), _kind(r)] for r in self.rows], keys=[r["id"] for r in self.rows])
        people = auth.chips(self.conn)
        show_people(self.counts, 5, people, [r["counted_by"] for r in self.rows])
        show_people(self.counts, 7, people, [r["approved_by"] for r in self.rows])
        for i, r in enumerate(self.rows):
            if r["id"] == selected:
                self.counts.selectRow(i)
        self._count_selected()

    def _selected(self) -> dict | None:
        key = selected_key(self.counts)
        return next((r for r in self.rows if r["id"] == key), None)

    def _count_selected(self):
        row = self._selected()
        mine = bool(row) and (row["counted_by"] == self.user.id or "ADMIN" in self.user.roles)
        lines = receiving.count_lines(self.conn, self.user, row["id"]) if mine else []
        fill_table(self.lines, [[i["item_code"], i["description"], i["size"], i["color"], i["uom"], fmt_qty(i["pieces_m"]), _damaged(i), i["remarks"]] for i in lines])
        _tips(self.lines, (1, C_REMARKS))
        # Once both counts match there's nothing to recount here. Only your own count that's still waiting can be redone, once.
        show_if(self.recount_btn, bool(row) and receiving.can_redo(self.conn, self.user, row["id"]))
        if not row:
            self.selected_note.setText("Select one of your counts to see it.")
        elif not mine:
            self.selected_note.setText(f"{row['po_number']}: only {row['counted_name']} can see these numbers.")
        else:
            self.selected_note.setText(f"{row['po_number']}, delivery {row['delivery_no']}")

    def _clear(self):
        self.po_id = self.recount_of = self.sheet_key = None
        self.items = []
        self.number.clear()
        self.picker.hide()
        self.heading.setText("")
        self.prompt.setText("Press a PO below, or type the number from your slip.")
        self.grid.setRowCount(0)
        self.save_btn.setEnabled(False)
        self.boxes.clear()
        for w in (self.boxes, self.boxes_label):
            w.hide()
        self._show_waiting()

    def open_number(self):
        found = receiving.find_pos(self.conn, self.user, self.number.text())
        self.recount_of = None
        self.picker.clear()
        for r in found:
            self.picker.addItem(f"{r['po_number']}  {r['branch_name']}", r["id"])
        self.picker.setVisible(len(found) > 1)
        self._load(found[0]["id"])

    def _load(self, po_id: int):
        # Which delivery and round this sheet is for, read first, so a sheet that goes out of date before it's saved is refused.
        key = receiving.sheet_key(self.conn, self.user, po_id)
        self.items = receiving.sheet(self.conn, self.user, po_id, self.recount_of)
        self.sheet_key = key
        po = self.conn.execute("SELECT po_number, branch_name FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
        self.po_id = po_id
        need = "Recount" if self.recount_of else receiving.need_label(self.conn, po_id)
        self.heading.setText(f"{po['po_number']}, {po['branch_name']}. {need}")
        start = "These products didn't match. " if need == receiving.AGAIN else ""
        self.prompt.setText(start + "Count every piece, including damaged ones, and type how many you received. Type 0 if a product didn't arrive. "
                            "In Damaged, type how many of those are damaged. If something arrived that isn't listed, add it. The expected quantity and the other count aren't shown on purpose.")
        fill_table(self.grid, [[i["item_code"], f"{i['description']} (not on PO)" if i["id"] < 0 else i["description"], i["size"], i["color"], i["uom"], "", "", "", _before(i["previous"])] for i in self.items], keys=[i["id"] for i in self.items])
        for r in range(self.grid.rowCount()):
            for c in (*range(C_PIECES), C_BEFORE):
                cell = self.grid.item(r, c)
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
        _tips(self.grid, range(C_PIECES))
        # Only their own earlier numbers, and only when they've counted this delivery before.
        self.grid.setColumnHidden(C_BEFORE, not any(i["previous"] for i in self.items))
        self._fit_soon()
        if any(i["previous"] for i in self.items):
            self.prompt.setText(self.prompt.text() + " Your own earlier count is shown on the right. Count again anyway: don't copy it.")
        self.save_btn.setEnabled(bool(self.items))
        # A recount of only the products that didn't match doesn't count boxes again.
        for w in (self.boxes, self.boxes_label):
            w.setVisible(bool(self.items) and need != receiving.AGAIN)
        self._show_waiting()
        if self.items:
            self.grid.setCurrentCell(0, C_PIECES)
            self.grid.setFocus()

    def add_extra_row(self):
        """A blank line for a product that arrived but isn't listed. All of it is typed in, and it doesn't join the catalog."""
        r = self.grid.rowCount()
        self.grid.setRowCount(r + 1)
        for c in range(self.grid.columnCount()):
            cell = QTableWidgetItem("PC" if c == 4 else "")
            if c in (4, C_BEFORE):
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.grid.setItem(r, c, cell)
        self.grid.setCurrentCell(r, 1)
        self.grid.setFocus()

    def _typed_rows(self) -> list[int]:
        return [r for r in range(self.grid.rowCount()) if any(self.grid.item(r, c) and self.grid.item(r, c).text().strip() for c in (C_PIECES, C_DAMAGED, C_REMARKS))]

    def _back_pressed(self):
        """Back to the list of POs to count. Anything typed and not saved is lost, so it asks first."""
        if self._typed_rows() and not confirm(self, "Go back", "Leave this count? What you've typed isn't saved and will be lost.", "Leave"):
            return
        self._clear()

    def _clear_pressed(self):
        """Empties the numbers typed on this sheet and stays on it."""
        if not self._typed_rows() or not confirm(self, "Start over", "Clear what you've typed on this sheet? Nothing has been saved yet.", "Clear"):
            return
        for r in range(self.grid.rowCount()):
            for c in (C_PIECES, C_DAMAGED, C_REMARKS):
                if self.grid.item(r, c):
                    self.grid.item(r, c).setText("")
        for r in range(self.grid.rowCount() - 1, len(self.items) - 1, -1):
            self.grid.removeRow(r)
        self.grid.setCurrentCell(0, C_PIECES)

    def _save_pressed(self):
        """A count is permanent, so it's checked once before saving. Anything that can't be saved, such as a blank or a number that isn't one, is shown first."""
        self._check_sheet()
        if confirm(self, "Save count", f"Save your count for {self.heading.text() or 'this PO'}? A saved count can't be changed, only counted again.", "Save count"):
            self.save_count()

    def _check_sheet(self) -> None:
        """Every cell that can't be saved turns red, and the message names each one, so nobody hunts for it. Nothing is saved here."""
        bad = QBrush(QColor(theme.current.RED_PALE))
        problems = []
        for r in range(self.grid.rowCount()):
            cells = {c: self.grid.item(r, c) for c in (1, C_PIECES, C_DAMAGED)}
            for c in (C_PIECES, C_DAMAGED):
                if cells[c] is not None:
                    cells[c].setBackground(QBrush())
            if r < len(self.items):
                description, uom = self.items[r]["description"], self.items[r]["uom"]
            else:
                if not any(self.grid.item(r, c) and self.grid.item(r, c).text().strip() for c in (0, 1, 2, 3, C_PIECES, C_DAMAGED, C_REMARKS)):
                    continue  # a blank line someone added and didn't use
                description, uom = (cells[1].text().strip() if cells[1] else "") or "the product you added", "PC"
            pieces_text = cells[C_PIECES].text().strip() if cells[C_PIECES] else ""
            counted = damaged = None
            if not pieces_text:
                problems.append(f"Type how many pieces of {description} you counted. Type 0 if it didn't arrive.")
                if cells[C_PIECES] is not None:
                    cells[C_PIECES].setBackground(bad)
            else:
                try:
                    counted = receiving.read_quantity(pieces_text, description, uom, "Pieces counted")
                except POError as exc:
                    problems.append(str(exc))
                    cells[C_PIECES].setBackground(bad)
            if cells[C_DAMAGED] is not None:
                try:
                    damaged = receiving.read_quantity(cells[C_DAMAGED].text().strip(), description, uom, "Damaged")
                except POError as exc:
                    problems.append(str(exc))
                    cells[C_DAMAGED].setBackground(bad)
            if counted is not None and damaged is not None and damaged > counted:
                problems.append(f"{description} has more damaged than counted. Count every piece, including the damaged ones.")
                cells[C_DAMAGED].setBackground(bad)
        if len(problems) == 1:
            raise POError(problems[0])
        if problems:
            shown = problems[:10] + ([f"And {len(problems) - 10} more."] if len(problems) > 10 else [])
            raise POError("Nothing was saved. Fix the cells marked in red:\n" + "\n".join(shown))

    def save_count(self):
        self._check_sheet()
        pieces, damaged, remarks, extras = {}, {}, {}, []
        for r, item in enumerate(self.items):
            pieces[item["id"]] = self.grid.item(r, C_PIECES).text()
            damaged[item["id"]] = self.grid.item(r, C_DAMAGED).text()
            remarks[item["id"]] = self.grid.item(r, C_REMARKS).text()
        for r in range(len(self.items), self.grid.rowCount()):
            text = [self.grid.item(r, c).text().strip() for c in (0, 1, 2, 3, C_PIECES, C_DAMAGED, C_REMARKS)]
            if any(text):
                extras.append(dict(zip(("item_code", "description", "size", "color", "pieces", "damaged", "remarks"), text)))
        boxes = self.boxes.text().strip() if self.boxes.isVisibleTo(self) else None
        result = receiving.save_count(self.conn, self.user, self.po_id, pieces, remarks, self.recount_of, damaged, extras, boxes=boxes or None, expect=self.sheet_key)
        self._clear()
        self.prompt.setText({receiving.WAITING: "Saved. Another receiver has to count it too, without seeing yours.",
                             receiving.APPROVED: "Both counts match, so the delivery is recorded.",
                             receiving.MISMATCH: f"The two counts don't match on {result['mismatched']} products. Two people need to count those again."}[result["outcome"]])
        self.refresh()
        self.changed.emit()

    def recount(self):
        row = self._selected()
        if not row:
            return
        self.number.setText(row["po_number"])
        self.picker.hide()
        self.recount_of = row["id"]
        self._load(row["po_id"])
        self.tabs.setCurrentIndex(0)


class ReceivedPage(QWidget):
    """The home office's view: what still needs checking, what arrived exactly as ordered, every person's count, and how each receiver is doing."""
    changed = Signal()
    BY_RECEIVER = "BY_RECEIVER"
    TABS = {receiving.TO_CHECK: "To check", receiving.COMPLETED: "Completed delivery", BY_RECEIVER: "By receiver"}

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.tab = receiving.TO_CHECK
        self.rows, self.history_rows = [], []
        root, top = page_layout(self, "Received")
        top.addWidget(label("Only the home office sees these differences", "Muted"))
        self.columns_btn = button("Columns", lambda: (self.receivers_columns if self.tab == self.BY_RECEIVER else self.table_columns).open_under(self.columns_btn))
        top.addWidget(self.columns_btn)
        self.tabs = register_tabs(self.TABS, self._set_tab)
        root.addWidget(self.tabs)
        self.views = QStackedWidget()
        root.addWidget(self.views, 1)

        view = QWidget()
        col = QVBoxLayout(view)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(10)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search PO, OF, supplier, code or branch")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(300)
        # The list is worked out again once typing pauses, not on every key.
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(300)
        self.search_timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(self._search_typed)
        bar.addWidget(self.search)
        self.tab_note = label("", "Muted")
        bar.addWidget(self.tab_note)
        bar.addStretch(1)
        self.dm_btn = button("Make debit memo", self.make_debit_memo, "Primary")
        self.dm_btn.setEnabled(False)
        self.cs_btn = button("Make claim stub", self.make_claim_stub, "Primary")
        self.cs_btn.setEnabled(False)
        if user.can("dm.issue"):
            bar.addWidget(self.cs_btn)
            bar.addWidget(self.dm_btn)
        # Counting again is the home office's call: once for a matched delivery, or one more round after two that didn't match.
        self.reopen_btn = button("Reopen for recount", self.reopen)
        self.reopen_btn.hide()
        if user.can("receiving.reopen"):
            bar.addWidget(self.reopen_btn)
        col.addLayout(bar)
        # One press for a branch. Its count says how many are on this tab.
        self.branch_code = None
        self.branch_buttons = {}
        self.branch_group = QButtonGroup(self)
        strip = QHBoxLayout()
        strip.setSpacing(6)
        for code, name in [(None, "All branches")] + [(b["code"], b["name"]) for b in masters.list_branches(conn, active_only=True, branches_only=True)]:
            b = QPushButton(name)
            b.setObjectName("Count")
            b.setCheckable(True)
            b.setProperty("branch_name", name)
            b.clicked.connect(lambda _=False, code=code: self._set_branch(code))
            self.branch_group.addButton(b)
            strip.addWidget(b)
            self.branch_buttons[code] = b
        self.branch_buttons[None].setChecked(True)
        strip.addStretch(1)
        col.addLayout(strip)
        # "DM / CS / item pass" is on two lines, so its column needs only the width of a document number (AUD-006).
        self.table = make_table(["PO number", "OF number", "Branch", "Supplier", "Ordered", "Received", "Difference", "Damaged", "DM / CS /\nitem pass", "Counting", "Status"], numeric=(4, 5, 6, 7), stretch=3)
        set_widths(self.table, {0: 102, 1: 108, 2: 70, 4: 60, 5: 66, 6: 74, 7: 66, 8: 122, 9: 176, 10: 190})
        # Status says what to do next, so it's wide enough for every status. Counting, Status and the documents go onto a second line in a row
        # where they don't fit on one, so on a 1366 laptop nothing there is cut off. Whatever else doesn't fit is on hover, headings included.
        self.table.setWordWrap(True)
        for c in (8, 9, 10):
            self.table.horizontalHeaderItem(c).setToolTip(" ".join(self.table.horizontalHeaderItem(c).text().split()))
        self.table.horizontalHeader().sectionResized.connect(lambda col, *_: col in _WRAPPED and _fit_rows(self.table, _WRAPPED))
        # Sorting moves the rows, so the taller ones are found again once the sort is done.
        self.table.horizontalHeader().sortIndicatorChanged.connect(lambda *_: QTimer.singleShot(0, self, lambda: _fit_rows(self.table, _WRAPPED)))
        self.table_columns = smart_columns(self.table, f"received-{user.username}")
        # Supplier takes the spare room. If Status stretched too, the two would share it and push Status past the right edge at 1366.
        # Only when Supplier is hidden does the last column take the room instead.
        self.table_columns.columns_changed.connect(self._one_stretch)
        self._one_stretch()
        self.table.itemSelectionChanged.connect(self._po_selected)
        col.addWidget(self.table, 3)
        # Completed delivery grows for good, so it shows a page at a time, newest first. To check is work to do and always shows every PO.
        self.pager = Pager(self._show_more)
        col.addWidget(self.pager)
        self.empty = label("", "Empty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.hide()
        col.addWidget(self.empty)
        self.transit = label("", "Muted")
        self.transit.setWordWrap(True)
        col.addWidget(self.transit)
        self.lines = make_table(["SKU", "Product", "Size", "Color", "UOM", "Ordered", "Left warehouse", "Received", "Difference", "Damaged"], numeric=(5, 6, 7, 8, 9), stretch=1)
        set_widths(self.lines, {0: 120, 2: 70, 3: 90, 4: 70, 5: 70, 6: 76, 7: 70, 8: 76, 9: 70})
        col.addWidget(self.lines, 2)
        self.history = make_table(["Delivery", "Pieces", "Damaged", "Status", "Counted by", "Counted at", "Matched with", "Matched at", "Type"], numeric=(0, 1, 2), stretch=8)
        set_widths(self.history, {0: 62, 1: 62, 2: 70, 3: 170, 4: 84, 5: 130, 6: 96, 7: 130})
        self.history.itemSelectionChanged.connect(self._count_selected)
        col.addWidget(self.history, 2)
        self.detail = make_table(["SKU", "Product", "Size", "Color", "UOM", "This person counted", "Damaged", "Remarks"], numeric=(5, 6), stretch=1)
        set_widths(self.detail, {0: 120, 2: 70, 3: 90, 4: 70, 5: 130, 6: 70, 7: 180})
        self.detail.hide()
        col.addWidget(self.detail, 2)
        self.views.addWidget(view)

        self.receivers = make_table(["Receiver", "Username", "Branch", "Counts made", "Didn't match", "Products wrong", "Replaced by a recount", "Recounts done"], numeric=(3, 4, 5, 6, 7), stretch=0)
        set_widths(self.receivers, {1: 110, 2: 100, 3: 90, 4: 90, 5: 100, 6: 150, 7: 100})
        self.receivers_columns = smart_columns(self.receivers, f"by-receiver-{user.username}")
        people_view = QWidget()
        people_col = QVBoxLayout(people_view)
        people_col.setContentsMargins(0, 0, 0, 0)
        people_col.addWidget(self.receivers, 1)
        self.receivers_pager = Pager(self._show_more_people)
        people_col.addWidget(self.receivers_pager)
        self.views.addWidget(people_view)
        self.refresh()

    def _one_stretch(self) -> None:
        self.table.horizontalHeader().setStretchLastSection(self.table.isColumnHidden(3))

    def _set_tab(self, tab):
        self.tab = tab
        self.views.setCurrentIndex(1 if tab == self.BY_RECEIVER else 0)
        self.table.clearSelection()
        self.table.setCurrentItem(None)
        self.pager.reset()
        self.receivers_pager.reset()
        self.refresh()

    def _search_typed(self, *_):
        self.pager.reset()
        self.search_timer.start()

    def _show_more(self):
        with keep_place(self.table):
            self.refresh()

    def _show_more_people(self):
        with keep_place(self.receivers):
            self.refresh()

    def refresh(self, *_):
        selected = selected_key(self.table)
        completed = self.tab == receiving.COMPLETED
        # Search looks through every PO in the database. Only Completed delivery is cut to the newest ones, and only what's shown is worked out in full.
        found = receiving.received_list(self.conn, self.user, self.search.text(), receiving.COMPLETED if completed else receiving.TO_CHECK, self.branch_code,
                                        self.pager.limit if completed else None)
        counts = found["counts"]
        show_tab_counts(self.tabs, {k: v for k, v in self.TABS.items() if k in counts} | {self.BY_RECEIVER: self.TABS[self.BY_RECEIVER]}, {**counts, self.BY_RECEIVER: ""})
        self.tabs.setTabText(2, self.TABS[self.BY_RECEIVER])
        self.rows = found["rows"]
        if completed:
            self.pager.show_counts(len(self.rows), found["total"])
            self.rows = self.rows[self.pager.offset:]
        else:
            self.pager.setVisible(False)
        for code, b in self.branch_buttons.items():
            b.setText(f"{b.property('branch_name')}  {found['branches'].get(code, 0)}")
        self.tab_note.setText("Arrived exactly as ordered, or settled" if completed else "Being counted, or short, damaged or excess and not settled")
        self.empty.setText("No delivery has been completed yet." if completed else "Nothing to check.")
        fill_table(self.table, [[r["po_number"], r["of_number"] or "", r["branch_name"], r["supplier"], fmt_qty(r["ordered_m"]), fmt_qty(r["received_m"]), receiving.signed_qty(r["difference_m"]),
                                 _damaged(r), ", ".join(x for x in (r["debit_memos"], r["claim_stubs"], r["item_passes"]) if x), r["progress"], _status(r) if completed else "Excess: branch to print item pass" if r["excess_left_m"] else orders.STATUS_LABELS[r["status"]]] for r in self.rows], keys=[r["id"] for r in self.rows])
        _tips(self.table, (2, 3, 8, 9, 10))
        _fit_rows(self.table, _WRAPPED)
        for i, r in enumerate(self.rows):
            if r["id"] == selected:
                self.table.selectRow(i)
        self.table.setVisible(bool(self.rows))
        self.empty.setVisible(not self.rows)
        self._po_selected()
        people = self.receivers_pager.take(receiving.by_receiver(self.conn, self.user))
        fill_table(self.receivers, [[p["full_name"], p["username"], p["branch"] or "", p["counted"], p["mismatched"], p["wrong"], p["replaced"], p["recounts"]] for p in people])
        show_people(self.receivers, 0, auth.chips(self.conn), [p["id"] for p in people], named=True)

    def _set_branch(self, code):
        self.branch_code = code
        self.pager.reset()
        self.refresh()

    def reopen(self):
        row = next((r for r in self.rows if r["id"] == selected_key(self.table)), None)
        if not row:
            return
        if self.reopen_btn.text() == "Allow one more count":
            prompt = (f"The two counts of {row['po_number']} didn't match twice, so the branch stopped. Allow one more round, by two people?\n\n"
                      "Write what you checked, for example: called the branch, the box was opened late.")
        else:
            prompt = (f"Both counts of {row['po_number']} matched. Reopen it so two receivers count it again? This can be done only once for a delivery.\n\n"
                      "Write why, for example: 4 pieces found in another box.")
        reason = ask_text(self, self.reopen_btn.text(), prompt)
        if reason is None:
            return
        receiving.reopen(self.conn, self.user, row["id"], reason)
        self.refresh()
        self.changed.emit()

    def make_claim_stub(self):
        row = next((r for r in self.rows if r["id"] == selected_key(self.table)), None)
        if row and ClaimStubDialog(self, self.conn, self.user, row).exec():
            self.refresh()
            self.changed.emit()

    def make_debit_memo(self):
        row = next((r for r in self.rows if r["id"] == selected_key(self.table)), None)
        if row and DebitMemoDialog(self, self.conn, self.user, row).exec():
            self.refresh()
            self.changed.emit()

    def _po_selected(self):
        key = selected_key(self.table)
        show_if(self.dm_btn, bool(key) and dm_service.can_issue(self.conn, key))
        show_if(self.cs_btn, bool(key) and cs_service.can_issue(self.conn, key))
        reopen = receiving.reopen_label(self.conn, key) if key and self.user.can("receiving.reopen") else ""
        self.reopen_btn.setText(reopen or "Reopen for recount")
        show_if(self.reopen_btn, bool(reopen))
        lines = receiving.po_lines(self.conn, self.user, key) if key else []
        fill_table(self.lines, [[i["item_code"], i["description"], i["size"], i["color"], i["uom"], fmt_qty(i["ordered_m"]), fmt_qty(i["left_central_m"]) if i.get("left_central_m") is not None else "",
                                 fmt_qty(i["received_m"]), receiving.signed_qty(i["difference_m"]), _damaged(i)] for i in lines])
        # What the Central Warehouse counted is what the supplier delivered. Less reaching the branch is lost on the way: the home office looks into it, the supplier isn't charged.
        self.lines.setColumnHidden(6, not any(i.get("left_central_m") is not None for i in lines))
        lost = sum(i.get("transit_m") or 0 for i in lines)
        boxes = receiving.boxes_check(self.conn, key) if key else None
        notes = []
        if lost:
            notes.append(f"{fmt_qty(lost)} pieces left the warehouse but didn't reach the branch. That's a loss on the way for the home office to look into, not a shortage charged to the supplier.")
        if boxes and boxes["missing"]:
            notes.append(f"The warehouse shipped {boxes['sent']} boxes and the branch says {boxes['arrived']} arrived: {boxes['missing']} missing.")
        if boxes and boxes["disagree"]:
            notes.append("The two receivers typed different box counts for delivery " + ", ".join(str(d) for d in boxes["disagree"]) + ".")
        self.transit.setText(" ".join(notes))
        self.transit.setVisible(bool(notes))
        self.history_rows = receiving.po_history(self.conn, self.user, key) if key else []
        fill_table(self.history, [[h["delivery_no"], fmt_qty(h["pieces_m"]), _damaged(h), receiving.COUNT_LABELS[h["status"]], h["counted_name"], fmt_datetime(h["counted_at"]),
                                   h["approved_name"] or "", fmt_datetime(h["approved_at"]), _kind(h)] for h in self.history_rows], keys=[h["id"] for h in self.history_rows])
        everyone = auth.chips(self.conn)
        show_people(self.history, 4, everyone, [h["counted_by"] for h in self.history_rows])
        show_people(self.history, 6, everyone, [h["approved_by"] for h in self.history_rows])
        self._count_selected()

    def _count_selected(self):
        key = selected_key(self.history)
        lines = receiving.review_lines(self.conn, self.user, key) if key else []
        fill_table(self.detail, [[i["item_code"], i["description"], i["size"], i["color"], i["uom"], fmt_qty(i["pieces_m"]), _damaged(i), i["remarks"]] for i in lines])
        self.detail.setVisible(bool(key))
