"""Inventory phase 2 screens: sales and customer returns (typed or from a POS file), reorder suggestions, and the sales reports.
They only call stock_sales and reorder; every rule lives there."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLineEdit, QTabWidget, QVBoxLayout,
                               QWidget)

from .. import db, reorder, stock_sales as service
from .. import inventory as inv
from ..db import POError, today
from ..money import fmt_peso, fmt_qty, parse_pesos
from .common import (ask_text, button, combo_value, confirm, date_edit, date_value, fill_table, fmt_date, fmt_datetime, info, label, make_table,
                     register_tabs, selected_key, set_widths, show_if, show_tab_counts)
from .inventory_page import ProductLines, _Dialog, _editable, _typed, place_combo
from .pages import page_layout

STOCK = {"GOOD": "For sale", "DAMAGED": "Damaged"}


def _signed(qty_m: int) -> str:
    return f"{'+' if qty_m > 0 else ''}{fmt_qty(qty_m)}"


# ------------------------------------------------------------------ sales

class RecordSalesDialog(_Dialog):
    """What a place sold on one day, or what customers brought back."""

    def __init__(self, parent, conn, user, kind=service.SALE):
        super().__init__(parent, "Record sales" if kind == service.SALE else "Customer return", "Save")
        self.conn, self.user, self.kind = conn, user, kind
        form = QFormLayout()
        self.place = place_combo(conn, user)
        self.day = date_edit(today().isoformat())
        self.note = QLineEdit()
        form.addRow("Where", self.place)
        form.addRow("Day", self.day)
        self.bucket = QComboBox()
        if kind == service.RETURN:
            self.bucket.addItem("Back to stock for sale", inv.GOOD)
            self.bucket.addItem("Damaged", inv.DAMAGED)
            form.addRow("Goes to", self.bucket)
        form.addRow("Note", self.note)
        self.root.addLayout(form)
        hint = label("A price is for one unit. Leave it blank to use what it last sold for here. A return to stock for sale waits for management's approval."
                     if kind == service.RETURN else
                     "A price is for one unit. Leave it blank to use the catalog selling price. A price below half the catalog price waits for management's approval.",
                     "Muted")
        hint.setWordWrap(True)
        self.root.addWidget(hint)
        self.lines = ProductLines(conn, "Quantity", "Price (blank: last sold)" if kind == service.RETURN else "Price (blank: catalog)")
        self.root.addWidget(self.lines, 1)
        self.sale_id = None
        self.finish()

    def _save(self):
        prices = self.lines.extras()
        bucket = combo_value(self.bucket) if self.kind == service.RETURN else inv.GOOD
        lines = [{"product_id": pid, "qty": qty, "price": prices.get(pid, ""), "bucket": bucket} for pid, qty in self.lines.values().items()]
        self.sale_id = service.record(self.conn, self.user, combo_value(self.place), date_value(self.day), lines, self.kind, self.note.text())
        self.accept()


class ImportSalesDialog(_Dialog):
    """A POS export: matched by SKU, checked on screen, then recorded as one sales paper."""

    def __init__(self, parent, conn, user):
        super().__init__(parent, "Import sales from a POS file", "Record these sales")
        self.conn, self.user, self.data, self.name, self.result = conn, user, None, "", None
        form = QFormLayout()
        self.place = place_combo(conn, user)
        self.day = date_edit(today().isoformat())
        form.addRow("Where", self.place)
        form.addRow("Day", self.day)
        self.root.addLayout(form)
        row = QHBoxLayout()
        self.file_label = label("Export the day's sales from the POS as a CSV file, with a SKU column and a quantity column.", "Muted")
        self.file_label.setWordWrap(True)
        row.addWidget(self.file_label, 1)
        row.addWidget(button("Choose file", self.choose, lock=False))
        self.root.addLayout(row)
        self.table = make_table(["SKU", "Product", "Unit", "Quantity", "Amount"], numeric=(3, 4), stretch=1)
        self.root.addWidget(self.table, 2)
        self.unmatched = label("", "Error")
        self.unmatched.setWordWrap(True)
        self.root.addWidget(self.unmatched)
        self.finish()
        self.buttons.button(self.buttons.StandardButton.Ok).setEnabled(False)

    def choose(self):
        path, _ = QFileDialog.getOpenFileName(self, "POS sales file", "", "Sales files (*.csv *.txt);;All files (*)")
        if not path:
            return
        self.load(Path(path).read_bytes(), Path(path).name)

    def load(self, data: bytes, name: str):
        read = service.read_file(self.conn, data)
        self.data, self.name = data, name
        self.file_label.setText(f"{name}: {len(read['lines'])} products matched")
        fill_table(self.table, [[l["item_code"], l["description"], l["unit"], l["qty"], fmt_peso(parse_pesos(l["amount"])) if l["amount"] else "Catalog price"]
                                for l in read["lines"]])
        missing = read["unmatched"]
        self.unmatched.setText("" if not missing else "Not in the catalog, so left out: " + ", ".join(f"{m['code']} ({m['qty']})" for m in missing[:20])
                               + (f" and {len(missing) - 20} more" if len(missing) > 20 else "") + ". Add them to the catalog, then type their sales in with Record sales.")
        self.buttons.button(self.buttons.StandardButton.Ok).setEnabled(bool(read["lines"]))

    def _save(self):
        if self.data is None:
            raise POError("Choose the POS file first.")
        self.result = service.import_file(self.conn, self.user, combo_value(self.place), date_value(self.day), self.data, self.name)
        self.accept()


class SalesPage(QWidget):
    changed = Signal()
    TABS = {"SALE": "Sales", "RETURN": "Customer returns", "WAITING": "Waiting for approval", "CANCELLED": "Cancelled or rejected", "ALL": "All"}

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.tab = "SALE"
        self.rows = []
        self.show_cost = user.can("inventory.view")
        root, top = page_layout(self, "Sales")
        top.addWidget(label("What each place sold, typed in or from a POS file", "Muted"))
        self.tabs = register_tabs(self.TABS, self._set_tab)
        root.addWidget(self.tabs)
        bar = QHBoxLayout()
        self.place = place_combo(conn, user, everywhere=True)
        self.date_from, self.date_to = date_edit((today() - timedelta(days=30)).isoformat()), date_edit(today().isoformat())
        bar.addWidget(self.place)
        for text, w in (("From", self.date_from), ("To", self.date_to)):
            bar.addWidget(label(text, "Muted"))
            bar.addWidget(w)
            w.dateChanged.connect(self.refresh)
        self.place.currentIndexChanged.connect(self.refresh)
        bar.addStretch(1)
        self.total = label("", "Muted")
        bar.addWidget(self.total)
        root.addLayout(bar)
        self.table = make_table(["Number", "Day", "Place", "Kind", "From", "Products", "Amount", "Recorded by", "Note"], numeric=(5, 6), stretch=8)
        set_widths(self.table, {0: 120, 1: 100, 2: 130, 3: 120, 4: 90, 5: 70, 6: 110, 7: 130})
        self.table.itemSelectionChanged.connect(self._selected)
        root.addWidget(self.table, 2)
        row = QHBoxLayout()
        self.note = label("", "Muted")
        self.note.setWordWrap(True)
        row.addWidget(self.note, 1)
        self.cancel_btn = button("Cancel these sales", self.cancel, "Danger")
        self.reject_btn = button("Reject", self.reject, "Danger")
        self.approve_btn = button("Approve", self.approve, "Primary")
        if user.can("stock.post"):
            row.addWidget(self.cancel_btn)
            row.addWidget(self.reject_btn)
            row.addWidget(self.approve_btn)
            row.addSpacing(32)
        if user.can("sales.record"):
            row.addWidget(button("Customer return", lambda: self.record(service.RETURN)))
            row.addWidget(button("Import POS file", self.import_file))
            row.addWidget(button("Record sales", lambda: self.record(service.SALE), "Primary"))
        root.addLayout(row)
        heads = ["SKU", "Product", "Size", "Color", "Unit", "Stock", "Quantity", "Price", "Amount"] + (["Cost", "Gross profit"] if self.show_cost else [])
        self.lines = make_table(heads, numeric=tuple(range(6, len(heads))), stretch=1)
        root.addWidget(self.lines, 2)
        self.refresh()

    def _set_tab(self, tab):
        self.tab = tab
        self.refresh()

    def refresh(self, *_):
        key = selected_key(self.table)
        everything = service.papers(self.conn, self.user, location=combo_value(self.place), date_from=date_value(self.date_from), date_to=date_value(self.date_to))
        pick = {"SALE": lambda r: r["state"] == service.POSTED and r["kind"] == service.SALE, "RETURN": lambda r: r["state"] == service.POSTED and r["kind"] == service.RETURN,
                "WAITING": lambda r: r["state"] == service.WAITING, "CANCELLED": lambda r: r["state"] in (service.CANCELLED, service.REJECTED), "ALL": lambda r: True}
        show_tab_counts(self.tabs, self.TABS, {t: sum(map(f, everything)) for t, f in pick.items()})
        self.rows = [r for r in everything if pick[self.tab](r)]
        fill_table(self.table, [[r["sale_number"], fmt_date(r["sale_date"]), r["location_name"], r["kind_label"], r["source_label"], r["lines"],
                                 fmt_peso(r["total_c"] if r["kind"] == service.SALE else -r["total_c"]), r["entered_name"], r["file_name"] or r["note"]]
                                for r in self.rows], keys=[r["id"] for r in self.rows])
        net = sum(r["total_c"] if r["kind"] == service.SALE else -r["total_c"] for r in everything if r["on_books"])
        self.total.setText(f"Net sales in these dates: {fmt_peso(net)}")
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
        mine = r is not None and r["entered_by"] == self.user.id
        show_if(self.cancel_btn, r is not None and r["state"] == service.POSTED and not mine and self.user.can("stock.post"))
        show_if(self.approve_btn, r is not None and r["state"] == service.WAITING and not mine and self.user.can("stock.post"))
        show_if(self.reject_btn, r is not None and r["state"] == service.WAITING and not mine and self.user.can("stock.post"))
        lines = service.paper_lines(self.conn, self.user, r["id"]) if r else []
        fill_table(self.lines, [[l["item_code"], l["description"], l["size"], l["color"], l["unit"], STOCK[l["bucket"]], fmt_qty(l["qty_m"]), fmt_peso(l["price_c"]),
                                 fmt_peso(l["amount_c"])] + ([("" if l["cost_c"] is None else fmt_peso(l["cost_c"])), ("" if l["profit_c"] is None else fmt_peso(l["profit_c"]))]
                                                             if self.show_cost else []) for l in lines])
        if r is None:
            self.note.setText("Nothing recorded in these dates." if not self.rows else "")
        elif r["status"] == service.CANCELLED:
            self.note.setText(f"Cancelled by {r['cancelled_name']}, {fmt_datetime(r['cancelled_at'])}: {r['cancel_reason']}")
        elif r["state"] == service.WAITING:
            self.note.setText(f"Waiting for approval before it changes the books: {r['hold_reason']}."
                              + (" Someone else approves it." if mine else ""))
        elif r["state"] == service.REJECTED:
            self.note.setText(f"Rejected by {r['hold_decided_name']}, {fmt_datetime(r['hold_decided_at'])}: {r['hold_note']}. It never reached the books.")
        else:
            self.note.setText(f"Recorded by {r['entered_name']}, {fmt_datetime(r['entered_at'])}" + (f", from {r['file_name']}" if r["file_name"] else ""))

    def _done(self):
        self.refresh()
        self.changed.emit()

    def record(self, kind):
        d = RecordSalesDialog(self, self.conn, self.user, kind)
        if d.exec():
            held = service.is_waiting(self.conn, d.sale_id)
            self.tab = "WAITING" if held else "SALE" if kind == service.SALE else "RETURN"
            if held:
                info(self, "Saved", "Saved. It waits for management's approval before it changes the books.")
            self._done()

    def approve(self):
        r = self._row()
        if r and confirm(self, "Approve", f"Approve {r['sale_number']}? It changes the books at {r['location_name']}.", "Approve"):
            service.decide(self.conn, self.user, r["id"], True)
            self._done()

    def reject(self):
        r = self._row()
        if r:
            reason = ask_text(self, "Reject", f"Why is {r['sale_number']} rejected? {r['entered_name']} will see this.")
            if reason is not None:
                service.decide(self.conn, self.user, r["id"], False, reason)
                self._done()

    def import_file(self):
        d = ImportSalesDialog(self, self.conn, self.user)
        if d.exec():
            self.tab = "SALE"
            self._done()

    def cancel(self):
        r = self._row()
        if r:
            reason = ask_text(self, "Cancel sales", f"Why is {r['sale_number']} being cancelled? Its stock goes back on the books; record the sales again correctly.")
            if reason is not None:
                service.cancel(self.conn, self.user, r["id"], reason)
                self._done()


# ------------------------------------------------------------------ reorder

class LevelsDialog(_Dialog):
    def __init__(self, parent, conn, user, row):
        super().__init__(parent, "Reorder levels", "Save")
        self.conn, self.user, self.row = conn, user, row
        self.root.addWidget(label(f"{row['item_code']} {row['description']} at {row['location_name']}, in {row['unit']}", "PanelTitle"))
        self.root.addWidget(label(f"From sales: about {fmt_qty(row['per_day_m'])} a day. Leave both blank to let the program work them out.", "Muted"))
        form = QFormLayout()
        self.minimum = QLineEdit(fmt_qty(row["min_m"]) if row["manual"] else "")
        self.target = QLineEdit(fmt_qty(row["target_m"]) if row["manual"] else "")
        self.minimum.setPlaceholderText(f"Program: {fmt_qty(row['min_m'])}" if not row["manual"] else "")
        self.target.setPlaceholderText(f"Program: {fmt_qty(row['target_m'])}" if not row["manual"] else "")
        form.addRow("Order when below", self.minimum)
        form.addRow("Order up to", self.target)
        self.root.addLayout(form)
        self.finish()
        self.resize(460, 220)

    def _save(self):
        reorder.set_levels(self.conn, self.user, self.row["location"], self.row["product_id"], self.minimum.text(), self.target.text())
        self.accept()


class ReorderSettingsDialog(_Dialog):
    KEYS = (("inventory.sales_days", "Average sales over (days)"), ("inventory.lead_days", "A delivery takes (days)"),
            ("inventory.cover_days", "Stock should last after it arrives (days)"), ("inventory.dead_days", "Not moving after (days with no sale)"))

    def __init__(self, parent, conn, user):
        super().__init__(parent, "How reorder levels are worked out", "Save")
        self.conn, self.user = conn, user
        form = QFormLayout()
        self.boxes = {}
        for key, text in self.KEYS:
            self.boxes[key] = QLineEdit(db.get_setting(conn, key))
            form.addRow(text, self.boxes[key])
        self.root.addLayout(form)
        self.root.addWidget(label("Minimum = average daily sales x delivery days. Target = average daily sales x (delivery days + days it should last).", "Muted"))
        self.finish()
        self.resize(520, 260)

    def _save(self):
        values = {}
        for key, box in self.boxes.items():
            text = box.text().strip()
            if not text.isdigit() or not 0 < int(text) <= 3650:
                raise POError("Each figure is a number of days, from 1 to 3650.")
            values[key] = text
        db.set_settings(self.conn, self.user, values)
        self.accept()


class ReorderPage(QWidget):
    changed = Signal()
    C_ORDER = 11

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.rows = []
        self.typed: dict = {}       # (branch, product) -> what the person typed in Order (units)
        self.suggested: dict = {}   # (branch, product) -> what the program put there
        self.can_order = user.can("of.edit")
        root, top = page_layout(self, "Reorder")
        top.addWidget(label("Products below their minimum, with what to order to reach the target", "Muted"))
        if user.can("settings.edit"):
            top.addWidget(button("How levels are worked out", self.settings))
        bar = QHBoxLayout()
        self.place = QComboBox()
        self.place.addItem("Every branch", None)
        for p in inv.locations(conn, user):
            if p["code"] != inv.WAREHOUSE:
                self.place.addItem(p["name"], p["code"])
        self.everything = QCheckBox("Show every product, not only those to order")
        self.place.currentIndexChanged.connect(self.refresh)
        self.everything.toggled.connect(self.refresh)
        bar.addWidget(self.place)
        bar.addWidget(self.everything)
        bar.addStretch(1)
        root.addLayout(bar)
        self.table = make_table(["Branch", "SKU", "Product", "Supplier", "For sale", "On the way", "On order", "Sold a day", "Order below", "Order up to", "Levels",
                                 "Order (units)", "Unit"], numeric=(4, 5, 6, 7, 8, 9, 11), stretch=2)
        set_widths(self.table, {0: 100, 1: 100, 3: 110, 4: 65, 5: 75, 6: 65, 7: 70, 8: 80, 9: 80, 10: 65, 11: 95, 12: 50})
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        root.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.note = label("", "Muted")
        self.note.setWordWrap(True)
        row.addWidget(self.note, 1)
        if user.can("reorder.edit"):
            row.addWidget(button("Set levels", self.set_levels))
        if user.can("of.edit"):
            row.addWidget(button("Make draft Order Forms", self.make_orders, "Primary"))
        root.addLayout(row)
        self.refresh()

    def _keep_typing(self):
        if self.table.rowCount():
            for key, text in _typed(self.table, self.C_ORDER).items():
                if text != self.suggested.get(key, ""):
                    self.typed[key] = text
                else:
                    self.typed.pop(key, None)

    def refresh(self, *_):
        self._keep_typing()
        self.rows = reorder.levels(self.conn, self.user, combo_value(self.place), only_low=not self.everything.isChecked())
        self.suggested = {(r["location"], r["product_id"]): fmt_qty(r["order_m"]) if r["low"] else "" for r in self.rows}
        keys = [(r["location"], r["product_id"]) for r in self.rows]
        fill_table(self.table, [[r["location_name"], r["item_code"], r["description"], r["supplier"] or "No supplier", fmt_qty(r["good_m"]), fmt_qty(r["transit_m"]),
                                 fmt_qty(r["on_order_m"]), fmt_qty(r["per_day_m"]), fmt_qty(r["min_m"]), fmt_qty(r["target_m"]), "By hand" if r["manual"] else "Program",
                                 self.typed.get(k, self.suggested[k]), r["uom"]] for r, k in zip(self.rows, keys)], keys=keys)
        self.table.setSortingEnabled(False)
        for i, r in enumerate(self.rows):
            if r["why_not"]:
                for c in (3, self.C_ORDER):
                    self.table.item(i, c).setToolTip(f"Can't be ordered from here: {r['why_not'].lower()}.")
        if self.can_order:
            _editable(self.table, self.C_ORDER)
        low = [r for r in self.rows if r["low"]]
        cannot = sum(not r["orderable"] for r in low)
        text = (f"{len(low)} to order" + (f", {cannot} that can't be ordered from here (hover the supplier to see why)" if cannot else "") + "."
                if low else "Nothing is below its minimum.")
        if self.can_order:
            text += " Type in Order (units) to change an amount or order something else. Select two or more rows to order only those."
        self.note.setText(text)

    def _picked(self) -> list[int]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return rows if len(rows) > 1 else []

    def settings(self):
        if ReorderSettingsDialog(self, self.conn, self.user).exec():
            self.refresh()

    def set_levels(self):
        key = selected_key(self.table)
        r = next((x for x in self.rows if (x["location"], x["product_id"]) == key), None)
        if r is None:
            raise POError("Pick a product first.")
        if LevelsDialog(self, self.conn, self.user, r).exec():
            self.refresh()
            self.changed.emit()

    def make_orders(self):
        typed = _typed(self.table, self.C_ORDER)
        chosen = self._picked()
        by_key = {(r["location"], r["product_id"]): r for r in self.rows}
        if chosen:
            keys = [self.table.item(i, 0).data(Qt.ItemDataRole.UserRole) for i in chosen]
        else:
            # Every low line that can be ordered, and any other line someone typed an amount for.
            keys = [k for k, r in by_key.items() if r["orderable"] and (r["low"] or typed.get(k, "") != self.suggested.get(k, ""))]
        keys = [k for k in keys if str(typed.get(k, "")).strip() not in ("", "0")]
        if not keys:
            raise POError("Nothing to order. Type a quantity in Order (units).")
        suppliers = {by_key[k]["supplier"] for k in keys}
        listed = [f"{by_key[k]['item_code']} at {by_key[k]['location_name']}: {typed[k]} {by_key[k]['uom']}" for k in keys]
        lines = "\n".join(listed[:12]) + (f"\n... and {len(listed) - 12} more" if len(listed) > 12 else "")
        n = len(suppliers)
        if not confirm(self, "Make draft Order Forms", f"Make {n} draft Order Form{'s' if n != 1 else ''} for these {len(keys)} lines?\n\n{lines}\n\n"
                       "They go through the usual approval.", "Make drafts"):
            return
        made = reorder.make_orders(self.conn, self.user, keys, {k: typed[k] for k in keys})
        for k in keys:
            self.typed.pop(k, None)
        self.table.clearContents()
        self.table.setRowCount(0)
        info(self, "Draft Order Forms made", f"{len(made)} draft Order Form{'s are' if len(made) != 1 else ' is'} under Purchasing, Orders. "
             "Check and submit them for approval.")
        self.refresh()
        self.changed.emit()


# ------------------------------------------------------------------ reports

class SalesReportsWidget(QTabWidget):
    """Sales, fast and slow movers, missing stock and stock value by department: tabs added to Inventory reports.
    Their filters sit in `filters`, which Inventory reports shows above its tabs; show_filters_for() shows the ones a tab uses."""

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.filters = QWidget()
        bar = QHBoxLayout(self.filters)
        bar.setContentsMargins(0, 0, 0, 0)
        self.date_from, self.date_to = date_edit((today() - timedelta(days=30)).isoformat()), date_edit(today().isoformat())
        self.place = place_combo(conn, user, everywhere=True)
        self.by = QComboBox()
        self.by.addItem("By product", "product")
        self.by.addItem("By place", "place")
        self.date_widgets = []
        for text, w in (("From", self.date_from), ("To", self.date_to)):
            caption = label(text, "Muted")
            bar.addWidget(caption)
            bar.addWidget(w)
            self.date_widgets += [caption, w]
            w.dateChanged.connect(self.refresh)
        for w in (self.place, self.by):
            bar.addWidget(w)
            w.currentIndexChanged.connect(self.refresh)
        bar.addStretch(1)

        sales = QWidget()
        sl = QVBoxLayout(sales)
        self.sales_total = label("", "Muted")
        self.sales_total.setWordWrap(True)
        sl.addWidget(self.sales_total)
        self.sales_table = make_table(["Place", "SKU", "Product", "Department", "Unit", "Sold", "Sales", "Cost", "Gross profit", "Margin"], numeric=(5, 6, 7, 8, 9), stretch=2)
        set_widths(self.sales_table, {0: 120, 1: 100, 3: 110, 4: 50, 5: 70, 6: 110, 7: 110, 8: 110, 9: 70})
        sl.addWidget(self.sales_table)
        self.addTab(sales, "Sales")

        moving = QWidget()
        ml = QVBoxLayout(moving)
        ml.addWidget(label("Sales over the reorder window, fastest first. Days left is what's for sale at the pace it's selling.", "Muted"))
        self.movers_table = make_table(["Place", "SKU", "Product", "Unit", "Sold", "A day", "For sale", "Value", "Days left", "Last sold", "Not moving"],
                                       numeric=(4, 5, 6, 7, 8), stretch=2)
        set_widths(self.movers_table, {0: 110, 1: 100, 3: 50, 4: 70, 5: 60, 6: 70, 7: 100, 8: 70, 9: 100, 10: 90})
        ml.addWidget(self.movers_table)
        self.addTab(moving, "Fast and slow")

        lost = QWidget()
        ll = QVBoxLayout(lost)
        ll.addWidget(label("What stock counts found missing or over the books, and what didn't arrive on transfers, between the dates chosen, at the last purchase cost", "Muted"))
        self.lost_total = label("", "Muted")
        ll.addWidget(self.lost_total)
        self.lost_table = make_table(["Place", "How", "SKU", "Product", "Unit", "Missing", "Missing value", "Over", "Over value"], numeric=(5, 6, 7, 8), stretch=3)
        set_widths(self.lost_table, {0: 110, 1: 120, 2: 100, 4: 50, 5: 70, 6: 110, 7: 70, 8: 100})
        ll.addWidget(self.lost_table)
        self.addTab(lost, "Missing stock")

        dept = QWidget()
        dl = QVBoxLayout(dept)
        self.dept_table = make_table(["Department", "Products", "For sale", "Damaged and held"], numeric=(1, 2, 3), stretch=0)
        set_widths(self.dept_table, {0: 180, 1: 80, 2: 130, 3: 140})
        dl.addWidget(self.dept_table)
        self.addTab(dept, "By department")
        self.refresh()

    def show_filters_for(self, tab: str):
        """Sales and Missing stock use the dates; every tab uses the place; only Sales groups by product or place."""
        for w in self.date_widgets:
            w.setVisible(tab in ("Sales", "Missing stock"))
        self.by.setVisible(tab == "Sales")

    def refresh(self, *_):
        a, b = date_value(self.date_from) or "0000-01-01", date_value(self.date_to) or "9999-12-31"
        place = combo_value(self.place)
        rows = service.sales_report(self.conn, self.user, a, b, place, combo_value(self.by))
        by_place = combo_value(self.by) == "place"
        fill_table(self.sales_table, [[r["location_name"] if by_place or place is not None else "Every place", "" if by_place else r["item_code"],
                                       "" if by_place else r["description"], "" if by_place else r["department"], "" if by_place else r["unit"],
                                       "" if by_place else fmt_qty(r["qty_m"]), fmt_peso(r["amount_c"]),
                                       "No cost" if r["cost_c"] is None else fmt_peso(r["cost_c"]), "" if r["profit_c"] is None else fmt_peso(r["profit_c"]),
                                       service.fmt_margin(r["margin_bp"])] for r in rows])
        amount = sum(r["amount_c"] for r in rows)
        profit = sum(r["profit_c"] for r in rows if r["profit_c"] is not None)
        base = sum(r["costed_c"] for r in rows)
        uncosted = sum(r["no_cost_c"] for r in rows)
        margin = service.fmt_margin(service._round(service.Decimal(profit) * 10000 / base)) if base else ""
        self.sales_total.setText(f"Net sales {fmt_peso(amount)}, gross profit {fmt_peso(profit)}" + (f" ({margin})" if margin else "")
                                 + (f". {fmt_peso(uncosted)} of sales are of products with no cost, so they're left out of the profit." if uncosted else ""))
        movers = service.movers(self.conn, self.user, place)
        fill_table(self.movers_table, [[r["location_name"], r["item_code"], r["description"], r["unit"], fmt_qty(r["sold_m"]), fmt_qty(r["per_day_m"]), fmt_qty(r["good_m"]),
                                        fmt_peso(r["value_c"]), "" if r["days_left"] is None else r["days_left"], fmt_date(r["last_sold"]),
                                        "Not moving" if r["not_moving"] else ""] for r in movers])
        lost = service.shrinkage(self.conn, self.user, a, b, place)
        fill_table(self.lost_table, [[r["location_name"], r["how_label"], r["item_code"], r["description"], r["unit"], fmt_qty(r["missing_m"]), fmt_peso(r["missing_c"]),
                                      fmt_qty(r["over_m"]), fmt_peso(r["over_c"])] for r in lost])
        self.lost_total.setText(f"Missing {fmt_peso(sum(r['missing_c'] for r in lost))}, over {fmt_peso(sum(r['over_c'] for r in lost))}")
        dept = service.value_by_department(self.conn, self.user, place)
        fill_table(self.dept_table, [[r["department"], r["products"], fmt_peso(r["good_value_c"]), fmt_peso(r["other_value_c"])] for r in dept])
