"""How the Order Form screen is arranged. An administrator's choice, for everyone. It changes where things sit, never what they mean."""
from __future__ import annotations

from .auth import User, require
from .db import get_setting, set_settings

FIELDS = {"supplier": "Supplier", "of_date": "OF date", "expected": "Expected delivery", "arrangement": "Delivery", "terms": "Payment terms",
          "instructions": "Delivery instructions", "remarks": "Remarks", "warehouse_notes": "Warehouse notes", "internal": "Internal notes"}
LEADING = ["Dept", "SKU", "Product", "Size", "Color", "UOM", "Unit cost", "Disc 1 %", "Disc 2 %"]
AFTER = ["Total", "Per pc", "Net unit", "Amount", "Order qty", "Supplier code", "Brand", "Box of", "Selling", "FX", "FX rate", "FX cost", "Remarks"]
BRANCHES = "Branch quantities"  # every branch's column, kept together so Enter still walks along them
COLUMNS = [*LEADING, BRANCHES, *AFTER]


def _ordered(saved: str, known: list[str]) -> list[str]:
    """The saved order, ignoring anything no longer known and adding anything new at the end. A stale setting can't lose a field."""
    chosen = [x for x in saved.split(",") if x in known]
    chosen = list(dict.fromkeys(chosen))
    return chosen + [x for x in known if x not in chosen]


def field_order(conn) -> list[str]:
    return _ordered(get_setting(conn, "layout.of_fields"), list(FIELDS))


def column_order(conn) -> list[str]:
    return _ordered(get_setting(conn, "layout.of_columns"), COLUMNS)


def save(conn, user: User, fields: list[str], columns: list[str]) -> None:
    require(user, "settings.edit")
    set_settings(conn, user, {"layout.of_fields": ",".join(_ordered(",".join(fields), list(FIELDS))),
                              "layout.of_columns": ",".join(_ordered(",".join(columns), COLUMNS))})
