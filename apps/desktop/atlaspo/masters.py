"""Supplier, branch and product masters."""
from __future__ import annotations

import difflib
import re
import unicodedata

from .auth import User, require
from .db import POError, audit, timestamp, tx
from .money import VAT_MODES, fmt_peso

SUPPLIER_FIELDS = ("code", "name", "registered_name", "address", "contact_person", "telephone", "email", "tin",
                   "payment_terms", "default_discount_bp", "vat_registered", "vat_mode", "status", "remarks", "sticker_rate_c", "term_discount_bp", "term_discount_days")
NO_TERMS = "None"
PAYMENT_TERMS = ("7D", "30D", "45D", "60D", "90D", NO_TERMS)
TERMS_MAX_LENGTH = 60
DEFAULT_STICKER_RATE_C = 50
# Safety limits only, set well past anything a business would use, so a typo can't save a number that breaks the program. The real limits are the owner's to choose.
MAX_TERM_DAYS = 3650
MAX_TERM_DISCOUNT_BP = 10000
MAX_STICKER_RATE_C = 100000
MAX_AMOUNT_C = 10**13
MAX_TEXT = 200
DAYS_MESSAGE = "Type a whole number of days, like 30."


def whole_number(value, message: str = DAYS_MESSAGE) -> int:
    """A whole number however it came in: 30, '30' or ' 30 '. Blank is 0. Anything else, such as '1.5' or 'abc', is refused with the message."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return 0
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value.strip()):
        return int(value.strip())
    raise POError(message)


def code_key(code) -> str:
    """How codes and usernames are compared: case doesn't matter in any language, so 'ñandu-1' and 'ÑANDU-1' are the same."""
    return unicodedata.normalize("NFC", str(code or "").strip()).casefold()


def check_length(value: str, what: str) -> None:
    if len(value) > MAX_TEXT:
        raise POError(f"Keep the {what} to {MAX_TEXT} characters.")


def clean_terms(value) -> str:
    """Days are written one way however they were typed, so 120, 120 d and 120 days are all 120D. Blank means None. Anything else is custom and stays as typed."""
    typed = " ".join(str(value or "").split())
    compact = typed.replace(" ", "").upper()
    days = re.fullmatch(r"(\d+)(D|DAY|DAYS)?", compact)
    if days:
        if int(days.group(1)) > MAX_TERM_DAYS:
            raise POError(f"Payment terms can be up to {MAX_TERM_DAYS:,} days.")
        return f"{int(days.group(1))}D"
    if not compact or compact == NO_TERMS.upper():
        return NO_TERMS
    if len(typed) > TERMS_MAX_LENGTH:
        raise POError(f"Keep payment terms to {TERMS_MAX_LENGTH} characters. Longer notes belong in Remarks.")
    return typed


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def find_similar_suppliers(conn, name: str, tin: str = "", exclude_id: int | None = None) -> list[dict]:
    """Possible duplicates: same TIN, or a very similar name."""
    target, tin_norm, out = _norm(name), _norm(tin), []
    for s in conn.execute("SELECT id, code, name, tin FROM suppliers"):
        if s["id"] == exclude_id:
            continue
        same_tin = tin_norm and _norm(s["tin"]) == tin_norm
        other = _norm(s["name"])
        close = target and other and (target == other or difflib.SequenceMatcher(None, target, other).ratio() >= 0.88)
        if same_tin or close:
            out.append(dict(s) | {"why": "same TIN" if same_tin else "similar name"})
    return out


def save_supplier(conn, user: User, data: dict, supplier_id: int | None = None, confirm_duplicate: bool = False) -> int:
    """Create or update. Raises DuplicateSupplier unless the person confirmed the warning. Admin only: a supplier's terms, discounts,
    stickers and VAT decide every payment to them, so Purchasers can look suppliers up but not add or change them."""
    require(user, "supplier.edit")
    d = {k: data.get(k, "") for k in SUPPLIER_FIELDS}
    d["code"] = str(d["code"]).strip().upper()
    d["name"] = str(d["name"]).strip()
    d["default_discount_bp"] = whole_number(d["default_discount_bp"], "Type the discount as a number, like 5%.")
    if not 0 <= d["default_discount_bp"] <= 10000:
        raise POError("A discount is from 0% to 100%.")
    d["sticker_rate_c"] = whole_number(d["sticker_rate_c"], "Type the sticker deduction as an amount, like 0.50.")
    if d["sticker_rate_c"] < 0:
        raise POError("A sticker deduction can't be negative.")
    if d["sticker_rate_c"] > MAX_STICKER_RATE_C:
        raise POError(f"A sticker deduction can be up to {fmt_peso(MAX_STICKER_RATE_C)} a piece.")
    d["term_discount_bp"] = whole_number(d["term_discount_bp"], "Type the term discount as a number, like 2%.")
    d["term_discount_days"] = whole_number(d["term_discount_days"])
    if d["term_discount_bp"] < 0 or d["term_discount_days"] < 0:
        raise POError("A term discount and its days can't be negative.")
    if d["term_discount_bp"] > MAX_TERM_DISCOUNT_BP:
        raise POError("A term discount can't be more than 100%.")
    if d["term_discount_days"] > MAX_TERM_DAYS:
        raise POError(f"Term discount days can be up to {MAX_TERM_DAYS:,}.")
    d["vat_registered"] = int(bool(d["vat_registered"]))
    d["vat_mode"] = d["vat_mode"] or "NONE"
    d["status"] = d["status"] or "ACTIVE"
    if not d["code"] or not d["name"]:
        raise POError("A supplier needs a code and a name.")
    if not re.fullmatch(r"[A-Z0-9]{1,4}", d["code"]):
        raise POError("A supplier code is up to 4 letters or numbers.")
    check_length(d["name"], "supplier name")
    check_length(str(d["registered_name"] or ""), "registered name")
    d["payment_terms"] = clean_terms(d["payment_terms"])
    if d["vat_mode"] not in VAT_MODES:
        raise POError("Choose a VAT setting.")
    if d["status"] not in ("ACTIVE", "INACTIVE"):
        raise POError("Choose whether the supplier is active.")
    with tx(conn):
        clash = conn.execute("SELECT id FROM suppliers WHERE code = ?", (d["code"],)).fetchone()
        if clash and clash["id"] != supplier_id:
            raise POError(f"Supplier code {d['code']} is already used.")
        if not confirm_duplicate:
            similar = find_similar_suppliers(conn, d["name"], d["tin"], supplier_id)
            if similar:
                raise DuplicateSupplier(similar)
        stamp = timestamp()
        if supplier_id is None:
            cols = ", ".join(SUPPLIER_FIELDS)
            marks = ", ".join("?" * len(SUPPLIER_FIELDS))
            cur = conn.execute(
                f"INSERT INTO suppliers({cols}, created_at, updated_at) VALUES ({marks}, ?, ?)",
                [d[k] for k in SUPPLIER_FIELDS] + [stamp, stamp],
            )
            audit(conn, user, "SUPPLIER_CREATE", "supplier", cur.lastrowid, summary=f"{d['code']} {d['name']}", new=d)
            return cur.lastrowid
        old = conn.execute("SELECT * FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
        if old is None:
            raise POError("That supplier no longer exists.")
        changed = {k: d[k] for k in SUPPLIER_FIELDS if str(old[k]) != str(d[k])}
        if changed:
            sets = ", ".join(f"{k} = ?" for k in changed)
            conn.execute(f"UPDATE suppliers SET {sets}, updated_at = ? WHERE id = ?",
                         [*changed.values(), stamp, supplier_id])
            audit(conn, user, "SUPPLIER_UPDATE", "supplier", supplier_id, summary=f"{d['code']} {d['name']}",
                  previous={k: old[k] for k in changed}, new=changed)
        return supplier_id


class DuplicateSupplier(POError):
    def __init__(self, matches: list[dict]):
        lines = "\n".join(f"  {m['code']}  {m['name']} ({m['why']})" for m in matches[:5])
        super().__init__(f"This may already exist:\n{lines}")
        self.matches = matches


def search_suppliers(conn, text: str = "", include_inactive: bool = True, limit: int = 500) -> list:
    where, args = [], []
    for like in (f"%{word}%" for word in text.split()):
        where.append("(code LIKE ? OR name LIKE ? OR registered_name LIKE ? OR tin LIKE ?)")
        args += [like] * 4
    if not include_inactive:
        where.append("status = 'ACTIVE'")
    sql = "SELECT * FROM suppliers" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY name LIMIT ?"
    return conn.execute(sql, [*args, limit]).fetchall()


def supplier_history(conn, supplier_id: int) -> list:
    return conn.execute(
        "SELECT p.id, p.po_number, p.po_date, p.status, p.branch_name AS branch, p.total_pieces_m, p.grand_total_c"
        " FROM purchase_orders p WHERE p.supplier_id = ? ORDER BY p.po_date DESC, p.id DESC", (supplier_id,)).fetchall()


def save_branch(conn, user: User, data: dict, code: int | None = None) -> int:
    """code is the existing branch's permanent code. Leave it out to add a branch with data['code']."""
    require(user, "branch.manage")
    name = str(data.get("name", "")).strip()
    kind = data.get("kind") or "BRANCH"
    short = str(data.get("short_code", "")).strip().upper()
    address = str(data.get("address", "")).strip()
    active = int(bool(data.get("active", True)))
    if not name:
        raise POError("A branch needs a name.")
    if kind not in ("BRANCH", "WAREHOUSE"):
        raise POError("A destination is a branch or a warehouse.")
    with tx(conn):
        if code is None:
            try:
                new_code = int(str(data.get("code", "")).strip())
            except ValueError:
                raise POError("A branch code is a whole number.") from None
            if new_code < 1:
                raise POError("A branch code is a whole number of at least 1.")
            if new_code > 999_999_999:
                raise POError("That branch code is too large.")
            if conn.execute("SELECT 1 FROM branches WHERE code = ?", (new_code,)).fetchone():
                raise POError(f"Branch code {new_code} is already used. Codes are permanent and never reused.")
            conn.execute("INSERT INTO branches(code, short_code, name, kind, address, active) VALUES (?,?,?,?,?,?)",
                         (new_code, short, name, kind, address, active))
            audit(conn, user, "BRANCH_CREATE", "branch", new_code, summary=f"{new_code} {name}")
            return new_code
        old = conn.execute("SELECT * FROM branches WHERE code = ?", (code,)).fetchone()
        if old is None:
            raise POError("That branch no longer exists.")
        conn.execute("UPDATE branches SET short_code=?, name=?, kind=?, address=?, active=? WHERE code=?",
                     (short, name, kind, address, active, code))
        changed = {"short_code": short, "name": name, "kind": kind, "address": address, "active": active}
        changed = {k: v for k, v in changed.items() if old[k] != v}
        if changed:
            action = "BRANCH_UPDATE"
            if "active" in changed:
                action = "BRANCH_REACTIVATE" if active else "BRANCH_DEACTIVATE"
            audit(conn, user, action, "branch", code, summary=f"{code} {name}",
                  previous={k: old[k] for k in changed}, new=changed)
        return code


def list_branches(conn, active_only: bool = False, branches_only: bool = False) -> list:
    where = [c for c, on in (("active = 1", active_only), ("kind = 'BRANCH'", branches_only)) if on]
    sql = "SELECT * FROM branches" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY kind, code"
    return conn.execute(sql).fetchall()


def list_departments(conn) -> list:
    return conn.execute("SELECT id, name FROM departments ORDER BY id").fetchall()


PRODUCT_FIELDS = ("item_code", "department_id", "description", "brand", "size", "color", "uom", "pieces_per_unit",
                  "box_size", "supplier_id", "supplier_item_code", "last_cost_c", "selling_c", "active")


def save_product(conn, user: User, data: dict, product_id: int | None = None) -> int:
    require(user, "master.edit")
    _check_product(conn, data)
    return _store_product(conn, user, data, product_id)


def _check_product(conn, data: dict) -> None:
    """What a person types on the Products screen. Rows an approved order adds were already checked on the order."""
    if data.get("department_id") and not conn.execute("SELECT 1 FROM departments WHERE id = ?", (whole_number(data["department_id"], "Choose a department."),)).fetchone():
        raise POError("Choose a department.")
    if data.get("supplier_id") and not conn.execute("SELECT 1 FROM suppliers WHERE id = ?", (whole_number(data["supplier_id"], "Choose a supplier."),)).fetchone():
        raise POError("Choose a supplier.")
    if data.get("active") not in (None, True, False, 0, 1):
        raise POError("A product is active or not.")
    check_length(str(data.get("item_code") or "").strip(), "item code")
    check_length(str(data.get("description") or "").strip(), "description")
    check_length(str(data.get("supplier_item_code") or ""), "supplier's item code")
    ppu = data.get("pieces_per_unit")
    if ppu is not None and str(ppu).strip() != "" and whole_number(ppu, "Pieces per unit is a whole number.") < 1:
        raise POError("Pieces per unit is at least 1.")
    for key, what in (("last_cost_c", "cost"), ("selling_c", "selling price")):
        if data.get(key) is None:
            continue
        amount = whole_number(data[key], f"Type the {what} as an amount, like 125.50.")
        if amount < 0:
            raise POError(f"A {what} can't be negative.")
        if amount > MAX_AMOUNT_C:
            raise POError(f"That {what} is too large. Check the amount.")


def _store_product(conn, user: User, data: dict, product_id: int | None = None) -> int:
    d = {k: data.get(k) for k in PRODUCT_FIELDS}
    d["item_code"] = str(d["item_code"] or "").strip()
    d["description"] = str(d["description"] or "").strip()
    d["uom"] = d["uom"] or "PC"
    d["pieces_per_unit"] = int(d["pieces_per_unit"] or 1)
    d["active"] = 1 if d["active"] is None else int(d["active"])
    for key in ("brand", "size", "color", "supplier_item_code"):
        d[key] = d[key] or ""
    if not d["item_code"] or not d["description"] or not d["department_id"]:
        raise POError("A product needs an item code, a description and a department.")
    with tx(conn):
        clash = next((r for r in _same_code(conn, d["item_code"]) if r["id"] != product_id), None)
        if clash:
            raise POError(f"Item code {d['item_code']} is already in the catalog.")
        stamp = timestamp()
        if product_id is None:
            cols = ", ".join(PRODUCT_FIELDS)
            marks = ", ".join("?" * len(PRODUCT_FIELDS))
            cur = conn.execute(f"INSERT INTO products({cols}, created_at, updated_at) VALUES ({marks}, ?, ?)",
                               [d[k] for k in PRODUCT_FIELDS] + [stamp, stamp])
            audit(conn, user, "PRODUCT_CREATE", "product", cur.lastrowid, summary=f"{d['item_code']} {d['description']}")
            return cur.lastrowid
        old = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        if old is None:
            raise POError("That product no longer exists.")
        changed = {k: d[k] for k in PRODUCT_FIELDS if old[k] != d[k]}
        if changed:
            sets = ", ".join(f"{k} = ?" for k in changed)
            conn.execute(f"UPDATE products SET {sets}, updated_at = ? WHERE id = ?", [*changed.values(), stamp, product_id])
            action = "PRODUCT_COST_CHANGE" if "last_cost_c" in changed else "PRODUCT_UPDATE"
            audit(conn, user, action, "product", product_id, summary=d["item_code"],
                  previous={k: old[k] for k in changed}, new=changed)
        return product_id


def _same_code(conn, code: str) -> list:
    """Catalog products whose item code is this one, whatever the case. SQLite only folds A to Z, so the comparison is done here."""
    key = code_key(code)
    sql = ("SELECT p.id, p.item_code, s.id AS owner_id, s.code AS owner_code, s.name AS owner_name FROM products p"
           " LEFT JOIN suppliers s ON s.id = p.supplier_id")
    if str(code).isascii() and key.isascii():
        # Only an exact match, or a code with letters outside A to Z, can fold to the same thing.
        rows = conn.execute(sql + " WHERE upper(p.item_code) = upper(?) OR p.item_code GLOB '*[^ -~]*' ORDER BY p.id", (str(code).strip(),))
    else:
        rows = conn.execute(sql + " ORDER BY p.id")
    return [r for r in rows if code_key(r["item_code"]) == key]


def check_skus(conn, supplier_id: int, items: list[dict]) -> None:
    """Each SKU belongs to one supplier. Refused as soon as an order is saved, not later at approval."""
    for item in items:
        code = item["item_code"]
        if not code:
            continue
        found = next((r for r in _same_code(conn, code) if r["owner_id"] is not None), None)
        owner = found and {"id": found["owner_id"], "code": found["owner_code"], "name": found["owner_name"]}
        if owner and owner["id"] != supplier_id:
            raise POError(f"SKU {code} belongs to {owner['code']} {owner['name']}. Each SKU belongs to one supplier, so use a different SKU here.")


def record_products(conn, user: User, supplier_id: int, items: list[dict]) -> None:
    """On approval, every new SKU goes in the catalog under its supplier and the row is linked to it. Rows without a SKU stay one-offs.

    An existing product is never overwritten. It's the order being approved that adds the product, so this doesn't need the approver to hold catalog rights,
    but it does need someone who works on orders or the catalog.
    """
    if not any(user.can(p) for p in ("master.edit", "of.edit", "of.amend", "of.approve")):
        require(user, "master.edit")
    check_skus(conn, supplier_id, items)
    for item in items:
        code = item["item_code"]
        if not code or item.get("product_id"):
            continue
        known = next(iter(_same_code(conn, code)), None)
        if known:
            item["product_id"] = known["id"]
            continue
        item["product_id"] = _store_product(conn, user, {
            "item_code": code, "department_id": item["department_id"], "description": item["description"], "brand": item["brand"], "size": item["size"],
            "color": item["color"], "uom": item["uom"], "pieces_per_unit": item["pieces_per_unit"], "box_size": item["box_size"], "supplier_id": supplier_id,
            "supplier_item_code": item["supplier_item_code"], "last_cost_c": item["unit_cost_c"], "selling_c": item["selling_c"]})


def search_products(conn, text: str = "", department_id: int | None = None, supplier_id: int | None = None,
                    active_only: bool = True, limit: int = 200, only_supplier: int | None = None) -> list:
    where, args = [], []
    for word in text.split():
        like = f"%{word}%"
        where.append("(p.item_code LIKE ? OR p.description LIKE ? OR p.supplier_item_code LIKE ? OR p.brand LIKE ?)")
        args += [like] * 4
    if department_id:
        where.append("p.department_id = ?")
        args.append(department_id)
    if supplier_id:
        where.append("(p.supplier_id = ? OR p.supplier_id IS NULL)")
        args.append(supplier_id)
    if only_supplier:
        where.append("p.supplier_id = ?")
        args.append(only_supplier)
    if active_only:
        where.append("p.active = 1")
    sql = ("SELECT p.*, d.name AS department, s.code AS supplier_code, s.name AS supplier_name FROM products p"
           " JOIN departments d ON d.id = p.department_id LEFT JOIN suppliers s ON s.id = p.supplier_id"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY s.name IS NULL, s.name, p.item_code LIMIT ?")
    return conn.execute(sql, [*args, limit]).fetchall()


def last_purchase_cost(conn, product_id: int) -> int | None:
    """Latest cost on a non-cancelled PO, otherwise the catalog's last recorded cost."""
    row = conn.execute(
        "SELECT i.unit_cost_c FROM purchase_order_items i JOIN purchase_orders p ON p.id = i.po_id"
        " WHERE i.product_id = ? AND p.status NOT IN ('DRAFT','CANCELLED') ORDER BY p.po_date DESC, p.id DESC LIMIT 1",
        (product_id,)).fetchone()
    if row:
        return row["unit_cost_c"]
    row = conn.execute("SELECT last_cost_c FROM products WHERE id = ?", (product_id,)).fetchone()
    return row["last_cost_c"] if row else None
