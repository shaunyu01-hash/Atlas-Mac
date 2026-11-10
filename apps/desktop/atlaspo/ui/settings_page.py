"""Settings: approval policy, company and tax, users, backup and data. Administrators only."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLineEdit, QListWidget, QListWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)

import shutil

from .. import auth, backup, db, of_layout, orders
from ..db import POError, data_dir, get_setting, set_settings
from ..money import fmt_plain, parse_percent, parse_pesos
from .common import (smart_columns, button, confirm, error, fill_table, guarded, info, label, left_aligned, make_table, narrow, select_data, selected_key, set_widths, show_people)
from .. import masters
from . import theme
from .pages import page_layout

# Shown on the Users list beside a login saved before the rule against both roles.
CONFLICT_NOTE = "Buying and Accounting together: remove one"


class UserDialog(QDialog):
    def __init__(self, parent, branches: list, user: dict | None = None, on_save=None):
        super().__init__(parent)
        self.on_save = on_save
        self.setWindowTitle("Edit user" if user else "New user")
        form = QFormLayout(self)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.username, self.full_name, self.password = QLineEdit(), QLineEdit(), QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Username", self.username)
        form.addRow("Full name", self.full_name)
        # Shown beside the name, such as Warehouse Head or Purchasing Head. It changes nothing this person can do: the roles below do that.
        self.title = QLineEdit()
        self.title.setMaxLength(60)
        self.title.setPlaceholderText("Warehouse Head, Purchasing Staff, Accounting Head, ...")
        form.addRow("Job title", self.title)
        if user is None:
            form.addRow("First password", self.password)
        self.roles = {}
        for role, text in auth.ROLES.items():
            self.roles[role] = QCheckBox(text)
            form.addRow("" if self.roles and role != "ADMIN" else "Roles", self.roles[role])
        self.branch = QComboBox()
        self.branch.addItem("None", None)
        for b in branches:
            self.branch.addItem(f"{b['code']} {b['name']}", b["code"])
        form.addRow("Receiver's branch", self.branch)
        form.addRow("", label("A receiver only sees the Receiving screen, for this branch.", "Muted"))
        self.initials = QLineEdit()
        self.initials.setMaxLength(auth.MAX_INITIALS)
        self.initials.setPlaceholderText(f"From the name: {user['chip']}" if user else "From the name")
        form.addRow("Initials", self.initials)
        self.color = QComboBox()
        if user is None:
            self.color.addItem("Automatic", None)
        for i, (name, value) in enumerate(theme.PERSON_COLORS):
            swatch = QPixmap(14, 14)
            swatch.fill(QColor(value))
            self.color.addItem(QIcon(swatch), name, i)
        form.addRow("Color", self.color)
        form.addRow("", label("These mark what this person counted. Hover shows their full name.", "Muted"))
        self.active = QCheckBox("Account is active")
        self.active.setChecked(True)
        form.addRow("", self.active)
        if user:
            self.username.setText(user["username"])
            self.username.setEnabled(False)
            self.full_name.setText(user["full_name"])
            self.title.setText(user.get("title", ""))
            self.active.setChecked(user["active"])
            for r in user["roles"]:
                self.roles[r].setChecked(True)
            self.branch.setCurrentIndex(max(self.branch.findData(user["branch_code"]), 0))
            self.initials.setText(user["initials"])
            self.color.setCurrentIndex(self.color.findData(user["color"]))
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        # Saved from inside the dialog: if the save is refused, the message shows and everything typed stays for correcting.
        self.buttons.accepted.connect(lambda: guarded(self, self._save))
        self.buttons.rejected.connect(self.reject)
        form.addRow(self.buttons)

    def _save(self):
        if self.on_save is not None:
            self.on_save(self)
        self.accept()

    def chosen_roles(self) -> list[str]:
        return [r for r, w in self.roles.items() if w.isChecked()]


class SettingsPage(QWidget):
    changed = Signal()
    restored = Signal()

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        root, _ = page_layout(self, "Settings")
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.addTab(self._policy_tab(), "Approval policy")
        tabs.addTab(self._company_tab(), "Company")
        tabs.addTab(self._layout_tab(), "Order Form layout")
        tabs.addTab(self._users_tab(), "Users")
        tabs.addTab(self._data_tab(), "Backup and data")
        root.addWidget(tabs, 1)
        self.refresh()

    def _policy_tab(self) -> QWidget:
        host = QWidget()
        form = QFormLayout(host)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.p_enabled = QCheckBox("Order forms above the limit need approval")
        self.p_threshold = QLineEdit()
        self.p_threshold.setMaximumWidth(180)
        self.p_compare = QComboBox()
        self.p_compare.addItem("Strictly greater than the limit", "GT")
        self.p_compare.addItem("At the limit or above", "GTE")
        self.p_basis = QComboBox()
        for code, text in orders.APPROVAL_BASES.items():
            self.p_basis.addItem(text, code)
        self.p_approvers = QListWidget()
        self.p_approvers.setFixedHeight(108)
        self.p_self = QCheckBox("Allow the person who prepared an OF to approve it (exception)")
        form.addRow("", self.p_enabled)
        form.addRow("Limit", self.p_threshold)
        form.addRow("Comparison", self.p_compare)
        form.addRow("Compare the limit with", self.p_basis)
        form.addRow("Approvers", self.p_approvers)
        hint = label("Tick everyone who can approve. Any one of them is enough. The list shows active users with the Approver or Administrator role.", "Muted")
        hint.setWordWrap(True)
        form.addRow("", hint)
        form.addRow("", self.p_self)
        form.addRow("", left_aligned(button("Save policy", self.save_policy, "Primary")))
        note = label("Comparing with the total OF stops a large order slipping through as several small branch POs. "
                     "Turning the policy off keeps all approval history. Every change here goes in the audit log.", "Muted")
        note.setWordWrap(True)
        form.addRow("", note)
        return narrow(host)

    def _company_tab(self) -> QWidget:
        host = QWidget()
        form = QFormLayout(host)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.c = {k: QLineEdit() for k in ("name", "address", "telephone", "email", "tin", "warehouse_name", "warehouse_address")}
        form.addRow("Company name", self.c["name"])
        form.addRow("Address", self.c["address"])
        form.addRow("Telephone", self.c["telephone"])
        form.addRow("Email", self.c["email"])
        form.addRow("TIN", self.c["tin"])
        form.addRow("Main warehouse", self.c["warehouse_name"])
        form.addRow("Warehouse address", self.c["warehouse_address"])
        self.vat_rate = QLineEdit()
        self.vat_rate.setMaximumWidth(100)
        self.vat_default = QComboBox()
        self.vat_default.addItem("VAT included in costs", "INCLUSIVE")
        self.vat_default.addItem("Add VAT on top", "EXCLUSIVE")
        self.vat_default.addItem("No VAT", "NONE")
        self.logo_note = label("", "Muted")
        logo_row = QHBoxLayout()
        logo_row.addWidget(button("Choose logo", self.choose_logo))
        logo_row.addWidget(self.logo_note, 1)
        form.addRow("Logo", logo_row)
        form.addRow("VAT rate %", self.vat_rate)
        form.addRow("VAT on new OFs", self.vat_default)
        # No order carries VAT for now, so there's nothing to set.
        form.setRowVisible(self.vat_rate, False)
        form.setRowVisible(self.vat_default, False)
        form.addRow("", left_aligned(button("Save", self.save_company, "Primary")))
        form.addRow("", label("These details print at the top of the supplier's PO and the warehouse OF.", "Muted"))
        return narrow(host)

    def _layout_tab(self) -> QWidget:
        host = QWidget()
        col = QVBoxLayout(host)
        note = label("Drag to rearrange, top to bottom, then save. This changes the Order Form screen for everyone, and only an administrator can do it. "
                     "It never changes the data or the printed documents.", "Muted")
        note.setWordWrap(True)
        col.addWidget(note)
        lists = QHBoxLayout()
        self.layout_fields, self.layout_columns = QListWidget(), QListWidget()
        for title, w in (("Fields at the top, left to right", self.layout_fields), ("Grid columns, left to right", self.layout_columns)):
            w.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
            w.setMaximumWidth(320)
            box = QVBoxLayout()
            box.addWidget(label(title, "PanelTitle"))
            box.addWidget(w, 1)
            lists.addLayout(box)
        lists.addStretch(1)
        col.addLayout(lists, 1)
        bar = QHBoxLayout()
        bar.addWidget(button("Save layout", self.save_layout, "Primary"))
        bar.addWidget(button("Back to the standard layout", self.reset_layout))
        bar.addStretch(1)
        col.addLayout(bar)
        return host

    def _fill_layout(self, fields: list[str], columns: list[str]):
        for w, keys, names in ((self.layout_fields, fields, of_layout.FIELDS), (self.layout_columns, columns, {})):
            w.clear()
            for key in keys:
                item = QListWidgetItem(names.get(key, key))
                item.setData(Qt.ItemDataRole.UserRole, key)
                w.addItem(item)

    def _layout_keys(self, w: QListWidget) -> list[str]:
        return [w.item(i).data(Qt.ItemDataRole.UserRole) for i in range(w.count())]

    def save_layout(self):
        of_layout.save(self.conn, self.user, self._layout_keys(self.layout_fields), self._layout_keys(self.layout_columns))
        self.changed.emit()
        info(self, "Saved", "The Order Form layout was saved.")

    def reset_layout(self):
        self._fill_layout(list(of_layout.FIELDS), of_layout.COLUMNS)
        self.save_layout()

    def _users_tab(self) -> QWidget:
        host = QWidget()
        col = QVBoxLayout(host)
        bar = QHBoxLayout()
        bar.addWidget(button("New user", self.new_user))
        bar.addWidget(button("Edit user", self.edit_user))
        bar.addWidget(button("Reset password", self.reset_password))
        bar.addStretch(1)
        col.addLayout(bar)
        # Logins saved before the rule can still hold Buying and Accounting together. They keep working until an admin removes one role.
        self.role_conflicts = label("", "NoticeBad")
        self.role_conflicts.setWordWrap(True)
        self.role_conflicts.setVisible(False)
        col.addWidget(self.role_conflicts)
        self.users = make_table(["Username", "Full name", "Job title", "Roles", "Status", "Chip"], stretch=3)
        set_widths(self.users, {0: 140, 1: 200, 2: 150, 4: 80, 5: 70})
        smart_columns(self.users, f"users-{self.user.username}")
        col.addWidget(self.users, 1)
        return host

    def _data_tab(self) -> QWidget:
        host = QWidget()
        col = QVBoxLayout(host)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.folder = QLineEdit()
        self.folder.setPlaceholderText("Default: the Backups folder beside the database")
        pick = QHBoxLayout()
        pick.addWidget(self.folder, 1)
        pick.addWidget(button("Choose", self._choose_folder))
        pick.addWidget(button("Save", self.save_backup_settings))
        form.addRow("Backup folder", pick)
        self.keep = QLineEdit()
        self.keep.setMaximumWidth(80)
        form.addRow("Keep daily copies for (days)", self.keep)
        # Read only: every computer should show the same folder here.
        self.data_folder = QLineEdit(str(data_dir()))
        self.data_folder.setReadOnly(True)
        form.insertRow(0, "Data folder", self.data_folder)
        col.addLayout(form)
        col.addWidget(label("A daily copy is made when the program opens and closes. Another drive is safer than this PC's main drive.", "Muted"))
        bar = QHBoxLayout()
        bar.addWidget(button("Back up now", self.backup_now, "Primary"))
        bar.addSpacing(24)  # Room between the everyday button and the one that replaces the data.
        bar.addWidget(button("Restore selected", self.restore_selected, "Danger"))
        bar.addWidget(button("Restore from a file", self.restore_file))
        bar.addWidget(button("Check database health", self.check_health))
        bar.addStretch(1)
        col.addLayout(bar)
        self.backups = make_table(["Backup", "Saved", "Size"], numeric=(2,), stretch=0)
        # Wide enough for the whole date and time.
        fm = self.backups.fontMetrics()
        set_widths(self.backups, {1: fm.horizontalAdvance("Sep 30, 2026 23:59") + 40, 2: fm.horizontalAdvance("9,999.9 MB") + 40})
        col.addWidget(self.backups, 1)
        return host

    def refresh(self):
        p = orders.policy(self.conn)
        self.p_enabled.setChecked(p["enabled"])
        self.p_threshold.setText(fmt_plain(p["threshold_c"]))
        select_data(self.p_compare, p["comparison"])
        select_data(self.p_basis, p["basis"])
        self.p_self.setChecked(p["allow_self_approval"])
        self.p_approvers.clear()
        all_users = auth.list_users(self.conn)
        for u in all_users:
            if u["active"] and ("APPROVER" in u["roles"] or "ADMIN" in u["roles"]):
                item = QListWidgetItem(u["full_name"])
                item.setData(Qt.ItemDataRole.UserRole, u["id"])
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if u["id"] in p["approver_user_ids"] else Qt.CheckState.Unchecked)
                self.p_approvers.addItem(item)
        self._fill_layout(of_layout.field_order(self.conn), of_layout.column_order(self.conn))
        for k, w in self.c.items():
            w.setText(get_setting(self.conn, f"company.{k}"))
        logo = get_setting(self.conn, "company.logo_file")
        self.logo_note.setText(logo if logo else "None yet. PDFs print the company name without one.")
        self.vat_rate.setText(str(int(get_setting(self.conn, "tax.vat_rate_bp") or 1200) / 100).rstrip("0").rstrip("."))
        select_data(self.vat_default, get_setting(self.conn, "tax.default_vat_mode"))
        conflicted = {u["id"] for u in all_users if auth.role_conflict(u["roles"])}
        fill_table(self.users, [[u["username"], u["full_name"], u["title"],
                                 ", ".join(auth.ROLES[r] for r in u["roles"]) + (f" ({CONFLICT_NOTE})" if u["id"] in conflicted else ""),
                                 "Active" if u["active"] else "Inactive", u["full_name"]] for u in all_users],
                   keys=[u["id"] for u in all_users])
        # Found by key: the list may have been re-sorted by a column the admin clicked.
        for row in range(self.users.rowCount()):
            if self.users.item(row, 0).data(Qt.ItemDataRole.UserRole) in conflicted:
                cell = self.users.item(row, 3)
                cell.setForeground(QColor(theme.current.RED))
                cell.setToolTip(auth.ROLE_CONFLICT)
        show_people(self.users, 5, auth.chips(self.conn), [u["id"] for u in all_users])
        n = len(conflicted)
        self.role_conflicts.setText(f"{n} login{'s' if n != 1 else ''} {'has' if n == 1 else 'have'} Buying and Accounting together. Open {'it' if n == 1 else 'each one'} and remove one role.")
        self.role_conflicts.setVisible(bool(n))
        self.folder.setText(get_setting(self.conn, "backup.folder"))
        self.keep.setText(get_setting(self.conn, "backup.keep_days"))
        self._load_backups()

    def _load_backups(self):
        items = backup.list_backups(self.conn)
        fill_table(self.backups, [[b["name"], datetime.fromtimestamp(b["modified"]).strftime("%b %d, %Y %H:%M"), f"{b['size'] / 1048576:,.1f} MB"] for b in items],
                   keys=[str(b["path"]) for b in items])

    def save_policy(self):
        auth.require(self.user, "settings.edit")
        threshold = parse_pesos(self.p_threshold.text())
        rows = (self.p_approvers.item(i) for i in range(self.p_approvers.count()))
        approvers = [r.data(Qt.ItemDataRole.UserRole) for r in rows if r.checkState() == Qt.CheckState.Checked]
        if self.p_enabled.isChecked() and not approvers:
            raise POError("Tick at least one approver, or turn the policy off.")
        set_settings(self.conn, self.user, {
            "approval.enabled": "1" if self.p_enabled.isChecked() else "0", "approval.threshold_c": str(threshold),
            "approval.comparison": self.p_compare.currentData(), "approval.basis": self.p_basis.currentData(), "approval.approver_user_ids": ",".join(map(str, approvers)),
            "approval.allow_self_approval": "1" if self.p_self.isChecked() else "0"})
        self.changed.emit()
        info(self, "Saved", "The approval policy was saved.")

    def save_company(self):
        auth.require(self.user, "settings.edit")
        rate = parse_percent(self.vat_rate.text())
        if not 0 <= rate <= 10000:
            raise POError("The VAT rate must be from 0 to 100%.")
        set_settings(self.conn, self.user, {**{f"company.{k}": w.text().strip() for k, w in self.c.items()},
                                            "tax.vat_rate_bp": str(rate), "tax.default_vat_mode": self.vat_default.currentData()})
        info(self, "Saved", "Company details were saved.")

    def choose_logo(self):
        auth.require(self.user, "settings.edit")
        path, _ = QFileDialog.getOpenFileName(self, "Choose the company logo", "", "Images (*.png *.jpg *.jpeg)")
        if not path:
            return
        name = "company-logo" + Path(path).suffix.lower()
        shutil.copy2(path, data_dir() / name)
        set_settings(self.conn, self.user, {"company.logo_file": name})
        self.refresh()

    def new_user(self):
        def save(d):
            auth.create_user(self.conn, self.user, d.username.text(), d.full_name.text(), d.password.text(), d.chosen_roles(), branch_code=d.branch.currentData(), initials=d.initials.text(), color=d.color.currentData(), title=d.title.text())
        d = UserDialog(self, masters.list_branches(self.conn, active_only=True), on_save=save)
        if d.exec():
            self.refresh()

    def edit_user(self):
        key = selected_key(self.users)
        if not key:
            raise POError("Select a user first.")
        current = next(u for u in auth.list_users(self.conn) if u["id"] == key)
        def save(d):
            auth.update_user(self.conn, self.user, key, d.full_name.text(), d.chosen_roles(), d.active.isChecked(), d.branch.currentData(), d.initials.text(), d.color.currentData(), d.title.text())
        d = UserDialog(self, masters.list_branches(self.conn, active_only=True), current, on_save=save)
        if d.exec():
            self.refresh()

    def reset_password(self):
        key = selected_key(self.users)
        if not key:
            raise POError("Select a user first.")
        # Hidden as it's typed, and kept exactly as typed: spaces at either end are part of the password.
        new, ok = QInputDialog.getText(self, "Reset password", "New password. The user must change it at next login.", QLineEdit.EchoMode.Password)
        if ok and new:
            auth.reset_password(self.conn, self.user, key, new)
            info(self, "Done", "The password was reset.")

    def _choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Choose the backup folder")
        if path:
            self.folder.setText(path)

    def save_backup_settings(self):
        auth.require(self.user, "settings.edit")
        keep = self.keep.text().strip()
        if not keep.isdigit() or int(keep) < 1:
            raise POError("Keep days must be a whole number of at least 1.")
        set_settings(self.conn, self.user, {"backup.folder": self.folder.text().strip(), "backup.keep_days": keep})
        self._load_backups()

    def backup_now(self):
        path = backup.manual_backup(self.conn, self.user)
        self._load_backups()
        info(self, "Backed up", f"Saved to {path}")

    def _restore(self, source):
        if not confirm(self, "Restore backup", "This replaces all current PO data with the backup. The current data is saved first. Continue?", "Restore"):
            return
        safety = backup.restore(self.conn, self.user, source, db.db_path())
        info(self, "Restored", f"Restored. The earlier data was saved as {safety.name}. The program will now reopen the database; please log in again.")
        self.restored.emit()

    def restore_selected(self):
        key = selected_key(self.backups)
        if not key:
            raise POError("Select a backup first.")
        self._restore(key)

    def restore_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a backup file", "", "PO backup (*.sqlite)")
        if path:
            self._restore(path)

    def check_health(self):
        problems = backup.integrity_report(self.conn)
        if problems:
            error(self, "Problems found:\n" + "\n".join(problems[:20]))
        else:
            info(self, "Healthy", "The database passed all checks, and every PO's total matches its rows.")
