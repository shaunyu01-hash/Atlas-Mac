"""Approvals, printing, reports, and master-data screens."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRegularExpression, QSize, Qt, Signal
from PySide6.QtGui import QKeySequence, QRegularExpressionValidator, QShortcut
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QFrame, QHBoxLayout, QLineEdit, QListWidget, QPlainTextEdit, QScrollArea,
                               QHeaderView, QSplitter, QTabWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import auth, delivery_receipts, masters, order_forms, orders, payments, pdf, receiving, reports
from . import theme
from ..db import POError, today
from ..money import OFFERED_UNITS, VAT_EXCLUSIVE, VAT_INCLUSIVE, VAT_NONE, fmt_peso, fmt_percent, fmt_plain, fmt_qty, parse_percent, parse_pesos, per_piece, pieces_per_unit
from .common import (month_picker, PIN_BOTTOM_ROLE, resort, show_if, smart_columns, ask_approve, ask_text, button, guarded, hide_unused, make_tree, ColumnChooser, WidthKeeper, confirm_reprint, combo_value, confirm, date_edit, date_value, fill_table, fmt_date, fmt_datetime, info,
                     label, left_aligned, make_table, open_email_draft, open_for_printing, panel, print_order_forms, register_tabs, save_file, search_combo, select_data, selected_key,
                     set_terms, set_widths, show_tab_counts, terms_combo, Pager, keep_place)


def page_layout(widget: QWidget, title: str) -> tuple[QVBoxLayout, QHBoxLayout]:
    root = QVBoxLayout(widget)
    root.setContentsMargins(20, 14, 20, 14)
    root.setSpacing(10)
    top = QHBoxLayout()
    top.addWidget(label(title, "Title"))
    top.addStretch(1)
    root.addLayout(top)
    return root, top


class StretchFloor(QObject):
    """One column takes the spare room, but never less than a minimum. On a window too small for that, it keeps the minimum and the list scrolls sideways,
    so the text that matters most is never cut to a few letters.

    `squeeze` names columns, with the least each may have, that give up room first, before the stretching column goes below `minimum`. The stretching
    column may then go down to `lowest` before the list scrolls, so the last column (a row's buttons) stays in view on a small window."""
    SLACK = 2  # a pixel or two short of the minimum is not worth a scroll bar

    def __init__(self, view, col: int, minimum: int, lowest: int | None = None, squeeze: dict[int, int] | None = None):
        super().__init__(view)
        self.view, self.col, self.minimum = view, col, minimum
        self.lowest = minimum if lowest is None else min(lowest, minimum)
        self.squeeze = dict(squeeze or {})
        self.natural: dict[int, int] = {}
        self.header = view.horizontalHeader() if hasattr(view, "horizontalHeader") else view.header()
        self._busy = False
        self._viewport = view.viewport()
        self._viewport.installEventFilter(self)
        self.header.sectionResized.connect(self._resized)

    def _resized(self, col, _old, new):
        if self._busy or col == self.col:
            return
        if col in self.squeeze:
            self.natural[col] = new
        self.fit()

    def eventFilter(self, obj, event):  # noqa: N802 (Qt's name)
        if event.type() == QEvent.Type.Resize and obj is self._viewport:
            self.fit()
        return False

    def fit(self):
        if self._busy:
            return
        self._busy = True
        try:
            h = self.header
            shown = [c for c in range(h.count()) if c != self.col and not h.isSectionHidden(c)]
            width = {c: self.natural.get(c, h.sectionSize(c)) if c in self.squeeze else h.sectionSize(c) for c in shown}
            need = self.minimum - self.SLACK - (self.view.viewport().width() - sum(width.values()))
            for c, least in self.squeeze.items():
                if c in width and need > 0:
                    give = max(0, min(need, width[c] - least))
                    width[c] -= give
                    need -= give
            for c in self.squeeze:
                if c in width and h.sectionSize(c) != width[c]:
                    h.resizeSection(c, width[c])
            spare = self.view.viewport().width() - sum(width.values())
            if spare >= self.lowest - self.SLACK:
                h.setSectionResizeMode(self.col, QHeaderView.ResizeMode.Stretch)
            else:
                h.setSectionResizeMode(self.col, QHeaderView.ResizeMode.Interactive)
                h.resizeSection(self.col, self.lowest)
        finally:
            self._busy = False


class ApprovalsPage(QWidget):
    """What waits for the approver, one row per OF or amendment. Each opens to its products, and has Approve and Reject on its own row, so the approver
    can work down the list without opening anything. Rejected keeps every rejection with its reason."""
    open_of = Signal(int)
    changed = Signal()
    WAITING, REJECTED = "WAITING", "REJECTED"
    TABS = {WAITING: "Waiting", REJECTED: "Rejected"}
    NOW = {"DRAFT": "Back with the purchaser", "PENDING_APPROVAL": "Submitted again", "APPROVED": "Approved later", "CANCELLED": "Cancelled"}

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.tab = self.WAITING
        self.can = user.can("of.approve")
        root, top = page_layout(self, "Approvals")
        self.policy_note = label("", "Muted")
        top.addWidget(self.policy_note)
        self.columns_btn = button("Columns", lambda: self.of_chooser.open_under(self.columns_btn))
        top.addWidget(self.columns_btn)
        self.tabs = register_tabs(self.TABS, self._set_tab)
        root.addWidget(self.tabs)
        # What was just approved, said here instead of in a box that needs an extra press.
        self.done_note = label("", "NoticeOk")
        self.done_note.setWordWrap(True)
        self.done_note.hide()
        root.addWidget(self.done_note)

        self.waiting = QWidget()
        col = QVBoxLayout(self.waiting)
        col.setContentsMargins(0, 0, 0, 0)
        col.addWidget(label("Order forms. Press the arrow to see the products, or Approve on the row.", "PanelTitle"))
        self.approval_branches = [(b["code"], b["name"]) for b in masters.list_branches(conn, active_only=True, branches_only=True)]
        headers = ["Draft #", "Date", "Supplier", "Stock code", "Product", "By", "Unit price", *[name for _, name in self.approval_branches], "Pieces", "Total", ""]
        self.of_numeric = list(range(6, len(headers) - 1))
        self.ofs = make_tree(headers, numeric=tuple(self.of_numeric), stretch=4)
        self.branch_cols = list(range(7, 7 + len(self.approval_branches)))
        # Each person chooses the columns, kept for their login. The number stays, and the buttons' column isn't a choice.
        self.of_chooser = ColumnChooser(self.ofs, f"approvals-ofs-{user.username}", {h.lower().replace(" ", "-"): c for c, h in enumerate(headers) if 0 < c < len(headers) - 1})
        self.of_chooser.columns_changed.connect(self._fit)
        self.ofs.itemDoubleClicked.connect(lambda *_: self._open())
        self.ofs.currentItemChanged.connect(lambda *_: self._sync_buttons())
        # The product keeps room to be read on a small window. Supplier and Stock code give up room first, then the product goes down to 100,
        # so the row's Approve stays in view. Below that the list scrolls sideways. A cut name reads in full on its tooltip.
        self.product_floor = StretchFloor(self.ofs, 4, 160, lowest=100, squeeze={3: 64, 2: 64})
        for c in (2, 3, 4):
            self.ofs.headerItem().setToolTip(c, headers[c])
        # Ctrl+O opens the selected OF in full.
        self.open_shortcut = QShortcut(QKeySequence("Ctrl+O"), self, activated=lambda: guarded(self, self._open))
        col.addWidget(self.ofs, 3)
        bar = QHBoxLayout()
        self.open_btn = button("Open the whole OF", self._open)
        bar.addWidget(self.open_btn)
        bar.addStretch(1)
        self.reject_btn = button("Reject", self._reject)
        self.approve_btn = button("Approve with remarks", self._approve, "Primary")
        bar.addWidget(self.reject_btn)
        bar.addWidget(self.approve_btn)
        col.addLayout(bar)
        col.addWidget(label("Amendments: changes to an order already approved. Press the arrow to see what changes.", "PanelTitle"))
        self.amend = make_tree(["Document", "Supplier", "By", "When", "Reason", "New total", ""], numeric=(5,), stretch=4)
        # Their rows carry Approve and Reject buttons, which a re-sort would drop, so these keep the order they waited in.
        for tree in (self.ofs, self.amend):
            tree.setProperty("no_sort", True)
        col.addWidget(self.amend, 2)
        # A width the person drags on Amendments stays, across a refresh and next time, as on the OF list above.
        self.amend_widths = WidthKeeper(self.amend, f"approvals-amend-{user.username}")
        root.addWidget(self.waiting, 1)

        self.rejected = make_table(["Rejected", "What", "Number", "Supplier", "Prepared by", "Total", "Rejected by", "Reason", "Now"], numeric=(5,), stretch=7)
        set_widths(self.rejected, {0: 125, 1: 100, 2: 110, 3: 80, 4: 100, 5: 90, 6: 120, 8: 160})
        # The reason is what this tab is for, so it keeps room to be read on a small window. On a small window the other columns
        # first give up the room they don't need for what they hold, then the reason goes down to 160 before the list scrolls sideways.
        # A cut reason reads in full on its tooltip.
        self.reason_floor = StretchFloor(self.rejected, 7, 200, lowest=160, squeeze={c: self.rejected.columnWidth(c) for c in (8, 6, 4, 2, 0, 5, 3)})
        self.reason_floor.natural = {c: self.rejected.columnWidth(c) for c in self.reason_floor.squeeze}
        # What it was (OF, OF amendment, PO amendment) always reads in full.
        fm = self.rejected.fontMetrics()
        self.rejected.setColumnWidth(1, max(100, max(fm.horizontalAdvance(t) for t in ("OF amendment", "PO amendment")) + 24))
        self.rejected.doubleClicked.connect(lambda *_: self._open_rejected())
        self.rejected_widths = WidthKeeper(self.rejected, f"approvals-rejected-{user.username}")
        # Rejected is history, so it shows a page at a time, newest first. Waiting always shows everything.
        self.rejected_box = QWidget()
        box = QVBoxLayout(self.rejected_box)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.rejected, 1)
        self.rejected_pager = Pager(self._show_more_rejected)
        box.addWidget(self.rejected_pager)
        root.addWidget(self.rejected_box, 1)
        self.empty = label("", "Empty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.empty)
        self.refresh()

    def _set_tab(self, tab):
        self.tab = tab
        self.done_note.hide()
        self.rejected_pager.reset()
        self.refresh()

    def _show_more_rejected(self):
        with keep_place(self.rejected):
            self.refresh()

    def _done(self, text: str):
        self.done_note.setText(text)
        self.done_note.show()

    def _may_decide_of(self, of_id: int, prepared_by: int) -> bool:
        from .of_page import may_approve
        return self.can and may_approve(self.conn, self.user, of_id, prepared_by)

    def _may_decide_amendment(self, kind: str, a: dict) -> bool:
        """The same checks the decision makes: the right permission, on the approver list, and not the person who asked for it unless self-approval is on."""
        policy = orders.policy(self.conn)
        if not self.user.can("of.approve" if kind == "OF" else "po.approve") or self.user.id not in policy["approver_user_ids"]:
            return False
        return policy["allow_self_approval"] or a["requested_by"] != self.user.id

    def count(self) -> int:
        return len(order_forms.pending(self.conn)) + len(order_forms.pending_amendments(self.conn)) + len(orders.pending_amendments(self.conn))

    def _row_buttons(self, tree, item, approve, reject, allowed: bool = True):
        """Approve and Reject on the row itself, so nothing has to be selected first. Only for someone who may decide this one."""
        if not allowed:
            return
        for c in range(tree.columnCount()):
            item.setSizeHint(c, QSize(0, 38))
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(2, 1, 2, 1)
        row.setSpacing(4)
        row.addWidget(button("Reject", reject))
        row.addWidget(button("Approve", approve, "Primary"))
        tree.setItemWidget(item, tree.columnCount() - 1, host)

    def refresh(self):
        import json
        pending = order_forms.pending(self.conn)
        amendments = [("OF", a, a["of_number"]) for a in order_forms.pending_amendments(self.conn)] + [("PO", a, a["po_number"]) for a in orders.pending_amendments(self.conn)]
        rejected = order_forms.rejections(self.conn)
        show_tab_counts(self.tabs, self.TABS, {self.WAITING: len(pending) + len(amendments), self.REJECTED: len(rejected)})

        chips = auth.chips(self.conn)
        initials = lambda user_id: chips[user_id].initials if user_id in chips else ""
        forms = [order_forms.get_of(self.conn, r["id"]) for r in pending]
        # A column for each branch, so the pieces line up under each one. A branch nothing goes to takes no room.
        codes = [c for c, _ in self.approval_branches]
        self.ofs.clear()
        self.decidable = {r["id"] for r in pending if self._may_decide_of(r["id"], r["prepared_by"])}
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for r, o in zip(pending, forms):
            skus = [i["item_code"] for i in o["items"] if i["item_code"]]
            per_branch = {c: sum(i["allocations_m"].get(c, 0) * i["pieces_per_unit"] for i in o["items"]) for c in codes}
            # The date without its year: these are all recent, and the room goes to the product.
            top = QTreeWidgetItem([str(r["id"]), fmt_date(r["of_date"]).rsplit(",", 1)[0], r["supplier"], ", ".join(skus[:3]) + (f" +{len(skus) - 3}" if len(skus) > 3 else ""),
                                   o["items"][0]["description"] if len(o["items"]) == 1 else f"{len(o['items'])} products", initials(r["prepared_by"]),
                                   fmt_plain(per_piece(o["items"][0]["net_unit_c"], o["items"][0]["pieces_per_unit"])) if len(o["items"]) == 1 else "",
                                   *[fmt_qty(per_branch[c]) if per_branch[c] else "" for c in codes], fmt_qty(r["total_pieces_m"]), fmt_plain(r["grand_total_c"]), ""])
            top.setData(0, Qt.ItemDataRole.UserRole, r["id"])
            top.setToolTip(5, r["prepared_name"])
            for c in (2, 3, 4):
                top.setToolTip(c, ", ".join(skus) if c == 3 else top.text(c))
            for c in self.of_numeric:
                top.setTextAlignment(c, right)
            # Each product on its own line: its stock code, what it is, and its pieces under each branch.
            for i in o["items"]:
                product = i["description"] + "".join(f", {x}" for x in (i["size"], i["color"]) if x)
                # The unit price is per piece, after the line's discount, so it reads against the pieces.
                child = QTreeWidgetItem(["", "", "", i["item_code"], product, "", fmt_plain(per_piece(i["net_unit_c"], i["pieces_per_unit"])),
                                         *[fmt_qty(i["allocations_m"][c] * i["pieces_per_unit"]) if i["allocations_m"].get(c) else "" for c in codes],
                                         fmt_qty(i["total_qty_m"] * i["pieces_per_unit"]), fmt_plain(i["line_total_c"]), ""])
                child.setData(0, Qt.ItemDataRole.UserRole, r["id"])
                child.setToolTip(4, product)
                for c in self.of_numeric:
                    child.setTextAlignment(c, right)
                top.addChild(child)
            self.ofs.addTopLevelItem(top)
            self._row_buttons(self.ofs, top, lambda _=False, of_id=r["id"]: guarded(self, self._quick_approve, of_id), lambda _=False, of_id=r["id"]: guarded(self, self._reject, of_id),
                              r["id"] in self.decidable)

        self.amend.clear()
        for kind, a, number in amendments:
            after = json.loads(a["after_json"])
            top = QTreeWidgetItem([number, a["supplier"], initials(a["requested_by"]), fmt_datetime(a["requested_at"]), a["reason"], fmt_plain(after["grand_total_c"]), ""])
            top.setToolTip(2, a["requested_name"])
            top.setData(0, Qt.ItemDataRole.UserRole, (kind, a["id"]))
            top.setTextAlignment(5, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            for text in _amendment_changes(json.loads(a["before_json"]), after):
                top.addChild(QTreeWidgetItem(["", "", "", "", text]))
            self.amend.addTopLevelItem(top)
            self._row_buttons(self.amend, top, lambda _=False, key=(kind, a["id"]): guarded(self, self._decide, True, key), lambda _=False, key=(kind, a["id"]): guarded(self, self._decide, False, key),
                              self._may_decide_amendment(kind, a))
        hide_unused(self.ofs, self.of_chooser, self.branch_cols)
        self._fit()

        # Newest first, by when it was rejected, then number. Only the rows taken are put in the table.
        rejected = sorted(rejected, key=lambda r: (r["decided_at"] or "", r["number"] or "", r["id"]), reverse=True)
        self.rejected_rows = rejected = self.rejected_pager.take(rejected)
        fill_table(self.rejected, [[fmt_datetime(r["decided_at"]), r["kind"], r["number"] or f"Draft OF #{r['id']}", r["supplier"], r["prepared_name"], fmt_plain(r["grand_total_c"]),
                                    r["rejected_by"] or "", r["reason"], self.NOW.get(r["status"], orders.STATUS_LABELS.get(r["status"], r["status"])) if r["kind"] == "OF" else ""] for r in rejected],
                   keys=list(range(len(rejected))))
        self._fit_rejected()

        policy = orders.policy(self.conn)
        self.policy_note.setText(f"OFs above {fmt_peso(policy['threshold_c'])} need approval from {orders.approver_names(self.conn)}" if policy["enabled"] else "The approval policy is off.")
        waiting_tab = self.tab == self.WAITING
        nothing = not pending and not amendments if waiting_tab else not rejected
        self.waiting.setVisible(waiting_tab and not nothing)
        self.rejected_box.setVisible(not waiting_tab and not nothing)
        self.empty.setText("Nothing waiting for approval." if waiting_tab else "Nothing has been rejected.")
        self.empty.setVisible(nothing)
        if self.ofs.topLevelItemCount() and self.ofs.currentItem() is None:
            self.ofs.setCurrentItem(self.ofs.topLevelItem(0))
        show_if(self.open_btn, bool(pending))
        self._sync_buttons()

    def _sync_buttons(self):
        """Approve with remarks and Reject below the list follow the selected OF: shown only when this person may decide it."""
        allowed = self._selected_of() in getattr(self, "decidable", set())
        for b in (self.reject_btn, self.approve_btn):
            show_if(b, allowed)

    def _fit(self):
        """Every column as wide as what it holds, the buttons their own room, and the rest to Branches or Reason. Only the top rows set the widths."""
        for tree, stretch in ((self.ofs, 4), (self.amend, 4)):
            last = tree.columnCount() - 1
            # The buttons' column is as wide as the row buttons need, a little room spare.
            hosts = [tree.itemWidget(tree.topLevelItem(i), last) for i in range(tree.topLevelItemCount())]
            tree.setColumnWidth(last, max([h.sizeHint().width() + 8 for h in hosts if h is not None], default=170))
            fm = tree.fontMetrics()
            for c in range(last):
                if c == stretch or tree.isColumnHidden(c):
                    continue
                rows = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
                texts = [tree.headerItem().text(c)] + [x.text(c) for top in rows for x in (top, *(top.child(k) for k in range(top.childCount())))]
                # The first column also holds the arrow that opens the row.
                width = max(fm.horizontalAdvance(t) for t in texts) + 18 + (tree.indentation() if c == 0 else 0)
                # A width the person dragged themselves stays across a refresh, as on the other pages.
                width = getattr(tree, "kept_widths", {}).get(c, width)
                if tree is self.ofs and c in self.product_floor.squeeze:
                    self.product_floor.natural[c] = width
                tree.setColumnWidth(c, width)
        self.product_floor.fit()

    def _fit_rejected(self):
        """Each column of Rejected can give up room down to what it holds, never cutting its text, before the reason goes below its floor."""
        t, floor = self.rejected, self.reason_floor
        fm = t.fontMetrics()
        for r in range(t.rowCount()):
            if t.item(r, 7):
                t.item(r, 7).setToolTip(t.item(r, 7).text())
        for c in floor.squeeze:
            texts = [t.horizontalHeaderItem(c).text()] + [t.item(r, c).text() for r in range(t.rowCount()) if t.item(r, c)]
            floor.squeeze[c] = min(floor.natural.get(c, t.columnWidth(c)), max(fm.horizontalAdvance(x) for x in texts) + 24)
        floor.fit()

    def showEvent(self, event):
        super().showEvent(event)
        # Coming back to the page, the last approval's note has done its job.
        self.done_note.hide()
        self._fit()

    def _selected_of(self) -> int | None:
        item = self.ofs.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def _what(self, of_id: int) -> str:
        """The OF in words: draft number, supplier and total, so nobody approves the wrong one."""
        r = next(p for p in order_forms.pending(self.conn) if p["id"] == of_id)
        what = f"draft OF #{of_id}, {r['supplier']}, {fmt_peso(r['grand_total_c'])}"
        return what

    def _open(self):
        key = self._selected_of()
        if key:
            self.open_of.emit(key)

    def _open_rejected(self):
        key = selected_key(self.rejected)
        if key is not None and self.rejected_rows[key]["kind"] != "PO amendment":
            self.open_of.emit(self.rejected_rows[key]["id"])

    def _quick_approve(self, of_id: int):
        """The row's own Approve: the same question as everywhere else, with remarks that may stay empty."""
        self._approve(of_id)

    def _approve(self, of_id: int | None = None):
        key = of_id or self._selected_of()
        if not key:
            raise POError("Select an OF first.")
        # The one approve question, shared with the OF screen: which OF, from whom, for how much, and optional remarks.
        remarks = ask_approve(self, self._what(key))
        if remarks is None:
            return
        number = order_forms.approve(self.conn, self.user, key, remarks)
        self.refresh()
        self.changed.emit()
        self._done(f"{number} is approved. It is now on Print.")

    def _reject(self, of_id: int | None = None):
        key = of_id or self._selected_of()
        if not key:
            raise POError("Select an OF first.")
        reason = ask_text(self, "Reject", f"Why is {self._what(key)} being rejected? The purchaser will see this.")
        if reason is None:
            return
        order_forms.reject(self.conn, self.user, key, reason)
        self.refresh()
        self.changed.emit()

    def _decide(self, approved: bool, key=None):
        if key is None:
            item = self.amend.currentItem()
            key = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not key:
            raise POError("Select an amendment first.")
        reason = ask_text(self, "Amendment", "Remarks are optional." if approved else "Why is this amendment being rejected?")
        if reason is None:
            return
        service = order_forms if key[0] == "OF" else orders
        service.decide_amendment(self.conn, self.user, key[1], approved, reason)
        self.refresh()
        self.changed.emit()


def _amendment_changes(before: dict, after: dict) -> list[str]:
    """What an amendment changes, one line each, in pieces: quantities, costs, products added or removed, and terms."""
    def pieces(item) -> int:
        ppu = item.get("pieces_per_unit") or 1
        if "allocations_m" in item:
            return sum(int(v) for v in item["allocations_m"].values()) * ppu
        return (item.get("qty_m") or 0) * ppu

    key = lambda i: (i.get("item_code") or "", i.get("description") or "", i.get("size") or "", i.get("color") or "")
    old = {key(i): i for i in before.get("items", [])}
    new = {key(i): i for i in after.get("items", [])}
    out = []
    for k, item in new.items():
        name = " ".join(x for x in k[1:] if x) or k[0]
        prior = old.get(k)
        if prior is None:
            out.append(f"Added: {name}, {fmt_qty(pieces(item))} pieces")
            continue
        if pieces(prior) != pieces(item):
            out.append(f"{name}: {fmt_qty(pieces(prior))} pieces, now {fmt_qty(pieces(item))}")
        if (prior.get("unit_cost_c") or 0) != (item.get("unit_cost_c") or 0):
            out.append(f"{name}: cost {fmt_plain(prior.get('unit_cost_c') or 0)}, now {fmt_plain(item.get('unit_cost_c') or 0)}")
    for k in old.keys() - new.keys():
        out.append(f"Removed: {' '.join(x for x in k[1:] if x) or k[0]}")
    for field, name in (("payment_terms", "Terms"), ("expected_delivery_date", "Expected delivery")):
        a, b = before.get("header", {}).get(field), after.get("header", {}).get(field)
        if (a or None) != (b or None):
            out.append(f"{name}: {a or 'none'}, now {b or 'none'}")
    if before.get("grand_total_c") is not None and before.get("grand_total_c") != after.get("grand_total_c"):
        out.append(f"Total {fmt_plain(before['grand_total_c'])}, now {fmt_plain(after['grand_total_c'])}")
    return out or ["No change to quantities or costs"]


SUPPLIER_MIN = 160  # the Print list's Supplier column, while the panel is open


def _times(prints: int) -> str:
    return f" ({prints}x)" if prints > 1 else ""


class PrintPage(QWidget):
    """Tick the OFs, press once: each OF's warehouse copy, slips and branch POs open together as one file to print, oldest OF first.
    DRs for suppliers who print their own wait on another tab, until someone emails them. The Final OF prints from Payments only, so there's one place for it."""
    open_of = Signal(int)
    changed = Signal()
    TO_PRINT, PRINTED, TO_EMAIL, RECEIPTS = "TO_PRINT", "PRINTED", "TO_EMAIL", "RECEIPTS"
    TABS = {TO_PRINT: "To print", PRINTED: "Printed", TO_EMAIL: "DRs to email", RECEIPTS: "All DRs"}

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.tab = self.TO_PRINT
        self.ticked: set[int] = set()
        self.rows = []
        self.groups, self.receipts = [], []
        root, top = page_layout(self, "Print")
        self.selected_note = label("", "Muted")
        top.addWidget(self.selected_note)
        top.addSpacing(12)
        # A new DR for a PO, for a supplier's next delivery. Whether every second delivery needs one is still to be decided.
        self.another_btn = button("Another DR", self.another_delivery)
        top.addWidget(self.another_btn)
        self.sent_btn = button("Mark as sent", self.mark_sent)
        top.addWidget(self.sent_btn)
        self.print_btn = button("", self.print_selected, "Primary")
        top.addWidget(self.print_btn)
        # The row keeps its height when no button shows, so the search box and tabs don't jump up on an empty tab.
        top.addStrut(self.print_btn.sizeHint().height())
        self.tabs = register_tabs(self.TABS, self._set_tab)
        root.addWidget(self.tabs)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search OF, PO or DR number, supplier or code")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(240)
        bar.addWidget(self.search, 2)
        self.tick_tools = [button("Select all", self.select_all), button("Clear", self.clear_ticks)]
        for b in self.tick_tools:
            bar.addWidget(b)
        self.paper_forms = QCheckBox("Add paper count forms")
        self.paper_forms.setToolTip("Receivers count in the program. Tick this if a branch might lose internet: it adds two blank forms per branch, which have to reach the branch before the internet goes down.")
        bar.addWidget(self.paper_forms)
        self.email_note = label("", "Muted")
        bar.addWidget(self.email_note)
        bar.addStretch(1)
        root.addLayout(bar)

        self.table = make_table(["", "OF number", "Date", "Supplier", "Status", "Branches", "POs", "Printed", "Pieces", "Total"], numeric=(5, 6, 8, 9), stretch=3)
        set_widths(self.table, {0: 36, 1: 130, 2: 96, 4: 150, 5: 70, 6: 50, 7: 130, 8: 70, 9: 112})
        self.table.itemChanged.connect(self._ticked)
        self.table.cellClicked.connect(self._row_clicked)
        self.table.cellDoubleClicked.connect(self._open)
        # Double-click shows the order on the right without leaving Print. Its button opens the whole OF.
        from .of_page import DocumentPanel
        self.panel = DocumentPanel(conn)
        self.panel.hide()
        self.panel.open_doc.connect(lambda _kind, of_id: self.open_of.emit(of_id))
        self.panel.closed.connect(self._close_panel)
        # Ctrl+O opens the selected OF in full.
        self.open_shortcut = QShortcut(QKeySequence("Ctrl+O"), self, activated=self._open_current)
        self.close_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=lambda: self.panel.isVisibleTo(self) and self._close_panel())
        self.print_split = QSplitter(Qt.Orientation.Horizontal)
        self.print_split.addWidget(self.table)
        self.print_split.addWidget(self.panel)
        self.print_split.setStretchFactor(0, 3)
        self.print_split.setStretchFactor(1, 2)
        self.print_split.setChildrenCollapsible(False)
        root.addWidget(self.print_split, 1)
        self.email_table = make_table(["Order", "Supplier", "Email", "DRs", "DR numbers"], numeric=(3,), stretch=4)
        set_widths(self.email_table, {0: 130, 1: 220, 2: 240, 3: 50})
        self.receipt_table = make_table(["DR number", "PO number", "OF number", "Supplier", "Branch", "How it goes", "Prints"], numeric=(6,), stretch=3)
        set_widths(self.receipt_table, {0: 160, 1: 120, 2: 130, 4: 110, 5: 260, 6: 60})
        self.choosers = {t: smart_columns(t, f"print-{name}-{user.username}") for t, name in ((self.table, "orders"), (self.email_table, "email"), (self.receipt_table, "drs"))}
        self.columns_btn = button("Columns", self._choose_columns)
        bar.addWidget(self.columns_btn)
        for table in (self.email_table, self.receipt_table):
            table.itemSelectionChanged.connect(self._show_selection)
            root.addWidget(table, 1)
        self.empty = label("", "Empty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.hide()
        # The empty message takes the room the list would, so the search box and tabs stay where they were.
        root.addWidget(self.empty, 1)

        self.search.textChanged.connect(self.refresh)
        self.refresh()

    def _tab_table(self):
        return {self.TO_EMAIL: self.email_table, self.RECEIPTS: self.receipt_table}.get(self.tab, self.table)

    def _choose_columns(self):
        """The columns of the list on this tab, even while it has nothing in it."""
        self.choosers[self._tab_table()].open_under(self.columns_btn)

    def count_to_print(self) -> int:
        return len(order_forms.search(self.conn, status=orders.APPROVED, tab=orders.OPEN))

    def _set_tab(self, tab):
        self.tab = tab
        self.ticked.clear()
        self.refresh()

    def refresh(self, *_):
        text = self.search.text()
        ready = order_forms.search(self.conn, text=text, status=orders.APPROVED, tab=orders.OPEN)
        printed = order_forms.search(self.conn, text=text, tab=orders.COMPLETED)
        again = pdf.reprint_counts(self.conn)
        self.groups, self.receipts = delivery_receipts.waiting_to_email(self.conn, text), delivery_receipts.search(self.conn, text)
        # The tabs always count everything, so a search that finds nothing doesn't read as no work waiting.
        if text.strip():
            totals = {self.TO_PRINT: len(order_forms.search(self.conn, status=orders.APPROVED, tab=orders.OPEN)), self.PRINTED: len(order_forms.search(self.conn, tab=orders.COMPLETED)),
                      self.TO_EMAIL: len(delivery_receipts.waiting_to_email(self.conn, "")), self.RECEIPTS: len(delivery_receipts.search(self.conn, ""))}
        else:
            totals = {self.TO_PRINT: len(ready), self.PRINTED: len(printed), self.TO_EMAIL: len(self.groups), self.RECEIPTS: len(self.receipts)}
        show_tab_counts(self.tabs, self.TABS, totals)
        self.email_note.setText(f"{len(self.groups)} order{'s' if len(self.groups) != 1 else ''} with DRs to email" if self.groups and self.tab == self.TO_PRINT else "")
        self._fill_receipts()
        self.paper_forms.setVisible(self.tab in (self.TO_PRINT, self.PRINTED))
        self.rows = sorted({self.TO_PRINT: ready, self.PRINTED: printed}.get(self.tab, []), key=lambda r: r["id"], reverse=self.tab != self.TO_PRINT)
        self.ticked &= {r["id"] for r in self.rows}
        for b in self.tick_tools:
            b.setVisible(bool(self.rows) and self._may_print())
        self.table.blockSignals(True)
        fill_table(self.table, [["", r["of_number"], fmt_date(r["of_date"]), r["supplier"], r["status"], r["branch_count"], r["po_count"], (fmt_date(r["printed_at"]) or r["printed_part"]) + _times(again.get(("OF", r["id"]), 0) + 1),
                                 fmt_qty(r["total_pieces_m"]), fmt_plain(r["grand_total_c"])] for r in self.rows],
                   keys=[r["id"] for r in self.rows], status_col=4)
        for row, r in enumerate(self.rows):
            item = self.table.item(row, 0)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
            item.setCheckState(Qt.CheckState.Checked if r["id"] in self.ticked else Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        has_rows = {self.TO_EMAIL: bool(self.groups), self.RECEIPTS: bool(self.receipts)}.get(self.tab, bool(self.rows))
        self.table.setVisible(bool(self.rows))
        self.print_split.setVisible(bool(self.rows))
        if self.panel.isVisibleTo(self) and self.panel.key and self.panel.key[1] not in {r["id"] for r in self.rows}:
            self._close_panel()
        self.email_table.setVisible(self.tab == self.TO_EMAIL and has_rows)
        self.receipt_table.setVisible(self.tab == self.RECEIPTS and has_rows)
        if text.strip() and totals[self.tab]:
            self.empty.setText(f"No match for '{text.strip()}'. Clear the search to see all {totals[self.tab]}.")
        else:
            self.empty.setText({self.TO_PRINT: "Nothing waiting to print. An Order Form appears here once it's approved.",
                                self.PRINTED: "No printed Order Forms match. They can be reprinted from here.",
                                self.TO_EMAIL: "No DRs to email. A DR appears here when a PO is issued for a supplier who prints their own.",
                                self.RECEIPTS: "No DRs match. Every PO gets one when it's issued."}[self.tab])
        self.empty.setVisible(not has_rows)
        self._show_selection()

    def _fill_receipts(self):
        for table in (self.email_table, self.receipt_table):
            table.blockSignals(True)
        fill_table(self.email_table, [[g["reference"], g["supplier"], g["email"] or "No email on file", len(g["receipts"]), ", ".join(d["dr_number"] for d in g["receipts"])] for g in self.groups],
                   keys=[g["key"] for g in self.groups])
        fill_table(self.receipt_table, [[d["dr_number"], d["po_number"], d["of_number"] or "", d["supplier"], d["branch"], self._how_it_goes(d), d["prints"] or ""] for d in self.receipts],
                   keys=[d["id"] for d in self.receipts])
        for table, count in ((self.email_table, len(self.groups)), (self.receipt_table, len(self.receipts))):
            table.blockSignals(False)
            # Always something selected, ready for the button.
            if count:
                table.selectRow(0)

    def _how_it_goes(self, d: dict) -> str:
        if d["channel"] == delivery_receipts.OFFICE:
            return delivery_receipts.CHANNELS[delivery_receipts.OFFICE]
        if delivery_receipts.needs_email(d):
            return "Amended, to email again" if d["emailed_at"] else "Supplier prints, to email"
        return f"Emailed {fmt_date(d['emailed_at'])} by {d['emailed_name']}" if d["emailed_at"] else delivery_receipts.CHANNELS[delivery_receipts.EMAIL]

    def _selected_group(self) -> dict | None:
        key = selected_key(self.email_table)
        return next((g for g in self.groups if g["key"] == tuple(key)), None) if key else None

    def _selected_receipt(self) -> dict | None:
        key = selected_key(self.receipt_table)
        return next((d for d in self.receipts if d["id"] == key), None) if key else None

    def _show_selection(self):
        n = len(self.ticked)
        may_issue = self.user.can("po.issue")
        self.sent_btn.setVisible(False)
        self.another_btn.setVisible(False)
        self.print_btn.setText({self.TO_PRINT: "Print and issue", self.PRINTED: "Print again", self.TO_EMAIL: "Save DRs and start email", self.RECEIPTS: "Print DR"}[self.tab])
        if self.tab == self.TO_EMAIL:
            group = self._selected_group()
            self.selected_note.setText("Attach the saved file, send the email, then press Mark as sent" if group else "")
            show_if(self.print_btn, group is not None and may_issue)
            show_if(self.sent_btn, group is not None and may_issue)
            return
        if self.tab == self.RECEIPTS:
            receipt = self._selected_receipt()
            self.selected_note.setText(f"{receipt['dr_number']}, {receipt['supplier']}" if receipt else "")
            # The first print of a DR is the original, so printing it takes the same right as issuing. Anyone may print it again.
            show_if(self.print_btn, receipt is not None and (may_issue or (receipt["prints"] or 0) > 0))
            show_if(self.another_btn, receipt is not None and may_issue)
            return
        self.selected_note.setText((f"{n} selected" if n else "Tick the Order Forms to print") if self.rows and self._may_print() else "")
        self.print_btn.setVisible(bool(self.rows) and self._may_print())
        self.print_btn.setEnabled(n > 0 and self._may_print())

    def _may_print(self) -> bool:
        """Issuing needs Issue permission. Reprints are open to anyone."""
        if self.tab == self.TO_PRINT:
            return self.user.can("po.issue")
        return True

    def start_email(self):
        group = self._selected_group()
        if not group:
            raise POError("Select an order first.")
        if not group["email"]:
            raise POError(f"{group['supplier']} has no email address. Add it under Suppliers, or print the DR here from All DRs.")
        path = pdf.receipts_for_email(self.conn, self.user, group)
        subject, body = delivery_receipts.email_text(group)
        open_email_draft(path, group["email"], subject, body)

    def mark_sent(self):
        group = self._selected_group()
        if not group:
            raise POError("Select an order first.")
        if confirm(self, "Mark as sent", f"Has the email with {group['reference']}'s DRs gone to {group['supplier']}? It's recorded with your name, and leaves this list.", "Mark as sent"):
            delivery_receipts.mark_emailed(self.conn, self.user, [d["id"] for d in group["receipts"]])
            self.refresh()
            self.changed.emit()

    def print_receipt(self):
        receipt = self._selected_receipt()
        if not receipt:
            raise POError("Select a DR first.")
        if not receipt["prints"] and not self.user.can("po.issue"):
            raise POError("The first print of a DR needs the right to issue POs. Anyone may print it again after that.")
        if receipt["prints"] and not confirm_reprint(self, receipt["dr_number"]):
            return
        open_for_printing([pdf.delivery_receipt(self.conn, self.user, receipt["id"])], receipt["dr_number"])
        self.refresh()
        self.changed.emit()

    def another_delivery(self):
        receipt = self._selected_receipt()
        if not receipt:
            raise POError("Select a DR first.")
        if not confirm(self, "Another DR", f"Make a new DR for another delivery on {receipt['po_number']}? Its number is never reused.", "Make DR"):
            return
        made = delivery_receipts.another_delivery(self.conn, self.user, receipt["po_id"])
        if made["channel"] == delivery_receipts.OFFICE:
            open_for_printing([pdf.delivery_receipt(self.conn, self.user, made["id"])], made["dr_number"])
        self.refresh()
        self.changed.emit()

    def _ticked(self, item):
        if item.column() != 0:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self.ticked.add(key)
        else:
            self.ticked.discard(key)
        self._show_selection()

    def _row_clicked(self, row: int, column: int):
        """Clicking anywhere on the row ticks it, so a long list doesn't need aiming at small boxes."""
        if column:
            item = self.table.item(row, 0)
            item.setCheckState(Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked else Qt.CheckState.Checked)

    def _open(self, row: int, _column: int):
        of_id = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        self.panel.show_doc(("OF", of_id))
        if not self.panel.isVisibleTo(self):
            # Branches, POs and Total are in the panel, so the table makes room for the supplier while it's open.
            # Printed is always blank on To print, so it gives way too.
            self._panel_hides = tuple(c for c in ((5, 6, 7, 9) if self.tab == self.TO_PRINT else (5, 6, 9)) if not self.table.isColumnHidden(c))
            for c in self._panel_hides:
                self.table.setColumnHidden(c, True)
            # Only the supplier takes the spare room, not Pieces as the last column showing.
            self.table.horizontalHeader().setStretchLastSection(False)
            self.panel.show()
            total = sum(self.print_split.sizes()) or self.width()
            # The table keeps room for the supplier's name, as far as the panel can spare it.
            t = self.table
            others = sum(t.columnWidth(c) for c in range(t.columnCount()) if c != 3 and not t.isColumnHidden(c))
            need = others + SUPPLIER_MIN + 2 * t.frameWidth() + t.verticalScrollBar().sizeHint().width()
            # The splitter still keeps the panel at least as wide as it needs.
            left = min(max(int(total * 0.55), need), total)
            self.print_split.setSizes([left, total - left])

    def _close_panel(self):
        self.panel.hide()
        for c in getattr(self, "_panel_hides", ()):
            self.table.setColumnHidden(c, False)
        self._panel_hides = ()
        self.table.horizontalHeader().setStretchLastSection(True)

    def _open_current(self):
        row = self.table.currentRow()
        if self.rows and row >= 0 and self.table.isVisibleTo(self):
            self.open_of.emit(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole))

    def tick_of(self, of_id: int):
        """Orders' Go to Print: this OF ticked and selected on To print, so it needs no finding and ticking again. Nothing else stays ticked, so only what was chosen prints."""
        if self.tab != self.TO_PRINT:
            self.tabs.setCurrentIndex(list(self.TABS).index(self.TO_PRINT))
        if self.search.text() and of_id not in {r["id"] for r in self.rows}:
            self.search.clear()
        self.ticked = set()
        self.refresh()
        ids = [r["id"] for r in self.rows]
        if of_id not in ids:
            return
        self.ticked = {of_id}
        self.refresh()
        row = ids.index(of_id)
        self.table.selectRow(row)
        self.table.scrollToItem(self.table.item(row, 0))

    def select_all(self):
        self.ticked = {r["id"] for r in self.rows}
        self.refresh()

    def clear_ticks(self):
        self.ticked.clear()
        self.refresh()

    def print_selected(self):
        if self.tab == self.TO_EMAIL:
            return self.start_email()
        if self.tab == self.RECEIPTS:
            return self.print_receipt()
        chosen = [r["id"] for r in self.rows if r["id"] in self.ticked]
        if not chosen:
            raise POError("Tick at least one Order Form.")
        if self.tab == self.TO_PRINT:
            if not print_order_forms(self, self.conn, self.user, chosen, self.paper_forms.isChecked()):
                return
        else:
            if not confirm_reprint(self, "What you ticked"):
                return
            open_for_printing([pdf.reprint_order_form(self.conn, self.user, of_id, self.paper_forms.isChecked()) for of_id in chosen], "OF and PO reprint")
        self.paper_forms.setChecked(False)
        self.ticked.clear()
        self.refresh()
        self.changed.emit()


class SuppliersPage(QWidget):
    open_po = Signal(int)
    changed = Signal()

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.current_id = None
        root, top = page_layout(self, "Suppliers")
        # A supplier's terms, discounts, stickers and VAT decide every payment, so only Admin adds or changes one. Everyone else looks them up.
        self.editable = user.can("supplier.edit")
        self.new_btn = button("New supplier", self._new_pressed)
        top.addWidget(self.new_btn)
        show_if(self.new_btn, self.editable)
        self.fold_btn = button("Hide details", self._toggle_details, lock=False)
        top.addWidget(self.fold_btn)
        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        col = QVBoxLayout(left)
        col.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search code, name or TIN")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter_changed)
        col.addWidget(self.search)
        self.table = make_table(["Code", "Name", "Terms", "Status"], stretch=1)
        set_widths(self.table, {0: 70, 2: 110, 3: 70})
        chooser = smart_columns(self.table, f"suppliers-{user.username}")
        columns_btn = button("Columns", lambda: chooser.open_under(columns_btn))
        top.addWidget(columns_btn)
        self.table.itemSelectionChanged.connect(self._selected)
        col.addWidget(self.table, 1)
        # What to do next when the list is empty: on a new database, or when the search finds nothing.
        self.empty = label("", "Muted")
        self.empty.setWordWrap(True)
        self.empty.hide()
        col.addWidget(self.empty)
        split.addWidget(left)

        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        host = QWidget()
        form = QFormLayout(host)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.f = {k: QLineEdit() for k in ("code", "name", "registered_name", "address", "contact_person", "telephone", "email", "tin")}
        self.f["code"].setMaxLength(4)
        self.f["code"].setValidator(QRegularExpressionValidator(QRegularExpression("[A-Za-z0-9]{0,4}"), self.f["code"]))
        self.f["code"].setPlaceholderText("Up to 4 letters or numbers")
        self.terms = terms_combo()
        self.discount = QLineEdit()
        self.term_discount, self.term_days = QLineEdit(), QLineEdit()
        for w in (self.term_discount, self.term_days):
            w.setMaximumWidth(60)
        self.sticker = QCheckBox("Deduct stickers, per piece")
        self.sticker_rate = QLineEdit()
        self.sticker_rate.setMaximumWidth(80)
        # Only what could be right can be typed. The limits themselves (100%, the most days, the most a sticker can be) are checked on Save, with a plain message.
        percent = QRegularExpression(r"[0-9]{0,3}(\.[0-9]{0,2})?%?")
        for w in (self.discount, self.term_discount):
            w.setValidator(QRegularExpressionValidator(percent, w))
        self.term_days.setValidator(QRegularExpressionValidator(QRegularExpression(r"[0-9]{0,%d}" % len(str(masters.MAX_TERM_DAYS))), self.term_days))
        self.sticker_rate.setValidator(QRegularExpressionValidator(QRegularExpression(r"[0-9,]{0,%d}(\.[0-9]{0,2})?" % (len(fmt_plain(masters.MAX_STICKER_RATE_C).split(".")[0]))), self.sticker_rate))
        self.sticker.toggled.connect(self._sticker_toggled)
        self.vat_registered = QCheckBox("VAT registered")
        self.vat_mode = QComboBox()
        self.vat_mode.addItem("VAT included in costs", VAT_INCLUSIVE)
        self.vat_mode.addItem("Add VAT on top", VAT_EXCLUSIVE)
        self.vat_mode.addItem("No VAT", VAT_NONE)
        self.status = QComboBox()
        self.status.addItem("Active", "ACTIVE")
        self.status.addItem("Inactive", "INACTIVE")
        self.remarks = QPlainTextEdit()
        self.remarks.setMaximumHeight(70)
        names = {"code": "Supplier code", "name": "Supplier name", "registered_name": "Registered business name", "address": "Address",
                 "contact_person": "Contact person", "telephone": "Phone", "email": "Email", "tin": "TIN"}
        for key, text in names.items():
            form.addRow(text, self.f[key])
        form.addRow("Payment terms", self.terms)
        form.addRow("Default discount %", self.discount)
        term_row = QHBoxLayout()
        term_row.addWidget(self.term_discount)
        term_row.addWidget(label("% if paid within", "Muted"))
        term_row.addWidget(self.term_days)
        term_row.addWidget(label("days", "Muted"))
        term_row.addStretch(1)
        form.addRow("Term discount", term_row)
        sticker_row = QHBoxLayout()
        sticker_row.addWidget(self.sticker)
        sticker_row.addWidget(self.sticker_rate)
        sticker_row.addStretch(1)
        form.addRow("Stickers", sticker_row)
        form.addRow("", self.vat_registered)
        form.addRow("VAT on POs", self.vat_mode)
        # No order carries VAT for now. VAT registered stays, so the suppliers who issue sales invoices can be checked.
        form.setRowVisible(self.vat_mode, False)
        form.addRow("Status", self.status)
        form.addRow("Remarks", self.remarks)
        self.save_btn = button("Save supplier", self.save, "Primary")
        show_if(self.save_btn, self.editable)
        form.addRow("", left_aligned(self.save_btn))
        if not self.editable:
            for w in (*self.f.values(), self.discount, self.term_discount, self.term_days, self.sticker_rate):
                w.setReadOnly(True)
            self.remarks.setReadOnly(True)
            for w in (self.terms, self.sticker, self.vat_registered, self.vat_mode, self.status):
                w.setEnabled(False)
            form.insertRow(0, "", label("Only Admin can add or change a supplier.", "Muted"))
        # The form scrolls on a short window (1280 x 688 at 150% scaling) instead of making the whole window taller than the screen.
        self.details_scroll = QScrollArea()
        self.details_scroll.setWidgetResizable(True)
        self.details_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.details_scroll.setWidget(host)
        tabs.addTab(self.details_scroll, "Details")
        self.details = tabs
        self.history = make_table(["PO", "Date", "Branch", "Status", "Pieces", "Total"], numeric=(4, 5), stretch=2)
        # The status chip always reads in full, even "Partially delivered" on a small window. The branch gives way instead.
        fm = self.history.fontMetrics()
        set_widths(self.history, {3: max(fm.horizontalAdvance(t) for t in (*orders.STATUS_LABELS.values(), "Status")) + 32})
        self.history.doubleClicked.connect(self._open_history)
        tabs.addTab(self.history, "Purchase history")
        split.addWidget(tabs)
        split.setSizes([620, 560])
        root.addWidget(split, 1)
        self.refresh()
        self.clear_form()
        try:
            folded = theme._settings().value(f"suppliers-folded/{user.username}") == "1"
        except Exception:  # noqa: BLE001 (a missing preference just means unfolded)
            folded = False
        self._show_details(not folded)

    def _show_details(self, on: bool):
        self.details.setVisible(on)
        self.fold_btn.setText("Hide details" if on else "Show details")

    def _toggle_details(self):
        """Folds the details away so the list gets the whole width. Remembered for this person."""
        on = self.details.isHidden()
        self._show_details(on)
        try:
            theme._settings().setValue(f"suppliers-folded/{self.user.username}", "0" if on else "1")
        except Exception:  # noqa: BLE001
            pass

    def _new_pressed(self):
        self._show_details(True)
        self.clear_form()

    def refresh(self, *_):
        text = self.search.text().strip()
        rows = masters.search_suppliers(self.conn, text)
        fill_table(self.table, [[s["code"], s["name"], s["payment_terms"], "Active" if s["status"] == "ACTIVE" else "Inactive"] for s in rows], keys=[s["id"] for s in rows])
        if text:
            self.empty.setText(f"No supplier matches '{text}'. Clear the search to see them all.")
        else:
            self.empty.setText("No suppliers yet. Press New supplier to add one." if self.editable else "No suppliers yet.")
        self.empty.setVisible(not rows)
        return rows

    def _filter_changed(self, *_):
        rows = self.refresh()
        # The search dropped the supplier in the form, so the form doesn't stay ready to save changes to it.
        # A supplier just saved stays on the form even when the search hides it (refresh after Save doesn't come here).
        if self.current_id is not None and self.current_id not in {s["id"] for s in rows}:
            self.clear_form(focus=False)
            self.save_btn.setEnabled(False)

    def clear_form(self, focus: bool = True):
        self.current_id = None
        self.save_btn.setEnabled(self.editable)
        for w in self.f.values():
            w.clear()
        set_terms(self.terms)
        self.discount.clear()
        self.term_discount.clear()
        self.term_days.clear()
        self.remarks.clear()
        self._show_sticker(0)
        self.vat_registered.setChecked(False)
        select_data(self.vat_mode, VAT_NONE)
        self.status.setCurrentIndex(0)
        self.history.setRowCount(0)
        if focus:
            self.f["code"].setFocus()

    def _selected(self):
        key = selected_key(self.table)
        if not key:
            return
        s = self.conn.execute("SELECT * FROM suppliers WHERE id=?", (key,)).fetchone()
        self.current_id = key
        self.save_btn.setEnabled(self.editable)
        for k, w in self.f.items():
            w.setText(s[k])
        set_terms(self.terms, s["payment_terms"])
        self._show_sticker(s["sticker_rate_c"])
        self.discount.setText(fmt_percent(s["default_discount_bp"]).rstrip("%"))
        self.term_discount.setText(fmt_percent(s["term_discount_bp"]).rstrip("%"))
        self.term_days.setText(str(s["term_discount_days"] or ""))
        self.vat_registered.setChecked(bool(s["vat_registered"]))
        select_data(self.vat_mode, s["vat_mode"])
        select_data(self.status, s["status"])
        self.remarks.setPlainText(s["remarks"])
        hist = masters.supplier_history(self.conn, key)
        fill_table(self.history, [[h["po_number"] or f"Draft #{h['id']}", fmt_date(h["po_date"]), h["branch"], h["status"], fmt_qty(h["total_pieces_m"]), fmt_plain(h["grand_total_c"])] for h in hist],
                   keys=[h["id"] for h in hist], status_col=3)

    def _show_sticker(self, rate_c: int):
        self.sticker.setChecked(bool(rate_c))
        self.sticker_rate.setText(fmt_plain(rate_c) if rate_c else "")
        self.sticker_rate.setEnabled(bool(rate_c))

    def _sticker_toggled(self, on: bool):
        self.sticker_rate.setEnabled(on)
        if on and not self.sticker_rate.text().strip():
            self.sticker_rate.setText(fmt_plain(masters.DEFAULT_STICKER_RATE_C))
        elif not on:
            self.sticker_rate.clear()

    def _open_history(self, *_):
        key = selected_key(self.history)
        if key:
            self.open_po.emit(key)

    @staticmethod
    def _number(parse, text: str, message: str) -> int:
        """What was typed, as the service takes it. Anything that isn't a number gets the same plain message the service gives."""
        try:
            return parse(text)
        except ValueError:
            raise POError(message) from None

    def save(self):
        data = {k: w.text() for k, w in self.f.items()} | {
            "payment_terms": self.terms.currentText(), "default_discount_bp": self._number(parse_percent, self.discount.text(), "Type the discount as a number, like 5%."),
            "term_discount_bp": self._number(parse_percent, self.term_discount.text(), "Type the term discount as a number, like 2%."),
            "term_discount_days": self.term_days.text().strip() or 0,
            "sticker_rate_c": self._number(parse_pesos, self.sticker_rate.text(), "Type the sticker deduction as an amount, like 0.50.") if self.sticker.isChecked() else 0,
            "vat_registered": self.vat_registered.isChecked(),
            "vat_mode": self.vat_mode.currentData(), "status": self.status.currentData(), "remarks": self.remarks.toPlainText()}
        try:
            self.current_id = masters.save_supplier(self.conn, self.user, data, self.current_id)
        except masters.DuplicateSupplier as dup:
            if not confirm(self, "Possible duplicate", f"{dup}\n\nSave anyway?", "Save anyway"):
                return
            self.current_id = masters.save_supplier(self.conn, self.user, data, self.current_id, confirm_duplicate=True)
        self.refresh()
        self.changed.emit()
        info(self, "Saved", f"{data['name']} was saved.")


class ProductsPage(QWidget):
    changed = Signal()

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.current_id = None
        root, top = page_layout(self, "Product catalog")
        self.editable = user.can("master.edit")
        new_product = button("New product", self.clear_form)
        top.addWidget(new_product)
        show_if(new_product, self.editable)
        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        col = QVBoxLayout(left)
        col.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search SKU, description or brand")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter_changed)
        self.dept_filter = QComboBox()
        self.dept_filter.addItem("All departments", None)
        depts = masters.list_departments(conn)
        for d in depts:
            self.dept_filter.addItem(f"{d['id']} {d['name']}", d["id"])
        self.dept_filter.currentIndexChanged.connect(self._filter_changed)
        self.supplier_filter = search_combo()
        self.supplier_filter.setMinimumWidth(190)
        self.supplier_filter.currentIndexChanged.connect(self._filter_changed)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.supplier_filter)
        bar.addWidget(self.dept_filter)
        col.addLayout(bar)
        self.table = make_table(["Supplier", "SKU", "Description", "Dept", "UOM", "Last cost"], numeric=(5,), stretch=2)
        set_widths(self.table, {0: 170, 1: 130, 3: 44, 4: 70, 5: 80})
        chooser = smart_columns(self.table, f"products-{user.username}")
        columns_btn = button("Columns", lambda: chooser.open_under(columns_btn))
        bar.addWidget(columns_btn)
        self.table.itemSelectionChanged.connect(self._selected)
        col.addWidget(self.table, 1)
        # What to do next when the list is empty: on a new database, or when the search or filters find nothing.
        self.empty = label("", "Muted")
        self.empty.setWordWrap(True)
        self.empty.hide()
        col.addWidget(self.empty)
        self.count = label("", "Muted")
        col.addWidget(self.count)
        split.addWidget(left)

        host, form_box = panel()
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.code = QLineEdit()
        self.desc = QLineEdit()
        self.dept = QComboBox()
        for d in depts:
            self.dept.addItem(f"{d['id']} {d['name']}", d["id"])
        self.brand, self.size, self.color = QLineEdit(), QLineEdit(), QLineEdit()
        self.uom = QComboBox()
        for u in OFFERED_UNITS:
            self.uom.addItem(u, pieces_per_unit(u))
        self.box = QLineEdit()
        self.supplier = search_combo()
        self.supplier_code = QLineEdit()
        self.cost, self.selling = QLineEdit(), QLineEdit()
        self.active = QCheckBox("Active")
        for text, w in (("SKU", self.code), ("Description", self.desc), ("Department", self.dept), ("Brand", self.brand), ("Size", self.size), ("Color", self.color),
                        ("UOM", self.uom), ("Box of", self.box), ("Supplier", self.supplier), ("Supplier item code", self.supplier_code),
                        ("Last cost", self.cost), ("Selling price", self.selling), ("", self.active)):
            form.addRow(text, w)
        form_box.addLayout(form)
        self.save_btn = button("Save product", self.save, "Primary")
        show_if(self.save_btn, self.editable)
        form_box.addWidget(left_aligned(self.save_btn))
        if not self.editable:
            # Someone who can't save a product sees it, but can't type into it as if they could.
            for w in (self.code, self.desc, self.brand, self.size, self.color, self.box, self.supplier_code, self.cost, self.selling):
                w.setReadOnly(True)
            for w in (self.dept, self.uom, self.supplier, self.active):
                w.setEnabled(False)
            form.insertRow(0, "", label("Only Admin or a Purchaser can add or change a product.", "Muted"))
        form_box.addStretch(1)
        split.addWidget(host)
        split.setSizes([760, 420])
        root.addWidget(split, 1)
        self.reload_suppliers()
        self.refresh()
        self.clear_form()

    def reload_suppliers(self):
        self.supplier.clear()
        self.supplier.addItem("", None)
        for s in masters.search_suppliers(self.conn, include_inactive=False):
            self.supplier.addItem(f"{s['code']}  {s['name']}", s["id"])
        chosen = self.supplier_filter.currentData()
        self.supplier_filter.blockSignals(True)
        self.supplier_filter.clear()
        self.supplier_filter.addItem("All suppliers", None)
        for s in masters.search_suppliers(self.conn):
            self.supplier_filter.addItem(f"{s['code']}  {s['name']}", s["id"])
        select_data(self.supplier_filter, chosen)
        self.supplier_filter.blockSignals(False)

    def refresh(self, *_):
        rows = masters.search_products(self.conn, self.search.text(), department_id=self.dept_filter.currentData(), only_supplier=self.supplier_filter.currentData(),
                                       active_only=False, limit=300)
        total = self.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        fill_table(self.table, [[f"{p['supplier_code']}  {p['supplier_name']}" if p["supplier_code"] else "", p["item_code"], p["description"], p["department_id"], p["uom"],
                                 fmt_plain(p["last_cost_c"]) if p["last_cost_c"] is not None else ""] for p in rows],
                   keys=[p["id"] for p in rows])
        self.count.setText(f"Showing {len(rows):,} of {total:,} products. Search to narrow the list." if total > len(rows) else f"{total:,} products")
        if total:
            self.empty.setText(f"No product matches. Clear the search and filters to see all {total:,}.")
        else:
            self.empty.setText("No products yet. Press New product to add one." if self.editable else "No products yet.")
        self.empty.setVisible(not rows)
        self.count.setVisible(bool(rows))
        return rows

    def _filter_changed(self, *_):
        rows = self.refresh()
        # The search or a filter dropped the product in the form, so the form doesn't stay ready to save changes to it.
        # A product just saved stays on the form even when the list doesn't show it (only the first 300 are listed).
        if self.current_id is not None and self.current_id not in {p["id"] for p in rows}:
            self.clear_form()
            self.save_btn.setEnabled(False)

    def clear_form(self):
        self.current_id = None
        self.save_btn.setEnabled(self.editable)
        for w in (self.code, self.desc, self.brand, self.size, self.color, self.box, self.supplier_code, self.cost, self.selling):
            w.clear()
        self.dept.setCurrentIndex(0)
        self.uom.setCurrentIndex(0)
        self.supplier.setCurrentIndex(0)
        self.active.setChecked(True)

    def _selected(self):
        key = selected_key(self.table)
        if not key:
            return
        p = self.conn.execute("SELECT * FROM products WHERE id=?", (key,)).fetchone()
        self.current_id = key
        self.save_btn.setEnabled(self.editable)
        self.code.setText(p["item_code"])
        self.desc.setText(p["description"])
        select_data(self.dept, p["department_id"])
        self.brand.setText(p["brand"])
        self.size.setText(p["size"])
        self.color.setText(p["color"])
        if self.uom.findText(p["uom"]) < 0:  # an older unit, no longer offered
            self.uom.addItem(p["uom"], pieces_per_unit(p["uom"]))
        self.uom.setCurrentText(p["uom"])
        self.box.setText(str(p["box_size"] or ""))
        select_data(self.supplier, p["supplier_id"])
        self.supplier_code.setText(p["supplier_item_code"])
        self.cost.setText(fmt_plain(p["last_cost_c"]) if p["last_cost_c"] is not None else "")
        self.selling.setText(fmt_plain(p["selling_c"]) if p["selling_c"] is not None else "")
        self.active.setChecked(bool(p["active"]))

    def save(self):
        box = self.box.text().strip()
        if box and not box.isdigit():
            raise POError("Box size must be a whole number.")
        self.current_id = masters.save_product(self.conn, self.user, {
            "item_code": self.code.text(), "description": self.desc.text(), "department_id": self.dept.currentData(), "brand": self.brand.text(),
            "size": self.size.text(), "color": self.color.text(), "uom": self.uom.currentText(), "pieces_per_unit": self.uom.currentData(),
            "box_size": int(box) if box else None, "supplier_id": combo_value(self.supplier), "supplier_item_code": self.supplier_code.text(),
            "last_cost_c": parse_pesos(self.cost.text()) if self.cost.text().strip() else None,
            "selling_c": parse_pesos(self.selling.text()) if self.selling.text().strip() else None, "active": self.active.isChecked()}, self.current_id)
        self.refresh()
        self.changed.emit()
        info(self, "Saved", "The product was saved.")


class BranchesPage(QWidget):
    """Branch codes are permanent. A closed branch goes inactive and keeps all its history."""
    changed = Signal()

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.current_code = None
        root, top = page_layout(self, "Branches")
        top.addWidget(label("Only active branches appear on new order forms.", "Muted"))
        self.new_btn = button("New branch", self.clear_form)
        show_if(self.new_btn, user.can("branch.manage"))
        top.addWidget(self.new_btn)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.table = make_table(["Code", "Name", "Type", "Address", "Status", "POs"], numeric=(5,), stretch=3)
        set_widths(self.table, {0: 56, 1: 150, 2: 90, 4: 90, 5: 56})
        chooser = smart_columns(self.table, f"branches-{user.username}")
        columns_btn = button("Columns", lambda: chooser.open_under(columns_btn))
        top.addWidget(columns_btn)
        self.table.itemSelectionChanged.connect(self._selected)
        split.addWidget(self.table)
        host, box = panel()
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.code, self.name, self.short, self.address = QLineEdit(), QLineEdit(), QLineEdit(), QLineEdit()
        self.code.setPlaceholderText("Permanent. It goes in every PO number")
        self.kind = QComboBox()
        self.kind.addItem("Branch", "BRANCH")
        self.kind.addItem("Warehouse", "WAREHOUSE")
        self.active = QCheckBox("Active")
        for text, w in (("Branch code", self.code), ("Name", self.name), ("Short code", self.short), ("Type", self.kind), ("Delivery address", self.address), ("", self.active)):
            form.addRow(text, w)
        box.addLayout(form)
        self.save_btn = button("Save branch", self.save, "Primary")
        show_if(self.save_btn, user.can("branch.manage"))
        box.addWidget(left_aligned(self.save_btn))
        if not user.can("branch.manage"):
            # Someone who can't save a branch sees it, but can't type into it as if they could.
            for w in (self.code, self.name, self.short, self.address):
                w.setReadOnly(True)
            for w in (self.kind, self.active):
                w.setEnabled(False)
            form.insertRow(0, "", label("Only Admin can add or change a branch.", "Muted"))
        self.note = label("The address prints on that branch's POs as the delivery destination.", "Muted")
        self.note.setWordWrap(True)
        box.addWidget(self.note)
        box.addStretch(1)
        split.addWidget(host)
        split.setSizes([760, 400])
        root.addWidget(split, 1)
        self.refresh()
        self.clear_form()

    def refresh(self):
        counts = {r[0]: r[1] for r in self.conn.execute("SELECT branch_code, COUNT(*) FROM purchase_orders GROUP BY branch_code")}
        rows = masters.list_branches(self.conn)
        fill_table(self.table, [[b["code"], b["name"], b["kind"].title(), b["address"], "APPROVED" if b["active"] else "CLOSED", counts.get(b["code"], 0)] for b in rows],
                   keys=[b["code"] for b in rows], status_col=4)
        for r, b in enumerate(rows):
            self.table.item(r, 4).setText("Active" if b["active"] else "Inactive")

    def clear_form(self):
        self.current_code = None
        for w in (self.code, self.name, self.short, self.address):
            w.clear()
        self.code.setEnabled(True)
        self.kind.setCurrentIndex(0)
        self.active.setChecked(True)

    def _selected(self):
        key = selected_key(self.table)
        if not key:
            return
        b = self.conn.execute("SELECT * FROM branches WHERE code=?", (key,)).fetchone()
        self.current_code = key
        self.code.setText(str(b["code"]))
        self.code.setEnabled(False)
        self.name.setText(b["name"])
        self.short.setText(b["short_code"])
        self.address.setText(b["address"])
        select_data(self.kind, b["kind"])
        self.active.setChecked(bool(b["active"]))

    def save(self):
        self.current_code = masters.save_branch(self.conn, self.user, {
            "code": self.code.text(), "name": self.name.text(), "short_code": self.short.text(), "kind": self.kind.currentData(),
            "address": self.address.text(), "active": self.active.isChecked()}, self.current_code)
        self.refresh()
        self.changed.emit()


class ReportsPage(QWidget):
    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.report = None
        root, top = page_layout(self, "Reports")
        self.title_note = label("", "Muted")
        top.addWidget(self.title_note)
        self.export_btn = button("Export to Excel", self.export)
        top.addWidget(self.export_btn)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.list = QListWidget()
        self.list.setObjectName("Nav")
        for d in reports.REPORTS:
            self.list.addItem(d.title)
        self.list.setMaximumWidth(270)
        self.list.currentRowChanged.connect(self.run)
        split.addWidget(self.list)
        right = QWidget()
        col = QVBoxLayout(right)
        col.setContentsMargins(4, 0, 0, 0)
        bar = QHBoxLayout()
        self.date_from = date_edit(optional=True)
        self.date_to = date_edit(optional=True)
        self.supplier = search_combo()
        self.supplier.setMinimumWidth(240)
        self.item = QLineEdit()
        self.item.setPlaceholderText("SKU or description")
        self.supplier_label, self.item_label = label("Supplier", "Muted"), label("Product", "Muted")
        for text, w in (("From", self.date_from), ("To", self.date_to)):
            bar.addWidget(label(text, "Muted"))
            bar.addWidget(w)
        self.month = month_picker(self.date_from, self.date_to)
        bar.addWidget(self.month)
        bar.addWidget(self.supplier_label)
        bar.addWidget(self.supplier)
        bar.addWidget(self.item_label)
        bar.addWidget(self.item, 1)
        bar.addStretch(0)
        col.addLayout(bar)
        self.table = make_table(["-"])
        col.addWidget(self.table, 1)
        self.note = label("", "Muted")
        col.addWidget(self.note)
        split.addWidget(right)
        split.setSizes([250, 950])
        root.addWidget(split, 1)
        self.date_from.dateChanged.connect(lambda *_: self.run())
        self.date_to.dateChanged.connect(lambda *_: self.run())
        self.supplier.currentIndexChanged.connect(lambda *_: self.run())
        self.item.textChanged.connect(lambda *_: self.run())
        self.reload_suppliers()
        self.list.setCurrentRow(0)

    def reload_suppliers(self):
        self.supplier.blockSignals(True)
        self.supplier.clear()
        self.supplier.addItem("", None)
        for s in masters.search_suppliers(self.conn):
            self.supplier.addItem(f"{s['code']}  {s['name']}", s["id"])
        self.supplier.blockSignals(False)

    def run(self, *_):
        row = self.list.currentRow()
        if row < 0:
            return
        d = reports.REPORTS[row]
        for w in (self.supplier, self.supplier_label):
            w.setVisible("supplier" in d.needs)
        for w in (self.item, self.item_label):
            w.setVisible("item" in d.needs)
        date_from, date_to = date_value(self.date_from), date_value(self.date_to)
        if date_from and date_to and date_from > date_to:
            # Nothing could match, so nothing runs, and the list doesn't read as there being no data.
            self.report = None
            self.table.setRowCount(0)
            self.note.setText("From is after To.")
            self.title_note.setText("")
            return
        self.report = d.fn(self.conn, date_from=date_from, date_to=date_to,
                           supplier_id=combo_value(self.supplier), item_text=self.item.text().strip())
        rep = self.report
        numeric = tuple(i for i, c in enumerate(rep.columns) if c.kind != reports.TEXT)
        self.table.setColumnCount(len(rep.columns))
        self.table.setHorizontalHeaderLabels([c.title for c in rep.columns])
        self.table.setProperty("numeric", list(numeric))
        for i in numeric:
            self.table.horizontalHeaderItem(i).setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        rows = [[reports.display(v, c.kind) for v, c in zip(r, rep.columns)] for r in rep.rows]
        total = reports.totals_row(rep)
        if total:
            rows.append([reports.display(v, c.kind) for v, c in zip(total, rep.columns)])
        # Filled in the order given, so the Total is still the last row when it's marked. A heading pressed earlier sorts it again below.
        sort_col = self.table.property("sort_col")
        self.table.setProperty("sort_col", None)
        fill_table(self.table, rows)
        self.table.setProperty("sort_col", sort_col)
        if total:
            last = self.table.rowCount() - 1
            # The Total stays last whichever way a heading sorts the report.
            self.table.item(last, 0).setData(PIN_BOTTOM_ROLE, True)
            for c in range(self.table.columnCount()):
                f = self.table.item(last, c).font()
                f.setBold(True)
                self.table.item(last, c).setFont(f)
        resort(self.table)
        self.table.resizeColumnsToContents()
        # An empty report says so, as well as any standing note it has.
        self.note.setText(" ".join(t for t in (rep.note, "" if rep.rows else "Nothing matches these dates.") if t))
        self.title_note.setText(f"{len(rep.rows):,} row" + ("" if len(rep.rows) == 1 else "s"))

    def _filter_text(self) -> str:
        a, b = date_value(self.date_from), date_value(self.date_to)
        return f"From {fmt_date(a) or 'the start'} to {fmt_date(b) or 'today'}"

    def export(self):
        if not self.report:
            return
        path = save_file(self, "Export report", str(Path.home() / "Documents" / f"{self.report.title} {today().isoformat()}.xlsx"), "Excel (*.xlsx)")
        if path:
            reports.to_excel(self.report, path, self._filter_text())
            info(self, "Exported", f"Saved to {path}")
