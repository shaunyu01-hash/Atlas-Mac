"""Login and the main window."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
                               QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

from .. import auth, backup, payments as payment_service, sample_data
from ..db import POError
from . import theme
from .common import BUSY_MESSAGE, app_icon, button, error, guarded, icon, is_busy, label, show_unexpected
from .of_page import InquiryPage, OFPage, OrdersPage
from .payments_page import PaymentsPage
from .warehouse_page import CentralWarehousePage
from .claim_stub_page import ClaimStubsPage
from .item_pass_page import ItemPassesPage
from .debit_memo_page import DebitMemosPage
from .pages import ApprovalsPage, BranchesPage, PrintPage, ProductsPage, ReportsPage, SuppliersPage
from .po_page import POPage
from .receiving_page import ReceivedPage, ReceivingPage
from .selling_page import SellingPricesPage
from .settings_page import SettingsPage
from .inventory_page import AdjustmentsPage, CountsPage, InventoryReportsPage, StockPage, TransfersPage


class FirstRunDialog(QDialog):
    """No default password ever ships: the first person sets up the administrator."""

    def __init__(self, conn):
        super().__init__()
        self.conn, self.user = conn, None
        self.setWindowTitle("Atlas PO ERP: first-time setup")
        self.setWindowIcon(app_icon())
        form = QFormLayout(self)
        form.setContentsMargins(24, 20, 24, 18)
        form.addRow(label("Create the administrator account", "PanelTitle"))
        form.addRow(label("This person is also the approver until you choose someone else in Settings.", "Muted"))
        self.username, self.full_name, self.password, self.again = QLineEdit(), QLineEdit(), QLineEdit(), QLineEdit()
        for w in (self.password, self.again):
            w.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Username", self.username)
        form.addRow("Full name", self.full_name)
        form.addRow("Password", self.password)
        form.addRow("Password again", self.again)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(lambda: guarded(self, self._create))
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _create(self):
        if self.password.text() != self.again.text():
            raise POError("The two passwords don't match.")
        self.user = auth.create_first_admin(self.conn, self.username.text(), self.full_name.text(), self.password.text())
        self.accept()


class LoginDialog(QDialog):
    def __init__(self, conn):
        super().__init__()
        self.conn, self.user = conn, None
        self.setWindowTitle("Atlas PO ERP")
        self.setWindowIcon(app_icon())
        form = QFormLayout(self)
        form.setContentsMargins(24, 20, 24, 18)
        form.setVerticalSpacing(8)
        mark = QLabel()
        mark.setPixmap(app_icon().pixmap(QSize(44, 44)))
        form.addRow(mark)
        form.addRow(label("Atlas PO ERP", "Title"))
        form.addRow(label("Purchasing: order forms and branch POs", "Muted"))
        self.username, self.password = QLineEdit(), QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Username", self.username)
        form.addRow("Password", self.password)
        if sample_data.is_sample(conn):
            self.setWindowTitle("Atlas PO ERP (sample data)")
            hint = label(f"Sample data. Log in as owner, or as admin, approver, purchaser, purchaser2, accounting, warehouse, warehouse2 or viewer to try a role, or as a receiver: nor1 and nor2 for North, or sou1, sou2, eas1, eas2, wes1, wes2. The password for all of them is {sample_data.PASSWORD}.", "Muted")
            hint.setWordWrap(True)
            form.addRow(hint)
        self.problem = label("", "Error")
        form.addRow(self.problem)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Log in")
        buttons.accepted.connect(self._login)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        # Tall enough for every line of the sample logins, which wrap.
        self.resize(400, max(280, self.heightForWidth(400)))

    def _login(self):
        try:
            self.user = auth.login(self.conn, self.username.text(), self.password.text())
            self.accept()
        except POError as exc:
            self.problem.setText(str(exc))
            self.password.clear()
            self.password.setFocus()
        except Exception as exc:  # noqa: BLE001 (a database error must not leave the login doing nothing)
            if is_busy(exc):
                self.problem.setText(BUSY_MESSAGE)
            else:
                self.problem.setText("Couldn't log in. The details are in errors.log in the data folder.")
                show_unexpected(self, exc)


class ChangePasswordDialog(QDialog):
    def __init__(self, conn, user, required=False):
        super().__init__()
        self.conn, self.user = conn, user
        self.setWindowTitle("Change password")
        form = QFormLayout(self)
        if required:
            form.addRow(label("Please choose your own password before continuing.", "Muted"))
        self.current, self.new, self.again = QLineEdit(), QLineEdit(), QLineEdit()
        for text, w in (("Current password", self.current), ("New password", self.new), ("New password again", self.again)):
            w.setEchoMode(QLineEdit.EchoMode.Password)
            form.addRow(text, w)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(lambda: guarded(self, self._save))
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _save(self):
        if self.new.text() != self.again.text():
            raise POError("The two new passwords don't match.")
        auth.change_own_password(self.conn, self.user, self.current.text(), self.new.text())
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, conn, user, on_logout=None, on_restored=None):
        super().__init__()
        self.conn, self.user = conn, user
        self.on_logout, self.on_restored = on_logout, on_restored
        sample = sample_data.is_sample(conn)
        self.setWindowTitle("Atlas PO ERP (sample data)" if sample else "Atlas PO ERP")
        self.setWindowIcon(app_icon())
        self.resize(1400, 860)
        host = QWidget()
        host.setObjectName("Main")
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.setCentralWidget(host)

        side = QFrame()
        side.setObjectName("Sidebar")
        side.setFixedWidth(212)
        col = QVBoxLayout(side)
        col.setContentsMargins(6, 10, 6, 8)
        col.setSpacing(4)
        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(12, 0, 0, 0)
        brand_row.setSpacing(9)
        mark = QLabel()
        mark.setPixmap(app_icon().pixmap(QSize(30, 30)))
        brand_row.addWidget(mark)
        words = QVBoxLayout()
        words.setSpacing(0)
        words.addWidget(label("Atlas PO ERP", "Brand"))
        self.brand_sub = label("Purchasing, sample data" if sample else "Purchasing", "BrandSub")
        self.sample = sample
        words.addWidget(self.brand_sub)
        brand_row.addLayout(words)
        brand_row.addStretch(1)
        col.addLayout(brand_row)
        # Purchasing and Inventory are two systems in one program: this switch swaps the whole sidebar and the screen.
        self.inventory_entries = self._inventory_layout(user)
        self.system = "po"
        self.po_switch = button("Purchasing", lambda: self.show_system("po"), "Count", lock=False)
        self.inv_switch = button("Inventory", lambda: self.show_system("inventory"), "Count", lock=False)
        switch = QHBoxLayout()
        switch.setSpacing(4)
        # Low, so every sidebar entry still fits a maximised window on a 1366 x 768 laptop.
        switch.setContentsMargins(6, 0, 6, 0)
        for b in (self.po_switch, self.inv_switch):
            b.setCheckable(True)
            b.setFixedHeight(22)
            switch.addWidget(b)
        self.po_switch.setChecked(True)
        if self.inventory_entries:
            col.addLayout(switch)
            # The switch already says which system is open, so the line under the name only says when it's sample data. That room keeps the sidebar on a laptop screen.
            self.brand_sub.setText("Sample data")
            self.brand_sub.setVisible(sample)
            mark.setPixmap(app_icon().pixmap(QSize(24, 24)))
        else:
            self.po_switch.hide()
            self.inv_switch.hide()
        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        self.nav.setIconSize(QSize(18, 18))
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Group headings sit in shorter rows (NAV_HEAD), so their space above is trimmed to keep the words whole. Colours still come from the theme.
        self.nav.setStyleSheet("QListWidget#Nav::item:disabled { padding: 4px 10px 0px 10px; }")
        col.addWidget(self.nav, 1)
        self.inv_nav = QListWidget()
        self.inv_nav.setObjectName("Nav")
        self.inv_nav.setIconSize(QSize(18, 18))
        self.inv_nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.inv_nav.setStyleSheet("QListWidget#Nav::item:disabled { padding: 4px 10px 0px 10px; }")
        self.inv_nav.hide()
        col.addWidget(self.inv_nav, 1)
        # Admin is told while nobody can receive at the Central Warehouse, so stubs, passes and deliveries don't sit there unrecorded.
        # One short line so the sidebar still fits a laptop screen; the full advice is on hover.
        self.warehouse_warning = label("No Warehouse login yet", "Error")
        self.warehouse_warning.setToolTip("No Warehouse login yet. Add the Warehouse Head under Settings, Users, so goods can be received at the warehouse.")
        self.warehouse_warning.setContentsMargins(12, 0, 6, 0)
        self.warehouse_warning.hide()
        col.addWidget(self.warehouse_warning)
        who = label(user.display, "PanelTitle")
        who.setContentsMargins(12, 0, 0, 0)
        # The job title, such as Warehouse Head, or the roles when there's none.
        roles = label(auth.job_line(user), "Muted")
        roles.setContentsMargins(12, 0, 0, 2)
        roles.setWordWrap(True)
        roles.setToolTip(", ".join(auth.ROLES[r] for r in sorted(user.roles)))
        col.addWidget(who)
        col.addWidget(roles)
        self.theme_button = button("", self._toggle_theme, lock=False)
        # Dark mode and Log out share a row so every sidebar entry fits a maximised window on a 1366 x 768 laptop.
        # Change password keeps its own row: beside Dark mode its words would be cut off in the narrow sidebar.
        col.addWidget(button("Change password", self._change_password))
        pair = QHBoxLayout()
        pair.setSpacing(4)
        pair.addWidget(self.theme_button)
        pair.addWidget(button("Log out", self._logout))
        col.addLayout(pair)
        row.addWidget(side)

        self.stack = QStackedWidget()
        self._stale = set()
        self._policies = {}
        self.stack.currentChanged.connect(self._shown)
        row.addWidget(self.stack, 1)

        # Each page is built the first time it's opened, not at login: building and filling every page, even ones this person can't reach,
        # kept the window from appearing for seconds once history grew. A page that isn't allowed is None.
        can_review = user.can("receiving.review")
        factories = {"orders_page": OrdersPage, "approvals": ApprovalsPage, "print_pos": PrintPage, "inquiry": InquiryPage, "selling": SellingPricesPage,
                     "receiving": ReceivingPage if user.can("receiving.count") else None,
                     "received": ReceivedPage if can_review else None, "debit_memos": DebitMemosPage if can_review else None,
                     "claim_stubs": ClaimStubsPage if can_review else None, "item_passes": ItemPassesPage if can_review else None,
                     "payments": PaymentsPage if user.can("payment.view") else None, "central": CentralWarehousePage if can_review else None,
                     "suppliers": SuppliersPage, "products": ProductsPage, "branches": BranchesPage, "reports": ReportsPage,
                     "of_page": OFPage, "po_page": POPage, "settings": SettingsPage if user.can("settings.edit") else None,
                     **{name: cls for name, cls in self._INVENTORY_PAGES.items() if any(e[2] == name for e in self.inventory_entries)}}
        for name, cls in factories.items():
            if cls is None:
                setattr(self, name, None)
        self._factories = {name: cls for name, cls in factories.items() if cls is not None}
        # A receiver counts blind, so nothing that shows quantities or costs is within reach.
        self.receiver_only = user.roles == {"RECEIVER"}
        # Accounting handles payments and checks what was received. Ordering isn't theirs to reach.
        self.accounting_only = user.roles == {"ACCOUNTING"}
        # The warehouse only records goods arriving there, from claim stubs and item passes.
        self.warehouse_only = user.roles == {"WAREHOUSE"}
        self.limited = self.receiver_only or self.accounting_only or self.warehouse_only
        has_settings = "settings" in self._factories
        # Purchasing is the work, step by step. Records is for looking things up: Inquiry holds every order's history beside the suppliers, products and branches.
        layout = [("Purchasing", [("Orders", "order-form", "orders_page"), ("Approvals", "approvals", "approvals"), ("Print", "print", "print_pos")]),
                  ("Sales", [("Selling prices", "tag", "selling")]),
                  ("Records", [("Inquiry", "search", "inquiry"), ("Suppliers", "suppliers", "suppliers"), ("Product catalog", "catalog", "products"), ("Branches", "branches", "branches")]),
                  ("Reports and settings" if has_settings else "Reports",
                   [("Reports", "reports", "reports")] + ([("Settings", "settings", "settings")] if has_settings else []))]
        delivered = [("Receiving", "receiving", "receiving")] + ([("Central Warehouse", "receiving", "central"), ("Received", "received", "received"), ("Debit memos", "debit", "debit_memos"), ("Claim stubs", "claim", "claim_stubs"), ("Item passes", "claim", "item_passes"), ("Payments", "payments", "payments")] if can_review else [])
        delivered = [entry for entry in delivered if entry[2] in self._factories]  # Purchasers have no Payments page.
        layout = [("Receiving", delivered)] if self.receiver_only else [(layout[0][0], layout[0][1] + delivered)] + layout[1:]
        if self.accounting_only:
            layout = [("Accounting", [entry for entry in delivered if entry[2] == "payments"] + [entry for entry in delivered if entry[2] != "payments"])]
        if self.warehouse_only:
            layout = [("Warehouse", [entry for entry in delivered if entry[2] in ("central", "claim_stubs", "item_passes")])]
        self._entries = []
        self._nav_names = {}
        self.nav_glyphs = {}
        for group, entries in layout:
            head = QListWidgetItem(group)
            head.setFlags(Qt.ItemFlag.NoItemFlags)
            head.setSizeHint(QSize(0, self.NAV_HEAD))
            self.nav.addItem(head)
            for text, glyph, name in entries:
                entry = QListWidgetItem(icon(glyph), text)
                entry.setSizeHint(QSize(0, self.NAV_ROW))
                self.nav.addItem(entry)
                self._nav_names[self.nav.count() - 1] = name
                self.nav_glyphs[self.nav.count() - 1] = glyph
                self._entries.append((text, name))
        row_of = {name: r for r, name in self._nav_names.items()}
        self.approvals_row = row_of.get("approvals")
        self.print_row = row_of.get("print_pos")
        self.payments_row = row_of.get("payments")
        self.claims_row = row_of.get("claim_stubs")
        # Leaving an Order Form or PO by the sidebar or Ctrl+Shift+N asks first, as Back does, when something is unsaved.
        self._nav_row = None
        self._leaving = self._left = self._skip_leave_check = False
        self.nav.currentRowChanged.connect(self._navigate)
        # Clicking the row that's already highlighted leaves an Order Form or PO for that page.
        self.nav.itemClicked.connect(self._nav_clicked)
        self.new_shortcut = QShortcut(QKeySequence("Ctrl+Shift+N"), self, activated=self.new_of)
        self.nav.setCurrentRow(min(self._nav_names))
        self._inv_names, self.inv_glyphs = {}, {}
        for text, glyph, name in self.inventory_entries:
            if text is None:
                head = QListWidgetItem(glyph)
                head.setFlags(Qt.ItemFlag.NoItemFlags)
                head.setSizeHint(QSize(0, self.NAV_HEAD))
                self.inv_nav.addItem(head)
                continue
            entry = QListWidgetItem(icon(glyph), text)
            entry.setSizeHint(QSize(0, self.NAV_ROW))
            self.inv_nav.addItem(entry)
            self._inv_names[self.inv_nav.count() - 1] = name
            self.inv_glyphs[self.inv_nav.count() - 1] = glyph
        self.inv_nav.currentRowChanged.connect(self._inv_navigate)
        self._update_counts()
        self._name_theme_button()

    _INVENTORY_PAGES = {"inv_stock": StockPage, "inv_counts": CountsPage, "inv_adjust": AdjustmentsPage, "inv_transfers": TransfersPage,
                        "inv_reports": InventoryReportsPage}

    @staticmethod
    def _inventory_layout(user) -> list:
        """(text, icon, page) for the Inventory sidebar, with (None, heading, None) for a heading. Empty when the person has no part in inventory."""
        stock = [("Stock on hand", "catalog", "inv_stock")] if user.can("inventory.view") else []
        if user.can("count.manage") or user.can("stock.count") or user.can("stock.post"):
            stock.append(("Stock counts", "approvals", "inv_counts"))
        if user.can("adjust.prepare") or user.can("stock.post") or user.can("inventory.view"):
            stock.append(("Adjustments", "debit", "inv_adjust"))
        if user.can("transfer.send") or user.can("transfer.receive") or user.can("inventory.view"):
            stock.append(("Transfers", "receiving", "inv_transfers"))
        out = [(None, "Inventory", None)] + stock if stock else []
        if user.can("inventory.view"):
            out += [(None, "Reports", None), ("Inventory reports", "reports", "inv_reports")]
        return out

    def show_system(self, system: str):
        """Purchasing or Inventory: the sidebar and the screen change together. An unsaved Order Form or PO asks first."""
        if system == self.system or (system == "inventory" and not self.inventory_entries) or (system == "inventory" and not self._leave_document()):
            self.po_switch.setChecked(self.system == "po")
            self.inv_switch.setChecked(self.system == "inventory")
            return
        self.system = system
        inventory = system == "inventory"
        self.po_switch.setChecked(not inventory)
        self.inv_switch.setChecked(inventory)
        self.nav.setVisible(not inventory)
        self.inv_nav.setVisible(inventory)
        if inventory:
            row = self.inv_nav.currentRow()
            if row in self._inv_names:
                self._inv_navigate(row)
            else:
                self.inv_nav.setCurrentRow(min(self._inv_names))
        else:
            self._skip_leave_check = True
            try:
                self._navigate(self.nav.currentRow())
            finally:
                self._skip_leave_check = False
        self._update_counts()

    def _inv_navigate(self, row: int):
        name = self._inv_names.get(row)
        if name is None:
            return
        page, fresh = self._page(name)
        if not fresh and hasattr(page, "refresh"):
            page.refresh()
        self._stale.discard(page)
        self.stack.setCurrentWidget(page)

    NAV_ROW = 26  # With NAV_HEAD, low enough that every sidebar entry fits a maximised window on a 1366 x 768 laptop without scrolling.
    NAV_HEAD = 20 # Group headings, such as Purchasing, are a little shorter than the entries.

    def __getattr__(self, name):
        """A page that hasn't been opened yet is built when something asks for it."""
        factories = self.__dict__.get("_factories")
        if factories is not None and name in factories:
            return self._page(name)[0]
        raise AttributeError(name)

    @property
    def pages(self) -> list:
        """(sidebar text, page) for every page in the sidebar. Asking for them builds them all."""
        return [(text, getattr(self, name)) for text, name in self._entries]

    @property
    def nav_rows(self) -> dict:
        """Sidebar row to page. Asking for them builds them all."""
        return {row: getattr(self, name) for row, name in self._nav_names.items()}

    def _built(self, name):
        """The page if it has been built, else None. Never builds it."""
        return self.__dict__.get(name)

    def _page(self, name) -> tuple:
        """The page, and whether it was built just now (and so already shows today's figures)."""
        page = self.__dict__.get(name)
        if page is not None:
            return page, False
        page = self._factories[name](self.conn, self.user)
        setattr(self, name, page)
        self._policies[page] = page.sizePolicy()
        self._wire(name, page)
        self.stack.addWidget(page)
        self._fit_to(self.stack.currentWidget())
        return page, True

    def _wire(self, name, page):
        """What a page asks of the window, and which other pages its changes reach. A page not built yet needs nothing: it's filled when built."""
        if name == "orders_page":
            page.new_of.connect(self.new_of)
            page.open_of.connect(self.open_of)
            page.open_po.connect(self.open_po)
            page.go_approvals.connect(lambda: self.nav.setCurrentRow(self.approvals_row))
            page.go_print.connect(self._go_print)
            page.go_completed.connect(self._show_completed)
        elif name in ("approvals", "print_pos"):
            page.open_of.connect(self.open_of)
        elif name == "inquiry":
            page.open_of.connect(self.open_of)
            page.open_po.connect(self.open_po)
        elif name == "suppliers":
            page.open_po.connect(self.open_po)
        elif name == "of_page":
            page.open_po.connect(self.open_po)
            page.closed.connect(self._back_from_document)
        elif name == "po_page":
            page.open_of.connect(self.open_of)
            page.closed.connect(self._back_from_document)
        elif name == "settings":
            page.changed.connect(self.refresh_all)
            page.changed.connect(lambda: self._call_built("of_page", "apply_layout"))
            page.restored.connect(self._restored)
        if name in ("of_page", "po_page", "orders_page", "approvals", "print_pos", "suppliers", "products", "branches"):
            page.changed.connect(self.refresh_all)
        links = {"receiving": ("received", "item_passes"), "received": ("debit_memos", "claim_stubs"),
                 "debit_memos": ("received",), "claim_stubs": ("received",), "item_passes": ("received",), "central": ("received",)}
        for target in links.get(name, ()):
            page.changed.connect(lambda *_, t=target: self._call_built(t, "refresh"))
        if name in self._INVENTORY_PAGES:
            page.changed.connect(self._update_counts)
        if name == "payments" and "received" in self._factories:
            page.changed.connect(self.refresh_all)

    def _call_built(self, name, method):
        page = self._built(name)
        if page is not None:
            getattr(page, method)()

    def _fit_to(self, current):
        """Only the page on screen sets how small the window may be, so one wide page doesn't keep the window wide after leaving it."""
        ignored = QSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        for i in range(self.stack.count()):
            w = self.stack.widget(i)
            w.setSizePolicy(self._policies.get(w, w.sizePolicy()) if w is current else ignored)

    def _go_print(self, of_id: int):
        """Orders' Go to Print: Print opens with that OF already ticked."""
        if self.print_row is None:
            return
        self.nav.setCurrentRow(self.print_row)
        page = self._built("print_pos")
        if page is not None and self.stack.currentWidget() is page:
            page.tick_of(of_id)

    def _show_completed(self):
        row = next((r for r, name in self._nav_names.items() if name == "inquiry"), None)
        if row is not None:
            self.nav.setCurrentRow(row)
            self.inquiry.show_completed()

    def _navigate(self, row: int):
        name = self._nav_names.get(row)
        if name is None:
            return
        if not self._skip_leave_check and not self._leave_document():
            # Stayed on the document: the sidebar goes back to the row it showed.
            self.nav.blockSignals(True)
            self.nav.setCurrentRow(self._nav_row if self._nav_row is not None else -1)
            self.nav.blockSignals(False)
            return
        self._nav_row = row
        page, fresh = self._page(name)
        if not fresh:
            self._prepare(page)
            if hasattr(page, "refresh"):
                page.refresh()
        self._stale.discard(page)
        self.stack.setCurrentWidget(page)

    def _nav_clicked(self, item):
        row = self.nav.row(item)
        name = self._nav_names.get(row)
        if name is not None and row == self.nav.currentRow() and self.stack.currentWidget() is not self._built(name):
            self._navigate(row)

    def _leave_document(self) -> bool:
        """True when nothing stops leaving the screen on show. On an Order Form or PO, its own Back asks first about anything unsaved."""
        page = self.stack.currentWidget()
        if page is None or page not in (self._built("of_page"), self._built("po_page")):
            return True
        self._leaving, self._left = True, False
        try:
            page._leave()
        finally:
            self._leaving = False
        return self._left

    def _shown(self, index: int):
        """A page that missed a change is redrawn as it's shown, however it was reached: the sidebar, Back, or opening a document."""
        page = self.stack.widget(index)
        self._fit_to(page)
        if page in self._stale:
            self._stale.discard(page)
            if hasattr(page, "refresh"):
                self._prepare(page)
                page.refresh()

    def _prepare(self, page):
        """Lists of suppliers and other choices are reloaded as a page is shown, in case someone added one elsewhere."""
        if page is None:
            return
        if page is self._built("inquiry") or page is self._built("orders_page"):
            page.reload_choices()
        elif page is self._built("reports") or page is self._built("products"):
            page.reload_suppliers()

    def new_of(self):
        if self.limited or not self.user.can("of.edit"):
            return
        if not self._leave_document():
            return
        self.of_page.new_of()
        self.stack.setCurrentWidget(self.of_page)

    def open_of(self, of_id: int):
        self.of_page.open_of(of_id)
        self.stack.setCurrentWidget(self.of_page)

    def open_po(self, po_id: int):
        self.po_page.open_po(po_id)
        self.stack.setCurrentWidget(self.po_page)

    def _back_from_document(self):
        if self._leaving:
            # The sidebar or Ctrl+Shift+N asked to leave, and the document agreed. They carry on from here.
            self._left = True
            return
        self.refresh_all()
        row = self._nav_row if self._nav_row is not None else self.nav.currentRow()
        self._skip_leave_check = True
        try:
            if self.nav.currentRow() != row:
                self.nav.setCurrentRow(row)
            else:
                self._navigate(row)
        finally:
            self._skip_leave_check = False

    def refresh_all(self):
        """After any change, the page on screen is redrawn and the sidebar counts are updated. Every other page redraws when it's opened,
        in _navigate, so none shows old figures. Redrawing them all here took seconds once history grew."""
        current = self.stack.currentWidget()
        if current is not None and hasattr(current, "refresh"):
            self._prepare(current)
            current.refresh()
        self._stale = {self.stack.widget(i) for i in range(self.stack.count())} - {current}
        self._update_counts()

    def _update_counts(self):
        """The warehouse warning and the counts beside the sidebar entries. Read straight from the data, so no page has to be built for them."""
        self.warehouse_warning.setVisible(self.user.can("users.edit") and auth.warehouse_login_missing(self.conn))
        # Adjustments waiting for management, beside the Inventory sidebar entry.
        adjust_row = next((r for r, n in self.__dict__.get("_inv_names", {}).items() if n == "inv_adjust"), None)
        if adjust_row is not None and self.user.can("stock.post"):
            from .. import inventory as inventory_service
            n = inventory_service.adjustments_waiting(self.conn, self.user)
            self.inv_nav.item(adjust_row).setText(f"Adjustments  ({n})" if n else "Adjustments")
        # Whoever signs off sees how many wait for them, without clicking through rows.
        # Damage the branches reported waits for Accounting or management.
        if self.claims_row is not None and self.user.can("cs.approve_request"):
            from .. import claim_stubs as cs_service
            n = cs_service.reports_waiting(self.conn)
            self.nav.item(self.claims_row).setText(f"Claim stubs  ({n})" if n else "Claim stubs")
        if self.payments_row is not None and self.user.can("payment.signoff"):
            n = payment_service.waiting_signoffs(self.conn)
            # Short, so it fits the sidebar; hovering says what the number is.
            self.nav.item(self.payments_row).setText(f"Payments  ({n})" if n else "Payments")
            self.nav.item(self.payments_row).setToolTip(f"{n} to sign off" if n else "")
        if self.limited:
            return
        # The same counts the Approvals and Print pages give. They only read the database, so they're asked here without building those pages.
        approvals, print_pos = self._built("approvals"), self._built("print_pos")
        n = approvals.count() if approvals is not None else ApprovalsPage.count(self)
        self.nav.item(self.approvals_row).setText(f"Approvals  ({n})" if n else "Approvals")
        waiting = print_pos.count_to_print() if print_pos is not None else PrintPage.count_to_print(self)
        self.nav.item(self.print_row).setText(f"Print  ({waiting})" if waiting else "Print")

    def _name_theme_button(self):
        self.theme_button.setText("Light mode" if theme.is_dark() else "Dark mode")

    def _toggle_theme(self):
        mode = theme.LIGHT_MODE if theme.is_dark() else theme.DARK_MODE
        theme.apply(mode)
        theme.save_mode(mode)
        self._retheme()

    def _retheme(self):
        """The stylesheet covers nearly everything. This is what paints its own colour."""
        self._name_theme_button()
        for row, glyph in self.nav_glyphs.items():
            self.nav.item(row).setIcon(icon(glyph))
        for row, glyph in self.inv_glyphs.items():
            self.inv_nav.item(row).setIcon(icon(glyph))
        # A page built later takes the new colours as it's built.
        self._call_built("of_page", "retheme")
        self._call_built("orders_page", "refresh")
        self._call_built("po_page", "retheme")

    def _change_password(self):
        ChangePasswordDialog(self.conn, self.user).exec()

    def _logout(self):
        guarded(self, backup.auto_backup, self.conn)
        if self.on_logout:
            self.on_logout()

    def _restored(self):
        if self.on_restored:
            self.on_restored()

    def closeEvent(self, event):
        try:
            backup.auto_backup(self.conn)
        except Exception as exc:  # A failed exit backup must never trap someone in the program, but they should know.
            error(self, f"The closing backup could not be made: {exc}")
        super().closeEvent(event)
