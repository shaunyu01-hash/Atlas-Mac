"""Inventory screens: stock on hand with each product's stock card, stock counts, adjustments, transfers and inventory reports.
They only call the inventory service; every rule lives there."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit, QMenu,
                               QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from .. import inventory as service
from .. import masters
from ..db import POError
from ..money import fmt_peso, fmt_qty
from .common import (ask_text, button, combo_value, confirm, date_edit, date_value, fill_table, fmt_date, fmt_datetime, info, label, make_table,
                     register_tabs, select_data, selected_key, set_widths, show_if, show_tab_counts)
from .pages import page_layout


def place_combo(conn, user, everywhere: bool = False) -> QComboBox:
    box = QComboBox()
    if everywhere:
        box.addItem("Every place", None)
    for place in service.locations(conn, user):
        box.addItem(place["name"], place["code"])
    return box


def department_combo(conn) -> QComboBox:
    box = QComboBox()
    box.addItem("Every department", None)
    for d in masters.list_departments(conn):
        box.addItem(d["name"], d["id"])
    return box


def _qty(qty_m, unit: str = "") -> str:
    return "" if qty_m is None else fmt_qty(qty_m)


def _editable(table: QTableWidget, col) -> None:
    """Only the figure columns take typing; every other cell is read-only."""
    cols = {col} if isinstance(col, int) else set(col)
    table.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
    for r in range(table.rowCount()):
        for c in range(table.columnCount()):
            item = table.item(r, c)
            if item is None:
                item = QTableWidgetItem("")
                table.setItem(r, c, item)
            flags = item.flags()
            item.setFlags(flags | Qt.ItemFlag.ItemIsEditable if c in cols else flags & ~Qt.ItemFlag.ItemIsEditable)


def _typed(table: QTableWidget, col: int) -> dict:
    """{row key: text typed in the figure column}."""
    out = {}
    for r in range(table.rowCount()):
        key = table.item(r, 0).data(Qt.ItemDataRole.UserRole)
        item = table.item(r, col)
        out[key] = item.text().strip() if item else ""
    return out


class ProductLines(QWidget):
    """Lines of products with a quantity each: find a product by SKU or words, add it, type the quantity."""

    def __init__(self, conn, qty_heading: str = "Quantity", extra: str | None = None, parent=None):
        super().__init__(parent)
        self.conn, self.extra = conn, extra
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.find = QLineEdit()
        self.find.setPlaceholderText("SKU or product name, then Enter")
        self.find.returnPressed.connect(self.add)
        row.addWidget(self.find, 1)
        self.add_btn = button("Add product", self.add, lock=False)
        row.addWidget(self.add_btn)
        row.addWidget(button("Remove line", self.remove, lock=False))
        col.addLayout(row)
        self.table = make_table(["SKU", "Product", "Size", "Color", "Unit", qty_heading] + ([extra] if extra else []), numeric=(5, 6) if extra else (5,), stretch=1)
        set_widths(self.table, {0: 110, 2: 70, 3: 80, 4: 60, 5: 90, 6: 150} if extra else {0: 110, 2: 70, 3: 80, 4: 60, 5: 90})
        self.table.setSortingEnabled(False)
        col.addWidget(self.table, 1)

    def add(self):
        text = self.find.text().strip()
        if not text:
            return
        exact = service.find_product(self.conn, text)
        matches = [dict(exact) | {"unit": service.unit(exact)}] if exact else service.products_for(self.conn, text)
        if not matches:
            raise POError(f"No product in the catalog matches '{text}'.")
        if len(matches) == 1:
            self._put(matches[0])
            return
        menu = QMenu(self)
        for m in matches:
            menu.addAction(f"{m['item_code']}  {m['description']} {m['size']} {m['color']}".strip(), lambda m=m: self._put(m))
        menu.exec(self.find.mapToGlobal(self.find.rect().bottomLeft()))

    def _put(self, p):
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) == p["id"]:
                self.table.setCurrentCell(r, 5)
                self.table.editItem(self.table.item(r, 5))
                return
        r = self.table.rowCount()
        self.table.insertRow(r)
        for c, text in enumerate([p["item_code"], p["description"], p["size"], p["color"], p["unit"], ""] + ([""] if self.extra else [])):
            item = QTableWidgetItem(text)
            if c == 0:
                item.setData(Qt.ItemDataRole.UserRole, p["id"])
            if c >= 5:
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(r, c, item)
        _editable(self.table, (5, 6) if self.extra else 5)
        self.find.clear()
        self.table.setCurrentCell(r, 5)
        self.table.editItem(self.table.item(r, 5))

    def remove(self):
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)

    def values(self) -> dict[int, str]:
        return _typed(self.table, 5)

    def extras(self) -> dict[int, str]:
        return _typed(self.table, 6) if self.extra else {}


class _Dialog(QDialog):
    """Enter alone never saves: it adds a product or moves to the next figure. Ctrl+Enter saves. Esc asks before throwing typing away."""

    def __init__(self, parent, title: str, ok: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 560)
        self.root = QVBoxLayout(self)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok_btn = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText(ok)
        ok_btn.setToolTip("Ctrl+Enter")
        for b in self.buttons.buttons():
            b.setAutoDefault(False)
            b.setDefault(False)
        self.buttons.accepted.connect(self._ok)
        self.buttons.rejected.connect(self.reject)

    def finish(self):
        self.root.addWidget(self.buttons)

    def _ok(self):
        from .common import guarded
        guarded(self, self._save)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier and self.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled():
                self._ok()
            event.accept()
            return
        super().keyPressEvent(event)

    def _typed_something(self) -> bool:
        for table in self.findChildren(QTableWidget):
            for r in range(table.rowCount()):
                for c in range(table.columnCount()):
                    item = table.item(r, c)
                    if item is not None and item.flags() & Qt.ItemFlag.ItemIsEditable and item.text().strip():
                        return True
        return any(lines.table.rowCount() for lines in self.findChildren(ProductLines))

    def reject(self):
        if self.result() == QDialog.DialogCode.Accepted or not self._typed_something() or confirm(
                self, self.windowTitle(), "Close without saving? What you typed here will be lost.", "Close without saving"):
            super().reject()


# ------------------------------------------------------------------ stock on hand

class StockPage(QWidget):
    """What's at each place, and every movement of the product picked."""
    changed = Signal()

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.rows = []
        root, top = page_layout(self, "Stock on hand")
        self.since = label("", "Muted")
        top.addWidget(self.since)
        if user.can("settings.edit"):
            top.addWidget(button("Change start date", self.change_since))
        bar = QHBoxLayout()
        self.place = place_combo(conn, user, everywhere=True)
        self.dept = department_combo(conn)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search SKU, product, brand, size or color")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(280)
        self.below = QCheckBox("Below zero only")
        for w in (self.place, self.dept):
            w.currentIndexChanged.connect(self.refresh)
        self.search.textChanged.connect(self.refresh)
        self.below.toggled.connect(self.refresh)
        for w in (self.place, self.dept, self.search, self.below):
            bar.addWidget(w)
        bar.addStretch(1)
        root.addLayout(bar)
        self.totals = label("", "Muted")
        root.addWidget(self.totals)
        split = QSplitter(Qt.Orientation.Vertical)
        self.table = make_table(["Place", "SKU", "Product", "Size", "Color", "Unit", "For sale", "Damaged", "Held for supplier", "On the way", "Value"],
                                numeric=(6, 7, 8, 9, 10), stretch=2)
        set_widths(self.table, {0: 130, 1: 110, 3: 70, 4: 80, 5: 50, 6: 80, 7: 80, 8: 130, 9: 90, 10: 110})
        self.table.itemSelectionChanged.connect(self._card)
        split.addWidget(self.table)
        card = QWidget()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(0, 6, 0, 0)
        self.card_title = label("Pick a product to see its stock card", "PanelTitle")
        cl.addWidget(self.card_title)
        self.card = make_table(["Date", "What happened", "Paper", "Stock", "In / out", "For sale after", "Damaged after", "Held after"], numeric=(4, 5, 6, 7), stretch=1)
        set_widths(self.card, {0: 140, 2: 130, 3: 80, 4: 80, 5: 110, 6: 115, 7: 90})
        cl.addWidget(self.card)
        split.addWidget(card)
        split.setSizes([500, 260])
        root.addWidget(split, 1)
        self.refresh()

    def refresh(self, *_):
        day = service.live_since(self.conn)
        self.since.setText(f"Counting purchasing movements from {fmt_date(day)}" if day else "Counting every order received")
        key = selected_key(self.table)
        self.rows = service.on_hand(self.conn, self.user, combo_value(self.place), self.search.text(), combo_value(self.dept), self.below.isChecked())
        fill_table(self.table, [[r["location_name"], r["item_code"], r["description"], r["size"], r["color"], r["unit"], fmt_qty(r["good_m"]), fmt_qty(r["damaged_m"]),
                                 fmt_qty(r["held_m"]), fmt_qty(r["transit_m"]), fmt_peso(r["value_c"]) if r["has_cost"] else "No cost"] for r in self.rows],
                   keys=[(r["location"], r["product_id"]) for r in self.rows])
        for i in range(self.table.rowCount()):
            if self.table.item(i, 0).data(Qt.ItemDataRole.UserRole) == key:
                self.table.selectRow(i)
        below = sum(r["below_zero"] for r in self.rows)
        self.totals.setText(f"{len(self.rows)} lines, worth {fmt_peso(sum(r['value_c'] for r in self.rows))} at the last purchase cost"
                            + (f". {below} below zero: count them to put the books right." if below else ""))
        self._card()

    def _card(self):
        key = selected_key(self.table)
        if key is None:
            self.card_title.setText("Pick a product to see its stock card")
            fill_table(self.card, [])
            return
        card = service.stock_card(self.conn, self.user, key[0], key[1])
        p = card["product"]
        self.card_title.setText(f"Stock card: {p['item_code']} {p['description']} at {card['location_name']}, in {card['unit']}")
        rows = list(reversed(card["rows"]))
        fill_table(self.card, [[fmt_datetime(r["moved_at"]), r["kind_label"], r["ref"] or "", {"GOOD": "For sale", "DAMAGED": "Damaged", "HELD": "Held"}[r["bucket"]],
                                f"{'+' if r['qty_m'] > 0 else ''}{fmt_qty(r['qty_m'])}", fmt_qty(r["good_m"]), fmt_qty(r["damaged_m"]), fmt_qty(r["held_m"])] for r in rows])

    def change_since(self):
        d = _Dialog(self, "Inventory start date", "Save")
        d.root.addWidget(label("Deliveries and other purchasing movements before this day stay off the books. The first stock count at each place gives its opening stock.", "Muted"))
        when = date_edit(service.live_since(self.conn) or None)
        d.root.addWidget(when)
        d._save = lambda: (service.set_live_since(self.conn, self.user, date_value(when)), d.accept())
        d.finish()
        d.resize(460, 160)
        if d.exec():
            self.refresh()


# ------------------------------------------------------------------ stock counts

class CountSheetDialog(_Dialog):
    """A counter's blind sheet: the products only. Type what's there, 0 for none, and add anything found that isn't listed."""

    def __init__(self, parent, conn, user, count_id):
        super().__init__(parent, "Count stock", "Save count")
        self.conn, self.user, self.count_id = conn, user, count_id
        sheet = service.count_sheet(conn, user, count_id)
        self.root.addWidget(label(f"{sheet['count_number']}, {sheet['location_name']}, {sheet['department']}, round {sheet['round']}", "PanelTitle"))
        hint = ("Count everything listed. Type 0 where there's none. Leave out goods still being received, held for a supplier, or packed to go back."
                if sheet["round"] == 1 else "These products didn't match last round. Count them again.")
        note = label(hint + " Don't look at anyone else's figures. Ctrl+Enter saves.", "Muted")
        note.setWordWrap(True)
        self.root.addWidget(note)
        self.table = make_table(["SKU", "Product", "Size", "Color", "Unit", "Counted"], numeric=(5,), stretch=1)
        set_widths(self.table, {0: 110, 2: 70, 3: 80, 4: 60, 5: 90})
        fill_table(self.table, [[l["item_code"], l["description"], l["size"], l["color"], l["unit"], ""] for l in sheet["lines"]], keys=[l["product_id"] for l in sheet["lines"]])
        self.table.setSortingEnabled(False)
        _editable(self.table, 5)
        self.root.addWidget(self.table, 3)
        self.root.addWidget(label("Found, not on the list", "PanelTitle"))
        self.found = ProductLines(conn, "Counted")
        self.root.addWidget(self.found, 2)
        self.outcome = None
        self.finish()
        if self.table.rowCount():
            self.table.setCurrentCell(0, 5)

    def _save(self):
        found = {}
        for pid, text in self.found.values().items():
            found[self.conn.execute("SELECT item_code FROM products WHERE id = ?", (pid,)).fetchone()[0]] = text
        if not confirm(self, "Save count", "Save your count? It can't be changed afterwards.", "Save count"):
            return
        self.outcome = service.save_count(self.conn, self.user, self.count_id, _typed(self.table, 5), found)
        self.accept()


class StartCountDialog(_Dialog):
    def __init__(self, parent, conn, user):
        super().__init__(parent, "Start a stock count", "Start count")
        self.conn, self.user, self.count_id = conn, user, None
        form = QFormLayout()
        self.place = place_combo(conn, user)
        self.dept = department_combo(conn)
        self.note = QLineEdit()
        form.addRow("Where", self.place)
        form.addRow("Department", self.dept)
        form.addRow("Note", self.note)
        self.root.addLayout(form)
        hint = label("Two people at that place count it blind. Count before the place opens or after it closes: sales typed in after a product "
                     "was counted are taken as made after the count. Transfers to that place are counted in once this count is posted.", "Muted")
        hint.setWordWrap(True)
        self.root.addWidget(hint)
        self.finish()
        self.resize(520, 260)

    def _save(self):
        self.count_id = service.open_count(self.conn, self.user, combo_value(self.place), combo_value(self.dept), self.note.text())
        self.accept()


class CountsPage(QWidget):
    changed = Signal()

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.counter = user.can("stock.count")
        self.manager = user.can("count.manage") or user.can("stock.post")
        self.tab = service.COUNTING
        self.rows = []
        root, top = page_layout(self, "Stock counts")
        top.addWidget(label("Two people count blind; management posts what they agree", "Muted"))
        self.labels = {**service.COUNT_LABELS, "ALL": "All"}
        self.tabs = register_tabs(self.labels, self._set_tab)
        root.addWidget(self.tabs)
        self.table = make_table(["Count", "Place", "Department", "Status", "Round", "Products", "To agree", "Started by", "Started"], numeric=(4, 5, 6), stretch=2)
        set_widths(self.table, {0: 120, 1: 140, 2: 150, 3: 120, 4: 60, 5: 70, 6: 70, 7: 130, 8: 140})
        self.table.itemSelectionChanged.connect(self._selected)
        root.addWidget(self.table, 2)
        row = QHBoxLayout()
        self.note = label("", "Muted")
        self.note.setWordWrap(True)
        row.addWidget(self.note, 1)
        self.start_btn = button("Start a count", self.start, "Primary")
        self.count_btn = button("Count", self.count, "Primary")
        self.post_btn = button("Post count", self.post, "Primary")
        self.cancel_btn = button("Cancel count", self.cancel, "Danger")
        if user.can("count.manage"):
            row.addWidget(self.cancel_btn)
            row.addSpacing(32)
            row.addWidget(self.start_btn)
        if user.can("stock.post"):
            row.addWidget(self.post_btn)
        if self.counter:
            row.addWidget(self.count_btn)
        root.addLayout(row)
        heads = (["SKU", "Product", "Size", "Color", "Unit", "Books when counted", "Agreed", "Difference", "Value", "Counts"] if self.manager
                 else ["Round", "SKU", "Product", "Size", "Color", "My figure"])
        self.lines = make_table(heads, numeric=(5, 6, 7, 8) if self.manager else (0, 5), stretch=1 if self.manager else 2)
        if self.manager:
            set_widths(self.lines, {0: 110, 2: 70, 3: 80, 4: 50, 5: 130, 6: 80, 7: 90, 8: 100, 9: 260})
        root.addWidget(self.lines, 2)
        self.table.doubleClicked.connect(self._open)
        self.refresh()

    def _set_tab(self, tab):
        self.tab = tab
        self.refresh()

    def refresh(self, *_):
        key = selected_key(self.table)
        everything = service.counts(self.conn, self.user)
        show_tab_counts(self.tabs, self.labels, {**{s: sum(r["status"] == s for r in everything) for s in service.COUNT_LABELS}, "ALL": len(everything)})
        self.rows = everything if self.tab == "ALL" else [r for r in everything if r["status"] == self.tab]
        fill_table(self.table, [[r["count_number"], r["location_name"], r["department"], r["status_label"], r["round_no"] if r["status"] == service.COUNTING else "",
                                 r["lines"], r["open_lines"] if r["status"] == service.COUNTING else "", r["opened_name"], fmt_datetime(r["opened_at"])] for r in self.rows],
                   keys=[r["id"] for r in self.rows])
        for i, r in enumerate(self.rows):
            if r["id"] == key:
                self.table.selectRow(i)
        if self.rows and self.table.currentRow() < 0:
            self.table.selectRow(0)
        self._selected()

    def _row(self):
        key = selected_key(self.table)
        return next((r for r in self.rows if r["id"] == key), None)

    def _selected(self):
        r = self._row()
        status = r["status"] if r else None
        show_if(self.count_btn, self.counter and status == service.COUNTING and self.user.id not in r["counted_this_round"])
        show_if(self.post_btn, status == service.AGREED and self.user.can("stock.post"))
        show_if(self.cancel_btn, status in (service.COUNTING, service.AGREED) and self.user.can("count.manage"))
        if r is None:
            self.note.setText(("" if self.rows else "No stock counts here." if self.manager else
                               "No stock count to do. When the office starts one here, it appears in this list for you to count."))
            fill_table(self.lines, [])
            return
        if self.manager:
            res = service.count_results(self.conn, self.user, r["id"])
            fill_table(self.lines, [[l["item_code"], l["description"], l["size"], l["color"], l["unit"], fmt_qty(l["book_m"]), _qty(l["agreed_m"]),
                                     "" if l["diff_m"] is None else f"{'+' if l['diff_m'] > 0 else ''}{fmt_qty(l['diff_m'])}",
                                     "" if l["diff_value_c"] is None or l["diff_m"] is None else fmt_peso(l["diff_value_c"]),
                                     (l["warning"] + " " if l["warning"] else "") + l["counts"]] for l in res["lines"]])
            text = {service.COUNTING: f"Round {r['round_no']}: " + (f"{len(r['counted_this_round'])} of 2 people have counted." if r["counted_this_round"] else "waiting for two people to count."),
                    service.AGREED: f"Both counts agree. Posting moves the books: {fmt_peso(res['short_value_c'])} missing, {fmt_peso(res['over_value_c'])} more than the books.",
                    service.POSTED: f"Posted by {r['closed_name']}, {fmt_datetime(r['closed_at'])}.",
                    service.CANCELLED: f"Cancelled by {r['closed_name']}: {r['cancel_reason']}"}[r["status"]]
            if r["status"] in (service.COUNTING, service.AGREED) and res["moved_since"]:
                text += f" Stock moved there {res['moved_since']} times since the count started; each difference allows for what was recorded before the product was counted."
            self.note.setText(text)
        else:
            mine = service.my_figures(self.conn, self.user, r["id"])
            fill_table(self.lines, [[m["round_no"], m["item_code"], m["description"], m["size"], m["color"], fmt_qty(m["qty_m"])] for m in mine])
            self.note.setText("You've counted this round. The other count must be someone else's." if self.user.id in r["counted_this_round"] and status == service.COUNTING
                              else r["status_label"])

    def _done(self):
        self.refresh()
        self.changed.emit()

    def _open(self, *_):
        if self.count_btn.isVisible():
            self.count()

    def start(self):
        d = StartCountDialog(self, self.conn, self.user)
        if d.exec():
            self.tab = service.COUNTING
            self._done()

    def count(self):
        r = self._row()
        if r:
            d = CountSheetDialog(self, self.conn, self.user, r["id"])
            if d.exec():
                out = d.outcome
                info(self, "Count saved", {"WAITING": "Saved. Someone else now counts the same stock.",
                                           "AGAIN": f"{out['mismatched']} products didn't match. Two people count those again.",
                                           service.AGREED: "Both counts agree. The office posts it."}[out["outcome"]])
                self._done()

    def post(self):
        r = self._row()
        if r and confirm(self, "Post count", f"Post {r['count_number']}? The books at {r['location_name']} move to what was counted. This can't be undone.", "Post count"):
            n = service.post_count(self.conn, self.user, r["id"])
            info(self, "Count posted", f"{n} products changed on the books.")
            self._done()

    def cancel(self):
        r = self._row()
        if r:
            reason = ask_text(self, "Cancel count", f"Why is {r['count_number']} being cancelled?")
            if reason is not None:
                service.cancel_count(self.conn, self.user, r["id"], reason)
                self._done()


# ------------------------------------------------------------------ adjustments

class AdjustmentDialog(_Dialog):
    def __init__(self, parent, conn, user):
        super().__init__(parent, "New stock adjustment", "Send for approval")
        self.conn, self.user = conn, user
        form = QFormLayout()
        self.place = place_combo(conn, user)
        self.reason = QComboBox()
        for code, text in service.REASONS.items():
            self.reason.addItem(text, code)
        self.bucket = QComboBox()
        self.bucket.addItem("Stock for sale", service.GOOD)
        self.bucket.addItem("Damaged stock", service.DAMAGED)
        self.note = QLineEdit()
        form.addRow("Where", self.place)
        form.addRow("Reason", self.reason)
        form.addRow("From", self.bucket)
        form.addRow("Note", self.note)
        self.root.addLayout(form)
        self.hint = label("", "Muted")
        self.hint.setWordWrap(True)
        self.root.addWidget(self.hint)
        self.lines = ProductLines(conn)
        self.root.addWidget(self.lines, 1)
        self.reason.currentIndexChanged.connect(self._reason)
        self._reason()
        self.finish()

    def _reason(self):
        code = combo_value(self.reason)
        self.bucket.setEnabled(code in ("WRITE_OFF", "INTERNAL_USE", "RETURNED", "OTHER"))
        if code == "WRITE_OFF":
            select_data(self.bucket, service.DAMAGED)
        self.hint.setText({"WRITE_OFF": "Takes the quantities off the books.", "DAMAGED": "Moves the quantities from stock for sale to damaged.",
                           "FOUND": "Adds the quantities to stock for sale.", "INTERNAL_USE": "Takes the quantities off the books.",
                           "RETURNED": "Takes the quantities off the books.",
                           "OTHER": "Type a minus sign to take stock off, or a plain number to add it. Say what happened in the note."}[code])

    def _save(self):
        lines = [{"product_id": pid, "qty": text, "bucket": combo_value(self.bucket)} for pid, text in self.lines.values().items()]
        service.prepare_adjustment(self.conn, self.user, combo_value(self.place), combo_value(self.reason), lines, self.note.text())
        self.accept()


class AdjustmentsPage(QWidget):
    changed = Signal()

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.tab = service.PENDING
        self.rows = []
        root, top = page_layout(self, "Adjustments")
        top.addWidget(label("Write-offs, damage, found stock. Someone else approves each one", "Muted"))
        self.labels = {**service.ADJUST_LABELS, "ALL": "All"}
        self.tabs = register_tabs(self.labels, self._set_tab)
        root.addWidget(self.tabs)
        self.table = make_table(["Adjustment", "Place", "Reason", "Status", "Prepared by", "Prepared", "Note"], stretch=6)
        set_widths(self.table, {0: 120, 1: 140, 2: 150, 3: 150, 4: 130, 5: 140})
        self.table.itemSelectionChanged.connect(self._selected)
        root.addWidget(self.table, 2)
        row = QHBoxLayout()
        self.note = label("", "Muted")
        self.note.setWordWrap(True)
        row.addWidget(self.note, 1)
        self.new_btn = button("New adjustment", self.new, "Primary")
        self.reject_btn = button("Reject", self.reject, "Danger")
        self.approve_btn = button("Approve", self.approve, "Primary")
        if user.can("stock.post"):
            row.addWidget(self.reject_btn)
            row.addSpacing(32)
            row.addWidget(self.approve_btn)
        if user.can("adjust.prepare"):
            row.addWidget(self.new_btn)
        root.addLayout(row)
        self.lines = make_table(["SKU", "Product", "Size", "Color", "Unit", "Stock", "Change", "Value"], numeric=(6, 7), stretch=1)
        root.addWidget(self.lines, 2)
        self.refresh()

    def _set_tab(self, tab):
        self.tab = tab
        self.refresh()

    def refresh(self, *_):
        key = selected_key(self.table)
        everything = service.adjustments(self.conn, self.user)
        show_tab_counts(self.tabs, self.labels, {**{s: sum(r["status"] == s for r in everything) for s in service.ADJUST_LABELS}, "ALL": len(everything)})
        self.rows = everything if self.tab == "ALL" else [r for r in everything if r["status"] == self.tab]
        fill_table(self.table, [[r["adj_number"], r["location_name"], r["reason_label"], r["status_label"], r["prepared_name"], fmt_datetime(r["prepared_at"]), r["note"]]
                                for r in self.rows], keys=[r["id"] for r in self.rows])
        for i, r in enumerate(self.rows):
            if r["id"] == key:
                self.table.selectRow(i)
        if self.rows and self.table.currentRow() < 0:
            self.table.selectRow(0)
        self._selected()

    def _row(self):
        key = selected_key(self.table)
        return next((r for r in self.rows if r["id"] == key), None)

    def _selected(self):
        r = self._row()
        waiting = r is not None and r["status"] == service.PENDING and r["prepared_by"] != self.user.id
        show_if(self.approve_btn, waiting and self.user.can("stock.post"))
        show_if(self.reject_btn, waiting and self.user.can("stock.post"))
        lines = service.adjustment_lines(self.conn, self.user, r["id"]) if r else []
        fill_table(self.lines, [[l["item_code"], l["description"], l["size"], l["color"], l["unit"], "For sale" if l["bucket"] == service.GOOD else "Damaged",
                                 f"{'+' if l['qty_m'] > 0 else ''}{fmt_qty(l['qty_m'])}", "" if l["value_c"] is None else fmt_peso(l["value_c"])] for l in lines])
        if r is None:
            self.note.setText("No adjustments here." if not self.rows else "")
        elif r["status"] == service.PENDING:
            self.note.setText("Waiting for someone else to approve it." if r["prepared_by"] == self.user.id else "Waiting for approval.")
        else:
            self.note.setText(f"{r['status_label']} by {r['decided_name']}, {fmt_datetime(r['decided_at'])}" + (f": {r['decision_note']}" if r["decision_note"] else ""))

    def _done(self):
        self.refresh()
        self.changed.emit()

    def new(self):
        if AdjustmentDialog(self, self.conn, self.user).exec():
            self.tab = service.PENDING
            self._done()

    def approve(self):
        r = self._row()
        if r and confirm(self, "Approve adjustment", f"Approve {r['adj_number']}? It changes the books at {r['location_name']}.", "Approve"):
            service.decide_adjustment(self.conn, self.user, r["id"], True)
            self._done()

    def reject(self):
        r = self._row()
        if r:
            reason = ask_text(self, "Reject adjustment", f"Why is {r['adj_number']} rejected? {r['prepared_name']} will see this.")
            if reason is not None:
                service.decide_adjustment(self.conn, self.user, r["id"], False, reason)
                self._done()


# ------------------------------------------------------------------ transfers

class SendTransferDialog(_Dialog):
    def __init__(self, parent, conn, user):
        super().__init__(parent, "Send stock to another place", "Send")
        self.conn, self.user = conn, user
        form = QFormLayout()
        self.source = place_combo(conn, user)
        self.dest = QComboBox()
        for place in service.locations(conn):
            self.dest.addItem(place["name"], place["code"])
        self.note = QLineEdit()
        form.addRow("From", self.source)
        form.addRow("To", self.dest)
        form.addRow("Note", self.note)
        self.root.addLayout(form)
        self.root.addWidget(label("The goods leave the books here now, and reach the other place's books when they count them in.", "Muted"))
        self.lines = ProductLines(conn, "Sending")
        self.root.addWidget(self.lines, 1)
        self.finish()

    def _save(self):
        if not confirm(self, "Send stock", f"Send these to {self.dest.currentText()}?", "Send"):
            return
        service.send_transfer(self.conn, self.user, combo_value(self.source), combo_value(self.dest), self.lines.values(), self.note.text())
        self.accept()


class ReceiveTransferDialog(_Dialog):
    def __init__(self, parent, conn, user, row):
        super().__init__(parent, "Count in a transfer", "Save count")
        self.conn, self.user, self.row = conn, user, row
        self.root.addWidget(label(f"{row['transfer_number']} from {row['from_name']}", "PanelTitle"))
        self.root.addWidget(label("Count what arrived. Type 0 for anything that didn't come.", "Muted"))
        lines = service.transfer_lines(conn, user, row["id"])
        self.table = make_table(["SKU", "Product", "Size", "Color", "Unit", "Counted"], numeric=(5,), stretch=1)
        fill_table(self.table, [[l["item_code"], l["description"], l["size"], l["color"], l["unit"], ""] for l in lines], keys=[l["product_id"] for l in lines])
        self.table.setSortingEnabled(False)
        _editable(self.table, 5)
        self.root.addWidget(self.table, 1)
        self.result = None
        self.finish()

    def _save(self):
        if confirm(self, "Save count", "Save what arrived? It can't be changed afterwards.", "Save count"):
            self.result = service.receive_transfer(self.conn, self.user, self.row["id"], _typed(self.table, 5))
            self.accept()


class TransfersPage(QWidget):
    changed = Signal()

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.tab = service.SENT
        self.rows = []
        root, top = page_layout(self, "Transfers")
        top.addWidget(label("Stock moved between the warehouse and the branches", "Muted"))
        self.labels = {**service.TRANSFER_LABELS, "ALL": "All"}
        self.tabs = register_tabs(self.labels, self._set_tab)
        root.addWidget(self.tabs)
        self.table = make_table(["Transfer", "From", "To", "Status", "Sent by", "Sent", "Note"], stretch=6)
        set_widths(self.table, {0: 120, 1: 140, 2: 140, 3: 100, 4: 130, 5: 140})
        self.table.itemSelectionChanged.connect(self._selected)
        root.addWidget(self.table, 2)
        row = QHBoxLayout()
        self.note = label("", "Muted")
        self.note.setWordWrap(True)
        row.addWidget(self.note, 1)
        self.cancel_btn = button("Cancel transfer", self.cancel, "Danger")
        self.receive_btn = button("Goods arrived: count them", self.receive, "Primary")
        self.send_btn = button("Send stock", self.send, "Primary")
        if user.can("stock.post"):
            row.addWidget(self.cancel_btn)
            row.addSpacing(32)
        if user.can("transfer.receive"):
            row.addWidget(self.receive_btn)
        if user.can("transfer.send"):
            row.addWidget(self.send_btn)
        root.addLayout(row)
        self.lines = make_table(["SKU", "Product", "Size", "Color", "Unit", "Sent", "Received"], numeric=(5, 6), stretch=1)
        root.addWidget(self.lines, 2)
        self.table.doubleClicked.connect(lambda *_: self.receive() if self.receive_btn.isVisible() else None)
        self.refresh()

    def _set_tab(self, tab):
        self.tab = tab
        self.refresh()

    def refresh(self, *_):
        key = selected_key(self.table)
        everything = service.transfers(self.conn, self.user)
        show_tab_counts(self.tabs, self.labels, {**{s: sum(r["status"] == s for r in everything) for s in service.TRANSFER_LABELS}, "ALL": len(everything)})
        self.rows = everything if self.tab == "ALL" else [r for r in everything if r["status"] == self.tab]
        fill_table(self.table, [[r["transfer_number"], r["from_name"], r["to_name"], r["status_label"], r["sent_name"], fmt_datetime(r["sent_at"]), r["note"]]
                                for r in self.rows], keys=[r["id"] for r in self.rows])
        for i, r in enumerate(self.rows):
            if r["id"] == key:
                self.table.selectRow(i)
        if self.rows and self.table.currentRow() < 0:
            self.table.selectRow(0)
        self._selected()

    def _row(self):
        key = selected_key(self.table)
        return next((r for r in self.rows if r["id"] == key), None)

    def _selected(self):
        r = self._row()
        show_if(self.receive_btn, r is not None and r["to_receive"])
        show_if(self.cancel_btn, r is not None and r["status"] == service.SENT and self.user.can("stock.post"))
        lines = service.transfer_lines(self.conn, self.user, r["id"]) if r else []
        fill_table(self.lines, [[l["item_code"], l["description"], l["size"], l["color"], l["unit"], "" if l["sent_m"] is None else fmt_qty(l["sent_m"]),
                                 _qty(l["received_m"])] for l in lines])
        if r is None:
            self.note.setText("No transfers here." if not self.rows else "")
        elif r["status"] == service.SENT:
            self.note.setText("On the way. The place it's going to counts it in when it arrives.")
        elif r["status"] == service.RECEIVED:
            self.note.setText(f"Counted in by {r['closed_name']}, {fmt_datetime(r['closed_at'])}")
        else:
            self.note.setText(f"Cancelled by {r['closed_name']}: {r['cancel_reason']}")

    def _done(self):
        self.refresh()
        self.changed.emit()

    def send(self):
        if SendTransferDialog(self, self.conn, self.user).exec():
            self.tab = service.SENT
            self._done()

    def receive(self):
        r = self._row()
        if r:
            d = ReceiveTransferDialog(self, self.conn, self.user, r)
            if d.exec():
                short = d.result["short_m"]
                if short:
                    info(self, "Transfer received", f"{fmt_qty(short)} didn't arrive. The office sees it on the Missing stock report as lost on transfer.")
                self._done()

    def cancel(self):
        r = self._row()
        if r:
            reason = ask_text(self, "Cancel transfer", f"Why is {r['transfer_number']} being cancelled? The goods go back on {r['from_name']}'s books.")
            if reason is not None:
                service.cancel_transfer(self.conn, self.user, r["id"], reason)
                self._done()


# ------------------------------------------------------------------ reports

class InventoryReportsPage(QWidget):
    changed = Signal()

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        root, _top = page_layout(self, "Inventory reports")
        tabs = QTabWidget()
        self.tabs = tabs

        value = QWidget()
        vl = QVBoxLayout(value)
        vl.addWidget(label("What each place holds, at the last purchase cost", "Muted"))
        self.value_table = make_table(["Place", "Products", "For sale", "Damaged and held", "Below zero", "No cost"], numeric=(1, 2, 3, 4, 5), stretch=0)
        set_widths(self.value_table, {1: 80, 2: 130, 3: 140, 4: 90, 5: 80})
        vl.addWidget(self.value_table)
        tabs.addTab(value, "Stock value")

        moves = QWidget()
        ml = QVBoxLayout(moves)
        bar = QHBoxLayout()
        from datetime import date
        self.date_from, self.date_to = date_edit(date.today().replace(day=1).isoformat()), date_edit(date.today().isoformat())
        self.move_place = place_combo(conn, user, everywhere=True)
        for text, w in (("From", self.date_from), ("To", self.date_to)):
            bar.addWidget(label(text, "Muted"))
            bar.addWidget(w)
            w.dateChanged.connect(self.refresh)
        bar.addWidget(self.move_place)
        self.move_place.currentIndexChanged.connect(self.refresh)
        bar.addStretch(1)
        ml.addLayout(bar)
        ml.addWidget(label("Negative stock count differences are what went missing, or was sold without being recorded", "Muted"))
        self.move_table = make_table(["What happened", "Stock", "In", "Out", "Value", "Products"], numeric=(2, 3, 4, 5), stretch=0)
        ml.addWidget(self.move_table)
        tabs.addTab(moves, "Movements")

        lost = QWidget()
        ll = QVBoxLayout(lost)
        ll.addWidget(label("Delivered lines that match no catalog product, so they're off the books. Add the product to the catalog with the same SKU.", "Muted"))
        self.lost_empty = label("Every delivered line matches a catalog product.", "Muted")
        ll.addWidget(self.lost_empty)
        self.lost_table = make_table(["Place", "PO number", "SKU", "Product", "Size", "Color", "Quantity", "Last moved"], numeric=(6,), stretch=3)
        ll.addWidget(self.lost_table)
        tabs.addTab(lost, "Not in the catalog")
        # Sales, fast and slow movers, missing stock and value by department come from the sales screens' module.
        from .sales_page import SalesReportsWidget
        self.sales = SalesReportsWidget(conn, user)
        self.sales_pages = {}
        for _ in range(self.sales.count()):
            text = self.sales.tabText(0)
            page = self.sales.widget(0)
            self.sales.removeTab(0)
            tabs.insertTab(tabs.count(), page, text)
            self.sales_pages[page] = text
        # The sales tabs share one row of filters, shown above the tabs while one of them is open.
        root.addWidget(self.sales.filters)
        root.addWidget(tabs, 1)
        tabs.currentChanged.connect(self._tab_changed)
        tabs.setCurrentIndex(0)
        self._tab_changed()
        self.refresh()

    def _tab_changed(self, *_):
        text = self.sales_pages.get(self.tabs.currentWidget())
        self.sales.filters.setVisible(text is not None)
        if text is not None:
            self.sales.show_filters_for(text)

    def refresh(self, *_):
        rows = service.valuation(self.conn, self.user)
        fill_table(self.value_table, [[r["location_name"], r["products"], fmt_peso(r["good_value_c"]), fmt_peso(r["other_value_c"]), r["below_zero"], r["no_cost"]] for r in rows]
                   + [["Total", sum(r["products"] for r in rows), fmt_peso(sum(r["good_value_c"] for r in rows)), fmt_peso(sum(r["other_value_c"] for r in rows)),
                       sum(r["below_zero"] for r in rows), sum(r["no_cost"] for r in rows)]])
        moves = service.movement_summary(self.conn, self.user, date_value(self.date_from) or "0000", date_value(self.date_to) or "9999", combo_value(self.move_place))
        fill_table(self.move_table, [[m["kind_label"], {"GOOD": "For sale", "DAMAGED": "Damaged", "HELD": "Held"}[m["bucket"]], fmt_qty(m["in_m"]), fmt_qty(m["out_m"]),
                                      fmt_peso(m["value_c"]), m["products"]] for m in moves])
        if hasattr(self, "sales"):
            self.sales.refresh()
        lost = service.not_in_catalog(self.conn, self.user)
        fill_table(self.lost_table, [[r["location_name"], r["po_number"], r["item_code"], r["description"], r["size"], r["color"], fmt_qty(r["qty_m"]), fmt_datetime(r["last_at"])]
                                     for r in lost])
        self.lost_empty.setVisible(not lost)
