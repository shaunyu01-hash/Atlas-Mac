"""Claim stubs: approving one from a delivery, and following the goods until they're collected or forfeited."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QCompleter, QDialog, QDialogButtonBox, QHBoxLayout, QLineEdit, QPushButton, QSplitter, QStyledItemDelegate, QTabBar,
                               QVBoxLayout, QWidget)

from .. import claim_stubs as cs_service
from ..money import fmt_peso, fmt_plain, fmt_qty, parse_qty, per_piece
from . import theme
from .common import month_picker, Pager, keep_place, show_if, smart_columns, ask_text, button, confirm, confirm_reprint, error, fill_table, guarded, fmt_date, fmt_datetime, label, make_table, open_for_printing, selected_key, set_widths, show_tab_counts, date_edit, date_value
from .debit_memo_page import C_PIECES as DM_PIECES, DebitMemoDialog
from .pages import page_layout


def editable_tint() -> QColor:
    """The background that says a cell can be typed in: the theme's EDITABLE color if it has one, else its pale accent."""
    p = theme.current
    return QColor(getattr(p, "EDITABLE", None) or p.ACCENT_PALE)


class TintDelegate(QStyledItemDelegate):
    """Paints a cell's own background first. The app's table style leaves it out, so without this a tinted cell shows plain."""

    def paint(self, painter, option, index):
        brush = index.data(Qt.ItemDataRole.BackgroundRole)
        if brush is not None:
            painter.fillRect(option.rect, brush)
        super().paint(painter, option, index)


def show_tints(table, column: int) -> None:
    if not isinstance(table.itemDelegateForColumn(column), TintDelegate):
        table.setItemDelegateForColumn(column, TintDelegate(table))


class FitTabs(QTabBar):
    """Tabs that always fit, without scroll arrows: when the window is narrow, only the long names shorten, in the middle so the count stays; short ones never do."""
    LONGEST_SHORT, SHORTEST_LONG = 130, 100

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setUsesScrollButtons(False)
        self.setElideMode(Qt.TextElideMode.ElideMiddle)

    def minimumTabSizeHint(self, index):  # noqa: N802 (Qt's name)
        hint = self.tabSizeHint(index)
        # The open tab is shown in bold and always in full.
        if index == self.currentIndex() or hint.width() <= self.LONGEST_SHORT:
            return hint
        return QSize(self.SHORTEST_LONG, hint.height())


class ClaimStubDialog(DebitMemoDialog):
    """The same screen as a debit memo, for damaged pieces."""
    service = cs_service
    noun = "claim stub"
    number_key = "cs_number"
    # A stub prints when it's needed, from the Claim stubs page, not the moment it's approved.
    print_on_approve = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        buttons = {b.text(): b for b in self.findChildren(QPushButton)}
        self.approve_btn = getattr(self, "approve_btn", None) or buttons.get("Approve and print") or buttons.get("Approve")
        self.cancel_btn = buttons.get("Cancel")
        # Nothing prints on approving, so the button says only what it does.
        if self.approve_btn is not None:
            if not self.print_on_approve:
                self.approve_btn.setText("Approve")
            # Enter in the remarks asks to approve, instead of closing the screen and losing what was typed.
            self.approve_btn.setAutoDefault(True)
            self.approve_btn.setDefault(True)
        if self.cancel_btn is not None:
            self.cancel_btn.setAutoDefault(False)
            self.cancel_btn.setDefault(False)
        # The pieces are the one thing that can be changed here, so they look it.
        tint = editable_tint()
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            self.table.item(r, DM_PIECES).setBackground(tint)
        self.table.blockSignals(False)
        show_tints(self.table, DM_PIECES)


class CountGoodsDialog(QDialog):
    """The warehouse counts goods coming back before anyone collects them. Every line needs a number, 0 if none came."""

    def __init__(self, parent, number: str, lines: list[dict]):
        super().__init__(parent)
        self.setWindowTitle(f"Count the goods on {number}")
        self.lines = lines
        self.counts: dict[int, str] = {}
        col = QVBoxLayout(self)
        col.addWidget(label(f"Count what came back on {number}, line by line. Type 0 for a line that didn't come. The count stays on record.", "Muted"))
        self.table = make_table(["SKU", "Product", "Size", "Color", "On the paper", "Counted"], numeric=(4, 5), stretch=1)
        set_widths(self.table, {0: 110, 2: 60, 3: 80, 4: 100, 5: 90})
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
        fill_table(self.table, [[i["item_code"], i["description"], i["size"], i["color"], fmt_qty(i["pieces_m"]), ""] for i in lines], keys=[i["id"] for i in lines])
        tint = editable_tint()
        for r in range(self.table.rowCount()):
            for c in range(self.table.columnCount()):
                item = self.table.item(r, c)
                if c == 5:
                    item.setBackground(tint)
                else:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        show_tints(self.table, 5)
        col.addWidget(self.table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Save count")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        col.addWidget(buttons)
        self.resize(720, 360)

    def _save(self):
        self.counts = {self.table.item(r, 0).data(Qt.ItemDataRole.UserRole): self.table.item(r, 5).text() for r in range(self.table.rowCount())}
        missing = [self.lines[r]["description"] for r in range(self.table.rowCount()) if not self.table.item(r, 5).text().strip()]
        if missing:
            error(self, f"Type the pieces counted for {missing[0]}, 0 if none came.")
            return
        self.accept()


def count_goods(parent, number: str, lines: list[dict]) -> dict[int, str] | None:
    """{line id: pieces counted}, or None if the person backed out."""
    dialog = CountGoodsDialog(parent, number, lines)
    return dialog.counts if dialog.exec() else None


class LateDamagePanel(QWidget):
    """Damage found after the delivery was settled, often after the check. Type part of the OF or PO number and pick it, narrow the products by typing,
    and type the damaged pieces: the amount works out as you type. It makes a claim stub per branch PO, and if the order is already paid,
    what it takes off goes on the supplier's next check."""
    done = Signal()
    closed = Signal()
    C_PIECES, C_AMOUNT = 4, 5

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.lines, self.made, self.loaded = [], [], ""
        self.setMinimumWidth(420)
        col = QVBoxLayout(self)
        col.setContentsMargins(10, 0, 0, 0)
        head = QHBoxLayout()
        head.addWidget(label("Damage found later", "PanelTitle"))
        head.addStretch(1)
        head.addWidget(button("Close", self.closed.emit, lock=False))
        col.addLayout(head)
        find = QHBoxLayout()
        # Typing part of a number, such as 13, offers the OF and PO numbers that match. Picking one loads it.
        self.number = QLineEdit()
        self.number.setPlaceholderText("OF or PO number, or part of it")
        self.number.setClearButtonEnabled(True)
        self.completer = QCompleter([], self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.completer.activated.connect(lambda text: guarded(self, self.find, text))
        self.number.setCompleter(self.completer)
        self.number.returnPressed.connect(lambda: guarded(self, self.find))
        find.addWidget(self.number, 1)
        find.addWidget(button("Find", lambda: guarded(self, self.find)))
        col.addLayout(find)
        # Every product on the order is listed. Typing part of the SKU or name narrows it, instead of picking from a long dropdown.
        self.narrow = QLineEdit()
        self.narrow.setPlaceholderText("Narrow the products: type part of the SKU or name")
        self.narrow.setClearButtonEnabled(True)
        self.narrow.textChanged.connect(self._filter)
        col.addWidget(self.narrow)
        # Narrow, so the pieces and the amount stay in view: the cost per piece is in the Amount's tip.
        self.table = make_table(["Branch", "SKU", "Product", "Up to", "Damaged", "Amount"], numeric=(3, 4, 5), stretch=2)
        set_widths(self.table, {0: 72, 1: 56, 3: 56, 4: 72, 5: 84})
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
        self.table.setProperty("no_sort", True)
        show_tints(self.table, self.C_PIECES)
        self.table.itemChanged.connect(self._typed)
        col.addWidget(self.table, 1)
        self.total = label("", "PanelTitle")
        self.total.setWordWrap(True)
        col.addWidget(self.total)
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("How was it found? For example: found torn when the branch unpacked it.")
        col.addWidget(self.reason)
        row = QHBoxLayout()
        row.addStretch(1)
        self.make_btn = button("Make claim stub", lambda: guarded(self, self.save), "Primary")
        row.addWidget(self.make_btn)
        col.addLayout(row)

    def start(self):
        """A clean start each time it opens, with the numbers to choose from brought up to date."""
        self.completer.model().setStringList(cs_service.late_numbers(self.conn, self.user))
        for w in (self.number, self.reason, self.narrow):
            w.clear()
        self.lines, self.loaded = [], ""
        self.table.setRowCount(0)
        self._typed()
        self.number.setFocus()

    def find(self, text=None):
        number = (text or self.number.text()).strip()
        if text:
            self.number.setText(text)
        self.lines = cs_service.late_lines(self.conn, self.user, number)
        self.loaded = number
        self.table.blockSignals(True)
        fill_table(self.table, [[l["branch_name"], l["item_code"], ", ".join(x for x in (l["description"], l["size"], l["color"]) if x), fmt_qty(l["room_m"]), "", ""]
                                for l in self.lines], keys=[l["po_item_id"] for l in self.lines])
        tint = editable_tint()
        for r, line in enumerate(self.lines):
            self.table.item(r, 0).setToolTip(f"{line['po_number']}, received {fmt_qty(line['received_m'])}")
            # The full product, for when the column is too narrow to show it.
            self.table.item(r, 2).setToolTip(self.table.item(r, 2).text())
            self.table.item(r, self.C_AMOUNT).setToolTip(f"{fmt_plain(per_piece(line['net_unit_c'], line['pieces_per_unit']))} a piece, after any discount")
            for c in range(self.table.columnCount()):
                item = self.table.item(r, c)
                if c == self.C_PIECES:
                    item.setBackground(tint)
                else:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.blockSignals(False)
        self._filter()
        self._typed()

    def _filter(self, *_):
        words = self.narrow.text().lower().split()
        for r, line in enumerate(self.lines):
            text = f"{line['item_code']} {line['description']} {line['size']} {line['color']}".lower()
            self.table.setRowHidden(r, not all(w in text for w in words))

    def _typed(self, *_):
        """Each row's amount and the stub's total, at the PO's cost after any discount, as the pieces are typed."""
        total, wrong = 0, ""
        tint, bad = editable_tint(), QColor(theme.current.RED_PALE)
        self.table.blockSignals(True)
        for r, line in enumerate(self.lines):
            text = self.table.item(r, self.C_PIECES).text().strip()
            amount, problem = "", ""
            if text:
                try:
                    wanted = parse_qty(text, "PC")
                    if wanted > line["room_m"]:
                        problem = f"{line['branch_name']}, {line['description']}: only {fmt_qty(line['room_m'])} pieces can be damaged."
                    worth = cs_service.amount_c(wanted, line["cost_c"], line["pieces_per_unit"], line["share"])
                    total += worth
                    amount = fmt_plain(worth)
                except ValueError:
                    problem = f"{line['branch_name']}, {line['description']}: {text} isn't a number of pieces."
            wrong = wrong or problem
            # The cell that's wrong turns red, so it's easy to find in a long list.
            self.table.item(r, self.C_PIECES).setBackground(bad if problem else tint)
            self.table.item(r, self.C_AMOUNT).setText(amount)
        self.table.blockSignals(False)
        self.total.setText(wrong or (f"Claim stub total {fmt_peso(total)}" if total else ""))
        self.total.setObjectName("Error" if wrong else "PanelTitle")
        self.total.style().unpolish(self.total)
        self.total.style().polish(self.total)
        # Nothing can be made while a figure is wrong.
        self.make_btn.setEnabled(not wrong)

    def _pieces(self) -> dict:
        return {self.table.item(r, 0).data(Qt.ItemDataRole.UserRole): self.table.item(r, self.C_PIECES).text() for r in range(self.table.rowCount())}

    def _ask_first(self, pieces: dict) -> bool:
        """Asks before numbering anything, naming the order, how many stubs, the total and, if it's paid, that it comes off the next check.
        Anything not ready to save goes straight to the save, which says what's missing."""
        chosen = {}
        try:
            for line in self.lines:
                text = str(pieces.get(line["po_item_id"], "")).strip()
                wanted = parse_qty(text, "PC") if text else 0
                if wanted > line["room_m"]:
                    return True
                if wanted:
                    chosen.setdefault(line["po_id"], [line["po_number"], 0])[1] += cs_service.amount_c(wanted, line["cost_c"], line["pieces_per_unit"], line["share"])
        except ValueError:
            return True
        if not chosen or not self.reason.text().strip():
            return True
        number = self.loaded or self.number.text().strip()
        count = len(chosen)
        total = sum(amount for _, amount in chosen.values())
        pos = [n for n, _ in chosen.values()]
        on = "" if pos == [number.upper()] else f" on {number.upper()}"
        if count == 1:
            message = f"Make a claim stub for {pos[0]}{on}, {fmt_peso(total)}? It gets a permanent number."
        else:
            message = f"Make {count} claim stubs{on}, one for each branch PO ({', '.join(pos)}), {fmt_peso(total)} in all? Each gets a permanent number."
        if any(cs_service.is_paid(self.conn, po_id) for po_id in chosen):
            message += f" The order is already paid, so this comes off the supplier's next check, and {'it' if count == 1 else 'they'} can't be cancelled."
        return confirm(self, "Make claim stub", message, "Make claim stub")

    def save(self):
        pieces = self._pieces()
        if not self._ask_first(pieces):
            return
        self.made = cs_service.issue_late(self.conn, self.user, self.loaded or self.number.text(), pieces, self.reason.text())
        self.start()
        self.done.emit()


class ClaimStubsPage(QWidget):
    """Every claim stub, by where its goods are. Approving one starts from a delivery on the Received page, so a stub always traces back to agreed counts."""
    changed = Signal()

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.tab = cs_service.ISSUED
        self.rows = []
        self.labels = cs_service.status_labels(conn)
        self.can_edit = user.can("dm.issue")
        root, top = page_layout(self, "Claim stubs")
        if self.can_edit:
            top.addWidget(label("Make one from a damaged PO on Received, To check", "Muted"))
            # Damage found after the delivery was settled, even after the check: typed here, and taken off the supplier's next check.
            top.addWidget(button("Damage found later", self._late))
        # All lists every stub whatever it's at, for looking one up by number, supplier, PO or date. Branch reports are damage the branches sent, waiting for approval.
        self.can_approve = user.can("cs.approve_request")
        self.labels = {**self.labels, ALL: "All", REPORTS: "Branch reports"}
        # On a narrow window the longest tab names shorten in the middle, keeping their counts, so every tab stays in view without arrows. The tip has the full name.
        self.tabs = FitTabs()
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        for key, text in self.labels.items():
            self.tabs.setTabData(self.tabs.addTab(text), key)
        self.tabs.currentChanged.connect(lambda i: self._set_tab(self.tabs.tabData(i)))
        root.addWidget(self.tabs)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search stub, PO, OF, supplier or external number")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(320)
        self.search.textChanged.connect(self._filter_changed)
        bar.addWidget(self.search)
        # From and To narrow every tab by the paper's date, so an older one is quick to find.
        self.date_from, self.date_to = date_edit(optional=True), date_edit(optional=True)
        for text, w in (("From", self.date_from), ("To", self.date_to)):
            bar.addWidget(label(text, "Muted"))
            bar.addWidget(w)
            w.dateChanged.connect(self._filter_changed)
        self.month = month_picker(self.date_from, self.date_to)
        bar.addWidget(self.month)
        bar.addWidget(button("Clear filters", self.clear_filters, lock=False))
        bar.addStretch(1)
        root.addLayout(bar)
        # The hint shares a second row with the printing buttons, so the filters never push the window past a laptop's width.
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.prompt = label("", "Muted")
        self.prompt.setWordWrap(True)
        actions.addWidget(self.prompt, 1)
        self.ticked: set[int] = set()
        self.select_btn = button("Select all not printed", self.select_unprinted, lock=False)
        self.print_ticked_btn = button("Print ticked", self.print_ticked, "Primary")
        self.can_print = self.can_edit or user.can("cs.approve_request")
        if self.can_print:
            actions.addWidget(self.select_btn)
            actions.addWidget(self.print_ticked_btn)
        root.addLayout(actions)
        self.list_side = QWidget()
        left = QVBoxLayout(self.list_side)
        left.setContentsMargins(0, 0, 0, 0)
        self.table = make_table(["Claim stub", "Date", "Supplier", "PO number", "Branch", "Pieces", "Amount", "External CS", "Approved by", "At warehouse", "Days left", "Printed"], numeric=(5, 6, 10), stretch=2)
        # Wide enough for the number beside its tick box, and for "Waiting for payment".
        set_widths(self.table, {0: 140, 1: 92, 3: 110, 4: 80, 5: 56, 6: 88, 7: 78, 8: 110, 9: 92, 10: 136, 11: 64})
        chooser = smart_columns(self.table, f"claim-stubs-{user.username}")
        columns_btn = button("Columns", lambda: chooser.open_under(columns_btn))
        actions.addWidget(columns_btn)
        self.table.itemSelectionChanged.connect(self._selected)
        self.table.itemChanged.connect(self._ticked_changed)
        left.addWidget(self.table, 3)
        # Collected, Forfeited, Cancelled and All grow for good, so they show a page at a time, newest first. Stubs coming back or at the warehouse
        # are work still to do, so those two tabs always show every one.
        self.pager = Pager(self._show_more)
        left.addWidget(self.pager)
        # Branch reports: the damage a branch sent, with what it comes to. Approving makes the claim stub as reported, and rejecting needs a reason.
        self.reports_view = QWidget()
        reports_col = QVBoxLayout(self.reports_view)
        reports_col.setContentsMargins(0, 0, 0, 0)
        self.reports = make_table(["Sent", "Branch", "PO number", "Supplier", "Products", "Pieces", "Amount", "Reported by", "How it was found", "Status"], numeric=(5, 6), stretch=8)
        set_widths(self.reports, {0: 130, 1: 90, 2: 120, 3: 100, 4: 180, 5: 60, 6: 90, 7: 120, 9: 170})
        reports_col.addWidget(self.reports, 1)
        # Every report waiting for approval always shows. Those already approved or rejected show a page at a time, newest first.
        self.reports_pager = Pager(self._show_more_reports)
        reports_col.addWidget(self.reports_pager)
        decide = QHBoxLayout()
        self.reports_note = label("", "Muted")
        self.reports_note.setWordWrap(True)
        decide.addWidget(self.reports_note, 1)
        self.reject_report_btn = button("Reject", self.reject_report)
        self.approve_report_btn = button("Approve: make the claim stub", self.approve_report, "Primary")
        if self.can_approve:
            decide.addWidget(self.reject_report_btn)
            decide.addWidget(self.approve_report_btn)
        reports_col.addLayout(decide)
        self.reports.itemSelectionChanged.connect(self._report_selected)
        self.reports_view.hide()
        self.empty = label("No claim stubs here.", "Empty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.hide()
        left.addWidget(self.empty)
        row = QHBoxLayout()
        self.note = label("", "Muted")
        self.note.setWordWrap(True)
        row.addWidget(self.note, 1)
        self.open_btn = button("Print again", self.print_again)
        row.addWidget(self.open_btn)
        self.external = QLineEdit()
        self.external.setPlaceholderText("External CS number")
        self.external.setMaximumWidth(140)
        self.external_btn = button("Save external number", self.save_external)
        self.arrived_btn = button("Goods arrived", self.arrived, "Primary")
        self.collected_btn = button("Collected", self.collected, "Primary")
        # Forfeiting can't be undone, so it looks like it, set apart from the routine buttons.
        self.forfeit_btn = button("Forfeit, for sale", self.forfeit, "Danger")
        self.cancel_btn = button("Cancel stub", self.cancel, "Danger")
        if self.can_edit:
            for w in (self.external, self.external_btn):
                row.addWidget(w)
        # The supplier collects at the warehouse, so the warehouse can record it. The office still can, for now.
        if user.can("goods.release"):
            row.addWidget(self.collected_btn)
        if self.can_edit:
            row.addSpacing(24)
            row.addWidget(self.forfeit_btn)
        # The warehouse received the goods, so only the warehouse says they arrived.
        if user.can("warehouse.receive"):
            row.addWidget(self.arrived_btn)
        # Cancelling gives the supplier back money for damaged goods, so only management sees it.
        if user.can("dm.cancel"):
            row.addSpacing(24)
            row.addWidget(self.cancel_btn)
        left.addLayout(row)
        self.lines = make_table(["SKU", "Product", "Size", "Color", "Pieces", "Cost per pc", "Amount", "Counted at warehouse"], numeric=(4, 5, 6, 7), stretch=1)
        set_widths(self.lines, {0: 120, 2: 70, 3: 90, 4: 70, 5: 90, 6: 100, 7: 120})
        left.addWidget(self.lines, 2)
        # Damage found later opens as a panel on the right, so the list stays in view.
        self.late = LateDamagePanel(conn, user)
        self.late.hide()
        self.late.closed.connect(self.late.hide)
        self.late.done.connect(self._done)
        self.split = split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.list_side)
        split.addWidget(self.reports_view)
        split.addWidget(self.late)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 1)
        split.setChildrenCollapsible(False)
        root.addWidget(split, 1)
        self.refresh()

    TO_DO = (cs_service.ISSUED, cs_service.AT_WAREHOUSE)  # tabs that always show every stub

    def _set_tab(self, tab):
        self.tab = tab
        self.table.clearSelection()
        self.table.setCurrentItem(None)
        self.pager.reset()
        self.reports_pager.reset()
        self.refresh()

    def _filter_changed(self, *_):
        self.pager.reset()
        self.reports_pager.reset()
        self.refresh()

    def _show_more(self):
        with keep_place(self.table):
            self.refresh()

    def _show_more_reports(self):
        with keep_place(self.reports):
            self.refresh()

    def clear_filters(self):
        """Search and dates back to blank, so every paper on the tab shows again."""
        from .common import set_date
        for w in (self.search, self.date_from, self.date_to):
            w.blockSignals(True)
        self.search.clear()
        set_date(self.date_from, None)
        set_date(self.date_to, None)
        for w in (self.search, self.date_from, self.date_to):
            w.blockSignals(False)
        self.pager.reset()
        self.reports_pager.reset()
        self.refresh()

    def refresh(self, *_):
        selected = selected_key(self.table)
        self.labels = {**cs_service.status_labels(self.conn), ALL: "All", REPORTS: "Branch reports"}
        # Every stub the search and dates find, not only the newest 2,000, so an old one is never hidden. Only the list on screen is cut.
        everything = _in_dates(cs_service.search(self.conn, self.user, self.search.text(), limit=EVERY), "cs_date", self.date_from, self.date_to)
        # Branch reports follow the same search and dates, by the day they were sent.
        self.report_rows = _in_dates(_matching(cs_service.reports(self.conn, self.user), self.search.text()), "requested_at", self.date_from, self.date_to)
        show_tab_counts(self.tabs, self.labels, {**{s: sum(r["status"] == s for r in everything) for s in self.labels}, ALL: len(everything),
                                                 REPORTS: sum(r["status"] == cs_service.PENDING for r in self.report_rows)})
        for i in range(self.tabs.count()):
            self.tabs.setTabToolTip(i, self.tabs.tabText(i))
        self.list_side.setVisible(self.tab != REPORTS)
        self.reports_view.setVisible(self.tab == REPORTS)
        if self.tab == REPORTS:
            self._show_reports()
        rows = everything if self.tab == ALL else [r for r in everything if r["status"] == self.tab]
        if self.tab in self.TO_DO:
            self.rows = rows
            self.pager.setVisible(False)
        else:
            self.rows = self.pager.take(rows)
        self.prompt.setText({cs_service.ISSUED: "Press Goods arrived when they reach the warehouse. The supplier's 90 days start once they've received the check that took the claim stub off." if self.user.can("warehouse.receive")
                             else "The warehouse presses Goods arrived when they get there. The supplier's 90 days start once they've received the check that took the claim stub off.",
                             cs_service.AT_WAREHOUSE: "The supplier collects these once they've received the check that took the claim stub off. From then they have 90 days, and after that the goods can be forfeited and sold.",
                             cs_service.FORFEITED: "Not collected in time, so these are ready to sell."}.get(self.tab, ""))
        # Refilling replaces the rows, which would read as unticking them, so the ticks are kept aside until it's done.
        self.table.blockSignals(True)
        fill_table(self.table, [[r["cs_number"], fmt_date(r["cs_date"]), r["supplier"], r["po_number"], r["branch_name"], fmt_qty(r["pieces_m"]), fmt_plain(r["total_c"]), r["legacy_cs_number"], r["approved_name"],
                                 fmt_date(r["arrived_at"]), "Waiting for payment" if r["status"] == cs_service.AT_WAREHOUSE and not r["paid_on"] else _days(r["days_left"]),
                                 "Not printed" if not r["prints"] else f"{r['prints']} times" if r["prints"] > 1 else "Printed"] for r in self.rows], keys=[r["id"] for r in self.rows])
        # A stub not printed yet has a tick box, so several print together.
        self.table.blockSignals(True)
        unprinted = {r["id"] for r in self.rows if not r["prints"] and r["status"] != cs_service.CANCELLED}
        self.ticked &= unprinted
        for i, r in enumerate(self.rows):
            if r["id"] in unprinted and self.can_print:
                item = self.table.item(i, 0)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if r["id"] in self.ticked else Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        show_if(self.select_btn, self.can_print and bool(unprinted))
        show_if(self.print_ticked_btn, self.can_print and bool(unprinted))
        self.print_ticked_btn.setText(f"Print {len(self.ticked)} ticked" if self.ticked else "Print ticked")
        self.print_ticked_btn.setEnabled(bool(self.ticked))
        for i, r in enumerate(self.rows):
            if r["id"] == selected:
                self.table.selectRow(i)
        if self.rows and self.table.currentRow() < 0:
            self.table.selectRow(0)
        self.table.setVisible(bool(self.rows))
        self.empty.setVisible(not self.rows)
        self._selected()

    def _row(self) -> dict | None:
        key = selected_key(self.table)
        return next((r for r in self.rows if r["id"] == key), None)

    def _selected(self):
        row = self._row()
        lines = cs_service.items(self.conn, self.user, row["id"]) if row else []
        got = cs_service.arrival_counts(self.conn, "CS", [i["id"] for i in lines])
        fill_table(self.lines, [[i["item_code"], i["description"], i["size"], i["color"], fmt_qty(i["pieces_m"]), fmt_plain(per_piece(i["net_unit_c"], i["pieces_per_unit"])), fmt_plain(i["amount_c"]),
                                 fmt_qty(got[i["id"]]) if i["id"] in got else ""] for i in lines])
        status = row["status"] if row else None
        counted = bool(lines) and len(got) == len(lines)
        # A cancelled stub is only a record, so nothing can be done with it.
        for w in (self.open_btn, self.external, self.external_btn):
            show_if(w, bool(row) and status != cs_service.CANCELLED)
        self.external.setText(row["legacy_cs_number"] if row else "")
        # Goods already at the warehouse from before counting was asked for are counted first, then collected.
        show_if(self.arrived_btn, status == cs_service.ISSUED or (status == cs_service.AT_WAREHOUSE and not counted))
        self.arrived_btn.setText("Goods arrived" if status != cs_service.AT_WAREHOUSE else "Count goods")
        show_if(self.collected_btn, self.user.can("goods.release") and status == cs_service.AT_WAREHOUSE and counted and bool(row["paid_on"]))
        show_if(self.forfeit_btn, status == cs_service.AT_WAREHOUSE and counted and row["days_left"] is not None and row["days_left"] < 0)
        show_if(self.cancel_btn, bool(row) and cs_service.can_cancel(self.conn, self.user, row))
        # Never printed, so it's the first print, not a reprint.
        self.open_btn.setText("Print again" if row and row["prints"] else "Print")
        if not row:
            self.note.setText("")
        elif status == cs_service.COLLECTED:
            self.note.setText(f"Collected by {row['collected_by_name']}, recorded by {row['closed_name']}, {fmt_datetime(row['closed_at'])}")
        elif status == cs_service.FORFEITED:
            self.note.setText(f"Forfeited by {row['closed_name']}, {fmt_datetime(row['closed_at'])}")
        elif status == cs_service.CANCELLED:
            self.note.setText(f"Cancelled by {row['closed_name']}, {fmt_datetime(row['closed_at'])}: {row['cancel_reason']}")
        else:
            self.note.setText(f"Approved by {row['approved_name']}, {fmt_datetime(row['approved_at'])}" + (f". {row['remarks']}" if row["remarks"] else ""))

    def print_again(self):
        """Prints the selected stub. Only one printed before asks first, since that print is marked as a copy."""
        row = self._row()
        if row and (not row["prints"] or confirm_reprint(self, row["cs_number"])):
            open_for_printing([cs_service.document(self.conn, self.user, row["id"])], row["cs_number"])
            self.refresh()

    def save_external(self):
        row = self._row()
        if row:
            cs_service.save_external_number(self.conn, self.user, row["id"], self.external.text())
            self.refresh()

    def _show_reports(self):
        # Waiting ones all show, whatever their age. The decided ones, newest first, a page at a time.
        all_decided = [r["id"] for r in self.report_rows if r["status"] != cs_service.PENDING]
        self.reports_pager.show_counts(0, len(all_decided))
        keep = set(all_decided[self.reports_pager.offset:self.reports_pager.limit])
        rows = [r for r in self.report_rows if r["status"] == cs_service.PENDING or r["id"] in keep]
        fill_table(self.reports, [[fmt_datetime(r["requested_at"]), r["branch_name"], r["po_number"], r["supplier"], ", ".join(l["description"] for l in r["lines"]), fmt_qty(r["pieces_m"]),
                                   fmt_plain(r.get("amount_c", 0)), r["requested_name"], r["reason"],
                                   cs_service.REQUEST_LABELS[r["status"]] if r["status"] != cs_service.APPROVED else f"Approved: {r['cs_number']}"] for r in rows], keys=[r["id"] for r in rows])
        for i in range(len(rows)):
            for c in (4, 8):
                self.reports.item(i, c).setToolTip(self.reports.item(i, c).text())
        first = next((i for i, r in enumerate(rows) if r["status"] == cs_service.PENDING), 0 if rows else None)
        if first is not None:
            self.reports.selectRow(first)
        self._report_selected()

    def _report(self):
        key = selected_key(self.reports)
        return next((r for r in self.report_rows if r["id"] == key), None)

    def _report_selected(self):
        row = self._report()
        pending = row is not None and row["status"] == cs_service.PENDING
        for b in (self.reject_report_btn, self.approve_report_btn):
            show_if(b, self.can_approve and pending)
        if row is None:
            self.reports_note.setText("No damage reported by the branches.")
        elif pending:
            self.reports_note.setText(f"{row['branch_name']} reports {fmt_qty(row['pieces_m'])} damaged pieces on {row['po_number']}, coming to {fmt_peso(row.get('amount_c', 0))}. "
                                      "Approving makes the claim stub as reported, and the branch prints the pass for the goods.")
        elif row["status"] == cs_service.REJECTED:
            self.reports_note.setText(f"Rejected by {row['decided_name']}: {row['decision_reason']}")
        else:
            self.reports_note.setText(f"Approved by {row['decided_name']} as {row['cs_number']}.")

    def approve_report(self):
        row = self._report()
        if row and confirm(self, "Approve damage report", f"Make a claim stub for the {fmt_qty(row['pieces_m'])} damaged pieces {row['branch_name']} reported on {row['po_number']}, "
                           f"{fmt_peso(row.get('amount_c', 0))}? If the order is already paid, it comes off the supplier's next check.", "Approve"):
            cs_service.approve_report(self.conn, self.user, row["id"])
            self.refresh()
            self.changed.emit()

    def reject_report(self):
        row = self._report()
        if row:
            reason = ask_text(self, "Reject damage report", f"Why is {row['branch_name']}'s report on {row['po_number']} rejected? The branch sees this, and can send a new one.")
            if reason is not None:
                cs_service.reject_report(self.conn, self.user, row["id"], reason)
                self.refresh()
                self.changed.emit()

    def _ticked_changed(self, item):
        if item.column() != 0 or not item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        (self.ticked.add if item.checkState() == Qt.CheckState.Checked else self.ticked.discard)(key)
        self.print_ticked_btn.setText(f"Print {len(self.ticked)} ticked" if self.ticked else "Print ticked")
        self.print_ticked_btn.setEnabled(bool(self.ticked))

    def select_unprinted(self):
        self.ticked = {r["id"] for r in self.rows if not r["prints"] and r["status"] != cs_service.CANCELLED}
        self.refresh()

    def print_ticked(self):
        """Every ticked stub in one file, oldest first. Each is its original, since none was printed before, and each print is recorded."""
        chosen = [r for r in sorted(self.rows, key=lambda r: r["id"]) if r["id"] in self.ticked]
        if not chosen:
            return
        open_for_printing([cs_service.document(self.conn, self.user, r["id"]) for r in chosen], f"{len(chosen)} claim stubs")
        self.ticked.clear()
        self.refresh()

    def _late(self):
        """Opens the panel on the right, beside the list, at about half the width, so the products can be read."""
        self.late.start()
        was_open = not self.late.isHidden()
        self.late.show()
        if not was_open:
            total = sum(self.split.sizes()) or self.split.width()
            panel = max(self.late.minimumWidth(), total // 2)
            self.split.setSizes([total - panel if self.list_side.isVisible() else 0, total - panel if self.reports_view.isVisible() else 0, panel])

    def _done(self):
        self.table.clearSelection()
        self.table.setCurrentItem(None)
        self.refresh()
        self.changed.emit()

    def arrived(self):
        """Counting is how the warehouse says the goods are here: saving the count records them arrived. The supplier's 90 days run from the check that took the claim stub off, not from arriving."""
        row = self._row()
        if not row:
            return
        pieces = count_goods(self, row["cs_number"], cs_service.items(self.conn, self.user, row["id"]))
        if pieces is None:
            return
        if row["status"] == cs_service.ISSUED:
            cs_service.arrived(self.conn, self.user, row["id"], pieces)
        else:
            cs_service.count_arrived(self.conn, self.user, row["id"], pieces)
        self._done()

    def collected(self):
        row = self._row()
        if row:
            who = ask_text(self, "Collected", f"Who collected the goods on {row['cs_number']} for {row['supplier']}?")
            if who is not None:
                cs_service.collected(self.conn, self.user, row["id"], who)
                self._done()

    def forfeit(self):
        row = self._row()
        if row and confirm(self, "Forfeit goods", f"{row['supplier']} didn't collect the goods on {row['cs_number']} within 90 days. Forfeit them so they can be sold? This can't be undone.", "Forfeit"):
            cs_service.forfeit(self.conn, self.user, row["id"])
            self._done()

    def cancel(self):
        row = self._row()
        if row:
            reason = ask_text(self, "Cancel claim stub", f"Why is {row['cs_number']} being cancelled? Its number will never be used again, and the PO goes back to To check.")
            if reason is not None:
                cs_service.cancel(self.conn, self.user, row["id"], reason)
                self._done()


ALL = "ALL"
REPORTS = "REPORTS"
# The lists ask the services for every paper, not their default newest 2,000, so search and dates never miss an old one.
# Only what's drawn on screen is cut, by a Pager.
EVERY = 10 ** 9


def _in_dates(rows: list[dict], key: str, date_from, date_to) -> list[dict]:
    """Rows dated within From and To, either of which may be blank."""
    start, end = date_value(date_from), date_value(date_to)
    return [r for r in rows if (not start or (r[key] or "") >= start) and (not end or (r[key] or "")[:10] <= end)]


def _matching(rows: list[dict], text: str) -> list[dict]:
    """Branch reports with every word typed somewhere in their PO, OF, supplier, branch, products, reporter, reason or claim stub."""
    words = text.lower().split()
    if not words:
        return rows

    def haystack(r):
        return " ".join(str(x or "") for x in (r["po_number"], r["of_number"], r["supplier"], r["branch_name"], r["requested_name"], r["reason"], r["cs_number"],
                                               *(x["item_code"] for x in r["lines"]), *(x["description"] for x in r["lines"]))).lower()
    return [r for r in rows if all(w in haystack(r) for w in words)]


def _days(left: int | None) -> str:
    if left is None:
        return ""
    return "Past 90 days" if left < 0 else str(left)
