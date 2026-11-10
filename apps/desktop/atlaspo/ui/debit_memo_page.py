"""Debit memos: issuing one from a delivery, and the list of those issued."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QAbstractItemView, QDialog, QHBoxLayout, QLineEdit, QStyledItemDelegate, QVBoxLayout, QWidget

from .. import debit_memos as dm_service
from ..money import fmt_peso, fmt_plain, fmt_qty, parse_qty, per_piece
from . import theme
from .common import month_picker, Pager, keep_place, show_if, smart_columns, ask_text, button, confirm, confirm_reprint, fill_table, fmt_date, fmt_datetime, guarded, label, make_table, open_for_printing, register_tabs, selected_key, set_widths, show_tab_counts, date_edit
from .pages import page_layout

C_PIECES, C_AMOUNT = 5, 7


def editable_tint() -> QColor:
    """The theme's color for a cell to type in."""
    p = theme.current
    return QColor(getattr(p, "EDITABLE", None) or p.ACCENT_PALE)


class PaintBackground(QStyledItemDelegate):
    """Paints a cell's own background first. The app's table style leaves it out, so without this a tinted cell shows plain."""

    def paint(self, painter, option, index):
        brush = index.data(Qt.ItemDataRole.BackgroundRole)
        if brush is not None:
            painter.fillRect(option.rect, brush)
        super().paint(painter, option, index)


class DebitMemoDialog(QDialog):
    """Shows what's uncovered on a PO. Lower a figure or clear it to leave it off, then approve it."""
    service = dm_service
    noun = "debit memo"
    number_key = "dm_number"

    def __init__(self, parent, conn, user, po: dict):
        super().__init__(parent)
        self.conn, self.user, self.po = conn, user, po
        self.issued = None
        self.setWindowTitle(f"{self.noun.capitalize()} for {po['po_number']}")
        self.resize(940, 460)
        col = QVBoxLayout(self)
        col.addWidget(label(f"{po['po_number']}, {po['branch_name']}, {po['supplier']}", "PanelTitle"))
        self.lines = self.service.outstanding(conn, user, po["id"])
        if any(line["reason"] == dm_service.SHORT for line in self.lines):
            note = label("If more of this delivery is still coming, wait for it. Charging now and receiving it later leaves this PO on To check until the debit memo is cancelled and made again.", "Muted")
            note.setWordWrap(True)
            col.addWidget(note)
        self.table = make_table(["SKU", "Product", "Size", "Color", "Reason", "Pieces", "Cost per pc", "Amount"], numeric=(C_PIECES, 6, C_AMOUNT), stretch=1)
        set_widths(self.table, {0: 110, 2: 60, 3: 80, 4: 76, C_PIECES: 66, 6: 84, C_AMOUNT: 96})
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
        self.table.blockSignals(True)
        fill_table(self.table, [[i["item_code"], i["description"], i["size"], i["color"], dm_service.REASONS[i["reason"]], fmt_qty(i["pieces_m"]), fmt_plain(per_piece(i["net_unit_c"], i["pieces_per_unit"])),
                                 fmt_plain(i["amount_c"])] for i in self.lines])
        # Pieces is the only figure that can be typed over, so it's tinted like every other cell that takes typing.
        tint = editable_tint()
        self.table.setItemDelegateForColumn(C_PIECES, PaintBackground(self.table))
        for r in range(self.table.rowCount()):
            for c in range(self.table.columnCount()):
                cell = self.table.item(r, c)
                if c == C_PIECES:
                    cell.setBackground(tint)
                else:
                    cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.blockSignals(False)
        self.table.itemChanged.connect(self._recalculate)
        col.addWidget(self.table, 1)
        self.remarks = QLineEdit()
        self.remarks.setPlaceholderText(f"Remarks, printed on the {self.noun}")
        col.addWidget(self.remarks)
        row = QHBoxLayout()
        self.total = label("", "PanelTitle")
        row.addWidget(self.total)
        row.addStretch(1)
        # Enter approves, after the usual question, rather than closing the dialog and losing what was typed.
        go_back = button("Cancel", self.reject)
        go_back.setAutoDefault(False)
        row.addWidget(go_back)
        # The button only promises a print when approving really prints.
        self.approve_btn = button("Approve and print" if self.print_on_approve else "Approve", self.issue, "Primary")
        self.approve_btn.setDefault(True)
        row.addWidget(self.approve_btn)
        col.addLayout(row)
        self._recalculate()

    def _pieces(self) -> dict:
        return {(line["po_item_id"], line["reason"]): self.table.item(r, C_PIECES).text() for r, line in enumerate(self.lines)}

    def _recalculate(self, *_):
        total = 0
        self.table.blockSignals(True)
        for r, line in enumerate(self.lines):
            try:
                amount = dm_service.amount_c(parse_qty(self.table.item(r, C_PIECES).text(), line["uom"]), line["net_unit_c"], line["pieces_per_unit"])
            except ValueError:
                amount = 0
            self.table.item(r, C_AMOUNT).setText(fmt_plain(amount))
            total += amount
        self.table.blockSignals(False)
        self.total.setText(f"Total charged to the supplier: {fmt_peso(total)}")

    def issue(self):
        if not confirm(self, f"Approve {self.noun}", f"Approve this {self.noun} to {self.po['supplier']}? Your name is recorded as approving it. It gets a permanent number, and this exact document is kept for good.", "Approve"):
            return
        self.issued = self.service.issue(self.conn, self.user, self.po["id"], self._pieces(), self.remarks.text())
        if self.print_on_approve:
            open_for_printing([self.service.document(self.conn, self.user, self.issued["id"])], self.issued[self.number_key])
        self.accept()


DebitMemoDialog.print_on_approve = True


class DebitMemosPage(QWidget):
    """Every debit memo. Issuing starts from a delivery on the Received page, so a memo always traces back to agreed counts."""
    changed = Signal()
    # All lists every memo, for looking one up by number, supplier, PO or date.
    TABS = {dm_service.ISSUED: "Approved", dm_service.CANCELLED: "Cancelled", "ALL": "All"}

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.tab = dm_service.ISSUED
        self.rows = []
        self.can_edit = user.can("dm.issue")
        root, top = page_layout(self, "Debit memos")
        top.addWidget(label("Make one from a short PO on Received, To check", "Muted"))
        self.tabs = register_tabs(self.TABS, self._set_tab)
        root.addWidget(self.tabs)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = QLineEdit()
        # Short enough to read whole at a laptop's width. Number covers the memo's own and the external one, and supplier its code too.
        self.search.setPlaceholderText("Search number, PO, OF or supplier")
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
        self.table = make_table(["Debit memo", "Date", "Supplier", "PO number", "OF number", "Branch", "Pieces", "Amount", "External DM", "Approved by", "Printed"], numeric=(6, 7))
        # Date is wide enough for the longest date, so it's never cut off on the selected row. Supplier keeps its room too: in a small window the list
        # scrolls sideways instead of squeezing names to a few letters, and Printed, the last, takes any room left over.
        set_widths(self.table, {0: 116, 1: 100, 2: 140, 3: 110, 4: 110, 5: 80, 6: 56, 7: 92, 8: 80, 9: 110, 10: 64})
        chooser = smart_columns(self.table, f"debit-memos-{user.username}")
        columns_btn = button("Columns", lambda: chooser.open_under(columns_btn))
        bar.addWidget(columns_btn)
        self.table.itemSelectionChanged.connect(self._selected)
        root.addWidget(self.table, 3)
        # Every tab is a record that grows for good, so each shows a page at a time, newest first. Search and dates still look through every memo.
        self.pager = Pager(self._show_more)
        root.addWidget(self.pager)
        self.empty = label("No debit memos here. Make one from a PO on Received, To check.", "Empty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.hide()
        root.addWidget(self.empty)
        row = QHBoxLayout()
        self.note = label("", "Muted")
        self.note.setWordWrap(True)
        row.addWidget(self.note, 1)
        self.open_btn = button("Print again", self.print_again)
        row.addWidget(self.open_btn)
        self.external = QLineEdit()
        self.external.setPlaceholderText("external number")
        self.external.setMaximumWidth(150)
        self.external_btn = button("Save external number", self.save_external)
        self.cancel_btn = button("Cancel debit memo", self.cancel, "Danger")
        if self.can_edit:
            row.addWidget(self.external)
            row.addWidget(self.external_btn)
        # Cancelling gives the supplier back money for a shortage, so only management sees it. A gap keeps it apart from the routine buttons.
        if user.can("dm.cancel"):
            row.addSpacing(32)
            row.addWidget(self.cancel_btn)
        root.addLayout(row)
        self.lines = make_table(["SKU", "Product", "Size", "Color", "Reason", "Pieces", "Cost per pc", "Amount"], numeric=(5, 6, 7), stretch=1)
        set_widths(self.lines, {0: 120, 2: 70, 3: 90, 4: 80, 5: 70, 6: 90, 7: 100})
        root.addWidget(self.lines, 2)
        self.refresh()

    def _set_tab(self, tab):
        self.tab = tab
        self.table.clearSelection()
        self.table.setCurrentItem(None)
        self.pager.reset()
        self.refresh()

    def _filter_changed(self, *_):
        self.pager.reset()
        self.refresh()

    def _show_more(self):
        with keep_place(self.table):
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
        self.refresh()

    def refresh(self, *_):
        selected = selected_key(self.table)
        from .claim_stub_page import EVERY, _in_dates
        # Every memo the search and dates find, not only the newest 2,000, so an old one is never hidden. Only the list on screen is cut.
        everything = _in_dates(dm_service.search(self.conn, self.user, self.search.text(), limit=EVERY), "dm_date", self.date_from, self.date_to)
        show_tab_counts(self.tabs, self.TABS, {**{s: sum(r["status"] == s for r in everything) for s in self.TABS}, "ALL": len(everything)})
        self.rows = self.pager.take(everything if self.tab == "ALL" else [r for r in everything if r["status"] == self.tab])
        fill_table(self.table, [[r["dm_number"], fmt_date(r["dm_date"]), r["supplier"], r["po_number"], r["of_number"] or "", r["branch_name"], fmt_qty(r["pieces_m"]), fmt_plain(r["total_c"]),
                                 r["legacy_dm_number"], r["prepared_name"], f"{r['prints']} times" if r["prints"] > 1 else ""] for r in self.rows], keys=[r["id"] for r in self.rows])
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
        lines = dm_service.items(self.conn, self.user, row["id"]) if row else []
        fill_table(self.lines, [[i["item_code"], i["description"], i["size"], i["color"], dm_service.REASONS[i["reason"]], fmt_qty(i["pieces_m"]), fmt_plain(per_piece(i["net_unit_c"], i["pieces_per_unit"])), fmt_plain(i["amount_c"])] for i in lines])
        # A cancelled memo is only a record, so nothing can be done with it.
        for w in (self.open_btn, self.external, self.external_btn):
            show_if(w, bool(row) and row["status"] == dm_service.ISSUED)
        # Again only once it has been printed, say when printing failed when it was approved.
        self.open_btn.setText("Print again" if row and row["prints"] else "Print")
        show_if(self.cancel_btn, bool(row) and dm_service.can_cancel(self.conn, self.user, row))
        self.external.setText(row["legacy_dm_number"] if row else "")
        if not row:
            self.note.setText("")
        elif row["status"] == dm_service.CANCELLED:
            self.note.setText(f"Cancelled by {row['cancelled_name']}, {fmt_datetime(row['cancelled_at'])}: {row['cancel_reason']}")
        else:
            note = f"Approved by {row['prepared_name']}, {fmt_datetime(row['prepared_at'])}" + (f". {row['remarks']}" if row["remarks"] else "")
            # A late delivery: pieces on this memo came after all. Management cancels it, and anything the check already took off goes on the supplier's next check.
            late = dm_service.arrived_since(self.conn, row)
            if late:
                note += f". {fmt_qty(late)} pieces on this memo have since been delivered. Management can cancel it, and the purchaser issues one for what's still short."
            self.note.setText(note)

    def print_again(self):
        row = self._row()
        if row and (not row["prints"] or confirm_reprint(self, row["dm_number"])):
            open_for_printing([dm_service.document(self.conn, self.user, row["id"])], row["dm_number"])
            self.refresh()

    def save_external(self):
        row = self._row()
        if row:
            dm_service.save_external_number(self.conn, self.user, row["id"], self.external.text())
            self.refresh()

    def cancel(self):
        row = self._row()
        if not row:
            return
        reason = ask_text(self, "Cancel debit memo", f"Why is {row['dm_number']} being cancelled? Its number will never be used again, and the PO goes back to To check.")
        if reason is None:
            return
        # Called directly, like the other registers: a refused cancel shows its message through the button and leaves the selection where it was.
        dm_service.cancel(self.conn, self.user, row["id"], reason)
        self.table.clearSelection()
        self.table.setCurrentItem(None)
        self.refresh()
        self.changed.emit()
