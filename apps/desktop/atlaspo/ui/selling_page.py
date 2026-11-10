"""The Selling prices screen. It belongs to sales, so purchasing's screens don't depend on it."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QAbstractItemView, QCheckBox, QHBoxLayout, QLineEdit, QWidget

from .. import selling
from ..money import fmt_plain, fmt_qty, parse_pesos
from . import theme
from .common import button, fill_table, guarded, label, make_table, register_tabs, set_widths, show_tab_counts, smart_columns
from .pages import page_layout

C_COST, C_ORDER, C_PRICE, C_MARGIN = 7, 8, 9, 10
BELOW = selling.BELOW_SRP


class SellingPricesPage(QWidget):
    """The price of each piece, for what was bought and for forfeited goods. Type over a price to change it, or clear it to go back."""

    BOUGHT, FORFEITED = "BOUGHT", "FORFEITED"
    TABS = {BOUGHT: "Bought", FORFEITED: "Forfeited goods"}
    EMPTY = {BOUGHT: "No selling prices yet. Products appear here once their Order Form is approved.",
             FORFEITED: "No forfeited goods. Claim stub and item pass goods appear here once the supplier's 90 days are over."}

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.rows = []
        self.tab = self.BOUGHT
        self.can_edit = user.can("price.edit")
        root, top = page_layout(self, "Selling prices")
        self.flagged = label("", "PanelTitle")
        top.addWidget(self.flagged)
        top.addSpacing(12)
        top.addWidget(label("SRP: 30% gross margin, then up to a multiple of ₱10. A price by hand is rounded up the same way", "Muted"))
        self.tabs = register_tabs(self.TABS, self._set_tab)
        root.addWidget(self.tabs)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = QLineEdit()
        # Short enough to read whole at a laptop's width. Number covers OF, claim stub and item pass numbers, and supplier its code too.
        self.search.setPlaceholderText("Search number, supplier, SKU or product")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(320)
        bar.addWidget(self.search)
        # Only the flagged ones, so a long list doesn't hide a price someone set too low.
        self.only_flagged = QCheckBox("Only below SRP")
        self.only_flagged.toggled.connect(self.refresh)
        bar.addWidget(self.only_flagged)
        if self.can_edit:
            bar.addWidget(label("Type in Selling price to change one. Clear it to go back.", "Muted"))
        bar.addStretch(1)
        root.addLayout(bar)

        self.table = make_table(["OF number", "Supplier", "SKU", "Product", "Size", "Color", "Pieces", "Cost per pc", "Order price", "Selling price", "Margin", "Price from"],
                                numeric=(6, C_COST, C_ORDER, C_PRICE, C_MARGIN))
        # OF number, Product, Color and Price from get room for their usual text instead of squeezing to fit: in a small window the list scrolls sideways
        # rather than cutting names off. Price from, the last, takes any room left over.
        set_widths(self.table, {0: 116, 1: 120, 2: 84, 3: 176, 4: 52, 5: 96, 6: 52, C_COST: 76, C_ORDER: 76, C_PRICE: 84, C_MARGIN: 56, 11: 150})
        chooser = smart_columns(self.table, f"selling-{user.username}")
        columns_btn = button("Columns", lambda: chooser.open_under(columns_btn))
        bar.addWidget(columns_btn)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed | QAbstractItemView.EditTrigger.AnyKeyPressed)
        self.table.itemChanged.connect(self._price_typed)
        root.addWidget(self.table, 1)
        self.empty = label("", "Empty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.hide()
        # The message takes the table's room, so the search box stays where it was when nothing matches.
        root.addWidget(self.empty, 1)

        self.search.textChanged.connect(self.refresh)
        self.refresh()

    def _set_tab(self, tab):
        self.tab = tab
        self.refresh()

    def refresh(self, *_):
        text = self.search.text()
        bought, forfeited = selling.list_items(self.conn, text), selling.forfeited_items(self.conn, text)
        show_tab_counts(self.tabs, self.TABS, {self.BOUGHT: len(bought), self.FORFEITED: len(forfeited)})
        self.rows = bought if self.tab == self.BOUGHT else forfeited
        self.matched = len(self.rows)
        below = sum(r["below_srp"] or r["below_cost"] for r in self.rows)
        if self.only_flagged.isChecked():
            self.rows = [r for r in self.rows if r["below_srp"] or r["below_cost"]]
        self.table.horizontalHeaderItem(0).setText("OF number" if self.tab == self.BOUGHT else "CS or IP number")
        self.table.setColumnHidden(C_ORDER, self.tab == self.FORFEITED)
        self.table.blockSignals(True)
        if self.tab == self.BOUGHT:
            values = [[r["of_number"], r["supplier"], r["item_code"], r["description"], r["size"], r["color"], fmt_qty(r["total_qty_m"] * r["pieces_per_unit"]), fmt_plain(r["cost_c"]),
                       fmt_plain(r["selling_c"]) if r["selling_c"] else "", fmt_plain(r["price_c"]) if r["price_c"] else "", f"{r['margin']:.1%}" if r["margin"] is not None else "", r["source"]] for r in self.rows]
        else:
            values = [[r["number"], r["supplier"], r["item_code"], r["description"], r["size"], r["color"], fmt_qty(r["pieces_m"]), fmt_plain(r["cost_c"]) if r["cost_c"] is not None else "", "",
                       fmt_plain(r["price_c"]) if r["price_c"] else "", f"{(r['price_c'] - r['cost_c']) / r['price_c']:.1%}" if r["price_c"] and r["cost_c"] is not None else "",
                       r["source"] if r["price_c"] else "Not on the PO: type a price"] for r in self.rows]
        for r, v in zip(self.rows, values):
            if r["below_cost"]:
                v[11] = f"Below cost, SRP {fmt_plain(r['auto_c'])}" if r["auto_c"] else "Below cost"
            elif r["below_srp"]:
                v[11] = f"{BELOW}, SRP {fmt_plain(r['auto_c'])}"
        fill_table(self.table, values)
        # The count is for this tab only, so it matches what's listed.
        self.flagged.setText(f"{below} below SRP on {self.TABS[self.tab]}" if below else "")
        self.flagged.setStyleSheet(f"color:{theme.current.RED};" if below else "")
        for row, r in enumerate(self.rows):
            if r["below_srp"] or r["below_cost"]:
                for col in (C_PRICE, 11):
                    item = self.table.item(row, col)
                    item.setForeground(QColor(theme.current.RED))
                    # Below cost loses money on every sale, so it stands out more than a price just under the SRP.
                    if r["below_cost"]:
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
        for row in range(self.table.rowCount()):
            for col in range(self.table.columnCount()):
                item = self.table.item(row, col)
                if col != C_PRICE or not self.can_edit:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.blockSignals(False)
        self.table.setVisible(bool(self.rows))
        self.empty.setText(self._empty_text(text))
        self.empty.setVisible(not self.rows)

    def _empty_text(self, text: str) -> str:
        """Says when it's the search or the tick that hides everything, so nobody thinks there's nothing there."""
        if self.rows:
            return ""
        if self.matched and self.only_flagged.isChecked():
            return f"Nothing below SRP on {self.TABS[self.tab]}{' that matches the search' if text.strip() else ''}. Untick Only below SRP to see them all."
        if text.strip():
            everything = selling.list_items(self.conn, "") if self.tab == self.BOUGHT else selling.forfeited_items(self.conn, "")
            if everything:
                return f"Nothing on {self.TABS[self.tab]} matches '{text.strip()}'. Clear the search to see all {len(everything)}."
        return self.EMPTY[self.tab]

    def _price_typed(self, item):
        if item.column() != C_PRICE:
            return
        row = self.rows[item.row()]
        guarded(self, self._save, row, item.text().strip())
        # After the editor has closed, so the table isn't rebuilt underneath it. A refused price goes back to what it was.
        QTimer.singleShot(0, self.refresh)

    def _save(self, row: dict, text: str):
        price = parse_pesos(text) if text else None
        if self.tab == self.BOUGHT:
            selling.set_price(self.conn, self.user, row["of_id"], row["key"], price)
        else:
            selling.set_forfeit_price(self.conn, self.user, row["kind"], row["item_id"], price)
