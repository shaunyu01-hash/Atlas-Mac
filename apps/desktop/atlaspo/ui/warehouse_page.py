"""Central Warehouse: every order arrives at the warehouse first. The warehouse counts the boxes for each branch, counts and stickers goods that come without stickers,
then ships each branch on. The branches still count what reaches them."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QAbstractItemView, QButtonGroup, QHBoxLayout, QLineEdit, QPushButton, QSplitter, QVBoxLayout, QWidget

from .. import masters, warehouse as service
from ..money import fmt_qty, parse_qty
from . import theme
from .common import Pager, keep_place, button, confirm, guarded, fill_table, info, fmt_date, fmt_datetime, label, make_table, register_tabs, selected_key, set_widths, show_if, show_tab_counts, smart_columns
from .pages import page_layout

C_BOXES = 4
# Goods on their way back from the branches: damaged ones on a claim stub, excess on an item pass. The warehouse counts them here too, as they arrive.
BACK = "BACK"
TABS = {**service.TABS, BACK: "Coming back"}
C_STICKERED = 7
STATUS = {service.TO_RECEIVE: "To receive", service.TO_SHIP: "Ready to ship", service.SHIPPED: "Shipped"}


class CentralWarehousePage(QWidget):
    """Orders by where they are at the warehouse. An order shows under every tab one of its branches is in."""
    changed = Signal()

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.tab = service.TO_RECEIVE
        self.rows, self.items, self.pos = [], [], []
        self.another = False
        # What someone typed and hasn't saved, per order, so clicking another order, a tab or the search never throws it away.
        self.drafts: dict[int, dict] = {}
        self._filling = False
        self.can_edit = user.can("warehouse.receive")
        root, top = page_layout(self, "Central Warehouse")
        top.addWidget(label("Every order comes here first. The branches count it again when it reaches them", "Muted"))
        self.tabs = register_tabs(TABS, self._set_tab)
        root.addWidget(self.tabs)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("OF or PO number, supplier or code")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(280)
        # The list follows the search once typing pauses, not on every key.
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(300)
        self.search_timer.timeout.connect(lambda: self.refresh())
        self.search.textChanged.connect(self._search_typed)
        bar.addWidget(self.search)
        # One press for a branch: the orders with something for it on this tab, and only its lines below. Each count says how many.
        self.branch_code = None
        self.branch_buttons = {}
        self.branch_group = QButtonGroup(self)
        for code, name in [(None, "All branches")] + [(b["code"], b["name"]) for b in masters.list_branches(conn, active_only=True, branches_only=True)]:
            b = QPushButton(name)
            b.setObjectName("Count")
            b.setCheckable(True)
            b.setProperty("branch_name", name)
            b.clicked.connect(lambda _=False, code=code: self._set_branch(code))
            self.branch_group.addButton(b)
            bar.addWidget(b)
            self.branch_buttons[code] = b
        self.branch_buttons[None].setChecked(True)
        bar.addStretch(1)
        root.addLayout(bar)
        self.table = make_table(["OF number", "Supplier", "Branches", "Pieces", "Stickers", "Boxes", "Received", "Shipped"], numeric=(2, 3, 5), stretch=1)
        set_widths(self.table, {0: 130, 2: 70, 3: 80, 4: max(120, self.table.fontMetrics().horizontalAdvance("Warehouse stickers") + 32), 5: 60, 6: 130, 7: 130})
        chooser = smart_columns(self.table, f"warehouse-{user.username}")
        columns_btn = button("Columns", lambda: chooser.open_under(columns_btn))
        bar.addWidget(columns_btn)
        self.table.itemSelectionChanged.connect(self._selected)
        self.empty = label("", "Empty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.hide()

        # The details sit under the list: what to do first, then the boxes, then any products to sticker, with the buttons last, under what they save.
        self.details = QWidget()
        col = QVBoxLayout(self.details)
        col.setContentsMargins(0, 8, 0, 0)
        col.setSpacing(6)
        self.note = label("", "PanelTitle")
        self.note.setWordWrap(True)
        col.addWidget(self.note)
        self.hint = label("", "Muted")
        self.hint.setWordWrap(True)
        col.addWidget(self.hint)
        self.boxes = make_table(["Branch", "PO number", "Delivery", "Pieces ordered", "Boxes", "Status", ""], numeric=(2, 3, C_BOXES), stretch=5)
        set_widths(self.boxes, {0: 130, 1: 130, 2: 70, 3: 110, C_BOXES: 80, 6: 120})
        self.boxes.verticalHeader().setDefaultSectionSize(36)
        self.boxes.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
        self.boxes.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.boxes.itemChanged.connect(self._typed)
        self.boxes.itemSelectionChanged.connect(self._ship_label)
        col.addWidget(self.boxes, 1)
        self.sticker_note = label("These come without stickers: count each product as you sticker it, 0 if none came.", "Muted")
        col.addWidget(self.sticker_note)
        self.stickers = make_table(["Branch", "SKU", "Product", "Size", "Color", "Ordered pcs", "Earlier deliveries", "Stickered pcs"], numeric=(5, 6, C_STICKERED), stretch=2)
        set_widths(self.stickers, {0: 110, 1: 100, 3: 70, 4: 90, 5: 90, 6: 120, C_STICKERED: 100})
        self.stickers.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
        self.stickers.itemChanged.connect(self._typed)
        col.addWidget(self.stickers, 2)
        actions = QHBoxLayout()
        self.remarks = QLineEdit()
        self.remarks.setPlaceholderText("Warehouse remarks, if any: a box damaged, the supplier's delivery person, ...")
        self.remarks.textEdited.connect(self._typed)
        actions.addWidget(self.remarks, 1)
        self.another_btn = button("Another delivery at the warehouse", self._start_another)
        self.save_btn = button("Save received", self.save, "Primary")
        self.ship_btn = button("Mark shipped", self.ship, "Primary")
        if self.can_edit:
            for w in (self.another_btn, self.save_btn, self.ship_btn):
                actions.addWidget(w)
        else:
            self.remarks.setReadOnly(True)
        col.addLayout(actions)

        # Shipped grows for good, so it shows a page at a time, newest first. To receive, To ship and Coming back are work to do and always show everything.
        self.list_side = QWidget()
        list_col = QVBoxLayout(self.list_side)
        list_col.setContentsMargins(0, 0, 0, 0)
        list_col.setSpacing(0)
        list_col.addWidget(self.table, 1)
        self.pager = Pager(self._show_more)
        list_col.addWidget(self.pager)
        self.split = QSplitter(Qt.Orientation.Vertical)
        self.split.addWidget(self.list_side)
        self.split.addWidget(self.details)
        self.split.setStretchFactor(0, 2)
        self.split.setStretchFactor(1, 3)
        self.split.setChildrenCollapsible(False)
        root.addWidget(self.split, 1)
        self.back = QWidget()
        back_col = QVBoxLayout(self.back)
        back_col.setContentsMargins(0, 0, 0, 0)
        self.returns = make_table(["Paper", "Number", "Branch", "Supplier", "PO number", "Pieces", "Where the goods are"], numeric=(5,), stretch=3)
        set_widths(self.returns, {0: 100, 1: 140, 2: 110, 3: max(120, self.returns.fontMetrics().horizontalAdvance("Supplier") + 40), 4: 130, 5: 70, 6: 220})
        back_col.addWidget(self.returns, 1)
        back_bar = QHBoxLayout()
        back_bar.addWidget(label("Count what came back when it reaches the warehouse. That's what lets the supplier collect it, and starts the 90 days.", "Muted"), 1)
        self.count_btn = button("Goods arrived: count them", self.count_return, "Primary")
        if self.can_edit:
            back_bar.addWidget(self.count_btn)
        back_col.addLayout(back_bar)
        root.addWidget(self.back, 1)
        self.back.hide()
        # The empty message takes the room the list had, so the search and tabs stay where they were.
        root.addWidget(self.empty, 1)
        self.refresh()

    def _set_tab(self, tab):
        self.tab = tab
        self.another = False
        self.pager.reset()
        self.refresh()

    def _set_branch(self, code):
        self.branch_code = code
        self.pager.reset()
        self.refresh()

    def _search_typed(self, *_):
        self.pager.reset()
        self.search_timer.start()

    def _show_more(self):
        with keep_place(self.table):
            self.refresh()

    def _for_branch(self, row: dict) -> dict | None:
        """The order cut down to the chosen branch's POs, or None if it has nothing for that branch."""
        if self.branch_code is None:
            return row
        pos = [p for p in row["pos"] if p["branch_code"] == self.branch_code]
        if not pos:
            return None
        return {**row, "pos": pos, "stages": {p["stage"] for p in pos}, "boxes": sum(p["boxes"] or 0 for p in pos), "ordered_m": sum(p["ordered_m"] for p in pos),
                "can_more": any(p["can_more"] for p in pos)}

    def _returns(self) -> list[dict]:
        """Claim stubs and item passes coming back to the warehouse, or there and not counted yet. A branch button narrows them too."""
        from .. import claim_stubs, item_passes
        out = []
        for kind, service_, number, waiting in (("Claim stub", claim_stubs, "cs_number", claim_stubs.ISSUED), ("Item pass", item_passes, "ip_number", item_passes.PRINTED)):
            # Only the two statuses that can be coming back are read, every one of them, newest first: an old one is never left off (no 2,000 cap).
            found = [r for status in (waiting, service_.AT_WAREHOUSE) for r in service_.search(self.conn, self.user, self.search.text(), status=status, limit=10 ** 9)]
            for r in sorted(found, key=lambda r: r["id"], reverse=True):
                at_central = r["status"] == service_.AT_WAREHOUSE and not service_.is_counted(self.conn, r["id"])
                if (r["status"] == waiting or at_central) and (self.branch_code is None or r["branch_code"] == self.branch_code):
                    out.append({**r, "kind": kind, "number": r[number], "service": service_, "where": "At the warehouse, not counted yet" if at_central else f"On its way from {r['branch_name']}"})
        return out

    def count_return(self):
        from . import claim_stub_page
        r = self.returns.currentRow()
        if r < 0 or r >= len(self.return_rows):
            return
        row = self.return_rows[r]
        service_ = row["service"]
        lines = service_.items(self.conn, self.user, row["id"])  # claim stubs and item passes both need the user (AUD-117)
        pieces = claim_stub_page.count_goods(self, row["number"], lines)
        if pieces is None:
            return
        if row["status"] == service_.AT_WAREHOUSE:
            service_.count_arrived(self.conn, self.user, row["id"], pieces)
        else:
            service_.arrived(self.conn, self.user, row["id"], pieces)
        self.refresh()
        self.changed.emit()

    def refresh(self, *_):
        everyone = service.search(self.conn, self.user, self.search.text())
        everything = [r for r in (self._for_branch(r) for r in everyone) if r]
        self.return_rows = self._returns()
        show_tab_counts(self.tabs, TABS, {**{t: sum(t in r["stages"] for r in everything) for t in service.TABS}, BACK: len(self.return_rows)})
        if self.tab == BACK:
            fill_table(self.returns, [[r["kind"], r["number"], r["branch_name"], r["supplier"], r["po_number"], fmt_qty(r["pieces_m"] or 0), r["where"]] for r in self.return_rows],
                       keys=[r["id"] for r in self.return_rows])
            if self.return_rows:
                self.returns.selectRow(0)
            self.split.hide()
            self.back.setVisible(bool(self.return_rows))
            self.empty.setText(self._no_match(len(self._returns_unsearched())) if self.search.text().strip() else "Nothing on its way back from the branches.")
            self.empty.setVisible(not self.return_rows)
            for code, b in self.branch_buttons.items():
                b.setText(b.property("branch_name"))
            show_if(self.count_btn, self.can_edit and bool(self.return_rows))
            return
        self.back.hide()
        rows = [r for r in everything if self.tab in r["stages"]]  # newest order first
        if self.tab == service.SHIPPED:
            self.rows = self.pager.take(rows)
        else:
            self.rows = rows
            self.pager.setVisible(False)
        for code, b in self.branch_buttons.items():
            b.setText(f"{b.property('branch_name')}  {sum(self.tab in (self._for_branch_code(r, code) or set()) for r in everyone)}")
        keep = selected_key(self.table)
        fill_table(self.table, [[r["of_number"], r["supplier"], len(r["pos"]), fmt_qty(r["ordered_m"]), "Warehouse stickers" if r["stickers"] else "Already on",
                                 r["boxes"] if r["received_at"] else "", fmt_datetime(r["received_at"]), fmt_datetime(r["shipped_at"])] for r in self.rows], keys=[r["id"] for r in self.rows])
        self.split.setVisible(bool(self.rows))
        if self.search.text().strip() and not self.rows:
            self.empty.setText(self._no_match(sum(self.tab in r["stages"] for r in (self._for_branch(r) for r in service.search(self.conn, self.user)) if r)))
        else:
            self.empty.setText({service.TO_RECEIVE: "Nothing on its way to the warehouse. An order appears here once its POs are printed.",
                                service.TO_SHIP: "Nothing waiting to ship.", service.SHIPPED: "Nothing shipped yet."}[self.tab])
        self.empty.setVisible(not self.rows)
        # The same order stays selected when it's still here; otherwise the first. The details follow either way, even when the row number didn't change.
        if self.rows:
            ids = [r["id"] for r in self.rows]
            self.table.blockSignals(True)
            self.table.selectRow(ids.index(keep) if keep in ids else 0)
            self.table.blockSignals(False)
        self._selected()

    def _no_match(self, total: int) -> str:
        text = f"Nothing matches \"{self.search.text().strip()}\"."
        return text + (f" Clear the search to see all {total}." if total else "")

    def _returns_unsearched(self) -> list[dict]:
        typed = self.search.text()
        self.search.blockSignals(True)
        self.search.setText("")
        try:
            return self._returns()
        finally:
            self.search.setText(typed)
            self.search.blockSignals(False)

    @staticmethod
    def _for_branch_code(row: dict, code) -> set:
        return {p["stage"] for p in row["pos"] if code is None or p["branch_code"] == code}

    def _row(self) -> dict | None:
        key = selected_key(self.table)
        return next((r for r in self.rows if r["id"] == key), None) if key else None

    def _po_editable(self, p) -> bool:
        if not self.can_edit:
            return False
        if self.another:
            return p["can_more"]
        return p["stage"] in (service.TO_RECEIVE, service.TO_SHIP)

    def _selected(self):
        row = self._row()
        if row is None or (self.another and not row["can_more"]):
            self.another = False
        self.pos = row["pos"] if row else []
        draft = self.drafts.get(row["id"], {}) if row else {}
        self._filling = True
        boxes = draft.get("boxes", {})
        fill_table(self.boxes, [[p["branch_name"], p["po_number"], p["delivery_no"] + (1 if self.another and p["can_more"] else 0), fmt_qty(p["ordered_m"]),
                                 boxes.get(p["id"], "" if (self.another and p["can_more"]) or p["boxes"] is None else p["boxes"]),
                                 ("Another delivery" if self.another and p["can_more"] else service.REISSUE if self._held(p)
                                  else STATUS[p["stage"]] + (f" {fmt_date(p['shipped_at'])}" if p["shipped_at"] else "")), ""]
                                for p in self.pos], keys=[p["id"] for p in self.pos])
        shown = {p["id"] for p in self.pos}
        self.items = [i for i in service.lines(self.conn, self.user, row["id"]) if i["po_id"] in shown] if row and row["stickers"] else []
        editable_pos = {p["id"] for p in self.pos if self._po_editable(p)}
        stickered = draft.get("stickered", {})
        fill_table(self.stickers, [[i["branch_name"], i["item_code"], i["description"], i["size"], i["color"], fmt_qty(i["ordered_m"]), fmt_qty(i["before_m"]) if i["before_m"] else "",
                                    stickered.get(i["po_item_id"], "" if self.another or i["stickered_m"] is None else fmt_qty(i["stickered_m"]))]
                                   for i in self.items], keys=[i["po_item_id"] for i in self.items])
        tint = QColor(theme.current.BRANCH_TINT)
        for table, col, ids in ((self.boxes, C_BOXES, [p["id"] if p["id"] in editable_pos else None for p in self.pos]),
                                (self.stickers, C_STICKERED, [i["po_item_id"] if i["po_id"] in editable_pos else None for i in self.items])):
            for r in range(table.rowCount()):
                for c in range(table.columnCount()):
                    item = table.item(r, c)
                    if c == col and ids[r] is not None:
                        item.setBackground(tint)  # the cells to type in
                    else:
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.remarks.setText(draft.get("remarks", next((p["remarks"] for p in self.pos if p["current"]), "")))
        self._filling = False
        self.details.setVisible(row is not None)
        for w in (self.stickers, self.sticker_note):
            w.setVisible(bool(self.items))
        # A Ship button on each branch ready to go, so one press marks that branch's boxes as left for the branch.
        for r, p in enumerate(self.pos):
            if self.can_edit and p["stage"] == service.TO_SHIP and not self._held(p) and not self.another:
                self.boxes.setCellWidget(r, 6, button("Ship", lambda _=False, po=p: guarded(self, self.ship_one, po), "Primary"))
            else:
                self.boxes.removeCellWidget(r, 6)
        # Branches ready to ship start selected, so Mark shipped sends them all unless someone picks fewer.
        self.boxes.clearSelection()
        for r, p in enumerate(self.pos):
            if p["stage"] == service.TO_SHIP and not self._held(p) and not self.another:
                self.boxes.selectRow(r)
        self._explain(row)
        editing = bool(editable_pos)
        show_if(self.save_btn, editing)
        self.save_btn.setText("Save received" if self.another or any(p["stage"] == service.TO_RECEIVE for p in self.pos) else "Save changes")
        show_if(self.another_btn, self.can_edit and row is not None and row["can_more"] and not self.another)
        self.remarks.setVisible(editing or bool(self.remarks.text()))
        self._ship_label()

    @staticmethod
    def _held(p) -> bool:
        """At the warehouse, but Purchasing changed the PO after printing it, so it can't ship until it's printed again."""
        return p["stage"] == service.TO_SHIP and p.get("reissue", False)

    def _explain(self, row):
        if row is None:
            self.note.setText("")
            self.hint.setText("")
            return
        waiting = [p for p in self.pos if p["stage"] == service.TO_RECEIVE]
        ready = [p for p in self.pos if p["stage"] == service.TO_SHIP and not self._held(p)]
        held = [p for p in self.pos if self._held(p)]
        if self.another:
            self.note.setText(f"{row['of_number']}, {row['supplier']}: another delivery. Type the boxes for each branch in it, and leave the others blank.")
        elif waiting:
            self.note.setText(f"{row['of_number']}, {row['supplier']}. " + ("Type the boxes for each branch, 0 if nothing came for it" + (", and the pieces stickered" if row["stickers"] else "")
                              + ", then Save received." if self.can_edit else "Waiting for the warehouse to count it."))
        elif ready:
            self.note.setText(f"{row['of_number']}: {sum(p['boxes'] for p in ready)} boxes ready. " + ("Select the branches whose boxes left, then Mark shipped." if self.can_edit else "Waiting to ship.")
                              + "".join(f" {p['po_number']} waits to be printed again." for p in held))
        elif held:
            self.note.setText(f"{row['of_number']}: {service.REISSUE} It can ship once it's printed again.")
        else:
            self.note.setText(f"{row['of_number']}: shipped. The branches count it when it arrives.")
        received = sorted({p["received_name"] for p in self.pos if p["received_name"]})
        self.hint.setText(("Received by " + ", ".join(received) + ". " if received else "") + ("Until a branch ships, its figures can still be corrected." if (ready or held) and self.can_edit else "")
                          + (" A later delivery for a branch that isn't finished: press Another delivery at the warehouse." if row["can_more"] and not self.another and self.can_edit else ""))

    def _ship_label(self):
        ready = self._to_ship()
        shippable = [p for p in self.pos if p["stage"] == service.TO_SHIP and not self._held(p)]
        show_if(self.ship_btn, self.can_edit and not self.another and bool(shippable))
        self.ship_btn.setEnabled(bool(ready))
        self.ship_btn.setText("Mark shipped" if len(ready) == len(shippable) else f"Mark {len(ready)} shipped")

    def _to_ship(self) -> list[dict]:
        rows = {i.row() for i in self.boxes.selectionModel().selectedRows()} if self.boxes.selectionModel() else set()
        return [p for r, p in enumerate(self.pos) if r in rows and p["stage"] == service.TO_SHIP and not self._held(p)]

    def _typed(self, *_):
        row = self._row()
        if self._filling or row is None:
            return
        self.drafts[row["id"]] = {"boxes": {self.boxes.item(r, 0).data(Qt.ItemDataRole.UserRole): self.boxes.item(r, C_BOXES).text() for r in range(self.boxes.rowCount())},
                                  "stickered": {self.stickers.item(r, 0).data(Qt.ItemDataRole.UserRole): self.stickers.item(r, C_STICKERED).text() for r in range(self.stickers.rowCount())},
                                  "remarks": self.remarks.text()}

    def _start_another(self):
        self.another = True
        row = self._row()
        if row:
            self.drafts.pop(row["id"], None)
        self._selected()

    def _over_ordered(self, stickered: dict, editable: set) -> bool:
        """Whether any product has more pieces stickered than are still to come for it. A figure that isn't a number is left for the save to explain."""
        for i in self.items:
            if i["po_id"] not in editable:
                continue
            try:
                got = parse_qty(stickered.get(i["po_item_id"], ""), "PC")
            except ValueError:
                continue
            if got > max(i["ordered_m"] - i["before_m"], 0):
                return True
        return False

    def save(self) -> bool:
        """True once it's saved. False if the person went back."""
        row = self._row()
        if not row:
            return False
        editable = {p["id"] for p in self.pos if self._po_editable(p)}
        boxes = {self.boxes.item(r, 0).data(Qt.ItemDataRole.UserRole): self.boxes.item(r, C_BOXES).text() for r in range(self.boxes.rowCount())}
        boxes = {k: v for k, v in boxes.items() if k in editable}
        stickered = {self.stickers.item(r, 0).data(Qt.ItemDataRole.UserRole): self.stickers.item(r, C_STICKERED).text() for r in range(self.stickers.rowCount())}
        if self._over_ordered(stickered, editable) and not confirm(self, "Save received", "You typed more stickered pieces than were ordered. Save anyway?", "Save anyway"):
            return False
        # What this screen showed for each branch, so figures someone else saved meanwhile aren't overwritten unseen.
        expect = {p["id"]: (p["current"]["mark"] if p["current"] else None) for p in self.pos if p["id"] in editable}
        # With a branch chosen, only that branch is received now; the others wait for their own turn.
        service.receive(self.conn, self.user, row["id"], boxes, stickered, self.remarks.text(), another=self.another,
                        only=[p["id"] for p in self.pos] if self.branch_code is not None else None, expect_delivery=expect)
        self.drafts.pop(row["id"], None)
        self.another = False
        self.refresh()
        self.changed.emit()
        return True

    def _save_then(self, row_id: int, po_ids: list[int], title: str, message: str) -> list[dict] | None:
        """Unsaved figures: one confirm saves them, then ships. The branches still ready to ship once saved, or None if nothing should ship.

        Saving refreshes the screen and can move the order to another tab, so the branches are looked up again for this order, not taken from the screen."""
        if not confirm(self, title, message, "Save and ship"):
            return None
        if not self.save():
            return None
        ready = [p for p in service._pos(self.conn, row_id) if p["id"] in po_ids and p["stage"] == service.TO_SHIP and not self._held(p)]
        if not ready:
            info(self, title, "Saved. Nothing left to ship for these branches.")
        return ready or None

    def _ship_now(self, row_id: int, ready: list[dict]) -> None:
        service.ship(self.conn, self.user, row_id, [p["id"] for p in ready])
        self.refresh()
        self.changed.emit()

    def ship_one(self, po: dict):
        row = self._row()
        if not row:
            return
        if row["id"] in self.drafts:
            ready = self._save_then(row["id"], [po["id"]], "Ship", f"You changed the figures and haven't saved them. Save them, then mark {po['branch_name']} ({po['po_number']}) shipped? "
                                    "The branch counts them when they arrive. After this the figures can't change.")
            if ready:
                self._ship_now(row["id"], ready)
            return
        if confirm(self, "Ship", f"Have the {po['boxes']} boxes for {po['branch_name']} ({po['po_number']}) left for the branch? The branch counts them when they arrive. After this the figures can't change.", "Shipped"):
            service.ship(self.conn, self.user, row["id"], [po["id"]])
            self.refresh()
            self.changed.emit()

    def ship(self):
        row = self._row()
        ready = self._to_ship()
        if not row or not ready:
            return
        names = ", ".join(p["branch_name"] for p in ready)
        if row["id"] in self.drafts:
            ready = self._save_then(row["id"], [p["id"] for p in ready], "Mark shipped", f"You changed the figures and haven't saved them. Save them, then mark {names} shipped? "
                                    "The branches count them when they arrive. After this the figures can't change.")
            if ready:
                self._ship_now(row["id"], ready)
            return
        if confirm(self, "Mark shipped", f"Have the {sum(p['boxes'] for p in ready)} boxes for {names} left for the branches? The branches count them when they arrive. After this the figures can't change.", "Shipped"):
            service.ship(self.conn, self.user, row["id"], [p["id"] for p in ready])
            self.refresh()
            self.changed.emit()
