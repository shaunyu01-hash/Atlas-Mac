"""Start the program: open the database, log in, show the main window."""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from . import auth, backup, db
from .ui import theme
from .ui import common
from .ui.common import app_icon
from .ui.main_window import ChangePasswordDialog, FirstRunDialog, LoginDialog, MainWindow


class Session:
    def __init__(self, app: QApplication):
        self.app, self.conn, self.window = app, None, None

    def start(self) -> bool:
        # A database that can't be opened (made by a newer version, damaged, or locked) is explained, not a silent exit.
        try:
            self.conn = db.open_database()
            problems = [r[0] for r in self.conn.execute("PRAGMA quick_check") if r[0] != "ok"]
        except db.POError as exc:
            self._close_quietly()
            QMessageBox.critical(None, "Can't open the data", str(exc))
            return False
        except Exception as exc:  # noqa: BLE001 (shown and written down, never swallowed)
            self._close_quietly()
            if isinstance(exc, db.sqlite3.OperationalError) and db._is_busy(exc):
                # Another computer sharing the data was saving at that moment: nothing is wrong, so no crash details.
                QMessageBox.critical(None, "Can't open the data", db.BUSY_MESSAGE)
                return False
            common.log_error(exc)
            QMessageBox.critical(None, "Can't open the data", f"The program couldn't open its data: {exc}\n\n"
                                 "The details are in errors.log in the data folder.")
            return False
        if problems:
            QMessageBox.critical(None, "Database problem",
                                 "The database failed its health check. Restore a backup before using it:\n" + "\n".join(problems[:5]))
        if not auth.has_users(self.conn):
            d = FirstRunDialog(self.conn)
            if not d.exec():
                return False
            user = d.user
        else:
            d = LoginDialog(self.conn)
            if not d.exec():
                return False
            user = d.user
        if user.must_change_password and not ChangePasswordDialog(self.conn, user, required=True).exec():
            return False
        user = auth.get_user(self.conn, user.id)
        try:
            # Several PCs log in one after another: a copy another PC made minutes ago is not made again.
            backup.auto_backup(self.conn, at_start=True)
        except db.POError as exc:
            QMessageBox.warning(None, "Backup", f"Today's backup could not be made: {exc}")
        self.window = MainWindow(self.conn, user, on_logout=self.restart, on_restored=self.restart_after_restore)
        self.window.show()
        return True

    def _close_quietly(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001 (already broken; the message that follows says why)
                pass
        self.conn = None

    def _begin_again(self):
        # Closing the only window must not end the program while the login dialog is coming up.
        self.app.setQuitOnLastWindowClosed(False)
        started = self.start()
        self.app.setQuitOnLastWindowClosed(True)
        if not started:
            self.app.quit()

    def restart(self):
        self.app.setQuitOnLastWindowClosed(False)
        self.window.close()
        self.window.deleteLater()
        self.conn.close()
        self._begin_again()

    def restart_after_restore(self):
        # restore() already closed the connection. Skip the closing backup on the old window.
        self.app.setQuitOnLastWindowClosed(False)
        self.window.hide()
        self.window.deleteLater()
        self._begin_again()


def install_error_handler() -> None:
    """An error outside a button, such as in a search box or a list, is shown and written to errors.log instead of vanishing."""
    sys.excepthook = common.excepthook


def main() -> int:
    install_error_handler()
    app = QApplication(sys.argv)
    app.setApplicationName("Atlas PO ERP")
    app.setWindowIcon(app_icon())
    theme.apply(theme.saved_mode())
    app.setQuitOnLastWindowClosed(True)
    session = Session(app)
    if not session.start():
        return 0
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
