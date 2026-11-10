"""Users, passwords, roles and permissions."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import unicodedata
from dataclasses import dataclass

from .db import POError, PermissionDenied, audit, set_settings, timestamp, tx

ROLES = {"ADMIN": "Administrator", "BUYER": "Purchaser", "APPROVER": "Approver", "VIEWER": "View only", "RECEIVER": "Receiver", "ACCOUNTING": "Accounting", "WAREHOUSE": "Warehouse"}

PERMISSIONS = {
    "of.edit": {"ADMIN", "BUYER"},
    "of.cancel": {"ADMIN", "BUYER"},
    "of.amend": {"ADMIN", "BUYER"},
    "of.approve": {"ADMIN", "APPROVER"},
    "of.generate": {"ADMIN", "BUYER"},
    "branch.manage": {"ADMIN"},
    "po.edit": {"ADMIN", "BUYER"},
    "po.issue": {"ADMIN", "BUYER"},
    "po.cancel": {"ADMIN", "BUYER"},
    "po.amend": {"ADMIN", "BUYER"},
    "po.approve": {"ADMIN", "APPROVER"},
    "master.edit": {"ADMIN", "BUYER"},
    # Purchasers set selling prices, for what was bought and for forfeited goods.
    "price.edit": {"ADMIN", "BUYER"},
    # Purchasers close POs that won't receive anything more.
    "po.close": {"ADMIN", "BUYER"},
    # Suppliers carry the money terms every payment uses, so only Admin adds or changes one. Purchasers look them up.
    "supplier.edit": {"ADMIN"},
    "settings.edit": {"ADMIN"},
    "users.edit": {"ADMIN"},
    "backup.manage": {"ADMIN"},
    # Only receivers count. An Admin can see what was ordered, so counting would no longer be blind.
    "receiving.count": {"RECEIVER"},
    "receiving.review": {"ADMIN", "BUYER", "APPROVER", "VIEWER", "ACCOUNTING", "WAREHOUSE"},
    # The Warehouse Head and warehouse receivers record goods arriving at the main warehouse. That's all the warehouse does here.
    "warehouse.receive": {"WAREHOUSE"},
    # Goods coming back on a claim stub or item pass are handed to the supplier at the warehouse. The warehouse records it; the office still can, for now.
    "goods.release": {"ADMIN", "BUYER", "WAREHOUSE"},
    "dm.issue": {"ADMIN", "BUYER"},
    # Cancelling a debit memo, claim stub or item pass puts money or goods back, so it's management only.
    "dm.cancel": {"ADMIN", "APPROVER"},
    # Counting a delivery again after it was accepted, or after two counts didn't match, is the home office's call.
    "receiving.reopen": {"ADMIN", "APPROVER"},
    # Until branches count in the program, Admin records a delivery received in full outside the program.
    "po.receive_outside": {"ADMIN"},
    "payment.edit": {"ADMIN", "ACCOUNTING"},
    "payment.view": {"ADMIN", "ACCOUNTING", "APPROVER", "VIEWER"},
    # Accounting does the payments, and Admin or management gives the final check on any amount that differs.
    "payment.signoff": {"ADMIN", "APPROVER"},
    # Damage a branch reports after its count: Accounting or management approves it, as they print the claim stubs.
    "cs.approve_request": {"ADMIN", "APPROVER", "ACCOUNTING"},
    # Inventory. The books, costs and stock values are for the office. Receivers and the warehouse count stock blind, so they never see them.
    "inventory.view": {"ADMIN", "BUYER", "APPROVER", "VIEWER", "ACCOUNTING"},
    # Starting and cancelling a stock count, and reading its results.
    "count.manage": {"ADMIN", "BUYER", "APPROVER"},
    # Only the people at the place count its stock, blind, two of them.
    "stock.count": {"RECEIVER", "WAREHOUSE"},
    # Posting a count, approving an adjustment and cancelling a transfer change the books, so they're management's.
    "stock.post": {"ADMIN", "APPROVER"},
    "adjust.prepare": {"ADMIN", "BUYER", "RECEIVER", "WAREHOUSE"},
    "transfer.send": {"ADMIN", "BUYER", "RECEIVER", "WAREHOUSE"},
    "transfer.receive": {"ADMIN", "RECEIVER", "WAREHOUSE"},
    # Branch staff record what their branch sold; the warehouse records forfeited goods it sells. Cancelling recorded sales is management's.
    "sales.record": {"ADMIN", "BUYER", "RECEIVER", "WAREHOUSE"},
    # Purchasers set the minimum and target stock of a product at a branch, over what the program works out.
    "reorder.edit": {"ADMIN", "BUYER"},
}

_N, _R, _P = 2**14, 8, 1
MIN_PASSWORD = 8
RECEIVER_COLORS = (0, 1)  # Blue and orange in theme.PERSON_COLORS.
CHIP_COLORS = 12
MAX_INITIALS = 4
MAX_NAME = 200
_NAME_SUFFIXES = {"JR", "SR", "II", "III", "IV"}
# Checked when nobody has the username typed, so a wrong username takes as long as a wrong password and doesn't give away who has a login.
_DUMMY_HASH = f"scrypt${_N}${_R}${_P}$dW5rbm93bi11c2VyLXNhbHQ=$" + base64.b64encode(bytes(32)).decode()
LOGIN_REFUSED = "Wrong username or password, or the account is turned off."


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(key).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, key = stored.split("$")
        expected = base64.b64decode(key)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


@dataclass(frozen=True)
class User:
    id: int
    username: str
    full_name: str
    roles: frozenset[str]
    must_change_password: bool = False
    branch_code: int | None = None
    # A job title such as Warehouse Head or Purchasing Head. Shown beside the name; it changes no permission.
    title: str = ""

    @property
    def display(self) -> str:
        return self.full_name or self.username

    def can(self, permission: str) -> bool:
        return bool(self.roles & PERMISSIONS[permission])


@dataclass(frozen=True)
class Chip:
    """A person at a glance: their initials on their own color, with the full name on hover."""
    initials: str
    color: int
    name: str


def require(user: User, permission: str) -> None:
    if not user.can(permission):
        raise PermissionDenied(f"{user.display} does not have permission for this ({permission}).")


def _load(conn, row) -> User:
    roles = frozenset(r["role"] for r in conn.execute("SELECT role FROM user_roles WHERE user_id = ?", (row["id"],)))
    branch = conn.execute("SELECT branch_code FROM user_branches WHERE user_id = ?", (row["id"],)).fetchone()
    return User(row["id"], row["username"], row["full_name"], roles, bool(row["must_change_password"]), branch["branch_code"] if branch else None, get_title(conn, row["id"]))


def get_title(conn, user_id: int) -> str:
    row = conn.execute("SELECT title FROM user_titles WHERE user_id = ?", (user_id,)).fetchone()
    return row["title"] if row else ""


def _set_title(conn, actor_id: int | None, user_id: int, title: str | None) -> tuple[str, str]:
    """Leaving the title out keeps it. Returns (old, new)."""
    old = get_title(conn, user_id)
    if title is None:
        return old, old
    title = " ".join(title.split())[:60]
    if title:
        conn.execute("INSERT INTO user_titles(user_id, title, updated_by, updated_at) VALUES (?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET title = excluded.title,"
                     " updated_by = excluded.updated_by, updated_at = excluded.updated_at", (user_id, title, actor_id, timestamp()))
    else:
        conn.execute("UPDATE user_titles SET title = '', updated_by = ?, updated_at = ? WHERE user_id = ?", (actor_id, timestamp(), user_id))
    return old, title


def job_line(user: User) -> str:
    """What the sidebar shows under the name: the job title, or the roles when there's none."""
    return user.title or ", ".join(ROLES[r] for r in sorted(user.roles))


def warehouse_login_missing(conn) -> bool:
    """Nobody active can record goods at the Central Warehouse, so claim stubs, item passes and deliveries can't be received there. Admin is told until one exists."""
    return conn.execute("SELECT 1 FROM users u JOIN user_roles r ON r.user_id = u.id WHERE r.role = 'WAREHOUSE' AND u.active = 1").fetchone() is None


def get_user(conn, user_id: int) -> User | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _load(conn, row) if row else None


def has_users(conn) -> bool:
    return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise POError(f"The password needs at least {MIN_PASSWORD} characters.")


def _name_key(text) -> str:
    """Usernames are the same whatever the case, in any language. SQLite on its own only folds A to Z."""
    return unicodedata.normalize("NFC", str(text or "").strip()).casefold()


def _check_names(username: str, full_name: str) -> None:
    if len(username) > MAX_NAME or len(full_name) > MAX_NAME:
        raise POError(f"Keep the username and the full name to {MAX_NAME} characters each.")


def _clean_initials(initials: str) -> str:
    initials = initials.strip().upper()
    if len(initials) > MAX_INITIALS or (initials and not initials.isalnum()):
        raise POError(f"Initials are up to {MAX_INITIALS} letters or numbers.")
    return initials


def _check_color(color: int) -> None:
    if color not in range(CHIP_COLORS):
        raise POError("Choose one of the colors.")


def _free_color(conn, branch_code: int | None, user_id: int | None = None) -> int:
    """Every branch uses the same two colors, one for each receiver, because the initials already say who it is. A new receiver takes the one nobody active is using there. Anyone without a branch gets another color, the least used."""
    rows = conn.execute("SELECT c.user_id, c.color, b.branch_code FROM user_chips c JOIN users u ON u.id = c.user_id LEFT JOIN user_branches b ON b.user_id = c.user_id WHERE u.active = 1").fetchall()
    rows = [r for r in rows if r["user_id"] != user_id]
    if branch_code is None:
        return min(range(len(RECEIVER_COLORS), CHIP_COLORS), key=lambda color: (sum(r["color"] == color for r in rows), color))
    return min(RECEIVER_COLORS, key=lambda color: (sum(r["color"] == color and r["branch_code"] == branch_code for r in rows), color))


def _auto_initials(name: str, taken: set[str]) -> str:
    """First and last name, SY for Sam Yu. Two people at one branch can't look alike, so the second takes more of the last name, MRE after MR."""
    words = [w.upper() for w in re.findall(r"[^\W_]+", name)]
    while len(words) > 2 and words[-1] in _NAME_SUFFIXES:
        words.pop()
    if not words:
        return "?"
    start, rest = (words[0][0], words[-1]) if len(words) > 1 else ("", words[0])
    shortest = 1 if start else 2
    tries = [start + rest[:n] for n in range(shortest, len(rest) + 1)] or [start + rest]
    base = tries[0]
    tries += [f"{base}{n}" for n in range(2, len(taken) + 3)]
    return next(t for t in tries if t not in taken)


def chips(conn) -> dict[int, Chip]:
    """Everyone's chip, by user id. Initials someone typed are kept. Everyone else's come from their name, oldest login first, so nobody's change when a new person joins."""
    rows = conn.execute("SELECT u.id, u.username, u.full_name, c.initials, c.color, b.branch_code FROM users u JOIN user_chips c ON c.user_id = u.id"
                        " LEFT JOIN user_branches b ON b.user_id = u.id ORDER BY u.id").fetchall()
    taken: dict[int | None, set[str]] = {}
    for r in rows:
        if r["initials"]:
            taken.setdefault(r["branch_code"], set()).add(r["initials"])
    out = {}
    for r in rows:
        name = r["full_name"] or r["username"]
        initials = r["initials"]
        if not initials:
            initials = _auto_initials(name, taken.setdefault(r["branch_code"], set()))
            taken[r["branch_code"]].add(initials)
        out[r["id"]] = Chip(initials, r["color"], name)
    return out


def _check_initials_free(conn, user_id: int) -> None:
    """Typed initials have to be someone's alone at their branch. Automatic ones already step around them."""
    row = conn.execute(
        "SELECT o.full_name FROM user_chips mine LEFT JOIN user_branches mb ON mb.user_id = mine.user_id"
        " JOIN user_chips other ON other.initials = mine.initials AND other.user_id <> mine.user_id JOIN users o ON o.id = other.user_id"
        " LEFT JOIN user_branches ob ON ob.user_id = other.user_id WHERE mine.user_id = ? AND mine.initials <> '' AND ob.branch_code IS mb.branch_code", (user_id,)).fetchone()
    if row:
        raise POError(f"{row['full_name']} already uses those initials at this branch. Choose others.")


def _insert_user(conn, username, full_name, password, roles, must_change, branch_code=None, initials="", color=None) -> int:
    username = username.strip()
    if not username or not full_name.strip():
        raise POError("Enter a username and a full name.")
    _check_password(password)
    unknown = set(roles) - set(ROLES)
    if unknown or not roles:
        raise POError("Choose at least one role.")
    _check_receiver_alone(roles)
    conflict = role_conflict(roles)
    if conflict:
        raise POError(conflict)
    _check_names(username, full_name.strip())
    key = _name_key(username)
    if any(_name_key(r["username"]) == key for r in conn.execute("SELECT username FROM users")):
        raise POError(f"The username '{username}' is already taken.")
    cur = conn.execute(
        "INSERT INTO users(username, full_name, password_hash, must_change_password, created_at) VALUES (?,?,?,?,?)",
        (username, full_name.strip(), hash_password(password), int(must_change), timestamp()),
    )
    conn.executemany("INSERT INTO user_roles(user_id, role) VALUES (?,?)", [(cur.lastrowid, r) for r in roles])
    if color is None:
        color = _free_color(conn, branch_code)
    _check_color(color)
    conn.execute("INSERT INTO user_chips(user_id, initials, color) VALUES (?,?,?)", (cur.lastrowid, _clean_initials(initials), color))
    return cur.lastrowid


def _check_receiver_alone(roles) -> None:
    """Receivers count blind. Any other role would let them see what was ordered, so a receiver has no other role.
    The warehouse checks what suppliers deliver, so it has its own login too: the Warehouse Head and warehouse receivers never also order or pay."""
    if "RECEIVER" in roles and len(set(roles)) > 1:
        raise POError("A receiver can't have another role, because receivers count without seeing what was ordered. Give this person a separate login for other work.")
    if "WAREHOUSE" in roles and len(set(roles)) > 1:
        raise POError("A warehouse login can't have another role, because the warehouse checks what suppliers deliver. Give this person a separate login for other work.")


ROLE_CONFLICT = "Buying and Accounting can't be on the same login. Accounting doesn't buy stock. Remove one of the two."


def role_conflict(roles) -> str | None:
    """Accounting doesn't buy stock, so one login can't both buy and pay. Buying and Accounting together is refused.
    Approver with either one is left as it is: the owner hasn't decided what approving should cover. Logins saved before this keep their roles until an admin fixes them."""
    roles = set(roles or ())
    return ROLE_CONFLICT if {"BUYER", "ACCOUNTING"} <= roles else None


def _set_branch(conn, user_id: int, roles: list[str], branch_code: int | None) -> None:
    """A receiver belongs to one branch, which is what limits what they can open. Nobody else has one."""
    if "RECEIVER" in roles and not branch_code:
        raise POError("Choose the branch this receiver works at.")
    if "RECEIVER" in roles and not conn.execute("SELECT 1 FROM branches WHERE code = ?", (branch_code,)).fetchone():
        raise POError("Choose the branch this receiver works at.")
    conn.execute("DELETE FROM user_branches WHERE user_id = ?", (user_id,))
    if "RECEIVER" in roles:
        conn.execute("INSERT INTO user_branches(user_id, branch_code) VALUES (?,?)", (user_id, branch_code))


def create_first_admin(conn, username: str, full_name: str, password: str) -> User:
    """First run only. Also makes this person the approver until an admin chooses someone else."""
    with tx(conn):
        if has_users(conn):
            raise POError("An administrator already exists.")
        uid = _insert_user(conn, username, full_name, password, ["ADMIN", "APPROVER"], False)
        user = get_user(conn, uid)
        audit(conn, user, "USER_CREATE", "user", uid, summary=f"First administrator {username}")
        set_settings(conn, user, {"approval.approver_user_ids": str(uid)})
    return user


def create_user(conn, actor: User, username: str, full_name: str, password: str, roles: list[str],
                must_change: bool = True, branch_code: int | None = None, initials: str = "", color: int | None = None, title: str = "") -> User:
    require(actor, "users.edit")
    with tx(conn):
        uid = _insert_user(conn, username, full_name, password, roles, must_change, branch_code if "RECEIVER" in roles else None, initials, color)
        _set_branch(conn, uid, roles, branch_code)
        _check_initials_free(conn, uid)
        _set_title(conn, actor.id, uid, title)
        audit(conn, actor, "USER_CREATE", "user", uid, summary=f"Created {username}", new={"roles": sorted(roles), "branch_code": branch_code, "title": title.strip()})
    return get_user(conn, uid)


def update_user(conn, actor: User, user_id: int, full_name: str, roles: list[str], active: bool, branch_code: int | None = None,
                initials: str | None = None, color: int | None = None, title: str | None = None) -> None:
    """Leaving initials, color or title out keeps them. Empty initials go back to the ones from the person's name."""
    require(actor, "users.edit")
    full_name = str(full_name or "")
    if not full_name.strip():
        raise POError("Enter a full name.")
    _check_names("", full_name.strip())
    if not roles or set(roles) - set(ROLES):
        raise POError("Choose at least one role.")
    _check_receiver_alone(roles)
    # Turning a login off always works, so an admin can stop one saved with Buying and Accounting together before fixing its roles.
    conflict = role_conflict(roles) if active else None
    if conflict:
        raise POError(conflict)
    with tx(conn):
        old = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if old is None:
            raise POError("That user no longer exists.")
        old_roles = sorted(r["role"] for r in conn.execute("SELECT role FROM user_roles WHERE user_id=?", (user_id,)))
        losing_admin = "ADMIN" in old_roles and ("ADMIN" not in roles or not active)
        if losing_admin:
            others = conn.execute(
                "SELECT COUNT(*) FROM users u JOIN user_roles r ON r.user_id = u.id"
                " WHERE r.role='ADMIN' AND u.active=1 AND u.id<>?", (user_id,)).fetchone()[0]
            if not others:
                raise POError("Keep at least one active administrator.")
        conn.execute("UPDATE users SET full_name=?, active=? WHERE id=?", (full_name.strip(), int(active), user_id))
        conn.execute("DELETE FROM user_roles WHERE user_id=?", (user_id,))
        conn.executemany("INSERT INTO user_roles(user_id, role) VALUES (?,?)", [(user_id, r) for r in roles])
        old_branch = conn.execute("SELECT branch_code FROM user_branches WHERE user_id = ?", (user_id,)).fetchone()
        _set_branch(conn, user_id, roles, branch_code)
        new_branch = conn.execute("SELECT branch_code FROM user_branches WHERE user_id = ?", (user_id,)).fetchone()
        chip = conn.execute("SELECT initials, color FROM user_chips WHERE user_id = ?", (user_id,)).fetchone()
        new_initials = chip["initials"] if initials is None else _clean_initials(initials)
        # Moving to another branch takes whichever receiver's color is free there.
        moved = (old_branch["branch_code"] if old_branch else None) != (new_branch["branch_code"] if new_branch else None)
        new_color = color if color is not None else _free_color(conn, new_branch["branch_code"] if new_branch else None, user_id) if moved else chip["color"]
        _check_color(new_color)
        conn.execute("UPDATE user_chips SET initials = ?, color = ? WHERE user_id = ?", (new_initials, new_color, user_id))
        _check_initials_free(conn, user_id)
        old_title, new_title = _set_title(conn, actor.id, user_id, title)
        audit(conn, actor, "USER_UPDATE", "user", user_id, summary=f"Updated {old['username']}",
              previous={"full_name": old["full_name"], "roles": old_roles, "active": bool(old["active"]), "branch_code": old_branch["branch_code"] if old_branch else None,
                        "initials": chip["initials"], "color": chip["color"], "title": old_title},
              new={"full_name": full_name.strip(), "roles": sorted(roles), "active": active, "branch_code": new_branch["branch_code"] if new_branch else None,
                   "initials": new_initials, "color": new_color, "title": new_title})


def reset_password(conn, actor: User, user_id: int, new_password: str) -> None:
    require(actor, "users.edit")
    _check_password(new_password)
    with tx(conn):
        changed = conn.execute("UPDATE users SET password_hash=?, must_change_password=1 WHERE id=?",
                               (hash_password(new_password), user_id)).rowcount
        if not changed:
            raise POError("That user no longer exists.")
        audit(conn, actor, "PASSWORD_RESET", "user", user_id, summary="Password reset by administrator")


def change_own_password(conn, user: User, current: str, new_password: str) -> None:
    row = conn.execute("SELECT password_hash FROM users WHERE id=?", (user.id,)).fetchone()
    if not row or not verify_password(current, row["password_hash"]):
        raise POError("The current password is not correct.")
    if new_password == current:
        raise POError("Choose a new password.")
    _check_password(new_password)
    with tx(conn):
        conn.execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
                     (hash_password(new_password), user.id))
        audit(conn, user, "PASSWORD_CHANGE", "user", user.id, summary="Changed own password")


def login(conn, username: str, password: str) -> User:
    """Every refusal reads the same and takes about as long, so nobody can tell a wrong username, a wrong password or a turned-off account apart. Each one is logged."""
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
    right = verify_password(password, row["password_hash"] if row is not None else _DUMMY_HASH)
    if row is None or not right or not row["active"]:
        why = "Turned-off account" if row is not None and right else "Wrong username or password"
        with tx(conn):
            conn.execute(
                "INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary) VALUES (?,?,?,?,?)",
                (timestamp(), username.strip()[:60] or "?", "LOGIN_FAILED", "user", why),
            )
        raise POError(LOGIN_REFUSED)
    user = _load(conn, row)
    with tx(conn):
        audit(conn, user, "LOGIN", "user", user.id)
    return user


def list_users(conn) -> list[dict]:
    out = []
    everyone = chips(conn)
    roles: dict[int, list[str]] = {}
    for r in conn.execute("SELECT user_id, role FROM user_roles ORDER BY user_id, role"):
        roles.setdefault(r["user_id"], []).append(r["role"])
    branches: dict[int, int] = {}
    for r in conn.execute("SELECT user_id, branch_code FROM user_branches ORDER BY rowid"):
        branches.setdefault(r["user_id"], r["branch_code"])
    titles = {r["user_id"]: r["title"] for r in conn.execute("SELECT user_id, title FROM user_titles")}
    for u in conn.execute("SELECT u.*, c.initials, c.color FROM users u JOIN user_chips c ON c.user_id = u.id ORDER BY u.full_name"):
        out.append({"id": u["id"], "username": u["username"], "full_name": u["full_name"],
                    "active": bool(u["active"]), "roles": roles.get(u["id"], []), "branch_code": branches.get(u["id"]),
                    "initials": u["initials"], "color": u["color"], "chip": everyone[u["id"]].initials, "title": titles.get(u["id"], "")})
    return out
