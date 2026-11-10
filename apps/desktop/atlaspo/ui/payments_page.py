"""Payments: compare a finished order's DRs if it had a problem, see which checks to make for which Saturday, record each check, follow it until it's claimed, and see the cash it needs."""
from __future__ import annotations

import re

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeySequence, QShortcut
from PySide6.QtWidgets import QAbstractItemDelegate, QAbstractItemView, QButtonGroup, QDialog, QFormLayout, QHBoxLayout, QInputDialog, QLineEdit, QPushButton, QSplitter, QTreeWidgetItem, QVBoxLayout, QWidget

from .. import masters, payments, pdf
from ..db import today
from ..db import POError
from ..money import fmt_peso, fmt_plain, fmt_qty, parse_pesos, parse_qty
from . import theme
from .claim_stub_page import editable_tint, show_tints
from .common import (show_if, ColumnChooser, smart_columns, ask_text, confirm_reprint, button, confirm, date_edit, error, date_value, fill_table, fit_tree_columns, resort, fmt_date, guarded, label, make_table, make_tree, open_for_printing, register_tabs, set_date, set_widths)
from .pages import page_layout


def _digits(text: str) -> bool:
    """Plain digits 0 to 9 a check number can be counted from. Other digit signs, say "²", aren't."""
    return bool(re.fullmatch(r"[0-9]{1,%d}" % payments.MAX_CHECK_DIGITS, text))


def _buttons(back: QPushButton, action: QPushButton) -> QHBoxLayout:
    """A dialog's buttons: going back on the left, the action on the right. Enter in a box presses the action, never going back, so nothing typed is lost."""
    back.setAutoDefault(False)
    action.setAutoDefault(True)
    action.setDefault(True)
    row = QHBoxLayout()
    row.addStretch(1)
    row.addWidget(back)
    row.addWidget(action)
    return row


def _readable(pieces: bool, text: str) -> bool:
    """Whether a cell typed on the DR panel can be read: a number of pieces, or a unit price not below nothing. Blank always can."""
    text = text.strip()
    if not text:
        return True
    try:
        return parse_qty(text, "PC") >= 0 if pieces else parse_pesos(text) >= 0
    except (ValueError, ArithmeticError):
        return False


def _header_widths(table, widths: dict[int, int]) -> dict[int, int]:
    """Each column at least as wide as its heading, so "PO unit cost" or "Difference" reads in full rather than ending in "...".
    The heading is drawn at 9pt semibold with 6px padding a side (theme)."""
    font = QFont(table.font())
    font.setPointSizeF(9)
    font.setWeight(QFont.Weight.DemiBold)
    fm = QFontMetrics(font)
    return {c: max(w, fm.horizontalAdvance(table.horizontalHeaderItem(c).text()) + 2 * 6 + 10) for c, w in widths.items()}


class DrPanel(QWidget):
    """The DRs of one order, on the right of the list. Pick the DR that differs, and its products show. On each product that differs,
    type what the supplier's DR says, the pieces, the unit price or both. The DR's total is worked out from them: a product left blank is the same as ours.
    DRs nobody touches keep what they had. Esc or Cancel closes it without saving."""
    done = Signal()
    closed = Signal()
    C_DR_PIECES, C_DR_UNIT, C_DR_AMOUNT, C_DIFFERENCE = 5, 6, 7, 8
    C_DATE = 2  # the date on the DR, in the DR table

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.order, self.receipts, self.info, self.typed, self.touched, self.dates, self.current = None, [], {}, {}, set(), {}, None
        self.problem = ""
        self.whole, self.groups = False, []  # the All DRs row: shown or not, and the OF's products, each once with its line on every DR
        self.setMinimumWidth(460)
        col = QVBoxLayout(self)
        col.setContentsMargins(10, 0, 0, 0)
        head = QHBoxLayout()
        self.title = label("", "PanelTitle")
        head.addWidget(self.title)
        head.addStretch(1)
        head.addWidget(button("Close", self.closed.emit, lock=False))
        col.addLayout(head)
        self.note = label("Pick the DR that differs. On each product that differs, type what the supplier's DR says: the pieces, the unit price, or both. "
                          "Leave the rest blank: blank is the same as ours.", "Muted")
        self.note.setWordWrap(True)
        col.addWidget(self.note)
        headers = ["DR number", "Branch", "Date on the DR", "Supplier's total"]
        # Every DR of an OF carries the same products, so the whole OF can be adjusted at once. "All DRs (whole OF)" is the
        # first row: a one-row table carrying the headings, sitting right on top of the branch DRs, so the branch DRs keep their own rows.
        self.all_drs = make_table(headers, numeric=(3,), stretch=0)
        self.all_drs.setProperty("no_sort", True)
        self.all_drs.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.all_drs.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.all_drs.itemSelectionChanged.connect(self._all_picked)
        self.drs = make_table(headers, numeric=(3,), stretch=0)
        widths = _header_widths(self.drs, {1: 80, 2: 120, 3: 100})
        set_widths(self.drs, widths)
        set_widths(self.all_drs, widths)
        self.drs.setProperty("no_sort", True)
        self.drs.itemSelectionChanged.connect(self._dr_picked)
        # Widening a column on the headings widens it on the branch DRs too, and a scroll bar on the DRs is matched above, so the columns line up.
        self.all_drs.horizontalHeader().sectionResized.connect(lambda c, _old, new: c and self.drs.setColumnWidth(c, new))
        self.drs.verticalScrollBar().rangeChanged.connect(self._match_scroll_bar)
        stack = QVBoxLayout()
        stack.setSpacing(0)
        stack.addWidget(self.all_drs)
        stack.addWidget(self.drs, 1)
        col.addLayout(stack, 1)
        self.dr_title = label("", "PanelTitle")
        self.dr_title.setWordWrap(True)
        col.addWidget(self.dr_title)
        self.lines = make_table(["SKU", "Product", "Counted", "PO unit cost", "PO amount", "DR pieces", "DR unit cost", "DR amount", "Difference"], numeric=(2, 3, 4, 5, 6, 7, 8), stretch=1)
        set_widths(self.lines, _header_widths(self.lines, {0: 80, 2: 64, 3: 86, 4: 86, 5: 74, 6: 86, 7: 86, 8: 86}))
        self.lines.setProperty("no_sort", True)
        self.lines.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
        for c, tip in ((2, "Pieces the branch counted, up to what was ordered. Pieces beyond the order go back on an item pass."),
                       (3, "The PO's cost for one unit as ordered (a dozen for DOZ), after any overall discount."),
                       (4, "The pieces counted at the PO unit cost: what we say this product comes to."),
                       (5, "Pieces on the supplier's DR, when they differ. Blank is the same as ours."),
                       (6, "Unit price on the supplier's DR, when it differs. Blank is the same as ours."),
                       (7, "What this product comes to on the supplier's DR."),
                       (8, "DR amount less PO amount. Red: the DR is higher than the PO. Green: the DR is lower. Blank: the same.")):
            self.lines.horizontalHeaderItem(c).setToolTip(tip)
        show_tints(self.lines, self.C_DR_PIECES)
        show_tints(self.lines, self.C_DR_UNIT)
        self.lines.itemChanged.connect(self._typed)
        self.lines.currentCellChanged.connect(lambda *_: self._same_price_state())
        col.addWidget(self.lines, 2)
        # One product's DR price, typed once, put on the same product on every DR of the order.
        same = QHBoxLayout()
        self.same_btn = button("Same price on all DRs", lambda: guarded(self, self.same_price_on_all), lock=False)
        self.same_btn.setToolTip("Puts this product's DR unit cost on the same product on every other DR of this order. "
                                 "Pieces stay as each branch counted. Nothing is saved until Save DRs.")
        self.same_btn.setAutoDefault(False)
        same.addWidget(self.same_btn)
        self.same_note = label("", "Muted")
        self.same_note.setWordWrap(True)
        same.addWidget(self.same_note, 1)
        col.addLayout(same)
        self.total = label("", "PanelTitle")
        self.total.setWordWrap(True)
        col.addWidget(self.total)
        self.save_btn = button("Save DRs", lambda: guarded(self, self.save), "Primary")
        col.addLayout(_buttons(button("Cancel", self.closed.emit, lock=False), self.save_btn))

    def start(self, order: dict):
        """A clean start for this order, each time it opens."""
        self.order, self.info, self.typed, self.touched, self.dates, self.current, self.problem = order, {}, {}, set(), {}, None, ""
        self.whole, self.groups = False, []
        self.same_note.setText("")
        self.title.setText(f"DRs for {order['of_number']}")
        self.title.setToolTip(order["supplier"])
        self.receipts = payments.receipts_to_enter(self.conn, self.user, order["id"])
        # The All DRs row shows when there is more than one DR to adjust together.
        several = len(self.receipts) > 1
        self.all_drs.blockSignals(True)
        self.all_drs.clearSelection()
        fill_table(self.all_drs, [["All DRs (whole OF)", "All", "", ""]])
        bold = QFont(self.all_drs.font())
        bold.setWeight(QFont.Weight.DemiBold)
        tip = "Every DR of this OF together: each product once. A DR unit cost typed there goes on every DR."
        for c in range(self.all_drs.columnCount()):
            self.all_drs.item(0, c).setFont(bold)
            self.all_drs.item(0, c).setToolTip(tip)
        self.all_drs.item(0, 3).setToolTip("All DRs added up, as typed. A DR left blank counts as ours.")
        self.all_drs.blockSignals(False)
        self.all_drs.setVisible(several)
        self.drs.horizontalHeader().setVisible(not several)
        if several:
            for d in self.receipts:
                self._load(d["id"])
        self.drs.blockSignals(True)
        fill_table(self.drs, [[d["dr_number"], d["branch"], "", ""] for d in self.receipts], keys=[d["id"] for d in self.receipts])
        for row, d in enumerate(self.receipts):
            day = date_edit(optional=True)
            set_date(day, d["dr_date"] or d["suggested_date"])
            day.dateChanged.connect(lambda *_, dr_id=d["id"]: self._date_changed(dr_id))
            self.drs.setCellWidget(row, self.C_DATE, day)
            self.dates[d["id"]] = day
        self._fit_dates()
        self.drs.blockSignals(False)
        self._show_totals()
        if self.receipts:
            self.drs.selectRow(0)
        self._dr_picked()

    def date_width(self) -> int:
        """What the date box needs to show a whole date, say 10-28-2026, with its calendar arrow, at any window size."""
        box = next(iter(self.dates.values()), None) or date_edit(optional=True)
        box.ensurePolished()
        digits = box.fontMetrics().horizontalAdvance("88-88-8888")
        # The text, the box's padding and border, and the arrow (theme: padding 6 a side, arrow 22), with a little room to spare.
        return max(box.sizeHint().width(), digits + 2 * 6 + 2 + 22 + 8)

    def _fit_dates(self):
        """The date box was cut short ("10-28-202..."). The column is made wide enough for the whole date, and Branch only as wide
        as its names, so the DR number keeps its room on a 1366-wide screen too."""
        width = self.date_width()
        for box in self.dates.values():
            box.setMinimumWidth(width)
        self.drs.setColumnWidth(self.C_DATE, max(self.drs.columnWidth(self.C_DATE), width + 2))
        names = max((self.drs.fontMetrics().horizontalAdvance(d["branch"] or "") for d in self.receipts), default=0) + 2 * 6 + 8
        self.drs.setColumnWidth(1, max(_header_widths(self.drs, {1: 0})[1], names))
        # The panel asks the splitter for room for the whole DR number beside them, so nothing in the DR table is cut short.
        names = [d["dr_number"] for d in self.receipts] + (["All DRs (whole OF)"] if len(self.receipts) > 1 else [])
        bold = QFont(self.drs.font())
        bold.setWeight(QFont.Weight.DemiBold)
        number = max((QFontMetrics(bold).horizontalAdvance(n) for n in names), default=0) + 2 * 6 + 8
        frame = 2 * self.drs.frameWidth() + self.drs.verticalScrollBar().sizeHint().width()
        self.setMinimumWidth(max(460, number + sum(self.drs.columnWidth(c) for c in (1, 2, 3)) + frame + self.layout().contentsMargins().left()))
        # The All DRs row above lines up with the branch DRs, and is exactly its headings and one row high.
        for c in (1, 2, 3):
            self.all_drs.setColumnWidth(c, self.drs.columnWidth(c))
        self.all_drs.ensurePolished()
        self.all_drs.setFixedHeight(self.all_drs.horizontalHeader().sizeHint().height() + self.all_drs.verticalHeader().defaultSectionSize() + 2 * self.all_drs.frameWidth())

    def _match_scroll_bar(self, _low: int, high: int):
        """When the branch DRs need a scroll bar, the All DRs row above keeps the same room for one, so its columns stay over theirs."""
        self.all_drs.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn if high > 0 else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def _date_changed(self, dr_id: int):
        self.touched.add(dr_id)

    def _commit_edit(self):
        """A product cell still being typed in is kept before the products change or are saved, so nothing typed is lost."""
        if self.lines.state() != QAbstractItemView.State.EditingState:
            return
        delegate = self.lines.itemDelegateForIndex(self.lines.currentIndex())
        for editor in self.lines.viewport().findChildren(QLineEdit):
            if editor.parent() is self.lines.viewport() and not editor.isHidden():
                delegate.commitData.emit(editor)
                delegate.closeEditor.emit(editor, QAbstractItemDelegate.EndEditHint.NoHint)

    @staticmethod
    def _product_key(line: dict):
        """The same product on another DR of the order: the same OF line, else the same SKU."""
        if line.get("of_item_id") is not None:
            return ("of", line["of_item_id"])
        return ("sku", line["item_code"]) if line["item_code"] else ("product", line["product"], line["uom"])

    def _selected_line(self) -> dict | None:
        row = self.lines.currentRow()
        if self.current is None or row < 0 or row >= len(self.info[self.current]["lines"]):
            return None
        return self.info[self.current]["lines"][row]

    def _same_price_state(self):
        """Same price on all DRs works once the selected product has a DR unit cost typed that can be read, and another DR to put it on.
        On the All DRs row it isn't needed: a price typed there already goes on every DR."""
        self.same_btn.setVisible(not self.whole)
        if self.whole:
            return
        line = self._selected_line()
        text = self.typed[self.current][line["po_item_id"]][1].strip() if line else ""
        self.same_btn.setEnabled(bool(text) and _readable(False, text) and len(self.receipts) > 1)

    def same_price_on_all(self):
        """The selected product's DR unit cost goes on the same product on every other DR of this order. Pieces stay per DR, since each branch
        counted its own. Each DR's total shows at once; nothing is saved until Save DRs, which then saves every DR changed."""
        self._commit_edit()
        line = self._selected_line()
        if line is None:
            return
        text = self.typed[self.current][line["po_item_id"]][1].strip()
        if not text or not _readable(False, text):
            return
        key, name, changed = self._product_key(line), line["item_code"] or line["product"], []
        for receipt in self.receipts:
            if receipt["id"] == self.current:
                continue
            same = [other for other in self._load(receipt["id"])["lines"] if self._product_key(other) == key]
            for other in same:
                self.typed[receipt["id"]][other["po_item_id"]][1] = text
            if same:
                self.touched.add(receipt["id"])
                changed.append(receipt["dr_number"])
        price = fmt_peso(parse_pesos(text))
        if changed:
            self.same_note.setText(f"{price} put on {name} on {len(changed)} other DR{'s' if len(changed) != 1 else ''}: {', '.join(changed)}. Save DRs to keep it.")
        else:
            self.same_note.setText(f"No other DR of this order has {name}.")
        self._typed()

    def _selected_dr(self):
        rows = self.drs.selectionModel().selectedRows() if self.drs.selectionModel() else []
        return self.receipts[rows[0].row()] if rows else None

    def _load(self, dr_id: int) -> dict:
        if dr_id not in self.info:
            self.info[dr_id] = payments.dr_lines(self.conn, self.user, self.order["id"], dr_id)
            self.typed[dr_id] = {line["po_item_id"]: [fmt_qty(line["dr_pieces_m"]) if line["dr_pieces_m"] is not None else "",
                                                      fmt_plain(line["dr_unit_c"]) if line["dr_unit_c"] is not None else ""] for line in self.info[dr_id]["lines"]}
        return self.info[dr_id]

    def _dr_picked(self):
        # What's typed on the DR shown so far stays with it: kept per DR until Save DRs or Cancel.
        self._commit_edit()
        receipt = self._selected_dr()
        if receipt is None and self.whole:
            return  # the branch DRs were only unselected for the All DRs row
        if self.whole:
            self.same_note.setText("")
        self.whole = False
        if self.all_drs.selectionModel() and self.all_drs.selectionModel().hasSelection():
            self.all_drs.blockSignals(True)
            self.all_drs.clearSelection()
            self.all_drs.blockSignals(False)
        self.current = receipt["id"] if receipt else None
        self.lines.blockSignals(True)
        if receipt is None:
            self.lines.setRowCount(0)
            self.dr_title.setText("")
        else:
            info = self._load(receipt["id"])
            self.dr_title.setText(f"{receipt['dr_number']}, {receipt['branch']}: its products")
            typed = self.typed[receipt["id"]]
            fill_table(self.lines, [[line["item_code"], line["product"], fmt_qty(line["pieces_m"]), fmt_plain(line["unit_c"]), fmt_plain(line["amount_c"]),
                                     typed[line["po_item_id"]][0], typed[line["po_item_id"]][1], "", ""] for line in info["lines"]], keys=[line["po_item_id"] for line in info["lines"]])
            tint = editable_tint()
            for r, line in enumerate(info["lines"]):
                self.lines.item(r, 1).setToolTip(line["product"] + (f", per {line['uom']}" if line["pieces_per_unit"] != 1 else ""))
                for c in range(self.lines.columnCount()):
                    item = self.lines.item(r, c)
                    if c in (self.C_DR_PIECES, self.C_DR_UNIT):
                        item.setBackground(tint)
                    else:
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.lines.blockSignals(False)
        self._typed()

    def _all_picked(self):
        """The All DRs row: the OF's products, each once, added up over every DR. A DR unit cost typed here goes on that product on every DR
        that carries it; pieces stay per branch. Nothing is saved until Save DRs."""
        if not self.all_drs.selectionModel().hasSelection():
            return
        self._commit_edit()
        self.drs.blockSignals(True)
        self.drs.clearSelection()
        self.drs.setCurrentCell(-1, -1)
        self.drs.blockSignals(False)
        self.whole, self.current = True, None
        groups: dict = {}
        for receipt in self.receipts:
            for line in self._load(receipt["id"])["lines"]:
                groups.setdefault(self._product_key(line), []).append((receipt["id"], line))
        self.groups = list(groups.values())
        self.dr_title.setText(f"All DRs of {self.order['of_number']}: each product once, added up")
        self.same_note.setText("A DR unit cost typed here goes on that product on every DR. Pieces stay per branch.")
        self.lines.blockSignals(True)
        rows = []
        for members in self.groups:
            first = members[0][1]
            units = {line["unit_c"] for _, line in members}
            rows.append([first["item_code"], first["product"], fmt_qty(sum(line["pieces_m"] for _, line in members)),
                         fmt_plain(first["unit_c"]) if len(units) == 1 else "", fmt_plain(sum(line["amount_c"] for _, line in members)), "", "", "", ""])
        fill_table(self.lines, rows, keys=list(range(len(rows))))
        faint = QColor(theme.current.FAINT)
        for r, members in enumerate(self.groups):
            first = members[0][1]
            self.lines.item(r, 1).setToolTip(first["product"] + (f", per {first['uom']}" if first["pieces_per_unit"] != 1 else "")
                                             + f". On {len(members)} DR{'s' if len(members) != 1 else ''}.")
            self.lines.item(r, 2).setToolTip("Pieces counted, all branches together.")
            if not self.lines.item(r, 3).text():
                self.lines.item(r, 3).setToolTip("Different on some POs: pick a branch DR to see each one.")
            for c in range(self.lines.columnCount()):
                item = self.lines.item(r, c)
                if c != self.C_DR_UNIT:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            pieces = self.lines.item(r, self.C_DR_PIECES)
            pieces.setForeground(faint)
            pieces.setToolTip("Pieces are per branch: type them on that branch's DR.")
        self.lines.blockSignals(False)
        self._typed()

    @staticmethod
    def _unit_value(line: dict, text: str):
        """A typed DR unit cost, to tell whether DRs say the same: blank or the PO's own cost is None, anything unreadable stays as typed."""
        text = text.strip()
        if not text:
            return None
        try:
            unit = parse_pesos(text)
        except (ValueError, ArithmeticError):
            return ("unreadable", text)
        if unit < 0:
            return ("unreadable", text)
        return None if unit == line["unit_c"] else unit

    def _show_difference(self, row: int, off: int | None):
        """The Difference cell: DR amount less PO amount. Red when the DR is higher, green when lower, blank when the same."""
        cell = self.lines.item(row, self.C_DIFFERENCE)
        cell.setText("" if off is None else (f"+{fmt_plain(off)}" if off > 0 else fmt_plain(off)))
        if off:
            cell.setForeground(QColor(theme.current.RED if off > 0 else theme.current.GREEN))
        else:
            cell.setData(Qt.ItemDataRole.ForegroundRole, None)
        cell.setToolTip(f"The DR is {fmt_peso(abs(off))} {'more' if off > 0 else 'less'} than the PO amount." if off else "")

    def _typed_whole(self, item=None):
        """On the All DRs row: a DR unit cost typed (or cleared) goes on that product on every DR that carries it, then each product is added up again."""
        if item is not None:
            if item.column() != self.C_DR_UNIT:
                return
            text = item.text()
            for dr_id, line in self.groups[item.row()]:
                self.typed[dr_id][line["po_item_id"]][1] = text
                self.touched.add(dr_id)
        diffs = {d["id"]: self._diffs(d["id"]) for d in self.receipts}
        bad, tint = QColor(theme.current.RED_PALE), editable_tint()
        self.lines.blockSignals(True)
        for r, members in enumerate(self.groups):
            pieces = ours = theirs = 0
            differs, values = False, set()
            for dr_id, line in members:
                said = diffs[dr_id][0].get(line["po_item_id"])
                pieces += said[0] if said and said[0] is not None else line["pieces_m"]
                theirs += payments.line_amount(line, *said) if said else line["amount_c"]
                ours += line["amount_c"]
                differs = differs or bool(said)
                values.add(self._unit_value(line, self.typed[dr_id][line["po_item_id"]][1]))
            self.lines.item(r, self.C_DR_PIECES).setText(fmt_qty(pieces))
            unit = self.lines.item(r, self.C_DR_UNIT)
            if len(values) == 1:
                first_dr, first = members[0]
                unit.setText(self.typed[first_dr][first["po_item_id"]][1])
                unit.setToolTip("Typed here, it goes on every DR with this product. Blank is the same as ours.")
            else:
                # DRs typed with different prices: blank until one price is typed for all of them.
                unit.setText("")
                unit.setToolTip("Different on some DRs. Type a price to put it on all of them.")
            unit.setBackground(tint if _readable(False, unit.text()) else bad)
            self.lines.item(r, self.C_DR_AMOUNT).setText(fmt_plain(theirs) if differs else "")
            self._show_difference(r, theirs - ours if differs else None)
        self.lines.blockSignals(False)
        self.problem = next((diffs[dr_id][1] for dr_id in self.touched if dr_id in diffs and diffs[dr_id][1]), "")
        self._show_totals()
        self._same_price_state()

    def _diffs(self, dr_id: int) -> tuple[dict, str]:
        """What's typed on a DR's products that differs from ours, and the first thing typed that can't be read."""
        info, typed = self.info[dr_id], self.typed[dr_id]
        diffs, problem = {}, ""
        for line in info["lines"]:
            pieces_text, unit_text = (t.strip() for t in typed[line["po_item_id"]])
            pieces = unit = None
            try:
                pieces = parse_qty(pieces_text, "PC") if pieces_text else None
            except ValueError:
                problem = problem or f"{line['item_code'] or line['product']}: {pieces_text} isn't a number of pieces."
            try:
                unit = parse_pesos(unit_text) if unit_text else None
                if unit is not None and unit < 0:
                    raise ValueError
            except (ValueError, ArithmeticError):
                problem = problem or f"{line['item_code'] or line['product']}: {unit_text} isn't a unit price."
                unit = None
            pieces = None if pieces == line["pieces_m"] else pieces
            unit = None if unit == line["unit_c"] else unit
            if pieces is not None or unit is not None:
                diffs[line["po_item_id"]] = (pieces, unit)
        return diffs, problem

    def _typed(self, item=None):
        """Each product's amount on the supplier's DR and the DR's total, as they are typed."""
        if self.whole:
            self._typed_whole(item)
            return
        if self.current is None:
            self._show_totals()
            self._same_price_state()
            return
        if item is not None:
            self.touched.add(self.current)
            self.typed[self.current][self.lines.item(item.row(), 0).data(Qt.ItemDataRole.UserRole)] = [self.lines.item(item.row(), c).text() for c in (self.C_DR_PIECES, self.C_DR_UNIT)]
        info = self.info[self.current]
        diffs, problem = self._diffs(self.current)
        bad = QColor(theme.current.RED_PALE)
        tint = editable_tint()
        self.lines.blockSignals(True)
        for r, line in enumerate(info["lines"]):
            said = diffs.get(line["po_item_id"])
            dr_amount = payments.line_amount(line, *said) if said else None
            self.lines.item(r, self.C_DR_AMOUNT).setText(fmt_plain(dr_amount) if said else "")
            # How the DR compares with the PO, beside it. Red when the DR is higher, green when lower, blank when the same.
            self._show_difference(r, dr_amount - line["amount_c"] if said else None)
            typed = self.typed[self.current][line["po_item_id"]]
            for c, text in ((self.C_DR_PIECES, typed[0]), (self.C_DR_UNIT, typed[1])):
                self.lines.item(r, c).setBackground(tint if _readable(c == self.C_DR_PIECES, text) else bad)
        self.lines.blockSignals(False)
        self.problem = problem or next((p for dr_id in self.touched if dr_id in self.info and dr_id != self.current for p in [self._diffs(dr_id)[1]] if p), "")
        self._show_totals()
        self._same_price_state()

    def _dr_total(self, receipt: dict) -> int | None:
        """The supplier's total as it stands on screen: worked out from the products typed, or what was saved, or nothing."""
        dr_id = receipt["id"]
        if dr_id in self.touched and dr_id in self.info:
            diffs, _ = self._diffs(dr_id)
            if diffs:
                return payments.dr_total(self.info[dr_id], diffs)
            if any(line["dr_pieces_m"] is not None or line["dr_unit_c"] is not None for line in self.info[dr_id]["lines"]):
                return None  # every product the same again: taken as matching
        return receipt["total_c"]

    def _whole_totals(self) -> tuple[int, int]:
        """The supplier's total for the whole OF, every DR as it stands on screen (a DR left blank counts as ours), and ours."""
        total = ours = 0
        for receipt in self.receipts:
            base = self._load(receipt["id"])["base_c"]
            said = self._dr_total(receipt)
            total += base if said is None else said
            ours += base
        return total, ours

    @staticmethod
    def _compare_text(name: str, total: int | None, ours: int) -> str:
        if total is None or total == ours:
            return f"{name}: same as ours, {fmt_peso(ours)}."
        off = total - ours
        text = f"{name}: supplier's total {fmt_peso(total)}, {fmt_peso(abs(off))} {'more' if off > 0 else 'less'} than ours ({fmt_peso(ours)})."
        if abs(off) > payments.TOLERANCE_C:
            # A lower DR is paid as it says; only a higher one waits for a sign-off.
            text += " Higher: Accounting accepts it and management signs off; ours is paid." if off > 0 else " Lower: paid as the DR says, no sign-off needed."
        return text

    def _show_totals(self):
        for row, receipt in enumerate(self.receipts):
            total = self._dr_total(receipt)
            self.drs.item(row, 3).setText(fmt_plain(total) if total is not None else "")
        whole = self._whole_totals() if len(self.receipts) > 1 and self.all_drs.rowCount() else None
        if whole:
            self.all_drs.item(0, 3).setText(fmt_plain(whole[0]))
        if self.problem:
            text = self.problem
        elif self.whole and whole:
            text = self._compare_text("All DRs", *whole)
        elif self.current is not None:
            receipt = next(d for d in self.receipts if d["id"] == self.current)
            text = self._compare_text(receipt["dr_number"], self._dr_total(receipt), self.info[self.current]["base_c"])
        else:
            text = ""
        self.total.setText(text)
        self.total.setObjectName("Error" if self.problem else "PanelTitle")
        self.total.style().unpolish(self.total)
        self.total.style().polish(self.total)
        self.save_btn.setEnabled(not self.problem)

    def save(self):
        """Saves only the DRs someone touched, the one shown and any changed by Same price on all DRs. A DR whose date alone changed keeps the products saved before."""
        self._commit_edit()
        entries = {}
        for receipt in self.receipts:
            dr_id = receipt["id"]
            if dr_id not in self.touched:
                continue
            self._load(dr_id)
            entries[dr_id] = (date_value(self.dates[dr_id]), {item: tuple(texts) for item, texts in self.typed[dr_id].items()})
        if entries:
            payments.save_dr_lines(self.conn, self.user, self.order["id"], entries)
        self.done.emit()


class CheckDialog(QDialog):
    """Records a check once it has been printed."""

    def __init__(self, parent, conn, user, group: dict):
        super().__init__(parent)
        self.conn, self.user, self.group = conn, user, group
        self.setWindowTitle(f"Record the check for {group['supplier']}")
        col = QVBoxLayout(self)
        numbers = ", ".join(o["of_number"] for o in group["orders"])
        note = label(f"{group['supplier']}: {numbers}. Type the number of the printed check.", "Muted")
        note.setWordWrap(True)
        col.addWidget(note)
        form = QFormLayout()
        self.number = QLineEdit()
        self.day = date_edit(group["due"].isoformat())
        self.amount = QLineEdit(fmt_plain(group["check_c"]))
        form.addRow("Check number", self.number)
        form.addRow("Check date", self.day)
        form.addRow("Amount", self.amount)
        col.addLayout(form)
        self.lower = label("", "Muted")
        self.lower.setWordWrap(True)
        col.addWidget(self.lower)
        self.amount.textChanged.connect(self._amount_changed)
        self._amount_changed()
        col.addLayout(_buttons(button("Cancel", self.reject), button("Record check", self.save, "Primary")))

    def _amount_changed(self, *_):
        """A lower check, with a reason and signed off, is final. Said before it's recorded, so nobody is surprised."""
        try:
            typed = payments._amount(self.amount.text())
        except POError:
            typed = None
        expected = self.group["check_c"]
        lower = typed is not None and expected - typed > payments.TOLERANCE_C
        self.lower.setText(f"Lower than the program's {fmt_peso(expected)}. Once signed off, the {fmt_peso(expected - typed)} difference is final and is not added to the next check."
                           if lower else "")
        self.lower.setVisible(lower)

    def save(self, reason: str = ""):
        ids = [o["id"] for o in self.group["orders"]]
        try:
            payments.record_check(self.conn, self.user, ids, self.number.text(), date_value(self.day), self.amount.text(), reason if isinstance(reason, str) else "")
        except payments.CheckDifference as difference:
            why = ask_text(self, "The amount is different", f"{difference}\n\nIf it's right, write why.")
            if why:
                self.save(why)
            elif why is not None:
                error(self, "Write why the amount is different.")
            return
        self.accept()


class BatchCheckDialog(QDialog):
    """Records a batch once it has been printed. Type the first and last numbers, and the last has to land where the count says it should, which catches a check skipped or spoiled."""

    def __init__(self, parent, conn, user, groups: list[dict]):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.groups = sorted(groups, key=lambda g: (g["due"], g["supplier"]))
        n = len(self.groups)
        self.setWindowTitle(f"Record {n} check{'s' if n != 1 else ''}")
        self.resize(820, 480)
        col = QVBoxLayout(self)
        col.addWidget(label(f"{n} check{'s' if n != 1 else ''}, {fmt_peso(sum(g['check_c'] for g in self.groups))}", "PanelTitle"))
        note = label("Print these in this order, soonest check date first. Type the first check number, or the last, and the list numbers them in that order. "
                     "Type both to check the run matches. If the printer skipped or spoiled one, change that number in the list.", "Muted")
        note.setWordWrap(True)
        col.addWidget(note)
        form = QHBoxLayout()
        self.first, self.last = QLineEdit(), QLineEdit()
        for text, box in (("First check number", self.first), ("Last check number", self.last)):
            form.addWidget(label(text, "Muted"))
            form.addWidget(box)
        form.addStretch(1)
        col.addLayout(form)
        self.table = make_table(["Check date", "Supplier", "Orders", "Amount", "Check number"], numeric=(3,), stretch=1)
        self.table.setProperty("no_sort", True)  # the print order
        set_widths(self.table, {0: 110, 2: 180, 3: 110, 4: 130})
        fill_table(self.table, [[fmt_date(g["due"].isoformat()), g["supplier"], ", ".join(o["of_number"] for o in g["orders"]), fmt_plain(g["check_c"]), ""] for g in self.groups])
        self.numbers = []
        for row in range(n):
            box = QLineEdit()
            self.table.setCellWidget(row, 4, box)
            box.textChanged.connect(self._compare)
            self.numbers.append(box)
        col.addWidget(self.table, 1)
        self.status = label("", "Muted")
        self.status.setWordWrap(True)
        col.addWidget(self.status)
        col.addLayout(_buttons(button("Cancel", self.reject), button("Record checks", self.save, "Primary")))
        self.first.textChanged.connect(self._fill)
        self.last.textChanged.connect(self._fill_from_last)
        # Printing runs on from the last check recorded, so the next number is offered to start from.
        following = payments.next_check_number(conn)
        if following:
            self.first.setText(following)
        self._compare()

    def _set_numbers(self, numbers: list[str]):
        for box, number in zip(self.numbers, numbers):
            box.blockSignals(True)
            box.setText(number)
            box.blockSignals(False)

    def _fill(self):
        try:
            numbers = payments.check_numbers(self.first.text(), len(self.numbers))
        except payments.POError:
            numbers = [""] * len(self.numbers)
        self._set_numbers(numbers)
        self._compare()

    def _fill_from_last(self):
        """Only the last typed: the list counts back from it, so the first is filled in too."""
        last = self.last.text().strip()
        if not self.first.text().strip() and _digits(last) and int(last) - len(self.numbers) + 1 > 0:
            first = str(int(last) - len(self.numbers) + 1).zfill(len(last))
            self.first.blockSignals(True)
            self.first.setText(first)
            self.first.blockSignals(False)
            self._set_numbers(payments.check_numbers(first, len(self.numbers)))
        self._compare()

    def _problem(self) -> str:
        """What's wrong with the numbers, in words, or nothing."""
        # First and last are a shortcut. Numbers typed straight into the list are just as good, and the last is only compared when it's typed.
        numbers = [box.text().strip() for box in self.numbers]
        if not all(numbers):
            return "Every check needs a number: type it in the list, or type the first or last check number above."
        if len(set(numbers)) != len(numbers):
            return "Two checks in the list have the same number."
        if self.last.text().strip() and self.last.text().strip() != numbers[-1]:
            last, first = self.last.text().strip(), self.first.text().strip()
            run = int(last) - int(first) + 1 if _digits(last) and _digits(first) else None
            count = f" {first} to {last} is {run} numbers for {len(numbers)} checks." if run is not None else ""
            return f"The list ends at {numbers[-1]}, but you typed {last}.{count} If the printer skipped or spoiled a check, change that number in the list to match the paper checks."
        return ""

    def _compare(self):
        n = len(self.numbers)
        self.status.setText(self._problem() or (f"Check {self.numbers[0].text().strip()}." if n == 1 else f"{n} checks, {self.numbers[0].text().strip()} to {self.numbers[-1].text().strip()}."))

    def save(self):
        problem = self._problem()
        if problem:
            raise payments.POError(problem)
        payments.record_checks(self.conn, self.user, [{"of_ids": [o["id"] for o in g["orders"]], "number": box.text(), "date": g["due"].isoformat(), "amount": fmt_plain(g["check_c"])} for g, box in zip(self.groups, self.numbers)])
        self.accept()


class ClaimDialog(QDialog):
    """Records that the supplier took a check."""

    def __init__(self, parent, conn, user, check: dict):
        super().__init__(parent)
        self.conn, self.user, self.check = conn, user, check
        self.setWindowTitle(f"Check {check['check_number']} claimed")
        col = QVBoxLayout(self)
        col.addWidget(label(f"{check['supplier']}, {fmt_peso(check['amount_c'])}", "PanelTitle"))
        form = QFormLayout()
        self.day = date_edit(today().isoformat())
        self.who = QLineEdit()
        self.who.setPlaceholderText("Optional")
        form.addRow("Date claimed", self.day)
        form.addRow("Claimed by", self.who)
        col.addLayout(form)
        col.addLayout(_buttons(button("Cancel", self.reject), button("Mark claimed", self.save, "Primary")))

    def save(self):
        payments.claim_check(self.conn, self.user, self.check["id"], date_value(self.day), self.who.text())
        self.accept()


class ClearDialog(QDialog):
    """Records that the bank paid a check, from the bank statement."""

    def __init__(self, parent, conn, user, check: dict):
        super().__init__(parent)
        self.conn, self.user, self.check = conn, user, check
        self.setWindowTitle(f"Check {check['check_number']} cleared")
        col = QVBoxLayout(self)
        col.addWidget(label(f"{check['supplier']}, {fmt_peso(check['amount_c'])}", "PanelTitle"))
        form = QFormLayout()
        self.day = date_edit(today().isoformat())
        form.addRow("Date cleared", self.day)
        col.addLayout(form)
        col.addLayout(_buttons(button("Cancel", self.reject), button("Mark cleared", self.save, "Primary")))

    def save(self):
        payments.clear_check(self.conn, self.user, self.check["id"], date_value(self.day))
        self.accept()


class ClearTickedDialog(QDialog):
    """Marks the ticked checks cleared, from one bank statement, with one date for all of them."""

    def __init__(self, parent, conn, user, checks: list[dict]):
        super().__init__(parent)
        self.conn, self.user, self.checks = conn, user, checks
        n = len(checks)
        self.setWindowTitle(f"{n} check{'s' if n != 1 else ''} cleared")
        col = QVBoxLayout(self)
        col.addWidget(label(f"{n} check{'s' if n != 1 else ''}, {fmt_peso(sum(c['amount_c'] for c in checks))}", "PanelTitle"))
        note = label(", ".join(f"{c['check_number']} {c['supplier']}" for c in checks), "Muted")
        note.setWordWrap(True)
        col.addWidget(note)
        form = QFormLayout()
        self.day = date_edit(today().isoformat())
        form.addRow("Date cleared", self.day)
        col.addLayout(form)
        col.addLayout(_buttons(button("Cancel", self.reject), button(f"Mark {n} cleared", self.save, "Primary")))

    def save(self):
        payments.mark_cleared_many(self.conn, self.user, [c["id"] for c in self.checks], date_value(self.day))
        self.accept()


def _when(check: dict) -> str:
    """How far off a check is, who took it, or when it cleared."""
    if check["bucket"] == payments.CLEARED:
        return f"Cleared {fmt_date(check['cleared_on'])}"
    if check["bucket"] == payments.CLAIMED:
        return fmt_date(check["claimed_on"]) + (f", {check['claimed_by_name']}" if check["claimed_by_name"] else "")
    days = check["days"]
    if days > 0:
        return f"In {days} day{'s' if days != 1 else ''}"
    return "Today" if days == 0 else f"{-days} day{'s' if days != -1 else ''} ago"


# The amounts that make up a check. Delivered is there to read, and comes off nothing: a shortage comes off as its debit memo, or under Short if nobody charged it.
MONEY = {"Order amount": "total_c", "Delivered": "delivered_c", "DM": "dm_c", "CS": "cs_c", "Short": "unclaimed_c", "Stickers": "sticker_c", "Discount": "term_c"}
NUMERIC = {*MONEY, "To pay", "DR total", "Difference", "Credit", "We owe", "Check amount"}
TIPS = {"Order amount": "What the live POs come to, already after any product discount.",
        "Delivered": "What arrived, at the order's costs. It's here to read: the check is the order amount less the columns after this one.",
        "DM": "Debit memos for pieces short.", "CS": "Claim stubs for pieces damaged.", "Short": "Pieces short that no debit memo charged yet.",
        "Stickers": "Sticker charge on pieces received.", "Discount": "Term discount.",
        "Credit": "Paid too much on earlier checks, say a debit memo came after the check. It comes off this one.",
        "We owe": "Paid too little on earlier checks, say a check was typed low. It's added to this one."}
SUPPLIER = "Supplier"
# These are usually empty, so they only take up room when a row has something in them.
ONLY_WHEN_USED = ("Short", "Credit", "We owe", "Note")


def _amount(value: int) -> str:
    return fmt_plain(value) if value else ""


def _since(orders: list[dict]) -> int:
    """How far what a check's orders come to today is from what the check paid them, say a claim stub after the check. Nothing within a peso."""
    off = sum(o["pay_c"] - o["paid_c"] for o in orders)
    return off if abs(off) > payments.TOLERANCE_C else 0


def _since_text(off: int) -> str:
    return f"Changed since this check: {'+' if off > 0 else ''}{fmt_plain(off)}" if off else ""


def _money(row: dict) -> dict:
    return {header: _amount(row[key]) for header, key in MONEY.items() if key in row}


def _added_up(orders: list[dict]) -> dict:
    return {key: sum(o[key] for o in orders) for key in MONEY.values()}


def _pieces(lines: list[dict], counted: bool = True) -> dict:
    """Pieces ordered beside pieces the branches counted, so the DRs can be checked against what came. Not counted says so."""
    ordered = fmt_qty(sum(line["ordered_m"] for line in lines))
    if not counted:
        return {"Pcs ordered": ordered, "Pcs delivered": "Not counted"}
    damaged = sum(line["damaged_m"] for line in lines)
    return {"Pcs ordered": ordered, "Pcs delivered": fmt_qty(sum(line["received_m"] for line in lines)) + (f", {fmt_qty(damaged)} damaged" if damaged else "")}


def _order_pieces(order: dict) -> dict:
    lines = [line for po in order["pos"] for line in po["lines"]]
    counted = [po["counted"] for po in order["pos"]]
    if any(counted) and not all(counted):
        return {**_pieces(lines), "Pcs delivered": f"{_pieces([l for po in order['pos'] if po['counted'] for l in po['lines']])['Pcs delivered']}, {counted.count(False)} PO not counted"}
    return _pieces(lines, all(counted))


def _branch_pieces(by: dict) -> str:
    """A branch's pieces: all of them when everything came, how many of how many when not, or what was ordered when there's no count."""
    if not by["counted"]:
        return fmt_qty(by["ordered"])
    return fmt_qty(by["ordered"]) if by["got"] == by["ordered"] else f"{fmt_qty(by['got'])} of {fmt_qty(by['ordered'])}"


def _product(line: dict) -> str:
    return ", ".join(part for part in (line["description"], line["size"], line["color"]) if part)


class DifferenceDialog(QDialog):
    """A DR difference laid out as a table: each branch PO with its DR, what the order says, and the gap. Each opens to its products, with the unit price,
    so the approver sees where the money differs before signing. With ask_reason, it's Accounting accepting it and asks why."""

    def __init__(self, parent, order: dict, ask_reason: bool = False):
        super().__init__(parent)
        self.order, self.reason = order, ""
        self.setWindowTitle("Accept the difference" if ask_reason else "Sign off")
        col = QVBoxLayout(self)
        col.addWidget(label(f"{order['of_number']}, {order['supplier']}", "PanelTitle"))
        col.addWidget(label("The supplier's DRs differ from what the order came to. Press the arrow on a PO to see its products. Products in bold differ on the DR.", "Muted"))
        self.tree = make_tree(["PO / product", "Branch", "Pieces", "Unit price", "Order", "DR", "Difference", "The DR says"], numeric=(2, 3, 4, 5, 6), stretch=0)
        said = order.get("line_diffs") or {}
        typed: dict[int, int] = {}
        for d in order["receipts"]:
            if d["dr_date"]:
                typed[d["po_id"]] = typed.get(d["po_id"], 0) + d["total_c"]
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for p in order["pos"]:
            if p["po_id"] not in typed:
                continue
            off = typed[p["po_id"]] - p["delivered_c"]
            pieces = sum(line["supplier_m"] if p["counted"] else line["ordered_m"] for line in p["lines"])
            top = QTreeWidgetItem([p["po_number"], p["branch"], fmt_qty(pieces) + ("" if p["counted"] else ", not counted"), "", fmt_plain(p["delivered_c"]), fmt_plain(typed[p["po_id"]]),
                                   (f"+{fmt_plain(off)}" if off > 0 else fmt_plain(off)) if abs(off) > payments.TOLERANCE_C else "Matches"])
            for line in p["lines"]:
                got = line["supplier_m"] if p["counted"] else line["ordered_m"]
                unit = round(line["order_c"] * 1000 / line["ordered_m"]) if line["ordered_m"] else 0
                # A product typed on the DR shows what the DR says, and what that does to the amount.
                d = said.get(line["id"])
                dr_says = ""
                if d:
                    dr_says = ", ".join(x for x in ((f"{fmt_qty(d['dr_pieces_m'])} pcs" if d["dr_pieces_m"] != d["pieces_m"] else ""),
                                                    (f"{fmt_plain(d['dr_unit_c'])} a unit" if d["dr_unit_c"] != d["unit_c"] else "")) if x)
                child = QTreeWidgetItem([f"{line['item_code']} {_product(line)}".strip(), "", fmt_qty(got), fmt_plain(unit), fmt_plain(line["delivered_c"]),
                                         fmt_plain(line["delivered_c"] + d["off_c"]) if d else "", (f"+{fmt_plain(d['off_c'])}" if d["off_c"] > 0 else fmt_plain(d["off_c"])) if d else "", dr_says])
                if d:
                    font = child.font(0)
                    font.setBold(True)
                    for c in range(8):
                        child.setFont(c, font)
                    child.setToolTip(0, d["text"])
                top.addChild(child)
            self.tree.addTopLevelItem(top)
            top.setExpanded(abs(off) > payments.TOLERANCE_C or any(line["id"] in said for line in p["lines"]))
        for item in [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]:
            for x in (item, *(item.child(k) for k in range(item.childCount()))):
                for c in (2, 3, 4, 5, 6):
                    x.setTextAlignment(c, right)
        for c in range(1, 8):
            self.tree.resizeColumnToContents(c)
            self.tree.setColumnWidth(c, self.tree.columnWidth(c) + 16)
        col.addWidget(self.tree, 1)
        lower = payments._lower_part({p: t for p, t in typed.items()}, order["pos"])
        # A lower DR is paid as it says, with no sign-off. Only the higher DRs are accepted and signed off here.
        follows = (f" The DRs {fmt_peso(lower)} lower are already paid as they say. Where a DR is higher, the order's costs are paid."
                   if lower else " The payment follows the order's costs.")
        total = label(f"DRs higher than the order by {fmt_peso(order.get('higher_c', order['off_c']))}.{follows}", "PanelTitle")
        total.setWordWrap(True)
        col.addWidget(total)
        if ask_reason:
            self.why = QLineEdit()
            self.why.setPlaceholderText("Why is this difference acceptable? For example: the supplier billed the delivery charge on the DR.")
            col.addWidget(self.why)
            self.missing = label("", "Error")
            self.missing.hide()
            col.addWidget(self.missing)
        else:
            self.why = None
            col.addWidget(label(f"Accepted because: {order['accepted_reason']}", "Muted"))
        col.addLayout(_buttons(button("Go back", self.reject), button("Accept the difference" if ask_reason else "Sign off", self._ok, "Primary")))
        self.resize(1000, 480)

    def _ok(self):
        if self.why is not None:
            self.reason = self.why.text().strip()
            if not self.reason:
                # Said in the dialog, next to the box, so it stays open with the box ready to type in.
                self.missing.setText("Write why the amount is different.")
                self.missing.show()
                self.why.setFocus()
                return
        self.accept()


def _after_supplier(tree, headers: list[str], names: tuple[str, ...]) -> None:
    """Shows these columns right after Supplier. Only where they show moves, so each column keeps its place in the saved choices and widths."""
    header = tree.header()
    at = header.visualIndex(headers.index(SUPPLIER))
    for k, name in enumerate(names, 1):
        header.moveSection(header.visualIndex(headers.index(name)), at + k)


def _flow_note(row: dict) -> str:
    """Which of a line's checks may not go out as dated: one still waiting for a sign-off, or one nobody claimed though its date has passed."""
    parts = []
    if row.get("waiting_n"):
        parts.append(f"{row['waiting_n']} waiting for sign-off, {fmt_plain(row['waiting_c'])}")
    if row.get("passed_n"):
        parts.append(f"{row['passed_n']} date passed, {fmt_plain(row['passed_c'])}")
    return "; ".join(parts)


class PaymentsPage(QWidget):
    """Every finished order, from its DRs to its check. Each tab states the one thing left to do."""
    changed = Signal()
    TO_CHECK, TO_PAY, FUTURE, DUE, CLAIMED, CLEARED, CANCELLED, FLOW = "TO_CHECK", "TO_PAY", "FUTURE", "DUE", "CLAIMED", "CLEARED", "CANCELLED", "FLOW"
    READY, FINAL_FIRST = "READY", "FINAL_FIRST"
    TABS = {TO_CHECK: "Check the DRs", TO_PAY: "Checks to make", FUTURE: "Future checks", DUE: "Unclaimed", CLAIMED: "Claimed", CLEARED: "Cleared", CANCELLED: "Cancelled checks", FLOW: "Cash flow"}
    CHECK_TABS = (FUTURE, DUE, CLAIMED, CLEARED, CANCELLED)

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.tab = self.TO_CHECK
        self.orders, self.groups, self.checks, self.flow_rows = [], [], [], []
        self.all_counts = None
        self.can_edit = user.can("payment.edit")
        # Accounting does the payments. Admin or management gives the final check on an amount that differs.
        self.can_sign = user.can("payment.signoff")
        root, top = page_layout(self, "Payments")
        top.addWidget(label("Checks are printed outside the program for now", "Muted"))
        top.addSpacing(12)
        self.columns_btn = button("Columns", lambda: self.choosers[self.tables[self.tab]].open_under(self.columns_btn))
        top.addWidget(self.columns_btn)
        self.tabs = register_tabs(self.TABS, self._set_tab)
        root.addWidget(self.tabs)
        self.totals = label("", "Muted")
        self.totals.setWordWrap(True)
        root.addWidget(self.totals)
        self.search = QLineEdit()
        self.search.setPlaceholderText("OF or DR number, check number, supplier or code")
        self.search.setClearButtonEnabled(True)
        # Typing waits for a pause before the lists are worked out again, not every key. Text put in by the program, or a cleared box, shows at once.
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(300)
        self.search_timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(self._search_changed)
        root.addWidget(self.search)
        action = QHBoxLayout()
        self.by_month = False
        self.flow_buttons = {}
        group = QButtonGroup(self)
        for monthly, text in ((False, "By date"), (True, "By month")):
            b = QPushButton(text)
            b.setObjectName("Count")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, monthly=monthly: self._set_flow_view(monthly))
            group.addButton(b)
            action.addWidget(b)
            self.flow_buttons[monthly] = b
        self.flow_buttons[False].setChecked(True)
        self.note = label("", "PanelTitle")
        self.note.setWordWrap(True)
        action.addWidget(self.note, 1)
        self.show_all_btn = button("Show all cleared", self.show_all_cleared)
        action.addWidget(self.show_all_btn)
        self.show_all = False
        self.differs_btn = button("DR differs", self._dr_differs)
        self.second_btn = button("", self._second)
        self.primary_btn = button("", self._primary, "Primary")
        self.cancel_btn = button("Cancel check", self.cancel_check, "Danger")
        for b in (self.differs_btn, self.second_btn, self.primary_btn):
            action.addWidget(b)
        # Room before Cancel check, so it isn't pressed in place of Mark claimed.
        action.addSpacing(32)
        action.addWidget(self.cancel_btn)
        root.addLayout(action)
        self.columns, self.choosers, self.opened = {}, {}, {}
        # A column for each branch, so an order's products read like Approvals: the pieces under every branch.
        self.branches = [(b["code"], b["name"]) for b in masters.list_branches(conn, active_only=True, branches_only=True)]
        names = [name for _, name in self.branches]
        self.branch_names = set(names)
        self.to_check = self._tree("to-check", ["OF number", SUPPLIER, "Pcs ordered", "Pcs delivered", *names, *MONEY, "To pay", "DRs typed", "DR total", "Difference", "Next step"], hidden=("DRs typed", "Next step"))
        # On the check tabs the check amount sits beside the supplier, so it's on the screen without scrolling. The pieces under each branch are for comparing DRs,
        # so there they start hidden, and Columns brings them back. Checks to make keeps the pieces ordered and delivered.
        self.to_pay = self._tree("to-pay", ["Check date", SUPPLIER, "Orders", "Pcs ordered", "Pcs delivered", *names, *MONEY, "Credit", "We owe", "Check amount", "Final OF"], hidden=("Orders", *names))
        self.paid = self._tree("paid", ["Check number", "Check date", SUPPLIER, "Orders", "Pcs ordered", "Pcs delivered", *names, *MONEY, "Credit", "We owe", "Check amount", "Recorded by", "Note", "When"],
                               hidden=("Orders", "Recorded by", "Pcs ordered", "Pcs delivered", *names))
        _after_supplier(self.to_pay, self.columns[self.to_pay], ("Check amount", "Final OF"))
        _after_supplier(self.paid, self.columns[self.paid], ("Check amount", "When"))
        self.ticked, self.just_ticked = set(), False
        # Claimed checks ticked to mark cleared together, from one bank statement.
        self.clear_ticked = set()
        self.paid.itemChanged.connect(self._clear_tick_changed)
        self.owed_alone, self.credit_alone = [], []
        self.to_pay.itemChanged.connect(self._tick_changed)
        # Checks to make runs by section and date, which is the order to print them in, so its headings don't re-sort it.
        self.to_pay.setProperty("no_sort", True)
        self.flow = make_table(["Check date", "Not yet claimed", "Claimed, not cleared", "Still to make", "Total for the date", "Running total", "Note"], numeric=(1, 2, 3, 4, 5), stretch=0)
        set_widths(self.flow, {1: 150, 2: 160, 3: 150, 4: 150, 5: 150})
        self.tables = {self.TO_CHECK: self.to_check, self.TO_PAY: self.to_pay, self.FLOW: self.flow, **{t: self.paid for t in self.CHECK_TABS}}
        self.choosers[self.flow] = smart_columns(self.flow, f"payments-flow-{user.username}")
        self.flow.itemSelectionChanged.connect(self._show_action)
        # The lists on the left. A DR that differs opens on the right, so the list stays in view.
        self.list_side = QWidget()
        lists = QVBoxLayout(self.list_side)
        lists.setContentsMargins(0, 0, 0, 0)
        for table in (self.to_check, self.to_pay, self.paid, self.flow):
            lists.addWidget(table, 1)
        self.empty = label("", "Empty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        # It takes the room an empty list leaves, so the search box and tabs stay where they are.
        lists.addWidget(self.empty, 1)
        self.dr_panel = DrPanel(conn, user)
        self.dr_panel.hide()
        self.dr_panel.closed.connect(self._close_panel)
        self.dr_panel.done.connect(self._panel_saved)
        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.addWidget(self.list_side)
        self.split.addWidget(self.dr_panel)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)
        self.split.setChildrenCollapsible(False)
        root.addWidget(self.split, 1)
        self.close_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=lambda: self.dr_panel.isVisibleTo(self) and self._close_panel())
        self.breakdown = label("", "Muted")
        self.breakdown.setWordWrap(True)
        root.addWidget(self.breakdown)
        self.refresh()

    def _tree(self, key: str, headers: list[str], hidden: tuple[str, ...] = ()):
        """A list whose rows open to show what's inside, with columns each person chooses. The first column holds the arrow, and Supplier says what each row is."""
        tree = make_tree(headers, numeric=tuple(i for i, h in enumerate(headers) if h in NUMERIC or h in self.branch_names))
        for col, header in enumerate(headers):
            if header in TIPS:
                tree.headerItem().setToolTip(col, TIPS[header])
        self.columns[tree] = headers
        slug = lambda header: header.lower().replace(" ", "-")
        self.choosers[tree] = ColumnChooser(tree, f"payments-{key}-{self.user.username}", {slug(h): i for i, h in enumerate(headers) if i and h != SUPPLIER}, tuple(slug(h) for h in hidden))
        self.choosers[tree].columns_changed.connect(lambda: self._fit(tree))
        self.opened[tree] = set()
        tree.currentItemChanged.connect(lambda *_: self._show_action())
        # Only the arrow opens a row. A click on the row just selects it.
        tree.itemExpanded.connect(lambda item: self._opening(tree, item, True))
        tree.itemCollapsed.connect(lambda item: self._opening(tree, item, False))
        return tree

    def _clicked(self, item, _column):
        """Opens or closes a row. Not tied to clicks any more: only the arrow opens a row."""
        if not self.just_ticked and item.childCount():
            item.setExpanded(not item.isExpanded())

    def _tick_changed(self, item, _column=0):
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if item.parent() or key is None:
            return
        self.just_ticked = True
        QTimer.singleShot(0, self._tick_settled)
        on = item.checkState(0) == Qt.CheckState.Checked
        if isinstance(key, str):  # A section's box ticks everything in it.
            chosen = [g for g in self.groups if self._section(g) == key]
        else:
            chosen = [self.groups[key - 1]]
        for g in chosen:
            (self.ticked.add if on else self.ticked.discard)(self._tick_key(g))
        self._show_ticks()
        self._show_action()

    def _tick_settled(self):
        self.just_ticked = False

    def _clear_tick_changed(self, item, _column=0):
        """A tick on the Claimed tab chooses that check for Mark cleared."""
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if self.tab != self.CLAIMED or item.parent() or not isinstance(key, int):
            return
        self.just_ticked = True
        QTimer.singleShot(0, self._tick_settled)
        (self.clear_ticked.add if item.checkState(0) == Qt.CheckState.Checked else self.clear_ticked.discard)(key)
        self._show_action()

    def _ticked_checks(self) -> list[dict]:
        return [c for c in self.checks if c["id"] in self.clear_ticked] if self.tab == self.CLAIMED else []

    def _section(self, group: dict) -> str:
        return self.FINAL_FIRST if any(o["final_of_due"] for o in group["orders"]) else self.READY

    def _tick_key(self, group: dict) -> tuple:
        return group["due"], group["supplier_id"]

    def _ticked_groups(self, section: str) -> list[dict]:
        return [g for g in self.groups if self._section(g) == section and self._tick_key(g) in self.ticked]

    def _show_ticks(self):
        """Each box shows what's chosen. A section's box is ticked once everything in it is."""
        self.to_pay.blockSignals(True)
        for i in range(self.to_pay.topLevelItemCount()):
            item = self.to_pay.topLevelItem(i)
            key = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(key, str):
                on = bool(self._groups_in(key)) and len(self._ticked_groups(key)) == len(self._groups_in(key))
            else:
                on = self._tick_key(self.groups[key - 1]) in self.ticked
            item.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self.to_pay.blockSignals(False)

    def _groups_in(self, section: str) -> list[dict]:
        return [g for g in self.groups if self._section(g) == section]

    def _path(self, item) -> tuple:
        path = []
        while item:
            path.insert(0, item.text(0))
            item = item.parent()
        return tuple(path)

    def _opening(self, tree, item, opened: bool):
        (self.opened[tree].add if opened else self.opened[tree].discard)(self._path(item))
        # Opening a row can bring in longer text, so the columns fit again. Queued, because the row's text settles first.
        QTimer.singleShot(0, lambda: self._fit(tree))

    def _fit(self, tree):
        """Every column as wide as what it holds, with the room left going to Supplier. If there are too many to fit, the list scrolls sideways."""
        headers, boxes = self.columns[tree], self.choosers[tree].checks
        for header in ONLY_WHEN_USED:
            if header in headers and boxes[header.lower().replace(" ", "-")].isChecked():
                col = headers.index(header)
                tree.setColumnHidden(col, not any(tree.topLevelItem(i).text(col) for i in range(tree.topLevelItemCount())))
        supplier = headers.index(SUPPLIER)
        fit_tree_columns(tree, supplier, None)
        others = sum(tree.columnWidth(c) for c in range(tree.columnCount()) if c != supplier)
        tree.setColumnWidth(supplier, max(200, tree.viewport().width() - others))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_current()

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_current()

    def _fit_current(self):
        if self.tables[self.tab] in self.columns:
            self._fit(self.tables[self.tab])

    def _row(self, tree, parent, cells: dict, key=None):
        headers = self.columns[tree]
        item = QTreeWidgetItem(parent, [cells.get(h, "") for h in headers])
        for col, header in enumerate(headers):
            if header in NUMERIC or header in self.branch_names:
                item.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        item.setToolTip(headers.index(SUPPLIER), cells.get(SUPPLIER, ""))
        if key is not None:
            item.setData(0, Qt.ItemDataRole.UserRole, key)
        return item

    def _order_row(self, tree, parent, order: dict, cells: dict, key=None):
        """An order, which opens to its products, each with its pieces under every branch, like Approvals. A product with damage opens to the damage:
        how many pieces, what a piece cost, and what they come to."""
        first = self.columns[tree][0]
        codes = [c for c, _ in self.branches]
        products: dict[tuple, dict] = {}
        totals = {c: {"ordered": 0, "got": 0, "counted": True} for c in codes}
        for po in order["pos"]:
            code = po["branch_code"]
            for line in po["lines"]:
                p = products.setdefault((line["item_code"], line["description"], line["size"], line["color"]), {
                    "line": line, "ordered": 0, "got": 0, "damaged": 0, "worth": 0, "order_c": 0, "delivered_c": 0, "unclaimed_c": 0, "counted": True, "by": {}})
                worth = round(line["order_c"] * line["damaged_m"] / line["ordered_m"]) if line["ordered_m"] else 0
                p["ordered"] += line["ordered_m"]
                p["got"] += line["received_m"]
                p["damaged"] += line["damaged_m"]
                p["worth"] += worth
                p["counted"] &= po["counted"]
                for k in ("order_c", "delivered_c", "unclaimed_c"):
                    p[k] += line[k]
                mine = p["by"].setdefault(code, {"ordered": 0, "got": 0, "damaged": 0, "counted": po["counted"]})
                mine["ordered"] += line["ordered_m"]
                mine["got"] += line["received_m"]
                mine["damaged"] += line["damaged_m"]
                if code in totals:
                    totals[code]["ordered"] += line["ordered_m"]
                    totals[code]["got"] += line["received_m"]
                    totals[code]["counted"] &= po["counted"]
        branch_cells = lambda by: {name: _branch_pieces(by[c]) for c, name in self.branches if c in by and by[c]["ordered"]}
        item = self._row(tree, parent, {first: order["of_number"], **_order_pieces(order), **branch_cells(totals), **_money(order), **cells}, key)
        for p in products.values():
            line = p["line"]
            delivered = fmt_qty(p["got"]) + (f", {fmt_qty(p['damaged'])} damaged" if p["damaged"] else "") if p["counted"] else "Not counted"
            row = self._row(tree, item, {first: line["item_code"], SUPPLIER: _product(line), "Pcs ordered": fmt_qty(p["ordered"]), "Pcs delivered": delivered, **branch_cells(p["by"]),
                                         "Order amount": _amount(p["order_c"]), "Delivered": _amount(p["delivered_c"]), "Short": _amount(p["unclaimed_c"])})
            if p["damaged"]:
                cost = round(line["order_c"] * 1000 / line["ordered_m"]) if line["ordered_m"] else 0
                self._row(tree, row, {first: "Damaged", SUPPLIER: f"{fmt_qty(p['damaged'])} pieces at {fmt_plain(cost)} a piece", "Pcs delivered": f"{fmt_qty(p['damaged'])} damaged",
                                      **{name: fmt_qty(p["by"][c]["damaged"]) for c, name in self.branches if c in p["by"] and p["by"][c]["damaged"]}, "CS": fmt_plain(p["worth"])})
        added = sum(po["total_c"] - sum(line["order_c"] for line in po["lines"]) for po in order["pos"])
        if abs(added) > len(products):
            self._row(tree, item, {SUPPLIER: "Freight and other PO amounts", "Order amount": fmt_plain(added), "Delivered": fmt_plain(added)})
        for po in order["cancelled_pos"]:
            self._row(tree, item, {first: po["po_number"] or "", SUPPLIER: f"{po['branch']}: PO cancelled"})
        return item

    def _reopen(self, tree, item=None):
        """A refresh keeps open what was open."""
        children = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())] if item is None else [item.child(i) for i in range(item.childCount())]
        for child in children:
            if self._path(child) in self.opened[tree]:
                child.setExpanded(True)
            self._reopen(tree, child)

    def _fill_lists(self):
        for tree in self.columns:
            tree.blockSignals(True)
            tree.clear()
        for r in self.orders:
            self._order_row(self.to_check, self.to_check, r, {SUPPLIER: r["supplier"], "To pay": fmt_plain(r["pay_c"]), "DRs typed": f"{r['entered']} of {len(r['receipts'])}", "DR total": fmt_plain(r["dr_total_c"]) if r["entered"] else "",
                                                              "Difference": fmt_plain(r["difference_c"]) if r["entered"] else "", "Next step": self._next_step(r)}, r["id"])
        section = None
        for i, g in enumerate(self.groups):
            if self._section(g) != section:
                section = self._section(g)
                self._section_row(section)
            top = self._row(self.to_pay, self.to_pay, {"Check date": fmt_date(g["due"].isoformat()), SUPPLIER: g["supplier"], "Orders": ", ".join(o["of_number"] for o in g["orders"]), **_money(_added_up(g["orders"])),
                                                       "Credit": _amount(g["credit_c"]), "We owe": _amount(g["owed_c"]), "Check amount": fmt_plain(g["check_c"]), "Final OF": self._final_of_text(g["orders"])}, i + 1)
            if self.can_edit:
                top.setCheckState(0, Qt.CheckState.Unchecked)
            for o in g["orders"]:
                self._order_row(self.to_pay, top, o, {SUPPLIER: f"Terms {o['terms']}" if o["terms"] else "", "Check amount": fmt_plain(o["pay_c"]), "Final OF": self._final_of_text([o])})
        for c in self.checks:
            orders = payments.orders_on_check(self.conn, c["id"], user=self.user)
            # The amounts beside the check are today's. When they moved after the check, say a claim stub for damage found later, the row says by how much.
            since = 0 if self.tab == self.CANCELLED else _since(orders)
            # A lower check signed off is final, so the row names the agreed deduction.
            agreed = payments.agreed_deduction(self.conn, c)
            note = "; ".join(part for part in (c["cancel_reason"] or c["difference_reason"], f"Agreed deduction {fmt_plain(agreed)}, final" if agreed else "", _since_text(since)) if part)
            top = self._row(self.paid, self.paid, {"Check number": c["check_number"], "Check date": fmt_date(c["check_date"]), SUPPLIER: c["supplier"], "Orders": c["of_numbers"] or "", **_money(_added_up(orders)), "Credit": _amount(max(c["credit_applied_c"], 0)), "We owe": _amount(max(-c["credit_applied_c"], 0)),
                                                   "Check amount": fmt_plain(c["amount_c"]), "Recorded by": c["recorded_name"], "Note": note,
                                                   "When": "" if self.tab == self.CANCELLED else "Waiting for sign-off" if c["waiting_signoff"] else _when(c)}, c["id"])
            if since or agreed:
                tip = (f"The orders on this check come to {fmt_peso(sum(o['pay_c'] for o in orders))} today. The check paid {fmt_peso(sum(o['paid_c'] for o in orders))} for them. "
                       "The difference goes, or went, on the supplier's next check.") if since else ""
                if agreed:
                    tip += (" " if tip else "") + (f"The check is {fmt_peso(agreed)} lower than the program worked out ({fmt_peso(c['expected_c'])}), with a reason, and signed off: "
                                                   "an agreed deduction, final, never added to the next check.")
                for col in range(self.paid.columnCount()):
                    top.setToolTip(col, tip)
            if self.tab == self.CLAIMED and self.can_edit:
                top.setCheckState(0, Qt.CheckState.Checked if c["id"] in self.clear_ticked else Qt.CheckState.Unchecked)
            for o in orders:
                self._order_row(self.paid, top, o, {"Check amount": fmt_plain(o["paid_c"]), "Note": _since_text(_since([o]))})
        for tree in self.columns:
            self._reopen(tree)
            resort(tree)
            tree.blockSignals(False)
        if self.can_edit:
            self._show_ticks()

    def _final_of_text(self, orders: list[dict]) -> str:
        if any(o["final_of_due"] for o in orders):
            return "Print"
        return "Printed" if any(o["changed"] for o in orders) else "Not needed"

    def _section_row(self, section: str):
        groups = self._groups_in(section)
        n = len(groups)
        title = {self.READY: "Ready for checks", self.FINAL_FIRST: "Print the Final OF first, and these move up"}[section]
        item = self._row(self.to_pay, self.to_pay, {"Check date": f"{title}:  {n} check{'s' if n != 1 else ''}, {fmt_peso(sum(g['check_c'] for g in groups))}"}, section)
        item.setFirstColumnSpanned(True)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable if self.can_edit else Qt.ItemFlag.ItemIsEnabled)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)

    def _set_tab(self, tab):
        self.tab = tab
        self.dr_panel.hide()
        self.show_all = False
        self.clear_ticked.clear()
        self.refresh()

    def show_all_cleared(self):
        self.show_all = True
        self.refresh()

    def _search_changed(self, text: str):
        if self.search.isModified() and text.strip():
            self.search_timer.start()
        else:
            self.refresh()

    def refresh(self, *_):
        self.search_timer.stop()
        # One refresh works out each order's figures and each supplier's balance once, however many lists show them.
        with payments.one_look(self.conn):
            self._refresh()

    def _refresh(self):
        text = self.search.text()
        everything = payments.orders_in_payment(self.conn, text, user=self.user)
        self.orders = [r for r in everything if r["stage"] in (payments.DM_FIRST, payments.ENTER_DRS, payments.DIFFERENCE, payments.SIGN_OFF, payments.TERMS)]
        # Ready checks on top, soonest first, and those waiting for a Final OF below them.
        due = payments.checks_due(self.conn, text, rows=everything, user=self.user)
        self.groups = sorted(due, key=lambda g: self._section(g) == self.FINAL_FIRST)
        self.ticked &= {self._tick_key(g) for g in self.groups}
        self.owed_alone = [] if text.strip() else payments.owed_without_orders(self.conn, self.groups, user=self.user)
        self.credit_alone = [] if text.strip() else payments.credit_without_orders(self.conn, self.groups, user=self.user)
        active, cancelled = payments.checks(self.conn, payments.ACTIVE, text, user=self.user), payments.checks(self.conn, payments.CANCELLED, text, user=self.user)
        by_bucket = {b: [c for c in active if c["bucket"] == b] for b in (payments.FUTURE, payments.DUE, payments.CLAIMED, payments.CLEARED)}
        # Soonest first, because the question on these tabs is what's coming.
        for bucket in (payments.FUTURE, payments.DUE):
            by_bucket[bucket].sort(key=lambda c: c["check_date"])
        self.checks = cancelled if self.tab == self.CANCELLED else by_bucket.get(self.tab, [])
        # Cleared checks only pile up, so that tab waits to be asked.
        self.waiting_to_be_asked = self.tab == self.CLEARED and not text.strip() and not self.show_all
        if self.waiting_to_be_asked:
            self.checks = []
        self.clear_ticked &= {c["id"] for c in self.checks} if self.tab == self.CLAIMED else set()
        # Cash flow covers everything, so a search box with something in it doesn't narrow it. Without one, the checks to make are the ones just worked out.
        self.flow_rows = payments.cash_flow(self.conn, rows=None, user=self.user) if text.strip() else payments.cash_flow(self.conn, rows=everything, groups=due, user=self.user)
        if self.by_month:
            self.flow_rows = payments.cash_flow_by_month(self.flow_rows)
        counts = {self.TO_CHECK: len(self.orders), self.TO_PAY: len(self.groups), self.FUTURE: len(by_bucket[payments.FUTURE]), self.DUE: len(by_bucket[payments.DUE]), self.CLAIMED: len(by_bucket[payments.CLAIMED]), self.CLEARED: len(by_bucket[payments.CLEARED]), self.CANCELLED: len(cancelled)}
        for i in range(self.tabs.count()):
            key = self.tabs.tabData(i)
            self.tabs.setTabText(i, self.TABS[key] if key == self.FLOW else f"{self.TABS[key]}  {counts[key]}")
        if not text.strip():
            self.all_counts = counts
        total = lambda bucket: sum(c["amount_c"] for c in by_bucket[bucket])
        self.totals.setText(f"Unclaimed {fmt_peso(total(payments.DUE))}   |   Future {fmt_peso(total(payments.FUTURE))}   |   To make {fmt_peso(sum(g['check_c'] for g in self.groups))}   |   Claimed, not cleared {fmt_peso(total(payments.CLAIMED))}")
        self._fill_lists()
        self._fill_flow()
        current = self.tables[self.tab]
        rows = {self.TO_CHECK: self.orders, self.TO_PAY: self.groups, self.FLOW: self.flow_rows}.get(self.tab, self.checks)
        for table in (self.to_check, self.to_pay, self.paid, self.flow):
            table.setVisible(table is current and bool(rows))
        if rows and current is self.flow:
            current.selectRow(0)
        elif rows:
            items = [item for item in (current.topLevelItem(i) for i in range(current.topLevelItemCount())) if not isinstance(item.data(0, Qt.ItemDataRole.UserRole), str)]
            waiting = set()
            if self.can_sign and current is self.to_check:
                waiting = {r["id"] for r in self.orders if r["stage"] == payments.SIGN_OFF}
            elif self.can_sign and current is self.paid:
                waiting = {c["id"] for c in self.checks if c["waiting_signoff"]}
            current.setCurrentItem(next((i for i in items if i.data(0, Qt.ItemDataRole.UserRole) in waiting), items[0]))
        self._fit_current()
        self.empty.setVisible(not rows)
        self.empty.setText({self.TO_CHECK: "Nothing to compare. A delivery that matches its order goes straight to Checks to make.",
                            self.TO_PAY: "No checks to make. An order comes here once its delivery is complete, and its DRs are compared if it had a problem." + self._balances_text(),
                            self.FUTURE: "No printed checks are dated in the future.", self.DUE: "No unclaimed checks have reached their date.", self.CLAIMED: "No claimed checks are waiting to clear the bank.", self.CLEARED: "Search above for a supplier code, OF or check number, or press Show all cleared." if self.waiting_to_be_asked else "No cleared checks match that search." if text.strip() else "No checks have cleared the bank yet.",
                            self.CANCELLED: "No cancelled checks.", self.FLOW: "Nothing to cover right now."}[self.tab])
        if not rows and text.strip() and self.tab != self.FLOW:
            # The search found nothing, though there may be plenty on the tab: say so, rather than that there's nothing.
            everything_here = self._count_without_search()
            if everything_here is None:
                self.empty.setText(f"No match for '{text.strip()}'. Clear the search to see all.")
            elif everything_here:
                how = "Clear the search and press Show all cleared" if self.tab == self.CLEARED else "Clear the search"
                self.empty.setText(f"No match for '{text.strip()}'. {how} to see all {everything_here}.")
        self._show_action()

    def _count_without_search(self) -> int | None:
        """How many the current tab holds with the search box empty. Checks are counted afresh. Orders and checks to make take long to work out,
        so they come from the last time the box was empty, unless something was done since. None when that isn't known."""
        if self.tab in self.CHECK_TABS:
            if self.tab == self.CANCELLED:
                return len(payments.checks(self.conn, payments.CANCELLED, "", user=self.user))
            bucket = {self.FUTURE: payments.FUTURE, self.DUE: payments.DUE, self.CLAIMED: payments.CLAIMED, self.CLEARED: payments.CLEARED}[self.tab]
            return sum(c["bucket"] == bucket for c in payments.checks(self.conn, payments.ACTIVE, "", user=self.user))
        return (self.all_counts or {}).get(self.tab)

    def _set_flow_view(self, monthly: bool):
        self.by_month = monthly
        self.refresh()

    def _fill_flow(self):
        """One line per date or month, with the totals on the last line."""
        rows = [[r["date"].strftime("%b %Y") if self.by_month else fmt_date(r["date"].isoformat()), fmt_plain(r["printed_c"]) if r["printed_n"] else "", fmt_plain(r["claimed_c"]) if r["claimed_n"] else "", fmt_plain(r["to_make_c"]) if r["to_make_n"] else "",
                 fmt_plain(r["total_c"]), fmt_plain(r["running_c"]), _flow_note(r)] for r in self.flow_rows]
        if rows:
            rows.append(["Total", *(fmt_plain(sum(r[key] for r in self.flow_rows)) for key in ("printed_c", "claimed_c", "to_make_c", "total_c")), "", ""])
        self.flow.horizontalHeaderItem(0).setText("Month" if self.by_month else "Check date")
        self.flow.horizontalHeaderItem(4).setText("Total for the month" if self.by_month else "Total for the date")
        fill_table(self.flow, rows)
        if rows:
            for col in range(self.flow.columnCount()):
                font = self.flow.item(len(rows) - 1, col).font()
                font.setBold(True)
                self.flow.item(len(rows) - 1, col).setFont(font)

    def _next_step(self, row: dict) -> str:
        if row["stage"] == payments.TERMS:
            return f"The terms on this order give no check date ({self._terms_cause(row)}). Admin has to look at it"
        if row["stage"] == payments.DM_FIRST:
            short = [p["po_number"] for p in row["pos"] if p["unclaimed_c"]]
            return f"Short with no debit memo ({', '.join(short)}): the purchaser makes it on Received first"
        if row["stage"] == payments.SIGN_OFF:
            return "Difference accepted: waiting for Admin or management to sign off"
        return f"Compare the DRs: {', '.join(row['why'])}" if row["stage"] == payments.ENTER_DRS else "DR total differs, higher than ours: look into it"

    def _top_key(self, tree):
        """Whichever row is selected inside it, the buttons act on the top row: the order or the check."""
        item = tree.currentItem()
        while item and item.parent():
            item = item.parent()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def _selected_order(self) -> dict | None:
        key = self._top_key(self.to_check)
        return next((r for r in self.orders if r["id"] == key), None) if key else None

    def _selected_group(self) -> dict | None:
        key = self._top_key(self.to_pay)
        return self.groups[key - 1] if isinstance(key, int) and key <= len(self.groups) else None

    def _selected_check(self) -> dict | None:
        key = self._top_key(self.paid)
        return next((c for c in self.checks if c["id"] == key), None) if key else None

    def _show_action(self):
        """One obvious button for what's selected, and what the amount is made of."""
        first, second, note, detail = "", "", "", ""
        if self.tab == self.TO_CHECK and (order := self._selected_order()):
            note = f"{order['of_number']}, {order['supplier']}. {self._next_step(order)}"
            if order["stage"] in (payments.DM_FIRST, payments.TERMS):
                first = second = ""
            elif order["stage"] == payments.SIGN_OFF:
                uncounted = [p["po_number"] for p in order["pos"] if not p["counted"]]
                first = "Sign off" if self.can_sign and not uncounted else ""
                second = "DR differs" if self.can_edit else ""
                if uncounted:
                    note += f". It can't be signed off: {', '.join(uncounted)} has no delivery count to check the DRs against."
            else:
                # Once a DR that differs is typed, the button left says what it does to the rest.
                first = ("The other DRs match" if order["entered"] else "DRs match") if order["stage"] == payments.ENTER_DRS else "Accept the difference"
                second = "DR differs" if order["stage"] == payments.DIFFERENCE else ""
            detail = self._breakdown(order)
        elif self.tab == self.TO_PAY and (self.ticked or self._selected_group()):
            ready, finals = self._ticked_groups(self.READY), self._ticked_groups(self.FINAL_FIRST)
            group = self._selected_group()
            if group:
                note = f"{group['supplier']}, check dated {fmt_date(group['due'].isoformat())}, {fmt_peso(group['check_c'])}"
                detail = "  ".join(self._breakdown(o) for o in group["orders"])
                late = payments.late_claims(self.conn, group["supplier_id"], user=self.user)
                if late and group["credit_c"]:
                    detail += "  Credit for damage found after the check: " + "; ".join(f"{c['cs_number']} {fmt_peso(c['total_c'])}, on check {c['paid_by']}" for c in late) + "."
                if group["owed_c"]:
                    detail += self._paid_back_text(group["supplier_id"])
            print_ticked = f"Print {self._final_count(finals)}" if finals else ""
            if ready:
                note = f"{len(ready)} ticked, {fmt_peso(sum(g['check_c'] for g in ready))}. Print them by check date, then record the numbers."
                first, second = f"Record {len(ready)} check{'s' if len(ready) != 1 else ''}", print_ticked
            elif finals:
                note = f"{len(finals)} ticked. Once the Final OF is printed, each moves up to Ready for checks."
                first = print_ticked
            elif self._section(group) == self.FINAL_FIRST:
                first = "Print Final OF"
            elif group["check_c"] <= payments.TOLERANCE_C:
                note += ". The supplier's credit from earlier checks covers it, so no check is printed."
                first = "Settle by credit"
            else:
                first = "Record check"
                second = "Print Final OF again" if any(o["changed"] for o in group["orders"]) else ""
        elif self.tab in (self.FUTURE, self.DUE) and (check := self._selected_check()):
            if check["waiting_signoff"]:
                note = f"Check {check['check_number']}, {check['supplier']}, is for a different amount than the program worked out: {check['difference_reason']}. Admin or management signs it off before the supplier gets it."
                first = "Sign off" if self.can_sign else ""
            else:
                note = f"Check {check['check_number']}, {check['supplier']}. To print it again, cancel it first."
                first = "Mark claimed"
            second = "Print Final OF again" if self._check_finals(check) else ""
        elif self.tab == self.CLAIMED and (ticked := self._ticked_checks()):
            note = f"{len(ticked)} ticked, {fmt_peso(sum(c['amount_c'] for c in ticked))}. Mark them cleared once they show on the bank statement."
            first = f"Mark {len(ticked)} cleared"
            second = "Undo claim" if self._selected_check() else ""
        elif self.tab == self.CLAIMED and (check := self._selected_check()):
            note = f"Check {check['check_number']}, {check['supplier']}, claimed {_when(check)}. Mark it cleared once it shows on the bank statement."
            first, second = "Mark cleared", "Undo claim"
        elif self.tab == self.CLEARED and (check := self._selected_check()) and check["waiting_signoff"]:
            note = f"{check['check_number']}, {check['supplier']}: {check['difference_reason']}. Admin or management signs it off."
            first = "Sign off" if self.can_sign else ""
        elif self.tab == self.CLEARED and (check := self._selected_check()) and payments.settled_by_credit(check):
            note = f"{check['check_number']}, {check['supplier']}: settled by the supplier's credit. No money moved."
        elif self.tab == self.CLEARED and (check := self._selected_check()):
            note = f"Check {check['check_number']}, {check['supplier']}, {_when(check).replace("Cleared", "cleared")}. The money has left the account."
            first = "Undo cleared"
            second = "Print Final OF again" if self._check_finals(check) else ""
        elif self.tab == self.FLOW:
            note = f"Cash needed by each {'month' if self.by_month else 'date'}: every check that hasn't cleared the bank, and checks still to make."
        elif self.tab == self.CANCELLED and (check := self._selected_check()):
            note = f"Check {check['check_number']} cancelled by {check['cancelled_name']}. Its number won't be used again."
        if self.tab == self.TO_PAY and (self.owed_alone or self.credit_alone):
            detail = (detail + "  " if detail else "") + self._balances_text().strip()
        self.note.setText(note)
        self.breakdown.setText(detail)
        for b in self.flow_buttons.values():
            b.setVisible(self.tab == self.FLOW)
        self.show_all_btn.setVisible(self.waiting_to_be_asked)
        # Cash flow always covers everything, so there's nothing there to search.
        self.search.setVisible(self.tab != self.FLOW)
        self.primary_btn.setText(first)
        # Only a copy of a Final OF is open to view-only people. Everything else here changes the payment.
        show_if(self.primary_btn, bool(first) and ((self.can_edit and first != "Sign off") or first == "Print Final OF again" or (first == "Sign off" and self.can_sign)))
        self.second_btn.setText(second)
        show_if(self.second_btn, bool(second) and (self.can_edit or second == "Print Final OF again"))
        show_if(self.differs_btn, self.can_edit and ((self.tab == self.TO_PAY and self._selected_group() is not None) or (self.tab == self.TO_CHECK and (order := self._selected_order()) is not None and order["stage"] == payments.ENTER_DRS)))
        show_if(self.cancel_btn, self.can_edit and self.tab in (self.FUTURE, self.DUE) and self._selected_check() is not None)

    def _paid_back_text(self, supplier_id: int) -> str:
        """Debit memos cancelled after payment, paid back on this check, with the number and why, so Accounting can judge them."""
        memos = payments.paid_back_memos(self.conn, supplier_id, user=self.user)
        if not memos:
            return ""
        return "  Paid back, debit memos cancelled after the check: " + "; ".join(
            f"{m['dm_number']} {fmt_peso(m['total_c'])} (check {m['paid_by']} had paid it), cancelled by {m['cancelled_name'] or 'management'}: {m['cancel_reason']}" for m in memos) + "."

    def _balances_text(self) -> str:
        """Money left over from earlier checks for suppliers with nothing due, either way, so neither is forgotten."""
        parts = []
        if self.owed_alone:
            parts.append("We still owe from earlier checks, with no order due to add it to: " + "; ".join(f"{s['supplier']} {fmt_peso(s['owed_c'])}" for s in self.owed_alone) + ". It goes on their next check.")
            parts += [self._paid_back_text(s["supplier_id"]).strip() for s in self.owed_alone if self._paid_back_text(s["supplier_id"])]
        if self.credit_alone:
            parts.append("These suppliers owe us from earlier checks, say a debit memo after the check: " + "; ".join(f"{s['supplier']} {fmt_peso(s['credit_c'])}" for s in self.credit_alone)
                         + ". It comes off their next order.")
        return (" " + " ".join(parts)) if parts else ""

    def _dr_gaps(self, order: dict) -> str:
        """Each PO whose typed DRs differ from what it came to, so the figures shown are the real difference and not all POs against only the DRs typed."""
        typed: dict[int, int] = {}
        for d in order["receipts"]:
            if d["dr_date"]:
                typed[d["po_id"]] = typed.get(d["po_id"], 0) + d["total_c"]
        gaps = []
        for p in order["pos"]:
            if p["po_id"] in typed and abs(typed[p["po_id"]] - p["delivered_c"]) > payments.TOLERANCE_C:
                off = typed[p["po_id"]] - p["delivered_c"]
                gaps.append(f"    {p['branch']}:  DR {fmt_peso(typed[p['po_id']])},  order {fmt_peso(p['delivered_c'])}  ({fmt_peso(abs(off))} {'more' if off > 0 else 'less'})")
        return "\n".join(gaps) + f"\n\nOff by {fmt_peso(order['off_c'])} in all."

    @staticmethod
    def _terms_cause(row: dict) -> str:
        """Names the value on the order that gives no check date, cut short if it is very long."""
        cause = row.get("terms_cause") or "payment terms"
        value = str(row.get("terms_value") or row.get("terms") or "")
        value = value if len(value) <= 20 else value[:20] + "..."
        return f"{cause} \"{value}\""

    def _breakdown(self, row: dict) -> str:
        parts = [f"{row['of_number']}: order {fmt_peso(row['total_c'])}"]
        for c in row["charges"]:
            parts.append(f"{c['kind']} {c['number']} -{fmt_peso(c['total_c'])}")
        for name, value in (("short, no debit memo", row["unclaimed_c"]), ("stickers", row["sticker_c"]), ("term discount", row["term_c"]), ("DR lower", row.get("dr_lower_c", 0))):
            if value:
                parts.append(f"{name} -{fmt_peso(value)}")
        for memo in row.get("paid_back", []):
            parts.append(f"debit memo {memo['dm_number']} cancelled after payment, paid back")
        text = ", ".join(parts) + f", to pay {fmt_peso(row['pay_c'])}."
        if row.get("line_diffs"):
            text += " Products that differ on the DR: " + "; ".join(d["text"] for d in row["line_diffs"].values()) + "."
        if row["terms_by_hand"]:
            text += f" Terms \"{row['terms']}\" were typed by hand, so check the date."
        if row.get("terms_unreadable"):
            text += f" The terms on this order give no check date ({self._terms_cause(row)}), so Admin has to look at it."
        if row["accepted_reason"]:
            text += f" DR difference accepted: {row['accepted_reason']}"
        return text

    def _primary(self):
        if self.tab == self.TO_CHECK and (self._selected_order() or {}).get("stage") == payments.SIGN_OFF:
            order = self._selected_order()
            if DifferenceDialog(self, order).exec():
                payments.sign_off_difference(self.conn, self.user, order["id"])
                self._done()
        elif self.tab in (self.FUTURE, self.DUE, self.CLEARED) and (self._selected_check() or {}).get("waiting_signoff"):
            check = self._selected_check()
            if confirm(self, "Sign off", f"Check {check['check_number']}, {check['supplier']}, {fmt_peso(check['amount_c'])}. Recorded because: {check['difference_reason']}\n\nSign this off so the supplier can get it?", "Sign off"):
                payments.sign_off_check(self.conn, self.user, check["id"])
                self._done()
        elif self.tab == self.TO_CHECK:
            order = self._selected_order()
            if order["stage"] == payments.ENTER_DRS:
                payments.drs_match(self.conn, self.user, order["id"])
                self._done()
            else:
                dialog = DifferenceDialog(self, order, ask_reason=True)
                if dialog.exec():
                    payments.accept_difference(self.conn, self.user, order["id"], dialog.reason)
                    self._done()
        elif self.tab == self.TO_PAY:
            ready, finals = self._ticked_groups(self.READY), self._ticked_groups(self.FINAL_FIRST)
            if ready:
                if BatchCheckDialog(self, self.conn, self.user, ready).exec():
                    self.ticked.clear()
                    self._done()
            elif finals:
                self._print_final(finals)
            elif self._section(self._selected_group()) == self.FINAL_FIRST:
                self._print_final([self._selected_group()])
            elif self._selected_group()["check_c"] <= payments.TOLERANCE_C:
                group = self._selected_group()
                if confirm(self, "Settle by credit", f"{group['supplier']}'s credit from earlier checks, {fmt_peso(group['credit_c'])}, covers these orders, so no check is printed. "
                           "Record them as settled? Admin or management signs it off.", "Settle by credit"):
                    payments.settle_by_credit(self.conn, self.user, [o["id"] for o in group["orders"]])
                    self._done()
            elif CheckDialog(self, self.conn, self.user, self._selected_group()).exec():
                self._done()
        elif self.tab in (self.FUTURE, self.DUE):
            if ClaimDialog(self, self.conn, self.user, self._selected_check()).exec():
                self._done()
        elif self.tab == self.CLAIMED and self._ticked_checks():
            if ClearTickedDialog(self, self.conn, self.user, self._ticked_checks()).exec():
                self.clear_ticked.clear()
                self._done()
        elif self.tab == self.CLAIMED:
            if ClearDialog(self, self.conn, self.user, self._selected_check()).exec():
                self._done()
        elif self.tab == self.CLEARED:
            check = self._selected_check()
            why = ask_text(self, "Undo cleared", f"Why is check {check['check_number']} not cleared after all?")
            if why is not None:
                payments.undo_clear(self.conn, self.user, check["id"], why)
                self._done()

    def _check_finals(self, check: dict) -> list[int]:
        """The orders on this check that had a Final OF printed, so a copy can be printed here too."""
        ids = [r[0] for r in self.conn.execute("SELECT of_id FROM payment_check_ofs WHERE check_id = ?", (check["id"],))]
        return [of_id for of_id in ids if pdf.office_printed_at(self.conn, of_id)]

    def _second(self):
        if self.tab in (self.FUTURE, self.DUE, self.CLEARED):
            if confirm_reprint(self, "This Final OF"):
                open_for_printing([pdf.office_copy(self.conn, self.user, of_id) for of_id in self._check_finals(self._selected_check())], "Final OF")
                self._done()
            return
        if self.tab == self.TO_CHECK:
            self._enter_drs(self._selected_order())
        elif self.tab == self.TO_PAY:
            finals = self._ticked_groups(self.FINAL_FIRST)
            if finals:
                self._print_final(finals)
            elif confirm_reprint(self, "This Final OF"):
                self._print_final([self._selected_group()])
        elif self.tab == self.CLAIMED:
            check = self._selected_check()
            why = ask_text(self, "Undo claim", f"Why is the claim on check {check['check_number']} being undone?")
            if why is not None:
                payments.undo_claim(self.conn, self.user, check["id"], why)
                self._done()

    def _final_count(self, groups: list[dict]) -> str:
        n = sum(o["changed"] for g in groups for o in g["orders"])
        return f"{n} Final OF{'s' if n != 1 else ''}"

    def _print_final(self, groups: list[dict]):
        """One file for all of them, in check date order. Printing moves each up to Ready for checks."""
        ids = [o["id"] for g in sorted(groups, key=lambda g: (g["due"], g["supplier"])) for o in g["orders"] if o["changed"]]
        open_for_printing([pdf.office_copy(self.conn, self.user, of_id) for of_id in ids], "Final OF")
        self.ticked -= {self._tick_key(g) for g in groups}
        self._done()

    def _dr_differs(self):
        """A DR's amount is different. On Checks to make a line can hold several orders, so it asks which."""
        if self.tab == self.TO_CHECK:
            self._enter_drs(self._selected_order())
            return
        orders = self._selected_group()["orders"]
        order = orders[0]
        if len(orders) > 1:
            number, ok = QInputDialog.getItem(self, "DR differs", "Which order's DR?", [o["of_number"] for o in orders], 0, False)
            if not ok:
                return
            order = next(o for o in orders if o["of_number"] == number)
        self._enter_drs(order)

    def _enter_drs(self, order: dict):
        """Opens the order's DRs on the right of the list."""
        self.dr_panel.start(order)
        self.dr_panel.show()
        self.dr_panel.drs.setFocus()

    def _close_panel(self):
        self.dr_panel.hide()

    def _panel_saved(self):
        self.dr_panel.hide()
        self._done()

    def cancel_check(self):
        check = self._selected_check()
        if not check:
            return
        # One question: the warning and the reason together.
        why = ask_text(self, "Cancel check", f"Cancel check {check['check_number']}? Its orders go back to Checks to make, and its number is never used again. Void the paper check too.\n\n"
                       "Why is this check being cancelled?")
        if why is not None:
            payments.cancel_check(self.conn, self.user, check["id"], why)
            self._done()

    def _done(self):
        # The counts kept from an empty search box may be out of date now.
        self.all_counts = None
        self.refresh()
        self.changed.emit()
