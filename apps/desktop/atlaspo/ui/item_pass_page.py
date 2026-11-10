"""Item passes for the home office: following excess goods from the branch until the supplier collects them or they're forfeited. Only the branch prints a pass."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QWidget

from .. import item_passes as service
from ..money import fmt_qty
from . import claim_stub_page
from .claim_stub_page import ALL, EVERY, _days, _in_dates
from .common import Pager, keep_place, show_if, ask_text, button, confirm, date_edit, fill_table, fmt_date, fmt_datetime, label, make_table, register_tabs, selected_key, set_date, set_widths, show_tab_counts, smart_columns
from .pages import page_layout


class ItemPassesPage(QWidget):
    """Every item pass, by where its goods are."""
    changed = Signal()
    TO_DO = (service.PRINTED, service.AT_WAREHOUSE)  # tabs that always show every pass

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.tab = service.PRINTED
        self.rows = []
        # All lists every pass whatever it's at, for looking one up by number, supplier, PO or date, like Claim stubs.
        self.labels = {**service.status_labels(conn), ALL: "All"}
        self.can_edit = user.can("dm.issue")
        root, top = page_layout(self, "Item passes")
        top.addWidget(label("The branch prints each pass once, for excess pieces", "Muted"))
        self.tabs = register_tabs(self.labels, self._set_tab)
        root.addWidget(self.tabs)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = QLineEdit()
        # Short enough to read whole at a laptop's width. Supplier covers its code too.
        self.search.setPlaceholderText("Search pass, PO, OF, supplier or branch")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(320)
        self.search.textChanged.connect(self._filter_changed)
        bar.addWidget(self.search)
        # From and To narrow every tab by the pass's date, so an older one is quick to find.
        self.date_from, self.date_to = date_edit(optional=True), date_edit(optional=True)
        for text, w in (("From", self.date_from), ("To", self.date_to)):
            bar.addWidget(label(text, "Muted"))
            bar.addWidget(w)
            w.dateChanged.connect(self._filter_changed)
        bar.addWidget(button("Clear filters", self.clear_filters, lock=False))
        self.prompt = label("", "Muted")
        # The hint goes on its own line, so the filters never push the window past a laptop's width.
        self.prompt.setWordWrap(True)
        bar.addStretch(1)
        self.table = make_table(["Item pass", "Date", "Supplier", "PO number", "OF number", "Branch", "Pieces", "Printed by", "At warehouse", "Days left"], numeric=(6, 9))
        # Supplier keeps its room: in a small window the list scrolls sideways instead of squeezing names to a few letters. Days left, the last, takes any room left over.
        set_widths(self.table, {0: 116, 1: 100, 2: 140, 3: 110, 4: 110, 5: 80, 6: 56, 7: 120, 8: 92, 9: 92})
        chooser = smart_columns(self.table, f"item-passes-{user.username}")
        columns_btn = button("Columns", lambda: chooser.open_under(columns_btn))
        bar.addWidget(columns_btn)
        root.addLayout(bar)
        root.addWidget(self.prompt)
        self.table.itemSelectionChanged.connect(self._selected)
        root.addWidget(self.table, 3)
        # Collected, Forfeited, Cancelled and All grow for good, so they show a page at a time, newest first. Passes still coming back or waiting to be collected
        # are work to do, so those two tabs always show every one.
        self.pager = Pager(self._show_more)
        root.addWidget(self.pager)
        self.empty = label("No item passes here.", "Empty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.hide()
        root.addWidget(self.empty)
        row = QHBoxLayout()
        self.note = label("", "Muted")
        self.note.setWordWrap(True)
        row.addWidget(self.note, 1)
        self.arrived_btn = button("Goods arrived", self.arrived, "Primary")
        self.collected_btn = button("Collected", self.collected, "Primary")
        # Forfeiting can't be undone, so it looks like the other steps that can't.
        self.forfeit_btn = button("Forfeit, for sale", self.forfeit, "Danger")
        self.cancel_btn = button("Cancel pass", self.cancel, "Danger")
        # A second pass would let the same goods leave twice, so only management can cancel one.
        if user.can("dm.cancel"):
            row.addWidget(self.cancel_btn)
        if self.can_edit:
            row.addWidget(self.forfeit_btn)
            # A gap keeps the red step that can't be undone apart from Collected, so one isn't pressed for the other.
            row.addSpacing(32)
        # The supplier picks up at the warehouse, so the warehouse can record it. The office still can, for now.
        if user.can("goods.release"):
            row.addWidget(self.collected_btn)
        # The warehouse received the goods, so only the warehouse says they arrived.
        if user.can("warehouse.receive"):
            row.addWidget(self.arrived_btn)
        root.addLayout(row)
        self.lines = make_table(["SKU", "Product", "Size", "Color", "UOM", "Pieces", "Counted at warehouse"], numeric=(5, 6), stretch=1)
        set_widths(self.lines, {0: 120, 2: 70, 3: 90, 4: 70, 5: 70, 6: 120})
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
        """Search and dates back to blank, so every pass on the tab shows again."""
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
        self.labels = {**service.status_labels(self.conn), ALL: "All"}
        # Every pass the search and dates find, not only the newest 2,000, so an old one is never hidden. Only the list on screen is cut.
        everything = _in_dates(service.search(self.conn, self.user, self.search.text(), limit=EVERY), "ip_date", self.date_from, self.date_to)
        show_tab_counts(self.tabs, self.labels, {**{s: sum(r["status"] == s for r in everything) for s in self.labels}, ALL: len(everything)})
        rows = everything if self.tab == ALL else [r for r in everything if r["status"] == self.tab]
        if self.tab in self.TO_DO:
            self.rows = rows
            self.pager.setVisible(False)
        else:
            self.rows = self.pager.take(rows)
        self.prompt.setText({service.PRINTED: "Press Goods arrived when they reach the warehouse. That starts the supplier's 90 days." if self.user.can("warehouse.receive")
                             else "The warehouse presses Goods arrived when they get there. That starts the supplier's 90 days.",
                             service.AT_WAREHOUSE: "The supplier has 90 days to pick these up. After that they can be forfeited and sold.",
                             service.FORFEITED: "Not collected in time, so these are ready to sell."}.get(self.tab, ""))
        self.prompt.setVisible(bool(self.prompt.text()))
        fill_table(self.table, [[r["ip_number"], fmt_date(r["ip_date"]), r["supplier"], r["po_number"], r["of_number"] or "", r["branch_name"], fmt_qty(r["pieces_m"]), r["printed_name"],
                                 fmt_date(r["arrived_at"]), _days(r["days_left"])] for r in self.rows], keys=[r["id"] for r in self.rows])
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
        lines = service.items(self.conn, self.user, row["id"]) if row else []
        from ..claim_stubs import arrival_counts
        got = arrival_counts(self.conn, "IP", [i["id"] for i in lines])
        counted = bool(lines) and len(got) == len(lines)
        fill_table(self.lines, [[i["item_code"], i["description"], i["size"], i["color"], i["uom"], fmt_qty(i["pieces_m"]), fmt_qty(got[i["id"]]) if i["id"] in got else ""] for i in lines])
        status = row["status"] if row else None
        show_if(self.arrived_btn, status == service.PRINTED or (status == service.AT_WAREHOUSE and not counted))
        self.arrived_btn.setText("Goods arrived" if status != service.AT_WAREHOUSE else "Count goods")
        show_if(self.collected_btn, self.user.can("goods.release") and status == service.AT_WAREHOUSE and counted)
        show_if(self.forfeit_btn, status == service.AT_WAREHOUSE and counted and row["days_left"] < 0)
        show_if(self.cancel_btn, status == service.PRINTED and self.user.can("dm.cancel"))
        if not row:
            self.note.setText("")
        elif status == service.COLLECTED:
            self.note.setText(f"Collected by {row['collected_by_name']}, recorded by {row['closed_name']}, {fmt_datetime(row['closed_at'])}")
        elif status == service.FORFEITED:
            self.note.setText(f"Forfeited by {row['closed_name']}, {fmt_datetime(row['closed_at'])}")
        elif status == service.CANCELLED:
            self.note.setText(f"Cancelled by {row['closed_name']}, {fmt_datetime(row['closed_at'])}: {row['cancel_reason']}")
        else:
            self.note.setText(f"Printed by {row['printed_name']} at {row['branch_name']}, {fmt_datetime(row['printed_at'])}")

    def _done(self):
        self.table.clearSelection()
        self.table.setCurrentItem(None)
        self.refresh()
        self.changed.emit()

    def arrived(self):
        """Counting is how the warehouse says the goods are here: saving the count records them arrived, which starts the supplier's 90 days."""
        row = self._row()
        if not row:
            return
        pieces = claim_stub_page.count_goods(self, row["ip_number"], service.items(self.conn, self.user, row["id"]))
        if pieces is None:
            return
        if row["status"] == service.PRINTED:
            service.arrived(self.conn, self.user, row["id"], pieces)
        else:
            service.count_arrived(self.conn, self.user, row["id"], pieces)
        self._done()

    def collected(self):
        row = self._row()
        if row:
            who = ask_text(self, "Collected", f"Who collected the goods on {row['ip_number']} for {row['supplier']}?")
            if who is not None:
                service.collected(self.conn, self.user, row["id"], who)
                self._done()

    def forfeit(self):
        row = self._row()
        if row and confirm(self, "Forfeit goods", f"{row['supplier']} didn't collect the goods on {row['ip_number']} within 90 days. Forfeit them so they can be sold? This can't be undone.", "Forfeit"):
            service.forfeit(self.conn, self.user, row["id"])
            self._done()

    def cancel(self):
        row = self._row()
        if row:
            reason = ask_text(self, "Cancel item pass", f"Why is {row['ip_number']} being cancelled? Its number will never be used again. The branch can then print a new pass with a new number.")
            if reason is not None:
                service.cancel(self.conn, self.user, row["id"], reason)
                self._done()
