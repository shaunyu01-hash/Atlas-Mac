"""Database schema. Money is integer centavos (_c), percents basis points (_bp), quantities and foreign amounts thousandths (_m).

Version 1 (DDL) was the PO-first design. Version 2 (MIGRATE_V2) makes the Order Form the master document:
ONE OF -> one PO per branch -> supplier DRs -> receiving -> branch dispatch. Every database, new or old, reaches
version 2 through the same migration, so that path is the one every test runs on.

Guards live in the database, not only in the code:
- audit_logs, approval history and issued PO versions are append-only.
- A numbered OF or PO can't be deleted and its number can't change. Branch codes never change.
- Items of an OF or PO past Draft can't change except inside a recorded operation,
  which holds a row in po_write_guard for the length of its transaction.
- One OF has at most one PO per branch, and only an approved OF can have POs.
Future modules point at stable ids. delivery_receipts: purchase_orders.id, many per PO, so a PO can be delivered in
batches. receiving_report_items: purchase_order_items.id, which carries of_item_id and of_allocation_id back to the OF.
branch_dispatches, debit_memos, claim_stubs, merchandise_payables and supplier_invoices follow from those.
received_qty_m, remaining_qty_m and delivery_count are already in place. A DR never marks anything received by itself.

Version 3 (MIGRATE_V3) removes the inactive branches that version 2 added, wherever no document ever used them.
Version 4 (MIGRATE_V4) brings supplier records to the standard codes and payment terms, and lets several people approve.
Issued documents keep what they printed.
"""

import re

SCHEMA_VERSION = 31

DDL = """
CREATE TABLE departments (
    id INTEGER PRIMARY KEY CHECK (id BETWEEN 1 AND 99),
    name TEXT NOT NULL UNIQUE
);
CREATE TRIGGER departments_id_fixed BEFORE UPDATE OF id ON departments
BEGIN SELECT RAISE(ABORT, 'Department numbers never change'); END;
CREATE TRIGGER departments_no_delete BEFORE DELETE ON departments
BEGIN SELECT RAISE(ABORT, 'Departments cannot be deleted'); END;

CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    full_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE TABLE user_roles (
    user_id INTEGER NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK (role IN ('ADMIN','BUYER','APPROVER','VIEWER')),
    PRIMARY KEY (user_id, role)
);

CREATE TABLE suppliers (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    registered_name TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    contact_person TEXT NOT NULL DEFAULT '',
    telephone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    tin TEXT NOT NULL DEFAULT '',
    payment_terms TEXT NOT NULL DEFAULT '',
    default_discount_bp INTEGER NOT NULL DEFAULT 0 CHECK (default_discount_bp BETWEEN 0 AND 10000),
    vat_registered INTEGER NOT NULL DEFAULT 0 CHECK (vat_registered IN (0,1)),
    vat_mode TEXT NOT NULL DEFAULT 'INCLUSIVE' CHECK (vat_mode IN ('INCLUSIVE','EXCLUSIVE','NONE')),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','INACTIVE')),
    remarks TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'MANUAL' CHECK (source IN ('MANUAL','LEGACY')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_suppliers_name ON suppliers(name);

CREATE TABLE destinations (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'BRANCH' CHECK (kind IN ('BRANCH','WAREHOUSE')),
    address TEXT NOT NULL DEFAULT '',
    legacy_outlet INTEGER,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1))
);

CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    item_code TEXT NOT NULL UNIQUE COLLATE NOCASE,
    legacy_stk_code INTEGER,
    department_id INTEGER NOT NULL REFERENCES departments(id),
    description TEXT NOT NULL,
    brand TEXT NOT NULL DEFAULT '',
    size TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '',
    uom TEXT NOT NULL DEFAULT 'PC',
    pieces_per_unit INTEGER NOT NULL DEFAULT 1 CHECK (pieces_per_unit >= 1),
    box_size INTEGER,
    supplier_id INTEGER REFERENCES suppliers(id),
    supplier_item_code TEXT NOT NULL DEFAULT '',
    last_cost_c INTEGER CHECK (last_cost_c >= 0),
    selling_c INTEGER CHECK (selling_c >= 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    source TEXT NOT NULL DEFAULT 'MANUAL' CHECK (source IN ('MANUAL','LEGACY')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_products_desc ON products(description);
CREATE INDEX idx_products_dept ON products(department_id);
CREATE INDEX idx_products_supplier ON products(supplier_id);
CREATE INDEX idx_products_stk ON products(legacy_stk_code);

CREATE TABLE po_number_sequences (
    year INTEGER PRIMARY KEY,
    last_number INTEGER NOT NULL CHECK (last_number >= 0)
);
CREATE TRIGGER sequence_never_backward BEFORE UPDATE ON po_number_sequences
WHEN NEW.last_number <= OLD.last_number
BEGIN SELECT RAISE(ABORT, 'PO numbers cannot be reused'); END;
CREATE TRIGGER sequence_no_delete BEFORE DELETE ON po_number_sequences
BEGIN SELECT RAISE(ABORT, 'PO number history cannot be deleted'); END;

CREATE TABLE purchase_orders (
    id INTEGER PRIMARY KEY,
    po_number TEXT UNIQUE,
    po_year INTEGER,
    po_seq INTEGER,
    po_date TEXT NOT NULL,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    destination_id INTEGER NOT NULL REFERENCES destinations(id),
    expected_delivery_date TEXT,
    delivery_arrangement TEXT NOT NULL DEFAULT 'COMPLETE' CHECK (delivery_arrangement IN ('COMPLETE','PARTIAL')),
    delivery_count INTEGER NOT NULL DEFAULT 0 CHECK (delivery_count >= 0),
    payment_terms TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN
        ('DRAFT','PENDING_APPROVAL','APPROVED','ISSUED','PARTIALLY_DELIVERED','FULLY_DELIVERED','CLOSED','CANCELLED')),
    payment_status TEXT NOT NULL DEFAULT 'UNPAID',
    remarks TEXT NOT NULL DEFAULT '',
    internal_notes TEXT NOT NULL DEFAULT '',
    legacy_po_number TEXT NOT NULL DEFAULT '',
    overall_discount_type TEXT NOT NULL DEFAULT 'NONE' CHECK (overall_discount_type IN ('NONE','PERCENT','AMOUNT')),
    overall_discount_value INTEGER NOT NULL DEFAULT 0 CHECK (overall_discount_value >= 0),
    vat_mode TEXT NOT NULL DEFAULT 'INCLUSIVE' CHECK (vat_mode IN ('INCLUSIVE','EXCLUSIVE','NONE')),
    vat_rate_bp INTEGER NOT NULL DEFAULT 1200 CHECK (vat_rate_bp >= 0),
    other_charges_c INTEGER NOT NULL DEFAULT 0 CHECK (other_charges_c >= 0),
    other_charges_note TEXT NOT NULL DEFAULT '',
    total_qty INTEGER NOT NULL DEFAULT 0,
    total_pieces INTEGER NOT NULL DEFAULT 0,
    gross_c INTEGER NOT NULL DEFAULT 0,
    item_discount_c INTEGER NOT NULL DEFAULT 0,
    overall_discount_c INTEGER NOT NULL DEFAULT 0,
    vat_c INTEGER NOT NULL DEFAULT 0,
    grand_total_c INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    prepared_by INTEGER NOT NULL REFERENCES users(id),
    approved_by INTEGER REFERENCES users(id),
    approval_date TEXT,
    auto_approved INTEGER NOT NULL DEFAULT 0 CHECK (auto_approved IN (0,1)),
    issued_at TEXT,
    issued_by INTEGER REFERENCES users(id),
    cancelled_at TEXT,
    cancelled_by INTEGER REFERENCES users(id),
    cancel_reason TEXT NOT NULL DEFAULT '',
    closed_at TEXT,
    closed_by INTEGER REFERENCES users(id),
    close_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((po_number IS NULL) = (po_year IS NULL)),
    CHECK ((po_number IS NULL) = (po_seq IS NULL)),
    CHECK (status IN ('DRAFT','PENDING_APPROVAL','CANCELLED') OR po_number IS NOT NULL),
    UNIQUE (po_year, po_seq)
);
CREATE INDEX idx_po_supplier ON purchase_orders(supplier_id);
CREATE INDEX idx_po_destination ON purchase_orders(destination_id);
CREATE INDEX idx_po_status ON purchase_orders(status);
CREATE INDEX idx_po_date ON purchase_orders(po_date);
CREATE INDEX idx_po_year ON purchase_orders(po_year);
CREATE INDEX idx_po_legacy ON purchase_orders(legacy_po_number);

CREATE TRIGGER po_number_fixed BEFORE UPDATE OF po_number, po_year, po_seq ON purchase_orders
WHEN OLD.po_number IS NOT NULL AND (NEW.po_number IS NOT OLD.po_number OR NEW.po_year IS NOT OLD.po_year OR NEW.po_seq IS NOT OLD.po_seq)
BEGIN SELECT RAISE(ABORT, 'A PO number can never change'); END;
CREATE TRIGGER po_no_delete_numbered BEFORE DELETE ON purchase_orders
WHEN OLD.po_number IS NOT NULL OR OLD.status <> 'DRAFT'
BEGIN SELECT RAISE(ABORT, 'Only an unnumbered draft can be deleted. Cancel the PO instead.'); END;

CREATE TABLE po_write_guard (token TEXT PRIMARY KEY);

CREATE TRIGGER po_terms_locked BEFORE UPDATE OF supplier_id, destination_id, po_date, payment_terms, delivery_arrangement,
    expected_delivery_date, overall_discount_type, overall_discount_value, vat_mode, vat_rate_bp, other_charges_c,
    total_qty, gross_c, item_discount_c, overall_discount_c, vat_c, grand_total_c, revision ON purchase_orders
WHEN OLD.status <> 'DRAFT' AND NOT EXISTS (SELECT 1 FROM po_write_guard)
BEGIN SELECT RAISE(ABORT, 'A submitted PO can only change through an amendment'); END;

CREATE TABLE purchase_order_items (
    id INTEGER PRIMARY KEY,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    line_no INTEGER NOT NULL CHECK (line_no >= 1),
    department_id INTEGER NOT NULL REFERENCES departments(id),
    product_id INTEGER REFERENCES products(id),
    item_code TEXT NOT NULL DEFAULT '',
    supplier_item_code TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    brand TEXT NOT NULL DEFAULT '',
    size TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '',
    qty INTEGER NOT NULL CHECK (qty > 0),
    uom TEXT NOT NULL DEFAULT 'PC',
    pieces_per_unit INTEGER NOT NULL DEFAULT 1 CHECK (pieces_per_unit >= 1),
    box_size INTEGER,
    unit_cost_c INTEGER NOT NULL CHECK (unit_cost_c >= 0),
    disc1_bp INTEGER NOT NULL DEFAULT 0 CHECK (disc1_bp BETWEEN 0 AND 10000),
    disc2_bp INTEGER NOT NULL DEFAULT 0 CHECK (disc2_bp BETWEEN 0 AND 10000),
    net_unit_c INTEGER NOT NULL,
    line_gross_c INTEGER NOT NULL,
    line_discount_c INTEGER NOT NULL,
    line_total_c INTEGER NOT NULL,
    selling_c INTEGER CHECK (selling_c >= 0),
    fx_currency TEXT NOT NULL DEFAULT '',
    fx_rate_m INTEGER NOT NULL DEFAULT 0 CHECK (fx_rate_m >= 0),
    fx_cost_m INTEGER NOT NULL DEFAULT 0 CHECK (fx_cost_m >= 0),
    remarks TEXT NOT NULL DEFAULT '',
    received_qty INTEGER NOT NULL DEFAULT 0 CHECK (received_qty >= 0),
    remaining_qty INTEGER GENERATED ALWAYS AS (qty - received_qty) VIRTUAL,
    UNIQUE (po_id, line_no)
);
CREATE INDEX idx_items_po ON purchase_order_items(po_id);
CREATE INDEX idx_items_dept ON purchase_order_items(department_id);
CREATE INDEX idx_items_product ON purchase_order_items(product_id);
CREATE INDEX idx_items_code ON purchase_order_items(item_code);

CREATE TRIGGER items_locked_insert BEFORE INSERT ON purchase_order_items
WHEN (SELECT status FROM purchase_orders WHERE id = NEW.po_id) <> 'DRAFT'
 AND NOT EXISTS (SELECT 1 FROM po_write_guard)
BEGIN SELECT RAISE(ABORT, 'Items of a submitted PO can only change through an amendment'); END;
CREATE TRIGGER items_locked_update BEFORE UPDATE ON purchase_order_items
WHEN (SELECT status FROM purchase_orders WHERE id = OLD.po_id) <> 'DRAFT'
 AND NOT EXISTS (SELECT 1 FROM po_write_guard)
BEGIN SELECT RAISE(ABORT, 'Items of a submitted PO can only change through an amendment'); END;
CREATE TRIGGER items_locked_delete BEFORE DELETE ON purchase_order_items
WHEN (SELECT status FROM purchase_orders WHERE id = OLD.po_id) <> 'DRAFT'
 AND NOT EXISTS (SELECT 1 FROM po_write_guard)
BEGIN SELECT RAISE(ABORT, 'Items of a submitted PO can only change through an amendment'); END;

CREATE TABLE po_approvals (
    id INTEGER PRIMARY KEY,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    amendment_id INTEGER REFERENCES po_amendments(id),
    level INTEGER NOT NULL DEFAULT 1 CHECK (level >= 1),
    decision TEXT NOT NULL CHECK (decision IN ('SUBMITTED','APPROVED','AUTO_APPROVED','REJECTED')),
    decided_by INTEGER REFERENCES users(id),
    decided_at TEXT NOT NULL,
    amount_c INTEGER NOT NULL,
    threshold_c INTEGER,
    policy_enabled INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    remarks TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_approvals_po ON po_approvals(po_id);
CREATE TRIGGER approvals_no_update BEFORE UPDATE ON po_approvals
BEGIN SELECT RAISE(ABORT, 'Approval history cannot be changed'); END;
CREATE TRIGGER approvals_no_delete BEFORE DELETE ON po_approvals
BEGIN SELECT RAISE(ABORT, 'Approval history cannot be deleted'); END;

CREATE TABLE po_amendments (
    id INTEGER PRIMARY KEY,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    revision INTEGER,
    status TEXT NOT NULL CHECK (status IN ('PENDING_APPROVAL','APPLIED','REJECTED')),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    requested_by INTEGER NOT NULL REFERENCES users(id),
    requested_at TEXT NOT NULL,
    decided_by INTEGER REFERENCES users(id),
    decided_at TEXT,
    UNIQUE (po_id, revision)
);
CREATE INDEX idx_amendments_po ON po_amendments(po_id);

CREATE TABLE po_attachments (
    id INTEGER PRIMARY KEY,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    file_name TEXT NOT NULL,
    stored_name TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    added_by INTEGER NOT NULL REFERENCES users(id),
    added_at TEXT NOT NULL,
    removed_at TEXT,
    removed_by INTEGER REFERENCES users(id)
);
CREATE INDEX idx_attachments_po ON po_attachments(po_id);

CREATE TABLE po_audit_logs (
    id INTEGER PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id),
    user_name TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    po_id INTEGER REFERENCES purchase_orders(id),
    summary TEXT NOT NULL DEFAULT '',
    previous_value TEXT,
    new_value TEXT
);
CREATE INDEX idx_audit_po ON po_audit_logs(po_id);
CREATE INDEX idx_audit_entity ON po_audit_logs(entity_type, entity_id);
CREATE INDEX idx_audit_time ON po_audit_logs(occurred_at);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON po_audit_logs
BEGIN SELECT RAISE(ABORT, 'The audit log cannot be changed'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON po_audit_logs
BEGIN SELECT RAISE(ABORT, 'The audit log cannot be deleted'); END;

CREATE TABLE system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by INTEGER REFERENCES users(id)
);

CREATE VIEW v_outstanding_items AS
SELECT i.*, p.po_number, p.status AS po_status, p.expected_delivery_date, p.supplier_id, p.destination_id
FROM purchase_order_items i JOIN purchase_orders p ON p.id = i.po_id
WHERE p.status IN ('APPROVED','ISSUED','PARTIALLY_DELIVERED') AND i.qty > i.received_qty;
"""

DEPARTMENTS = [
    (1, "Grocery"), (2, "Beverages"), (3, "Household"), (4, "Health and Beauty"),
    (5, "Hardware"), (6, "Electronics"), (7, "Office and School"), (8, "Toys and Baby"), (9, "General Merchandise"),
]

# Starting branches and their codes. Editable under Destinations.
DESTINATIONS = [
    ("NTH", "North", 2), ("STH", "South", 4), ("EST", "East", 6), ("WST", "West", 8),
]

DEFAULT_SETTINGS = {
    "approval.enabled": "1",
    # Branches count a delivery only once the Central Warehouse marked it shipped to them. It applies once the warehouse has a login.
    "receiving.after_shipping": "1",
    "approval.threshold_c": "10000000",
    "approval.comparison": "GT",
    "approval.approver_user_ids": "",
    "approval.allow_self_approval": "0",
    "tax.vat_rate_bp": "1200",
    "tax.default_vat_mode": "NONE",
    "company.name": "ATLAS PO ERP",
    "company.address": "",
    "company.telephone": "",
    "company.tin": "",
    "company.warehouse_name": "Central Warehouse",
    "company.warehouse_address": "",
    "company.email": "",
    "company.logo_file": "",
    "approval.basis": "OF_TOTAL",
    "backup.folder": "",
    "backup.keep_days": "30",
    # Reorder levels the program works out: average daily sales over the last sales_days, times the days a delivery takes (lead_days),
    # for the minimum, plus the days the stock should last after it arrives (cover_days), for the target.
    "inventory.sales_days": "60",
    "inventory.lead_days": "14",
    "inventory.cover_days": "14",
    # Stock with no sale in this many days is listed as not moving.
    "inventory.dead_days": "90",
}


# ------------------------------------------------------------------ version 2: the Order Form comes first

# Branch codes: 1 Old Town, 2 North, 3 Hillside, 4 South,
# 5 Lakeside, 6 East, 8 Bayside. Code 8 is now West. Runs inside one transaction with foreign keys off.
MIGRATE_V2 = """
DROP VIEW v_outstanding_items;
DROP TRIGGER po_number_fixed;
DROP TRIGGER po_no_delete_numbered;
DROP TRIGGER po_terms_locked;
DROP TRIGGER items_locked_insert;
DROP TRIGGER items_locked_update;
DROP TRIGGER items_locked_delete;

CREATE TABLE branches (
    code INTEGER PRIMARY KEY CHECK (code >= 1),
    short_code TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    kind TEXT NOT NULL DEFAULT 'BRANCH' CHECK (kind IN ('BRANCH','WAREHOUSE')),
    address TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1))
);
CREATE TRIGGER branch_code_fixed BEFORE UPDATE OF code ON branches
BEGIN SELECT RAISE(ABORT, 'Branch codes never change'); END;
CREATE TRIGGER branch_no_delete BEFORE DELETE ON branches
BEGIN SELECT RAISE(ABORT, 'Branches cannot be deleted. Make the branch inactive instead.'); END;

-- A destination's outlet number becomes its permanent code. Anything without one gets a code above 1000.
CREATE TABLE _dest_map AS
SELECT d.id AS dest_id,
       CASE WHEN d.legacy_outlet >= 1 AND d.id = (SELECT MIN(x.id) FROM destinations x WHERE x.legacy_outlet = d.legacy_outlet)
            THEN d.legacy_outlet ELSE 1000 + d.id END AS code
FROM destinations d;
INSERT INTO branches(code, short_code, name, kind, address, active)
SELECT m.code, d.code, d.name, d.kind, d.address, d.active FROM destinations d JOIN _dest_map m ON m.dest_id = d.id;
INSERT OR IGNORE INTO branches(code, short_code, name, active) VALUES
    (1, 'OTN', 'Old Town', 0), (2, 'NTH', 'North', 1), (3, 'HIL', 'Hillside', 0), (4, 'STH', 'South', 1),
    (5, 'LAK', 'Lakeside', 0), (6, 'EST', 'East', 1), (8, 'WST', 'West', 1);
UPDATE branches SET name = 'West' WHERE code = 8;
UPDATE branches SET active = 0 WHERE code IN (1, 3, 5);
UPDATE branches SET active = 1 WHERE code IN (2, 4, 6, 8);

CREATE TABLE order_forms (
    id INTEGER PRIMARY KEY,
    of_number TEXT UNIQUE,
    of_year INTEGER,
    of_seq INTEGER,
    of_date TEXT NOT NULL,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    supplier_snapshot TEXT,
    expected_delivery_date TEXT,
    delivery_arrangement TEXT NOT NULL DEFAULT 'COMPLETE' CHECK (delivery_arrangement IN ('COMPLETE','PARTIAL')),
    payment_terms TEXT NOT NULL DEFAULT '',
    delivery_instructions TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','PENDING_APPROVAL','APPROVED','CANCELLED')),
    remarks TEXT NOT NULL DEFAULT '',
    warehouse_notes TEXT NOT NULL DEFAULT '',
    internal_notes TEXT NOT NULL DEFAULT '',
    overall_discount_type TEXT NOT NULL DEFAULT 'NONE' CHECK (overall_discount_type IN ('NONE','PERCENT','AMOUNT')),
    overall_discount_value INTEGER NOT NULL DEFAULT 0 CHECK (overall_discount_value >= 0),
    vat_mode TEXT NOT NULL DEFAULT 'INCLUSIVE' CHECK (vat_mode IN ('INCLUSIVE','EXCLUSIVE','NONE')),
    vat_rate_bp INTEGER NOT NULL DEFAULT 1200 CHECK (vat_rate_bp >= 0),
    other_charges_c INTEGER NOT NULL DEFAULT 0 CHECK (other_charges_c >= 0),
    other_charges_note TEXT NOT NULL DEFAULT '',
    total_qty_m INTEGER NOT NULL DEFAULT 0,
    total_pieces_m INTEGER NOT NULL DEFAULT 0,
    gross_c INTEGER NOT NULL DEFAULT 0,
    item_discount_c INTEGER NOT NULL DEFAULT 0,
    overall_discount_c INTEGER NOT NULL DEFAULT 0,
    vat_c INTEGER NOT NULL DEFAULT 0,
    grand_total_c INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    prepared_by INTEGER NOT NULL REFERENCES users(id),
    approved_by INTEGER REFERENCES users(id),
    approval_date TEXT,
    auto_approved INTEGER NOT NULL DEFAULT 0 CHECK (auto_approved IN (0,1)),
    pos_generated_at TEXT,
    pos_generated_by INTEGER REFERENCES users(id),
    cancelled_at TEXT,
    cancelled_by INTEGER REFERENCES users(id),
    cancel_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((of_number IS NULL) = (of_year IS NULL)),
    CHECK ((of_number IS NULL) = (of_seq IS NULL)),
    CHECK (status IN ('DRAFT','PENDING_APPROVAL','CANCELLED') OR of_number IS NOT NULL),
    UNIQUE (of_year, of_seq)
);
CREATE INDEX idx_of_supplier ON order_forms(supplier_id);
CREATE INDEX idx_of_status ON order_forms(status);
CREATE INDEX idx_of_date ON order_forms(of_date);

CREATE TRIGGER of_number_fixed BEFORE UPDATE OF of_number, of_year, of_seq ON order_forms
WHEN OLD.of_number IS NOT NULL AND (NEW.of_number IS NOT OLD.of_number OR NEW.of_year IS NOT OLD.of_year OR NEW.of_seq IS NOT OLD.of_seq)
BEGIN SELECT RAISE(ABORT, 'An OF number can never change'); END;
CREATE TRIGGER of_no_delete_numbered BEFORE DELETE ON order_forms
WHEN OLD.of_number IS NOT NULL OR OLD.status <> 'DRAFT'
BEGIN SELECT RAISE(ABORT, 'Only an unnumbered draft can be deleted. Cancel the OF instead.'); END;
CREATE TRIGGER of_terms_locked BEFORE UPDATE OF supplier_id, of_date, payment_terms, delivery_arrangement, expected_delivery_date,
    delivery_instructions, overall_discount_type, overall_discount_value, vat_mode, vat_rate_bp, other_charges_c,
    total_qty_m, gross_c, item_discount_c, overall_discount_c, vat_c, grand_total_c, revision ON order_forms
WHEN OLD.status <> 'DRAFT' AND NOT EXISTS (SELECT 1 FROM po_write_guard)
BEGIN SELECT RAISE(ABORT, 'A submitted OF can only change through an amendment'); END;

CREATE TABLE order_form_items (
    id INTEGER PRIMARY KEY,
    of_id INTEGER NOT NULL REFERENCES order_forms(id),
    line_no INTEGER NOT NULL CHECK (line_no >= 1),
    department_id INTEGER NOT NULL REFERENCES departments(id),
    product_id INTEGER REFERENCES products(id),
    item_code TEXT NOT NULL DEFAULT '',
    supplier_item_code TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    brand TEXT NOT NULL DEFAULT '',
    size TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '',
    uom TEXT NOT NULL DEFAULT 'PC',
    pieces_per_unit INTEGER NOT NULL DEFAULT 1 CHECK (pieces_per_unit >= 1),
    box_size INTEGER,
    unit_cost_c INTEGER NOT NULL CHECK (unit_cost_c >= 0),
    disc1_bp INTEGER NOT NULL DEFAULT 0 CHECK (disc1_bp BETWEEN 0 AND 10000),
    disc2_bp INTEGER NOT NULL DEFAULT 0 CHECK (disc2_bp BETWEEN 0 AND 10000),
    net_unit_c INTEGER NOT NULL,
    total_qty_m INTEGER NOT NULL DEFAULT 0 CHECK (total_qty_m >= 0),
    line_gross_c INTEGER NOT NULL DEFAULT 0,
    line_discount_c INTEGER NOT NULL DEFAULT 0,
    line_total_c INTEGER NOT NULL DEFAULT 0,
    selling_c INTEGER CHECK (selling_c >= 0),
    fx_currency TEXT NOT NULL DEFAULT '',
    fx_rate_m INTEGER NOT NULL DEFAULT 0 CHECK (fx_rate_m >= 0),
    fx_cost_m INTEGER NOT NULL DEFAULT 0 CHECK (fx_cost_m >= 0),
    remarks TEXT NOT NULL DEFAULT '',
    UNIQUE (of_id, line_no)
);
CREATE INDEX idx_of_items_of ON order_form_items(of_id);
CREATE INDEX idx_of_items_dept ON order_form_items(department_id);
CREATE INDEX idx_of_items_product ON order_form_items(product_id);

CREATE TABLE order_form_allocations (
    id INTEGER PRIMARY KEY,
    of_item_id INTEGER NOT NULL REFERENCES order_form_items(id),
    branch_code INTEGER NOT NULL REFERENCES branches(code),
    qty_m INTEGER NOT NULL CHECK (qty_m >= 0),
    UNIQUE (of_item_id, branch_code)
);
CREATE INDEX idx_of_alloc_branch ON order_form_allocations(branch_code);

CREATE TRIGGER of_items_locked_insert BEFORE INSERT ON order_form_items
WHEN (SELECT status FROM order_forms WHERE id = NEW.of_id) <> 'DRAFT' AND NOT EXISTS (SELECT 1 FROM po_write_guard)
BEGIN SELECT RAISE(ABORT, 'Items of a submitted OF can only change through an amendment'); END;
CREATE TRIGGER of_items_locked_update BEFORE UPDATE ON order_form_items
WHEN (SELECT status FROM order_forms WHERE id = OLD.of_id) <> 'DRAFT' AND NOT EXISTS (SELECT 1 FROM po_write_guard)
BEGIN SELECT RAISE(ABORT, 'Items of a submitted OF can only change through an amendment'); END;
CREATE TRIGGER of_items_locked_delete BEFORE DELETE ON order_form_items
WHEN (SELECT status FROM order_forms WHERE id = OLD.of_id) <> 'DRAFT' AND NOT EXISTS (SELECT 1 FROM po_write_guard)
BEGIN SELECT RAISE(ABORT, 'Items of a submitted OF can only change through an amendment'); END;
CREATE TRIGGER of_alloc_locked_insert BEFORE INSERT ON order_form_allocations
WHEN (SELECT f.status FROM order_forms f JOIN order_form_items i ON i.of_id = f.id WHERE i.id = NEW.of_item_id) <> 'DRAFT'
 AND NOT EXISTS (SELECT 1 FROM po_write_guard)
BEGIN SELECT RAISE(ABORT, 'Branch allocations of a submitted OF can only change through an amendment'); END;
CREATE TRIGGER of_alloc_locked_update BEFORE UPDATE ON order_form_allocations
WHEN (SELECT f.status FROM order_forms f JOIN order_form_items i ON i.of_id = f.id WHERE i.id = OLD.of_item_id) <> 'DRAFT'
 AND NOT EXISTS (SELECT 1 FROM po_write_guard)
BEGIN SELECT RAISE(ABORT, 'Branch allocations of a submitted OF can only change through an amendment'); END;
CREATE TRIGGER of_alloc_locked_delete BEFORE DELETE ON order_form_allocations
WHEN (SELECT f.status FROM order_forms f JOIN order_form_items i ON i.of_id = f.id WHERE i.id = OLD.of_item_id) <> 'DRAFT'
 AND NOT EXISTS (SELECT 1 FROM po_write_guard)
BEGIN SELECT RAISE(ABORT, 'Branch allocations of a submitted OF can only change through an amendment'); END;

CREATE TABLE of_number_sequences (
    year INTEGER PRIMARY KEY,
    last_number INTEGER NOT NULL CHECK (last_number >= 0)
);
CREATE TRIGGER of_sequence_never_backward BEFORE UPDATE ON of_number_sequences
WHEN NEW.last_number <= OLD.last_number
BEGIN SELECT RAISE(ABORT, 'OF numbers cannot be reused'); END;
CREATE TRIGGER of_sequence_no_delete BEFORE DELETE ON of_number_sequences
BEGIN SELECT RAISE(ABORT, 'OF number history cannot be deleted'); END;

CREATE TABLE of_amendments (
    id INTEGER PRIMARY KEY,
    of_id INTEGER NOT NULL REFERENCES order_forms(id),
    revision INTEGER,
    status TEXT NOT NULL CHECK (status IN ('PENDING_APPROVAL','APPLIED','REJECTED')),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    requested_by INTEGER NOT NULL REFERENCES users(id),
    requested_at TEXT NOT NULL,
    decided_by INTEGER REFERENCES users(id),
    decided_at TEXT,
    UNIQUE (of_id, revision)
);
CREATE INDEX idx_of_amendments_of ON of_amendments(of_id);

CREATE TABLE of_approvals (
    id INTEGER PRIMARY KEY,
    of_id INTEGER NOT NULL REFERENCES order_forms(id),
    amendment_id INTEGER REFERENCES of_amendments(id),
    level INTEGER NOT NULL DEFAULT 1 CHECK (level >= 1),
    decision TEXT NOT NULL CHECK (decision IN ('SUBMITTED','APPROVED','AUTO_APPROVED','REJECTED')),
    decided_by INTEGER REFERENCES users(id),
    decided_at TEXT NOT NULL,
    amount_c INTEGER NOT NULL,
    basis TEXT NOT NULL,
    threshold_c INTEGER,
    policy_enabled INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    remarks TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_of_approvals_of ON of_approvals(of_id);
CREATE TRIGGER of_approvals_no_update BEFORE UPDATE ON of_approvals
BEGIN SELECT RAISE(ABORT, 'Approval history cannot be changed'); END;
CREATE TRIGGER of_approvals_no_delete BEFORE DELETE ON of_approvals
BEGIN SELECT RAISE(ABORT, 'Approval history cannot be deleted'); END;

-- The old single counter stays as the record of the old 2026PO-00001 numbers. New POs count per branch and year.
-- legacy_po_number_sequences is a frozen legacy table: nothing writes it any more, and it is kept only for old data.
ALTER TABLE po_number_sequences RENAME TO legacy_po_number_sequences;
CREATE TABLE po_number_sequences (
    year INTEGER NOT NULL,
    branch_code INTEGER NOT NULL REFERENCES branches(code),
    last_number INTEGER NOT NULL CHECK (last_number >= 0),
    PRIMARY KEY (year, branch_code)
);
CREATE TRIGGER po_sequence_never_backward BEFORE UPDATE ON po_number_sequences
WHEN NEW.last_number <= OLD.last_number
BEGIN SELECT RAISE(ABORT, 'PO numbers cannot be reused'); END;
CREATE TRIGGER po_sequence_no_delete BEFORE DELETE ON po_number_sequences
BEGIN SELECT RAISE(ABORT, 'PO number history cannot be deleted'); END;

CREATE TABLE new_purchase_orders (
    id INTEGER PRIMARY KEY,
    po_number TEXT UNIQUE,
    po_year INTEGER,
    po_seq INTEGER,
    po_date TEXT NOT NULL,
    of_id INTEGER REFERENCES order_forms(id),
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    supplier_snapshot TEXT,
    branch_code INTEGER NOT NULL REFERENCES branches(code),
    branch_name TEXT NOT NULL,
    branch_address TEXT NOT NULL DEFAULT '',
    expected_delivery_date TEXT,
    delivery_arrangement TEXT NOT NULL DEFAULT 'COMPLETE' CHECK (delivery_arrangement IN ('COMPLETE','PARTIAL')),
    delivery_count INTEGER NOT NULL DEFAULT 0 CHECK (delivery_count >= 0),
    payment_terms TEXT NOT NULL DEFAULT '',
    delivery_instructions TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN
        ('DRAFT','PENDING_APPROVAL','APPROVED','ISSUED','PARTIALLY_DELIVERED','FULLY_DELIVERED','CLOSED','CANCELLED')),
    payment_status TEXT NOT NULL DEFAULT 'UNPAID',
    remarks TEXT NOT NULL DEFAULT '',
    internal_notes TEXT NOT NULL DEFAULT '',
    legacy_po_number TEXT NOT NULL DEFAULT '',
    overall_discount_type TEXT NOT NULL DEFAULT 'NONE' CHECK (overall_discount_type IN ('NONE','PERCENT','AMOUNT')),
    overall_discount_value INTEGER NOT NULL DEFAULT 0 CHECK (overall_discount_value >= 0),
    vat_mode TEXT NOT NULL DEFAULT 'INCLUSIVE' CHECK (vat_mode IN ('INCLUSIVE','EXCLUSIVE','NONE')),
    vat_rate_bp INTEGER NOT NULL DEFAULT 1200 CHECK (vat_rate_bp >= 0),
    other_charges_c INTEGER NOT NULL DEFAULT 0 CHECK (other_charges_c >= 0),
    other_charges_note TEXT NOT NULL DEFAULT '',
    total_qty_m INTEGER NOT NULL DEFAULT 0,
    total_pieces_m INTEGER NOT NULL DEFAULT 0,
    gross_c INTEGER NOT NULL DEFAULT 0,
    item_discount_c INTEGER NOT NULL DEFAULT 0,
    overall_discount_c INTEGER NOT NULL DEFAULT 0,
    vat_c INTEGER NOT NULL DEFAULT 0,
    grand_total_c INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    issued_revision INTEGER,
    prepared_by INTEGER NOT NULL REFERENCES users(id),
    approved_by INTEGER REFERENCES users(id),
    approval_date TEXT,
    auto_approved INTEGER NOT NULL DEFAULT 0 CHECK (auto_approved IN (0,1)),
    issued_at TEXT,
    issued_by INTEGER REFERENCES users(id),
    cancelled_at TEXT,
    cancelled_by INTEGER REFERENCES users(id),
    cancel_reason TEXT NOT NULL DEFAULT '',
    closed_at TEXT,
    closed_by INTEGER REFERENCES users(id),
    close_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((po_number IS NULL) = (po_year IS NULL)),
    CHECK ((po_number IS NULL) = (po_seq IS NULL)),
    CHECK (status IN ('DRAFT','PENDING_APPROVAL','CANCELLED') OR po_number IS NOT NULL)
);
INSERT INTO new_purchase_orders(id, po_number, po_year, po_seq, po_date, supplier_id, branch_code, branch_name, branch_address,
    expected_delivery_date, delivery_arrangement, delivery_count, payment_terms, status, payment_status, remarks, internal_notes,
    legacy_po_number, overall_discount_type, overall_discount_value, vat_mode, vat_rate_bp, other_charges_c, other_charges_note,
    total_qty_m, total_pieces_m, gross_c, item_discount_c, overall_discount_c, vat_c, grand_total_c, revision,
    issued_revision, prepared_by, approved_by, approval_date, auto_approved, issued_at, issued_by, cancelled_at, cancelled_by,
    cancel_reason, closed_at, closed_by, close_reason, created_at, updated_at)
SELECT p.id, p.po_number, p.po_year, p.po_seq, p.po_date, p.supplier_id, m.code, d.name, d.address,
    p.expected_delivery_date, p.delivery_arrangement, p.delivery_count, p.payment_terms, p.status, p.payment_status, p.remarks, p.internal_notes,
    p.legacy_po_number, p.overall_discount_type, p.overall_discount_value, p.vat_mode, p.vat_rate_bp, p.other_charges_c, p.other_charges_note,
    p.total_qty * 1000, p.total_pieces * 1000, p.gross_c, p.item_discount_c, p.overall_discount_c, p.vat_c, p.grand_total_c, p.revision,
    CASE WHEN p.issued_at IS NULL THEN NULL ELSE p.revision END, p.prepared_by, p.approved_by, p.approval_date, p.auto_approved, p.issued_at, p.issued_by,
    p.cancelled_at, p.cancelled_by, p.cancel_reason, p.closed_at, p.closed_by, p.close_reason, p.created_at, p.updated_at
FROM purchase_orders p JOIN destinations d ON d.id = p.destination_id JOIN _dest_map m ON m.dest_id = d.id;

CREATE TABLE new_purchase_order_items (
    id INTEGER PRIMARY KEY,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    line_no INTEGER NOT NULL CHECK (line_no >= 1),
    of_item_id INTEGER REFERENCES order_form_items(id),
    of_allocation_id INTEGER REFERENCES order_form_allocations(id),
    department_id INTEGER NOT NULL REFERENCES departments(id),
    product_id INTEGER REFERENCES products(id),
    item_code TEXT NOT NULL DEFAULT '',
    supplier_item_code TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    brand TEXT NOT NULL DEFAULT '',
    size TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '',
    qty_m INTEGER NOT NULL CHECK (qty_m > 0),
    uom TEXT NOT NULL DEFAULT 'PC',
    pieces_per_unit INTEGER NOT NULL DEFAULT 1 CHECK (pieces_per_unit >= 1),
    box_size INTEGER,
    unit_cost_c INTEGER NOT NULL CHECK (unit_cost_c >= 0),
    disc1_bp INTEGER NOT NULL DEFAULT 0 CHECK (disc1_bp BETWEEN 0 AND 10000),
    disc2_bp INTEGER NOT NULL DEFAULT 0 CHECK (disc2_bp BETWEEN 0 AND 10000),
    net_unit_c INTEGER NOT NULL,
    line_gross_c INTEGER NOT NULL,
    line_discount_c INTEGER NOT NULL,
    line_total_c INTEGER NOT NULL,
    selling_c INTEGER CHECK (selling_c >= 0),
    fx_currency TEXT NOT NULL DEFAULT '',
    fx_rate_m INTEGER NOT NULL DEFAULT 0 CHECK (fx_rate_m >= 0),
    fx_cost_m INTEGER NOT NULL DEFAULT 0 CHECK (fx_cost_m >= 0),
    remarks TEXT NOT NULL DEFAULT '',
    received_qty_m INTEGER NOT NULL DEFAULT 0 CHECK (received_qty_m >= 0),
    remaining_qty_m INTEGER GENERATED ALWAYS AS (qty_m - received_qty_m) VIRTUAL,
    UNIQUE (po_id, line_no)
);
INSERT INTO new_purchase_order_items(id, po_id, line_no, department_id, product_id, item_code, supplier_item_code, description, brand, size, color,
    qty_m, uom, pieces_per_unit, box_size, unit_cost_c, disc1_bp, disc2_bp, net_unit_c, line_gross_c, line_discount_c, line_total_c,
    selling_c, fx_currency, fx_rate_m, fx_cost_m, remarks, received_qty_m)
SELECT id, po_id, line_no, department_id, product_id, item_code, supplier_item_code, description, brand, size, color,
    qty * 1000, uom, pieces_per_unit, box_size, unit_cost_c, disc1_bp, disc2_bp, net_unit_c, line_gross_c, line_discount_c, line_total_c,
    selling_c, fx_currency, fx_rate_m, fx_cost_m, remarks, received_qty * 1000
FROM purchase_order_items;

DROP TABLE purchase_order_items;
DROP TABLE purchase_orders;
DROP TABLE destinations;
DROP TABLE _dest_map;
ALTER TABLE new_purchase_orders RENAME TO purchase_orders;
ALTER TABLE new_purchase_order_items RENAME TO purchase_order_items;

CREATE INDEX idx_po_supplier ON purchase_orders(supplier_id);
CREATE INDEX idx_po_branch ON purchase_orders(branch_code);
CREATE INDEX idx_po_status ON purchase_orders(status);
CREATE INDEX idx_po_date ON purchase_orders(po_date);
CREATE INDEX idx_po_year ON purchase_orders(po_year);
CREATE INDEX idx_po_legacy ON purchase_orders(legacy_po_number);
CREATE INDEX idx_po_of ON purchase_orders(of_id);
-- Pre-OF POs share one counter across branches, so these two apply only to POs generated from an OF.
CREATE UNIQUE INDEX ux_po_year_branch_seq ON purchase_orders(po_year, branch_code, po_seq) WHERE of_id IS NOT NULL;
CREATE UNIQUE INDEX ux_po_of_branch ON purchase_orders(of_id, branch_code) WHERE of_id IS NOT NULL;
CREATE INDEX idx_items_po ON purchase_order_items(po_id);
CREATE INDEX idx_items_dept ON purchase_order_items(department_id);
CREATE INDEX idx_items_product ON purchase_order_items(product_id);
CREATE INDEX idx_items_code ON purchase_order_items(item_code);
CREATE INDEX idx_items_of_item ON purchase_order_items(of_item_id);
CREATE INDEX idx_items_of_alloc ON purchase_order_items(of_allocation_id);

CREATE TRIGGER po_number_fixed BEFORE UPDATE OF po_number, po_year, po_seq ON purchase_orders
WHEN OLD.po_number IS NOT NULL AND (NEW.po_number IS NOT OLD.po_number OR NEW.po_year IS NOT OLD.po_year OR NEW.po_seq IS NOT OLD.po_seq)
BEGIN SELECT RAISE(ABORT, 'A PO number can never change'); END;
CREATE TRIGGER po_parent_fixed BEFORE UPDATE OF of_id, branch_code, branch_name, branch_address, supplier_snapshot ON purchase_orders
WHEN NOT EXISTS (SELECT 1 FROM po_write_guard)
BEGIN SELECT RAISE(ABORT, 'A PO keeps its parent OF and branch for good'); END;
CREATE TRIGGER po_requires_approved_of BEFORE INSERT ON purchase_orders
WHEN NEW.of_id IS NOT NULL AND (SELECT status FROM order_forms WHERE id = NEW.of_id) IS NOT 'APPROVED'
BEGIN SELECT RAISE(ABORT, 'Only an approved OF can generate POs'); END;
CREATE TRIGGER po_no_delete_numbered BEFORE DELETE ON purchase_orders
WHEN OLD.po_number IS NOT NULL OR OLD.status <> 'DRAFT'
BEGIN SELECT RAISE(ABORT, 'Only an unnumbered draft can be deleted. Cancel the PO instead.'); END;
CREATE TRIGGER po_terms_locked BEFORE UPDATE OF supplier_id, po_date, payment_terms, delivery_arrangement, delivery_instructions,
    expected_delivery_date, overall_discount_type, overall_discount_value, vat_mode, vat_rate_bp, other_charges_c,
    total_qty_m, gross_c, item_discount_c, overall_discount_c, vat_c, grand_total_c, revision ON purchase_orders
WHEN OLD.status <> 'DRAFT' AND NOT EXISTS (SELECT 1 FROM po_write_guard)
BEGIN SELECT RAISE(ABORT, 'A submitted PO can only change through an amendment'); END;
CREATE TRIGGER items_locked_insert BEFORE INSERT ON purchase_order_items
WHEN (SELECT status FROM purchase_orders WHERE id = NEW.po_id) <> 'DRAFT'
 AND NOT EXISTS (SELECT 1 FROM po_write_guard)
BEGIN SELECT RAISE(ABORT, 'Items of a submitted PO can only change through an amendment'); END;
CREATE TRIGGER items_locked_update BEFORE UPDATE ON purchase_order_items
WHEN (SELECT status FROM purchase_orders WHERE id = OLD.po_id) <> 'DRAFT'
 AND NOT EXISTS (SELECT 1 FROM po_write_guard)
BEGIN SELECT RAISE(ABORT, 'Items of a submitted PO can only change through an amendment'); END;
CREATE TRIGGER items_locked_delete BEFORE DELETE ON purchase_order_items
WHEN (SELECT status FROM purchase_orders WHERE id = OLD.po_id) <> 'DRAFT'
 AND NOT EXISTS (SELECT 1 FROM po_write_guard)
BEGIN SELECT RAISE(ABORT, 'Items of a submitted PO can only change through an amendment'); END;

-- What the supplier was actually handed: the exact PDF of each issued revision, kept for good.
CREATE TABLE po_issued_versions (
    id INTEGER PRIMARY KEY,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    revision INTEGER NOT NULL,
    issued_at TEXT NOT NULL,
    issued_by INTEGER NOT NULL REFERENCES users(id),
    snapshot_json TEXT NOT NULL,
    pdf BLOB NOT NULL,
    sha256 TEXT NOT NULL,
    UNIQUE (po_id, revision)
);
CREATE TRIGGER issued_versions_no_update BEFORE UPDATE ON po_issued_versions
BEGIN SELECT RAISE(ABORT, 'An issued PO version can never change'); END;
CREATE TRIGGER issued_versions_no_delete BEFORE DELETE ON po_issued_versions
BEGIN SELECT RAISE(ABORT, 'An issued PO version can never be deleted'); END;

ALTER TABLE po_audit_logs RENAME TO audit_logs;
ALTER TABLE audit_logs ADD COLUMN of_id INTEGER REFERENCES order_forms(id);
ALTER TABLE audit_logs ADD COLUMN document_number TEXT;
CREATE INDEX idx_audit_of ON audit_logs(of_id);
CREATE INDEX idx_audit_document ON audit_logs(document_number);

CREATE VIEW v_outstanding_items AS
SELECT i.*, p.po_number, p.of_id, p.status AS po_status, p.expected_delivery_date, p.supplier_id, p.branch_code
FROM purchase_order_items i JOIN purchase_orders p ON p.id = i.po_id
WHERE p.status IN ('APPROVED','ISSUED','PARTIALLY_DELIVERED') AND i.qty_m > i.received_qty_m;

INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V2', 'system',
        'Order Form workflow added. Code 8 named West. Old Town, Hillside and Lakeside inactive. Existing POs kept with their numbers.');
PRAGMA user_version = 2;
"""


# ------------------------------------------------------------------ version 3: unused old branches removed

# 1 Old Town, 3 Hillside and 5 Lakeside are old inactive branches. One with a document against it, or switched back on, stays.
MIGRATE_V3 = """
DROP TRIGGER branch_no_delete;
DELETE FROM branches
WHERE code IN (1, 3, 5) AND active = 0
  AND code NOT IN (SELECT branch_code FROM purchase_orders)
  AND code NOT IN (SELECT branch_code FROM order_form_allocations)
  AND code NOT IN (SELECT branch_code FROM po_number_sequences);
CREATE TRIGGER branch_no_delete BEFORE DELETE ON branches
BEGIN SELECT RAISE(ABORT, 'Branches cannot be deleted. Make the branch inactive instead.'); END;

INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V3', 'system',
        'Unused branches 1 Old Town, 3 Hillside and 5 Lakeside removed. Any with documents kept.');
PRAGMA user_version = 3;
"""


# ------------------------------------------------------------------ version 4: standard supplier codes and terms

# Days are written one way, such as 60 D to 60D, and blank becomes None. Custom terms are allowed, so anything else stays as it is.
MIGRATE_V4 = """
UPDATE suppliers SET payment_terms = CASE
    WHEN replace(upper(payment_terms), ' ', '') IN ('', 'NONE') THEN 'None'
    WHEN replace(upper(payment_terms), ' ', '') GLOB '[0-9]*D'
         AND rtrim(replace(upper(payment_terms), ' ', ''), 'D') NOT GLOB '*[^0-9]*' THEN replace(upper(payment_terms), ' ', '')
    ELSE payment_terms END;
UPDATE suppliers SET
    remarks = ltrim(remarks || char(10) || 'Old code ' || code, char(10)),
    code = substr(code, 2)
WHERE code GLOB '-?*' AND substr(code, 2) NOT IN (SELECT code FROM suppliers);

-- One approver becomes a list, with that person first on it.
INSERT OR REPLACE INTO system_settings(key, value, updated_at, updated_by)
SELECT 'approval.approver_user_ids', value, updated_at, updated_by FROM system_settings WHERE key = 'approval.approver_user_id';
DELETE FROM system_settings WHERE key = 'approval.approver_user_id';

INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V4', 'system',
        'Supplier payment terms written the standard way. Approver setting now holds a list.');
PRAGMA user_version = 4;
"""


# ------------------------------------------------------------------ version 5: stickers charged to a supplier

# Centavos per piece, 0 for a supplier who isn't charged. Set per supplier.
MIGRATE_V5 = """
ALTER TABLE suppliers ADD COLUMN sticker_rate_c INTEGER NOT NULL DEFAULT 0 CHECK (sticker_rate_c >= 0);
-- The catalog moves to the units now offered. Documents keep what they say.
UPDATE products SET uom = '1 DOZEN' WHERE uom = 'DOZ';
UPDATE products SET uom = '4 IN 1' WHERE uom = 'PK4';
UPDATE products SET uom = 'PC' WHERE uom IN ('PAIR', 'SET');
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V5', 'system', 'Suppliers can now carry a sticker deduction per piece. Catalog units updated.');
PRAGMA user_version = 5;
"""


# ------------------------------------------------------------------ version 6: No VAT by default

# A supplier marked VAT registered keeps its setting. Neither setting changes a total here, since VAT included adds nothing, and documents keep theirs.
MIGRATE_V6 = """
UPDATE system_settings SET value = 'NONE' WHERE key = 'tax.default_vat_mode';
UPDATE suppliers SET vat_mode = 'NONE' WHERE vat_mode = 'INCLUSIVE' AND vat_registered = 0;
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V6', 'system', 'No VAT is now the default, including for suppliers not marked VAT registered.');
PRAGMA user_version = 6;
"""


# ------------------------------------------------------------------ version 7: the warehouse's name and address

# Only the old defaults change, so what someone typed stays. Issued POs keep the PDF they printed.
MIGRATE_V7 = """
UPDATE system_settings SET value = 'Central Warehouse' WHERE key = 'company.warehouse_name' AND value = 'Main Warehouse';
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V7', 'system', 'The main warehouse is now Central Warehouse, on new documents.');
PRAGMA user_version = 7;
"""


# ------------------------------------------------------------------ version 8: selling prices changed by hand

# Automatic prices are worked out when needed, so this holds only what someone changed. It's keyed on the product, not the
# item row, because an OF amendment replaces its rows and a price set by hand has to survive that.
MIGRATE_V8 = """
CREATE TABLE IF NOT EXISTS selling_prices (
    of_id INTEGER NOT NULL REFERENCES order_forms(id),
    item_key TEXT NOT NULL,
    price_c INTEGER NOT NULL CHECK (price_c > 0),
    set_by INTEGER REFERENCES users(id),
    set_at TEXT NOT NULL,
    PRIMARY KEY (of_id, item_key)
);
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V8', 'system', 'Selling prices changed by hand can now be kept.');
PRAGMA user_version = 8;
"""


# ------------------------------------------------------------------ version 9: branch receiving

# A count is never edited: a recount is a new one, and the one it replaces stays. The product is copied onto each counted line so the
# record still reads the same if the PO is amended later, which is also why po_item_id isn't a foreign key.
MIGRATE_V9 = """
CREATE TABLE new_user_roles (
    user_id INTEGER NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK (role IN ('ADMIN','BUYER','APPROVER','VIEWER','RECEIVER')),
    PRIMARY KEY (user_id, role)
);
INSERT INTO new_user_roles(user_id, role) SELECT user_id, role FROM user_roles;
DROP TABLE user_roles;
ALTER TABLE new_user_roles RENAME TO user_roles;
CREATE TABLE IF NOT EXISTS user_branches (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    branch_code INTEGER NOT NULL REFERENCES branches(code)
);
CREATE TABLE IF NOT EXISTS branch_counts (
    id INTEGER PRIMARY KEY,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    delivery_no INTEGER NOT NULL CHECK (delivery_no >= 1),
    status TEXT NOT NULL CHECK (status IN ('WAITING','APPROVED','REPLACED')),
    counted_by INTEGER NOT NULL REFERENCES users(id),
    counted_at TEXT NOT NULL,
    approved_by INTEGER REFERENCES users(id),
    approved_at TEXT,
    recount_of INTEGER REFERENCES branch_counts(id),
    reason TEXT NOT NULL DEFAULT '',
    CHECK (approved_by IS NULL OR approved_by <> counted_by)
);
CREATE INDEX IF NOT EXISTS idx_branch_counts_po ON branch_counts(po_id);
CREATE TABLE IF NOT EXISTS branch_count_items (
    id INTEGER PRIMARY KEY,
    count_id INTEGER NOT NULL REFERENCES branch_counts(id),
    po_item_id INTEGER NOT NULL,
    item_code TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL,
    size TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '',
    uom TEXT NOT NULL DEFAULT 'PC',
    pieces_m INTEGER NOT NULL CHECK (pieces_m >= 0),
    remarks TEXT NOT NULL DEFAULT '',
    UNIQUE (count_id, po_item_id)
);
CREATE INDEX IF NOT EXISTS idx_branch_count_items_item ON branch_count_items(po_item_id);
CREATE TRIGGER IF NOT EXISTS branch_count_fixed BEFORE UPDATE OF po_id, delivery_no, counted_by, counted_at, recount_of, reason ON branch_counts
BEGIN SELECT RAISE(ABORT, 'A count is permanent. Recount instead'); END;
CREATE TRIGGER IF NOT EXISTS branch_count_no_delete BEFORE DELETE ON branch_counts
BEGIN SELECT RAISE(ABORT, 'A count is permanent. Recount instead'); END;
CREATE TRIGGER IF NOT EXISTS branch_count_items_no_update BEFORE UPDATE ON branch_count_items
BEGIN SELECT RAISE(ABORT, 'A count is permanent. Recount instead'); END;
CREATE TRIGGER IF NOT EXISTS branch_count_items_no_delete BEFORE DELETE ON branch_count_items
BEGIN SELECT RAISE(ABORT, 'A count is permanent. Recount instead'); END;
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V9', 'system', 'Branch receiving added, with a Receiver role tied to one branch.');
PRAGMA user_version = 9;
"""


# ------------------------------------------------------------------ version 10: damaged pieces in a count

# How many of a line's counted pieces are damaged, kept only where there are some. As permanent as the count it belongs to.
MIGRATE_V10 = """
CREATE TABLE IF NOT EXISTS branch_count_damage (
    count_id INTEGER NOT NULL REFERENCES branch_counts(id),
    po_item_id INTEGER NOT NULL,
    damaged_m INTEGER NOT NULL CHECK (damaged_m > 0),
    PRIMARY KEY (count_id, po_item_id)
);
CREATE TRIGGER IF NOT EXISTS branch_count_damage_no_update BEFORE UPDATE ON branch_count_damage
BEGIN SELECT RAISE(ABORT, 'A count is permanent. Recount instead'); END;
CREATE TRIGGER IF NOT EXISTS branch_count_damage_no_delete BEFORE DELETE ON branch_count_damage
BEGIN SELECT RAISE(ABORT, 'A count is permanent. Recount instead'); END;
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V10', 'system', 'Branch counts can now record damaged pieces.');
PRAGMA user_version = 10;
"""


# ------------------------------------------------------------------ version 11: two blind counts instead of approval

# Two people count on their own, and only what they agree on is received. A delivery can take several rounds, recounting only what disagreed, and an
# accepted delivery can be counted again as a new attempt. approved_by now means whose count this one matched. Counts already approved carry over as agreed.
MIGRATE_V11 = """
DROP TRIGGER IF EXISTS branch_count_fixed;
DROP TRIGGER IF EXISTS branch_count_no_delete;
CREATE TABLE new_branch_counts (
    id INTEGER PRIMARY KEY,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    delivery_no INTEGER NOT NULL CHECK (delivery_no >= 1),
    attempt_no INTEGER NOT NULL DEFAULT 1 CHECK (attempt_no >= 1),
    round_no INTEGER NOT NULL DEFAULT 1 CHECK (round_no >= 1),
    status TEXT NOT NULL CHECK (status IN ('WAITING','APPROVED','MISMATCH','REPLACED')),
    counted_by INTEGER NOT NULL REFERENCES users(id),
    counted_at TEXT NOT NULL,
    approved_by INTEGER REFERENCES users(id),
    approved_at TEXT,
    recount_of INTEGER REFERENCES branch_counts(id),
    reason TEXT NOT NULL DEFAULT '',
    CHECK (approved_by IS NULL OR approved_by <> counted_by)
);
INSERT INTO new_branch_counts(id, po_id, delivery_no, attempt_no, round_no, status, counted_by, counted_at, approved_by, approved_at, recount_of, reason)
SELECT id, po_id, delivery_no, ROW_NUMBER() OVER (PARTITION BY po_id, delivery_no ORDER BY id), 1, status, counted_by, counted_at, approved_by, approved_at, recount_of, reason FROM branch_counts;
DROP TABLE branch_counts;
ALTER TABLE new_branch_counts RENAME TO branch_counts;
CREATE INDEX IF NOT EXISTS idx_branch_counts_po ON branch_counts(po_id);
CREATE TRIGGER IF NOT EXISTS branch_count_fixed BEFORE UPDATE OF po_id, delivery_no, attempt_no, round_no, counted_by, counted_at, recount_of, reason ON branch_counts
BEGIN SELECT RAISE(ABORT, 'A count is permanent. Recount instead'); END;
CREATE TRIGGER IF NOT EXISTS branch_count_no_delete BEFORE DELETE ON branch_counts
BEGIN SELECT RAISE(ABORT, 'A count is permanent. Recount instead'); END;
CREATE TABLE IF NOT EXISTS branch_received (
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    delivery_no INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL,
    po_item_id INTEGER NOT NULL,
    pieces_m INTEGER NOT NULL CHECK (pieces_m >= 0),
    damaged_m INTEGER NOT NULL DEFAULT 0 CHECK (damaged_m >= 0),
    accepted_at TEXT NOT NULL,
    PRIMARY KEY (po_id, delivery_no, attempt_no, po_item_id)
);
CREATE INDEX IF NOT EXISTS idx_branch_received_item ON branch_received(po_item_id);
CREATE TRIGGER IF NOT EXISTS branch_received_no_update BEFORE UPDATE ON branch_received
BEGIN SELECT RAISE(ABORT, 'A count is permanent. Recount instead'); END;
CREATE TRIGGER IF NOT EXISTS branch_received_no_delete BEFORE DELETE ON branch_received
BEGIN SELECT RAISE(ABORT, 'A count is permanent. Recount instead'); END;
INSERT OR IGNORE INTO branch_received(po_id, delivery_no, attempt_no, po_item_id, pieces_m, damaged_m, accepted_at)
SELECT b.po_id, b.delivery_no, b.attempt_no, c.po_item_id, c.pieces_m, COALESCE(d.damaged_m, 0), COALESCE(b.approved_at, b.counted_at)
FROM branch_counts b JOIN branch_count_items c ON c.count_id = b.id LEFT JOIN branch_count_damage d ON d.count_id = c.count_id AND d.po_item_id = c.po_item_id
WHERE b.status = 'APPROVED';
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V11', 'system', 'Branch receiving now takes two separate blind counts that must agree, instead of an approval.');
PRAGMA user_version = 11;
"""


# ------------------------------------------------------------------ version 12: debit memos

# Numbers are never reused and an issued memo can only be cancelled. The pdf starts empty inside the issuing transaction and is fixed once written.
# Product and cost are copied onto each line, so a memo still reads the same if the PO is amended, which is also why po_item_id isn't a foreign key.
MIGRATE_V12 = """
CREATE TABLE IF NOT EXISTS dm_number_sequences (
    year INTEGER PRIMARY KEY,
    last_number INTEGER NOT NULL CHECK (last_number >= 1)
);
CREATE TRIGGER IF NOT EXISTS dm_sequence_never_backward BEFORE UPDATE ON dm_number_sequences
WHEN NEW.last_number <= OLD.last_number
BEGIN SELECT RAISE(ABORT, 'Debit memo numbers cannot be reused'); END;
CREATE TRIGGER IF NOT EXISTS dm_sequence_no_delete BEFORE DELETE ON dm_number_sequences
BEGIN SELECT RAISE(ABORT, 'Debit memo number history cannot be deleted'); END;
CREATE TABLE IF NOT EXISTS debit_memos (
    id INTEGER PRIMARY KEY,
    dm_number TEXT NOT NULL UNIQUE,
    dm_year INTEGER NOT NULL,
    dm_seq INTEGER NOT NULL,
    dm_date TEXT NOT NULL,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    status TEXT NOT NULL CHECK (status IN ('ISSUED','CANCELLED')),
    total_c INTEGER NOT NULL CHECK (total_c >= 0),
    remarks TEXT NOT NULL DEFAULT '',
    prepared_by INTEGER NOT NULL REFERENCES users(id),
    prepared_at TEXT NOT NULL,
    cancelled_by INTEGER REFERENCES users(id),
    cancelled_at TEXT,
    cancel_reason TEXT NOT NULL DEFAULT '',
    legacy_dm_number TEXT NOT NULL DEFAULT '',
    pdf BLOB NOT NULL DEFAULT x'',
    sha256 TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_debit_memos_po ON debit_memos(po_id);
CREATE TABLE IF NOT EXISTS debit_memo_items (
    id INTEGER PRIMARY KEY,
    dm_id INTEGER NOT NULL REFERENCES debit_memos(id),
    line_no INTEGER NOT NULL,
    po_item_id INTEGER NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN ('SHORT','DAMAGED')),
    item_code TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL,
    size TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '',
    uom TEXT NOT NULL DEFAULT 'PC',
    pieces_m INTEGER NOT NULL CHECK (pieces_m > 0),
    net_unit_c INTEGER NOT NULL,
    pieces_per_unit INTEGER NOT NULL,
    amount_c INTEGER NOT NULL,
    UNIQUE (dm_id, line_no)
);
CREATE INDEX IF NOT EXISTS idx_debit_memo_items_item ON debit_memo_items(po_item_id);
CREATE TRIGGER IF NOT EXISTS debit_memo_fixed BEFORE UPDATE OF dm_number, dm_year, dm_seq, dm_date, po_id, supplier_id, total_c, remarks, prepared_by, prepared_at ON debit_memos
BEGIN SELECT RAISE(ABORT, 'An issued debit memo cannot change. Cancel it and issue another'); END;
CREATE TRIGGER IF NOT EXISTS debit_memo_pdf_fixed BEFORE UPDATE OF pdf, sha256 ON debit_memos
WHEN length(OLD.pdf) > 0
BEGIN SELECT RAISE(ABORT, 'An issued debit memo cannot change. Cancel it and issue another'); END;
CREATE TRIGGER IF NOT EXISTS debit_memo_no_delete BEFORE DELETE ON debit_memos
BEGIN SELECT RAISE(ABORT, 'A debit memo cannot be deleted. Cancel it instead'); END;
CREATE TRIGGER IF NOT EXISTS debit_memo_items_no_update BEFORE UPDATE ON debit_memo_items
BEGIN SELECT RAISE(ABORT, 'An issued debit memo cannot change. Cancel it and issue another'); END;
CREATE TRIGGER IF NOT EXISTS debit_memo_items_no_delete BEFORE DELETE ON debit_memo_items
BEGIN SELECT RAISE(ABORT, 'An issued debit memo cannot change. Cancel it and issue another'); END;
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V12', 'system', 'Debit memos added.');
PRAGMA user_version = 12;
"""


# ------------------------------------------------------------------ version 13: excess delivery

# A product that arrived but wasn't on the PO is a line receivers add. It isn't a catalog product or a PO item. Counts refer to it as minus its id,
# so it's counted and compared like any other line. Excess is returned to the supplier, and who recorded that is kept.
MIGRATE_V13 = """
CREATE TABLE IF NOT EXISTS branch_extra_items (
    id INTEGER PRIMARY KEY,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    delivery_no INTEGER NOT NULL,
    item_code TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    size TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '',
    added_by INTEGER NOT NULL REFERENCES users(id),
    added_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_branch_extra_po ON branch_extra_items(po_id);
CREATE TABLE IF NOT EXISTS excess_returns (
    id INTEGER PRIMARY KEY,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    po_item_id INTEGER NOT NULL,
    pieces_m INTEGER NOT NULL CHECK (pieces_m > 0),
    returned_by INTEGER NOT NULL REFERENCES users(id),
    returned_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_excess_returns_po ON excess_returns(po_id);
CREATE TRIGGER IF NOT EXISTS branch_extra_no_update BEFORE UPDATE ON branch_extra_items
BEGIN SELECT RAISE(ABORT, 'A count is permanent. Recount instead'); END;
CREATE TRIGGER IF NOT EXISTS branch_extra_no_delete BEFORE DELETE ON branch_extra_items
BEGIN SELECT RAISE(ABORT, 'A count is permanent. Recount instead'); END;
CREATE TRIGGER IF NOT EXISTS excess_returns_no_update BEFORE UPDATE ON excess_returns
BEGIN SELECT RAISE(ABORT, 'A recorded return is permanent'); END;
CREATE TRIGGER IF NOT EXISTS excess_returns_no_delete BEFORE DELETE ON excess_returns
BEGIN SELECT RAISE(ABORT, 'A recorded return is permanent'); END;
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V13', 'system', 'Excess delivery added: products not on a PO, and returns.');
PRAGMA user_version = 13;
"""


# ------------------------------------------------------------------ version 14: claim stubs

# Damaged goods. The figures and pdf are fixed once approved, the same as a debit memo. Only where the goods are can change after that.
MIGRATE_V14 = """
CREATE TABLE IF NOT EXISTS cs_number_sequences (
    year INTEGER PRIMARY KEY,
    last_number INTEGER NOT NULL CHECK (last_number >= 1)
);
CREATE TRIGGER IF NOT EXISTS cs_sequence_never_backward BEFORE UPDATE ON cs_number_sequences
WHEN NEW.last_number <= OLD.last_number
BEGIN SELECT RAISE(ABORT, 'Claim stub numbers cannot be reused'); END;
CREATE TRIGGER IF NOT EXISTS cs_sequence_no_delete BEFORE DELETE ON cs_number_sequences
BEGIN SELECT RAISE(ABORT, 'Claim stub number history cannot be deleted'); END;
CREATE TABLE IF NOT EXISTS claim_stubs (
    id INTEGER PRIMARY KEY,
    cs_number TEXT NOT NULL UNIQUE,
    cs_year INTEGER NOT NULL,
    cs_seq INTEGER NOT NULL,
    cs_date TEXT NOT NULL,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    status TEXT NOT NULL CHECK (status IN ('ISSUED','AT_WAREHOUSE','COLLECTED','FORFEITED','CANCELLED')),
    total_c INTEGER NOT NULL CHECK (total_c >= 0),
    remarks TEXT NOT NULL DEFAULT '',
    approved_by INTEGER NOT NULL REFERENCES users(id),
    approved_at TEXT NOT NULL,
    arrived_by INTEGER REFERENCES users(id),
    arrived_at TEXT,
    closed_by INTEGER REFERENCES users(id),
    closed_at TEXT,
    collected_by_name TEXT NOT NULL DEFAULT '',
    cancel_reason TEXT NOT NULL DEFAULT '',
    legacy_cs_number TEXT NOT NULL DEFAULT '',
    pdf BLOB NOT NULL DEFAULT x'',
    sha256 TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_claim_stubs_po ON claim_stubs(po_id);
CREATE TABLE IF NOT EXISTS claim_stub_items (
    id INTEGER PRIMARY KEY,
    cs_id INTEGER NOT NULL REFERENCES claim_stubs(id),
    line_no INTEGER NOT NULL,
    po_item_id INTEGER NOT NULL,
    item_code TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL,
    size TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '',
    uom TEXT NOT NULL DEFAULT 'PC',
    pieces_m INTEGER NOT NULL CHECK (pieces_m > 0),
    net_unit_c INTEGER NOT NULL,
    pieces_per_unit INTEGER NOT NULL,
    amount_c INTEGER NOT NULL,
    UNIQUE (cs_id, line_no)
);
CREATE TRIGGER IF NOT EXISTS claim_stub_fixed BEFORE UPDATE OF cs_number, cs_year, cs_seq, cs_date, po_id, supplier_id, total_c, remarks, approved_by, approved_at ON claim_stubs
BEGIN SELECT RAISE(ABORT, 'An approved claim stub cannot change. Cancel it and make another'); END;
CREATE TRIGGER IF NOT EXISTS claim_stub_pdf_fixed BEFORE UPDATE OF pdf, sha256 ON claim_stubs
WHEN length(OLD.pdf) > 0
BEGIN SELECT RAISE(ABORT, 'An approved claim stub cannot change. Cancel it and make another'); END;
CREATE TRIGGER IF NOT EXISTS claim_stub_no_delete BEFORE DELETE ON claim_stubs
BEGIN SELECT RAISE(ABORT, 'A claim stub cannot be deleted. Cancel it instead'); END;
CREATE TRIGGER IF NOT EXISTS claim_stub_items_no_update BEFORE UPDATE ON claim_stub_items
BEGIN SELECT RAISE(ABORT, 'An approved claim stub cannot change. Cancel it and make another'); END;
CREATE TRIGGER IF NOT EXISTS claim_stub_items_no_delete BEFORE DELETE ON claim_stub_items
BEGIN SELECT RAISE(ABORT, 'An approved claim stub cannot change. Cancel it and make another'); END;
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V14', 'system', 'Claim stubs added for damaged goods.');
PRAGMA user_version = 14;
"""

MIGRATE_V15 = """
CREATE TABLE IF NOT EXISTS user_chips (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    initials TEXT NOT NULL DEFAULT '',
    color INTEGER NOT NULL DEFAULT 0 CHECK (color BETWEEN 0 AND 11)
);
INSERT OR IGNORE INTO user_chips(user_id, color) SELECT id, (id - 1) % 12 FROM users;
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V15', 'system', 'Each person gets initials and a color, to mark what they did.');
PRAGMA user_version = 15;
"""

MIGRATE_V16 = """
CREATE TABLE IF NOT EXISTS delivery_receipts (
    id INTEGER PRIMARY KEY,
    dr_number TEXT NOT NULL UNIQUE,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    delivery_no INTEGER NOT NULL CHECK (delivery_no >= 1),
    channel TEXT NOT NULL CHECK (channel IN ('OFFICE','EMAIL')),
    created_at TEXT NOT NULL,
    created_by INTEGER NOT NULL REFERENCES users(id),
    emailed_at TEXT,
    emailed_by INTEGER REFERENCES users(id),
    emailed_revision INTEGER,
    UNIQUE (po_id, delivery_no)
);
CREATE INDEX IF NOT EXISTS idx_dr_po ON delivery_receipts(po_id);
CREATE TRIGGER IF NOT EXISTS dr_number_fixed BEFORE UPDATE OF dr_number, po_id, delivery_no ON delivery_receipts
BEGIN SELECT RAISE(ABORT, 'DR numbers never change'); END;
CREATE TRIGGER IF NOT EXISTS dr_no_delete BEFORE DELETE ON delivery_receipts
BEGIN SELECT RAISE(ABORT, 'DRs cannot be deleted'); END;
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V16', 'system', 'POs issued from now on get a numbered Delivery Receipt.');
PRAGMA user_version = 16;
"""

# Added by db.initialize, once only: SQLite can't say IF NOT EXISTS for a column.
SUPPLIER_TERM_COLUMNS = {"term_discount_bp": "ALTER TABLE suppliers ADD COLUMN term_discount_bp INTEGER NOT NULL DEFAULT 0 CHECK (term_discount_bp >= 0);",
                         "term_discount_days": "ALTER TABLE suppliers ADD COLUMN term_discount_days INTEGER NOT NULL DEFAULT 0 CHECK (term_discount_days >= 0);"}

MIGRATE_V17 = """
CREATE TABLE IF NOT EXISTS dr_entries (
    dr_id INTEGER PRIMARY KEY REFERENCES delivery_receipts(id),
    dr_date TEXT NOT NULL,
    total_c INTEGER NOT NULL CHECK (total_c >= 0),
    entered_by INTEGER NOT NULL REFERENCES users(id),
    entered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_differences (
    of_id INTEGER PRIMARY KEY REFERENCES order_forms(id),
    difference_c INTEGER NOT NULL,
    reason TEXT NOT NULL,
    accepted_by INTEGER NOT NULL REFERENCES users(id),
    accepted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_checks (
    id INTEGER PRIMARY KEY,
    check_number TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL CHECK (source IN ('EXTERNAL','PROGRAM')),
    check_date TEXT NOT NULL,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    amount_c INTEGER NOT NULL CHECK (amount_c >= 0),
    credit_applied_c INTEGER NOT NULL DEFAULT 0,
    difference_reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','CANCELLED')),
    recorded_by INTEGER NOT NULL REFERENCES users(id),
    recorded_at TEXT NOT NULL,
    cancelled_by INTEGER REFERENCES users(id),
    cancelled_at TEXT,
    cancel_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS payment_check_ofs (
    check_id INTEGER NOT NULL REFERENCES payment_checks(id),
    of_id INTEGER NOT NULL REFERENCES order_forms(id),
    amount_c INTEGER NOT NULL,
    PRIMARY KEY (check_id, of_id)
);
CREATE INDEX IF NOT EXISTS idx_check_ofs_of ON payment_check_ofs(of_id);
CREATE TRIGGER IF NOT EXISTS check_number_fixed BEFORE UPDATE OF check_number, amount_c, supplier_id ON payment_checks
BEGIN SELECT RAISE(ABORT, 'A recorded check cannot be changed. Cancel it instead'); END;
CREATE TRIGGER IF NOT EXISTS check_no_delete BEFORE DELETE ON payment_checks
BEGIN SELECT RAISE(ABORT, 'Checks cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS check_stays_cancelled BEFORE UPDATE OF status ON payment_checks
WHEN OLD.status = 'CANCELLED'
BEGIN SELECT RAISE(ABORT, 'A cancelled check stays cancelled'); END;
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V17', 'system', 'Payments added: DR totals, what to pay and when, and a record of each check. Suppliers can carry a term discount.');
PRAGMA user_version = 17;
"""


# ------------------------------------------------------------------ version 18: an Accounting role, and claimed checks

# A check is claimed when the supplier takes it. Printed or due is worked out from its date, so only the claim needs keeping.
MIGRATE_V18 = """
CREATE TABLE new_user_roles (
    user_id INTEGER NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK (role IN ('ADMIN','BUYER','APPROVER','VIEWER','RECEIVER','ACCOUNTING')),
    PRIMARY KEY (user_id, role)
);
INSERT INTO new_user_roles(user_id, role) SELECT user_id, role FROM user_roles;
DROP TABLE user_roles;
ALTER TABLE new_user_roles RENAME TO user_roles;
CREATE TABLE IF NOT EXISTS check_claims (
    check_id INTEGER PRIMARY KEY REFERENCES payment_checks(id),
    claimed_on TEXT NOT NULL,
    claimed_by_name TEXT NOT NULL DEFAULT '',
    recorded_by INTEGER NOT NULL REFERENCES users(id),
    recorded_at TEXT NOT NULL
);
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V18', 'system', 'Accounting role added, and checks can be marked claimed.');
PRAGMA user_version = 18;
"""


# ------------------------------------------------------------------ version 19: checks clearing the bank

# Claimed means the supplier has it. Cleared means the money has left the account, which is what cash flow really turns on.
MIGRATE_V19 = """
CREATE TABLE IF NOT EXISTS check_clears (
    check_id INTEGER PRIMARY KEY REFERENCES payment_checks(id),
    cleared_on TEXT NOT NULL,
    recorded_by INTEGER NOT NULL REFERENCES users(id),
    recorded_at TEXT NOT NULL
);
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V19', 'system', 'Checks can now be marked cleared by the bank.');
PRAGMA user_version = 19;
"""


# ------------------------------------------------------------------ version 20: item passes

# Excess pieces leave a branch on an item pass, which prints once. Its number and lines never change. A line may be a product that wasn't on the PO, which counts refer to as minus its id.
MIGRATE_V20 = """
CREATE TABLE IF NOT EXISTS ip_number_sequences (
    year INTEGER PRIMARY KEY,
    last_number INTEGER NOT NULL CHECK (last_number >= 1)
);
CREATE TRIGGER IF NOT EXISTS ip_sequence_never_backward BEFORE UPDATE ON ip_number_sequences
WHEN NEW.last_number <= OLD.last_number
BEGIN SELECT RAISE(ABORT, 'Item pass numbers cannot be reused'); END;
CREATE TRIGGER IF NOT EXISTS ip_sequence_no_delete BEFORE DELETE ON ip_number_sequences
BEGIN SELECT RAISE(ABORT, 'Item pass number history cannot be deleted'); END;
CREATE TABLE IF NOT EXISTS item_passes (
    id INTEGER PRIMARY KEY,
    ip_number TEXT NOT NULL UNIQUE,
    ip_year INTEGER NOT NULL,
    ip_seq INTEGER NOT NULL,
    ip_date TEXT NOT NULL,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    status TEXT NOT NULL CHECK (status IN ('PRINTED','AT_WAREHOUSE','COLLECTED','FORFEITED','CANCELLED')),
    printed_by INTEGER NOT NULL REFERENCES users(id),
    printed_at TEXT NOT NULL,
    pdf BLOB,
    sha256 TEXT,
    arrived_by INTEGER REFERENCES users(id),
    arrived_at TEXT,
    closed_by INTEGER REFERENCES users(id),
    closed_at TEXT,
    collected_by_name TEXT NOT NULL DEFAULT '',
    cancel_reason TEXT NOT NULL DEFAULT '',
    UNIQUE (ip_year, ip_seq)
);
CREATE INDEX IF NOT EXISTS idx_item_passes_po ON item_passes(po_id);
CREATE TABLE IF NOT EXISTS item_pass_items (
    id INTEGER PRIMARY KEY,
    ip_id INTEGER NOT NULL REFERENCES item_passes(id),
    line_no INTEGER NOT NULL,
    po_item_id INTEGER NOT NULL,
    item_code TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL,
    size TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '',
    uom TEXT NOT NULL DEFAULT '',
    pieces_m INTEGER NOT NULL CHECK (pieces_m > 0),
    UNIQUE (ip_id, line_no)
);
CREATE TRIGGER IF NOT EXISTS item_pass_fixed BEFORE UPDATE OF ip_number, ip_year, ip_seq, ip_date, po_id, supplier_id, printed_by, printed_at ON item_passes
BEGIN SELECT RAISE(ABORT, 'A printed item pass cannot change. Cancel it and print another'); END;
CREATE TRIGGER IF NOT EXISTS item_pass_pdf_fixed BEFORE UPDATE OF pdf, sha256 ON item_passes
WHEN OLD.pdf IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'A printed item pass cannot change. Cancel it and print another'); END;
CREATE TRIGGER IF NOT EXISTS item_pass_no_delete BEFORE DELETE ON item_passes
BEGIN SELECT RAISE(ABORT, 'An item pass cannot be deleted. Cancel it instead'); END;
CREATE TRIGGER IF NOT EXISTS item_pass_items_no_update BEFORE UPDATE ON item_pass_items
BEGIN SELECT RAISE(ABORT, 'A printed item pass cannot change. Cancel it and print another'); END;
CREATE TRIGGER IF NOT EXISTS item_pass_items_no_delete BEFORE DELETE ON item_pass_items
BEGIN SELECT RAISE(ABORT, 'A printed item pass cannot change. Cancel it and print another'); END;
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V20', 'system', 'Item passes added for excess pieces.');
PRAGMA user_version = 20;
"""


# ------------------------------------------------------------------ version 21: records that stay as they are, and faster lookups

# Found in the November 6 audit: a cancelled memo, stub or pass could be revived directly in the file, the link between a check and its orders
# could be changed or deleted (a way to pay twice), and count statuses could be rewritten. The indexes stop list screens reading the whole audit log per row.
MIGRATE_V21 = """
CREATE TRIGGER IF NOT EXISTS debit_memo_stays_cancelled BEFORE UPDATE OF status ON debit_memos
WHEN OLD.status = 'CANCELLED' AND NEW.status <> 'CANCELLED'
BEGIN SELECT RAISE(ABORT, 'A cancelled debit memo stays cancelled'); END;
CREATE TRIGGER IF NOT EXISTS claim_stub_finished_stays BEFORE UPDATE OF status ON claim_stubs
WHEN OLD.status IN ('COLLECTED', 'FORFEITED', 'CANCELLED') AND NEW.status <> OLD.status
BEGIN SELECT RAISE(ABORT, 'A finished claim stub stays as it is'); END;
CREATE TRIGGER IF NOT EXISTS item_pass_finished_stays BEFORE UPDATE OF status ON item_passes
WHEN OLD.status IN ('COLLECTED', 'FORFEITED', 'CANCELLED') AND NEW.status <> OLD.status
BEGIN SELECT RAISE(ABORT, 'A finished item pass stays as it is'); END;
CREATE TRIGGER IF NOT EXISTS check_orders_no_update BEFORE UPDATE ON payment_check_ofs
BEGIN SELECT RAISE(ABORT, 'What a check paid is permanent. Cancel the check instead'); END;
CREATE TRIGGER IF NOT EXISTS check_orders_no_delete BEFORE DELETE ON payment_check_ofs
BEGIN SELECT RAISE(ABORT, 'What a check paid is permanent. Cancel the check instead'); END;
CREATE TRIGGER IF NOT EXISTS check_date_and_credit_fixed BEFORE UPDATE OF check_date, credit_applied_c ON payment_checks
WHEN NEW.check_date IS NOT OLD.check_date OR NEW.credit_applied_c IS NOT OLD.credit_applied_c
BEGIN SELECT RAISE(ABORT, 'A recorded check cannot change. Cancel it and record it again'); END;
CREATE TRIGGER IF NOT EXISTS branch_count_status_path BEFORE UPDATE OF status ON branch_counts
WHEN NEW.status <> OLD.status AND NOT ((OLD.status = 'WAITING' AND NEW.status IN ('APPROVED', 'MISMATCH', 'REPLACED')) OR (OLD.status = 'APPROVED' AND NEW.status = 'REPLACED'))
BEGIN SELECT RAISE(ABORT, 'A count can only move forward. Recount instead'); END;
CREATE INDEX IF NOT EXISTS idx_audit_action_entity ON audit_logs(action, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_action_of ON audit_logs(action, of_id);
CREATE INDEX IF NOT EXISTS idx_audit_action_po ON audit_logs(action, po_id);
CREATE INDEX IF NOT EXISTS idx_payment_checks_supplier ON payment_checks(supplier_id, status);
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V21', 'system', 'Permanent records protected further, and faster lookups.');
PRAGMA user_version = 21;
"""

# Two home office steps, each kept for good. A PO received in full outside the program, while branches can't count in it yet,
# and a delivery the home office allowed to be counted once more: an accepted one reopened for a recount, or one whose counts didn't match twice.
MIGRATE_V22 = """
CREATE TABLE IF NOT EXISTS outside_receipts (
    po_id INTEGER PRIMARY KEY REFERENCES purchase_orders(id),
    reason TEXT NOT NULL,
    recorded_by INTEGER NOT NULL REFERENCES users(id),
    recorded_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS outside_receipts_no_update BEFORE UPDATE ON outside_receipts
BEGIN SELECT RAISE(ABORT, 'A delivery received outside the program stays on record'); END;
CREATE TRIGGER IF NOT EXISTS outside_receipts_no_delete BEFORE DELETE ON outside_receipts
BEGIN SELECT RAISE(ABORT, 'A delivery received outside the program stays on record'); END;
CREATE TABLE IF NOT EXISTS count_reopens (
    id INTEGER PRIMARY KEY,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    delivery_no INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('RECOUNT', 'ROUND')),
    reason TEXT NOT NULL,
    opened_by INTEGER NOT NULL REFERENCES users(id),
    opened_at TEXT NOT NULL,
    used_by_count_id INTEGER REFERENCES branch_counts(id)
);
CREATE INDEX IF NOT EXISTS idx_count_reopens_po ON count_reopens(po_id, delivery_no);
CREATE TRIGGER IF NOT EXISTS count_reopens_used_once BEFORE UPDATE ON count_reopens
WHEN OLD.used_by_count_id IS NOT NULL OR NEW.po_id IS NOT OLD.po_id OR NEW.delivery_no IS NOT OLD.delivery_no OR NEW.attempt_no IS NOT OLD.attempt_no
  OR NEW.kind IS NOT OLD.kind OR NEW.reason IS NOT OLD.reason OR NEW.opened_by IS NOT OLD.opened_by OR NEW.opened_at IS NOT OLD.opened_at
BEGIN SELECT RAISE(ABORT, 'A reopened count stays on record'); END;
CREATE TRIGGER IF NOT EXISTS count_reopens_no_delete BEFORE DELETE ON count_reopens
BEGIN SELECT RAISE(ABORT, 'A reopened count stays on record'); END;
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V22', 'system', 'Received outside the program, and recounts the home office allows.');
PRAGMA user_version = 22;
"""


# ------------------------------------------------------------------ version 23: warehouse role, sign-offs, forfeited goods' prices

# The warehouse records goods arriving at the main warehouse. Admin or management signs off any payment amount that differs, and forfeited goods get a selling price.
MIGRATE_V23 = """
CREATE TABLE new_user_roles (
    user_id INTEGER NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK (role IN ('ADMIN','BUYER','APPROVER','VIEWER','RECEIVER','ACCOUNTING','WAREHOUSE')),
    PRIMARY KEY (user_id, role)
);
INSERT INTO new_user_roles(user_id, role) SELECT user_id, role FROM user_roles;
DROP TABLE user_roles;
ALTER TABLE new_user_roles RENAME TO user_roles;
CREATE TABLE IF NOT EXISTS payment_signoffs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('DIFFERENCE', 'CHECK')),
    ref_id INTEGER NOT NULL,
    amount_c INTEGER NOT NULL,
    requested_by INTEGER NOT NULL REFERENCES users(id),
    requested_at TEXT NOT NULL,
    signed_by INTEGER REFERENCES users(id),
    signed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_payment_signoffs_ref ON payment_signoffs(kind, ref_id);
CREATE TRIGGER IF NOT EXISTS payment_signoffs_signed_stays BEFORE UPDATE ON payment_signoffs
WHEN OLD.signed_by IS NOT NULL OR NEW.kind IS NOT OLD.kind OR NEW.ref_id IS NOT OLD.ref_id OR NEW.amount_c IS NOT OLD.amount_c
  OR NEW.requested_by IS NOT OLD.requested_by OR NEW.requested_at IS NOT OLD.requested_at
BEGIN SELECT RAISE(ABORT, 'A sign-off stays on record'); END;
CREATE TRIGGER IF NOT EXISTS payment_signoffs_no_delete BEFORE DELETE ON payment_signoffs
BEGIN SELECT RAISE(ABORT, 'A sign-off stays on record'); END;
CREATE TABLE IF NOT EXISTS forfeit_prices (
    kind TEXT NOT NULL CHECK (kind IN ('CS', 'IP')),
    item_id INTEGER NOT NULL,
    price_c INTEGER NOT NULL CHECK (price_c > 0),
    set_by INTEGER NOT NULL REFERENCES users(id),
    set_at TEXT NOT NULL,
    PRIMARY KEY (kind, item_id)
);
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V23', 'system', 'Warehouse role, sign-offs on payment differences, and selling prices for forfeited goods.');
PRAGMA user_version = 23;
"""


# ------------------------------------------------------------------ version 24: Central Warehouse

# Every order reaches the warehouse first. The warehouse records the boxes for each branch PO, the pieces it stickered for suppliers whose goods come without stickers,
# and when it shipped them on. Orders printed before today went straight to the branches, so they aren't asked for.
# Since version 25, warehouse_receipts and warehouse_stickered are frozen legacy tables: V25 copied them into warehouse_deliveries and
# warehouse_delivery_items, nothing writes them any more, and they are kept only so older data stays as it was. Never drop them.
MIGRATE_V24 = """
CREATE TABLE IF NOT EXISTS warehouse_receipts (
    po_id INTEGER PRIMARY KEY REFERENCES purchase_orders(id),
    boxes INTEGER NOT NULL CHECK (boxes > 0),
    remarks TEXT NOT NULL DEFAULT '',
    received_by INTEGER NOT NULL REFERENCES users(id),
    received_at TEXT NOT NULL,
    shipped_by INTEGER REFERENCES users(id),
    shipped_at TEXT
);
CREATE TABLE IF NOT EXISTS warehouse_stickered (
    po_item_id INTEGER PRIMARY KEY REFERENCES purchase_order_items(id),
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    pieces_m INTEGER NOT NULL CHECK (pieces_m >= 0)
);
CREATE TRIGGER IF NOT EXISTS warehouse_shipped_stays BEFORE UPDATE ON warehouse_receipts
WHEN OLD.shipped_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'Goods shipped from the warehouse stay on record'); END;
CREATE TRIGGER IF NOT EXISTS warehouse_receipts_no_delete BEFORE DELETE ON warehouse_receipts
BEGIN SELECT RAISE(ABORT, 'Goods received at the warehouse stay on record'); END;
CREATE TRIGGER IF NOT EXISTS warehouse_stickered_after_shipping BEFORE UPDATE ON warehouse_stickered
WHEN (SELECT shipped_at FROM warehouse_receipts WHERE po_id = OLD.po_id) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'Goods shipped from the warehouse stay on record'); END;
INSERT OR IGNORE INTO system_settings(key, value, updated_at) VALUES ('warehouse.live_since', datetime('now','localtime'), datetime('now','localtime'));
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V24', 'system', 'Central Warehouse: boxes, stickering and shipping to the branches.');
PRAGMA user_version = 24;
"""


# ------------------------------------------------------------------ version 25: one warehouse record per delivery, boxes at the branches, counts on arrival, job titles

# A supplier can deliver an order in more than one go, and a branch can get nothing in one of them, so the warehouse keeps one record per delivery
# to each branch, 0 boxes allowed, and ships each branch on its own. Whoever received it first stays on record; corrections go in the log.
# The branches type the boxes that reached them, so a box lost on the way shows. Goods coming back on a claim stub or item pass are counted
# at the warehouse before anyone collects them. A person's job title, such as Warehouse Head, is shown beside their name and changes no permission.
MIGRATE_V25 = """
CREATE TABLE IF NOT EXISTS warehouse_deliveries (
    id INTEGER PRIMARY KEY,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    delivery_no INTEGER NOT NULL CHECK (delivery_no > 0),
    boxes INTEGER NOT NULL CHECK (boxes >= 0),
    remarks TEXT NOT NULL DEFAULT '',
    received_by INTEGER NOT NULL REFERENCES users(id),
    received_at TEXT NOT NULL,
    corrected_by INTEGER REFERENCES users(id),
    corrected_at TEXT,
    shipped_by INTEGER REFERENCES users(id),
    shipped_at TEXT,
    UNIQUE (po_id, delivery_no)
);
CREATE TABLE IF NOT EXISTS warehouse_delivery_items (
    delivery_id INTEGER NOT NULL REFERENCES warehouse_deliveries(id),
    po_item_id INTEGER NOT NULL REFERENCES purchase_order_items(id),
    pieces_m INTEGER NOT NULL CHECK (pieces_m >= 0),
    PRIMARY KEY (delivery_id, po_item_id)
);
INSERT OR IGNORE INTO warehouse_deliveries(po_id, delivery_no, boxes, remarks, received_by, received_at, shipped_by, shipped_at)
SELECT po_id, 1, boxes, remarks, received_by, received_at, shipped_by, shipped_at FROM warehouse_receipts;
INSERT OR IGNORE INTO warehouse_delivery_items(delivery_id, po_item_id, pieces_m)
SELECT d.id, s.po_item_id, s.pieces_m FROM warehouse_stickered s JOIN warehouse_deliveries d ON d.po_id = s.po_id AND d.delivery_no = 1;
CREATE TRIGGER IF NOT EXISTS warehouse_delivery_fixed BEFORE UPDATE ON warehouse_deliveries
WHEN OLD.shipped_at IS NOT NULL OR NEW.po_id IS NOT OLD.po_id OR NEW.delivery_no IS NOT OLD.delivery_no OR NEW.received_by IS NOT OLD.received_by OR NEW.received_at IS NOT OLD.received_at
BEGIN SELECT RAISE(ABORT, 'Goods received at the warehouse stay on record'); END;
CREATE TRIGGER IF NOT EXISTS warehouse_delivery_no_delete BEFORE DELETE ON warehouse_deliveries
BEGIN SELECT RAISE(ABORT, 'Goods received at the warehouse stay on record'); END;
CREATE TRIGGER IF NOT EXISTS warehouse_delivery_items_shipped_insert BEFORE INSERT ON warehouse_delivery_items
WHEN (SELECT shipped_at FROM warehouse_deliveries WHERE id = NEW.delivery_id) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'Goods shipped from the warehouse stay on record'); END;
CREATE TRIGGER IF NOT EXISTS warehouse_delivery_items_shipped_update BEFORE UPDATE ON warehouse_delivery_items
WHEN (SELECT shipped_at FROM warehouse_deliveries WHERE id = OLD.delivery_id) IS NOT NULL OR NEW.delivery_id IS NOT OLD.delivery_id OR NEW.po_item_id IS NOT OLD.po_item_id
BEGIN SELECT RAISE(ABORT, 'Goods shipped from the warehouse stay on record'); END;
CREATE TRIGGER IF NOT EXISTS warehouse_delivery_items_no_delete BEFORE DELETE ON warehouse_delivery_items
BEGIN SELECT RAISE(ABORT, 'Goods received at the warehouse stay on record'); END;
CREATE TRIGGER IF NOT EXISTS warehouse_stickered_no_delete BEFORE DELETE ON warehouse_stickered
BEGIN SELECT RAISE(ABORT, 'Goods received at the warehouse stay on record'); END;
CREATE TRIGGER IF NOT EXISTS warehouse_stickered_shipped_insert BEFORE INSERT ON warehouse_stickered
WHEN (SELECT shipped_at FROM warehouse_receipts WHERE po_id = NEW.po_id) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'Goods shipped from the warehouse stay on record'); END;
CREATE TABLE IF NOT EXISTS branch_count_boxes (
    count_id INTEGER PRIMARY KEY REFERENCES branch_counts(id),
    boxes INTEGER NOT NULL CHECK (boxes >= 0)
);
CREATE TRIGGER IF NOT EXISTS branch_count_boxes_no_update BEFORE UPDATE ON branch_count_boxes
BEGIN SELECT RAISE(ABORT, 'Counts are permanent'); END;
CREATE TRIGGER IF NOT EXISTS branch_count_boxes_no_delete BEFORE DELETE ON branch_count_boxes
BEGIN SELECT RAISE(ABORT, 'Counts are permanent'); END;
CREATE TABLE IF NOT EXISTS goods_arrivals (
    kind TEXT NOT NULL CHECK (kind IN ('CS', 'IP')),
    item_id INTEGER NOT NULL,
    pieces_m INTEGER NOT NULL CHECK (pieces_m >= 0),
    counted_by INTEGER NOT NULL REFERENCES users(id),
    counted_at TEXT NOT NULL,
    PRIMARY KEY (kind, item_id)
);
CREATE TRIGGER IF NOT EXISTS goods_arrivals_no_update BEFORE UPDATE ON goods_arrivals
BEGIN SELECT RAISE(ABORT, 'Counts are permanent'); END;
CREATE TRIGGER IF NOT EXISTS goods_arrivals_no_delete BEFORE DELETE ON goods_arrivals
BEGIN SELECT RAISE(ABORT, 'Counts are permanent'); END;
CREATE TABLE IF NOT EXISTS user_titles (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    title TEXT NOT NULL,
    updated_by INTEGER REFERENCES users(id),
    updated_at TEXT NOT NULL
);
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V25', 'system', 'One warehouse record per delivery, boxes counted at the branches, counts on goods coming back, and job titles.');
PRAGMA user_version = 25;
"""


# ------------------------------------------------------------------ version 26: damage found after the delivery was settled

# Sometimes damage shows only after the check was made, when the goods are unpacked. It goes on a claim stub like any damage, typed by a purchaser
# instead of coming from the branch count, so this marks which stubs those are, who found it and why. The credit goes on the supplier's next check.
MIGRATE_V26 = """
CREATE TABLE IF NOT EXISTS late_damage (
    cs_id INTEGER PRIMARY KEY REFERENCES claim_stubs(id),
    reason TEXT NOT NULL,
    found_by INTEGER NOT NULL REFERENCES users(id),
    found_at TEXT NOT NULL,
    paid_before INTEGER
);
CREATE TRIGGER IF NOT EXISTS late_damage_no_update BEFORE UPDATE ON late_damage
BEGIN SELECT RAISE(ABORT, 'A claim stub stays on record'); END;
CREATE TRIGGER IF NOT EXISTS late_damage_no_delete BEFORE DELETE ON late_damage
BEGIN SELECT RAISE(ABORT, 'A claim stub stays on record'); END;
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V26', 'system', 'Claim stubs for damage found after the delivery was settled.');
PRAGMA user_version = 26;
"""


# ------------------------------------------------------------------ version 27: damage the branch reports, for Accounting or management to approve

# A receiver who finds damage after the count was agreed reports it here, in pieces only. Accounting or management approves it, which makes the claim stub,
# or rejects it with a reason. What the branch sent stays on record exactly as sent.
MIGRATE_V27 = """
CREATE TABLE IF NOT EXISTS claim_requests (
    id INTEGER PRIMARY KEY,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED')),
    requested_by INTEGER NOT NULL REFERENCES users(id),
    requested_at TEXT NOT NULL,
    decided_by INTEGER REFERENCES users(id),
    decided_at TEXT,
    decision_reason TEXT NOT NULL DEFAULT '',
    cs_id INTEGER REFERENCES claim_stubs(id)
);
CREATE TABLE IF NOT EXISTS claim_request_items (
    request_id INTEGER NOT NULL REFERENCES claim_requests(id),
    po_item_id INTEGER NOT NULL REFERENCES purchase_order_items(id),
    pieces_m INTEGER NOT NULL CHECK (pieces_m > 0),
    PRIMARY KEY (request_id, po_item_id)
);
CREATE TRIGGER IF NOT EXISTS claim_request_decided_stays BEFORE UPDATE ON claim_requests
WHEN OLD.status <> 'PENDING' OR NEW.po_id IS NOT OLD.po_id OR NEW.reason IS NOT OLD.reason OR NEW.requested_by IS NOT OLD.requested_by OR NEW.requested_at IS NOT OLD.requested_at
BEGIN SELECT RAISE(ABORT, 'A damage report stays on record'); END;
CREATE TRIGGER IF NOT EXISTS claim_request_no_delete BEFORE DELETE ON claim_requests
BEGIN SELECT RAISE(ABORT, 'A damage report stays on record'); END;
CREATE TRIGGER IF NOT EXISTS claim_request_items_no_update BEFORE UPDATE ON claim_request_items
BEGIN SELECT RAISE(ABORT, 'A damage report stays on record'); END;
CREATE TRIGGER IF NOT EXISTS claim_request_items_no_delete BEFORE DELETE ON claim_request_items
BEGIN SELECT RAISE(ABORT, 'A damage report stays on record'); END;
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V27', 'system', 'Damage reported by the branches, approved by Accounting or management.');
PRAGMA user_version = 27;
"""


# ------------------------------------------------------------------ version 28: more records the database itself keeps as they are, and faster lookups

# Found in the November 7 audit. An OF or PO could be moved to any status directly in the file, for example back to Draft to change its
# prices, and lines could be added to a finished memo, stub, pass, agreed count or decided damage report. Each status may now only move the
# way the program moves it, unless a recorded operation holds the write guard. Nothing here changes or removes a row: only guards and indexes.
# Six indexes that only repeated the start of another index are dropped. idx_po_year stays: the index it repeats covers only POs made from an OF.
# A damage report's lines are written before the report is logged, and a live check's orders before the check is logged, so once either is
# logged its lines are fixed. A late damage claim stub may only name a check that is on record as the one it came after.
#
# Business rules added in version 28: three columns, added only where missing (db._upgrade puts V28_COLUMNS in front):
# - payment_checks.expected_c: what the program worked out for the check when it was recorded. A lower check with a reason, signed off,
#   is final, and the difference is an agreed deduction measured against this. Checks recorded before the upgrade get it from their
#   CHECK_RECORD log line; a check with no readable line stays empty (no agreed deduction, as before). Once set it never changes.
# - payment_differences.dr_lower_c: how much lower than the program's figure the accepted DRs are, in centavos, fixed when Accounting accepts.
#   Empty on differences accepted before the upgrade.
# - debit_memos.paid_back_check_id: the live check that had already paid the order when the memo was cancelled, so the next check pays the
#   memo back. Set only in the same change that cancels the memo, never afterwards. Memos cancelled before the upgrade get the earliest live
#   check on their OF recorded before the cancel, the payment their cancel record promised (AUD-062).
# - dr_line_differences: what the supplier's DR says on each product that differs from ours, pieces and/or unit price, so the
#   difference and its sign-off show which products differ and by how much. Never changed or removed: each save adds a numbered set
#   (save_no) for the DR, and the newest set is what the DR says now. A set with only an empty row (no po_item_id) means no product differs.
#   po_item_id has no link to the PO's products on purpose, so a product rewritten by an amendment never blocks it.
# A damage report already waiting when the file was upgraded takes no new line either (AUD-130): the upgrade note keeps the highest report
# number there was, and those reports count as sent. A report number, not a time, so a PC whose clock is off can't block or free one.
V28_COLUMNS = {
    ("payment_checks", "expected_c"): "ALTER TABLE payment_checks ADD COLUMN expected_c INTEGER;\n",
    ("payment_differences", "dr_lower_c"): "ALTER TABLE payment_differences ADD COLUMN dr_lower_c INTEGER CHECK (dr_lower_c IS NULL OR dr_lower_c >= 0);\n",
    ("debit_memos", "paid_back_check_id"): "ALTER TABLE debit_memos ADD COLUMN paid_back_check_id INTEGER REFERENCES payment_checks(id);\n",
}

MIGRATE_V28 = """
UPDATE payment_checks SET expected_c = (
    SELECT CASE WHEN json_valid(a.new_value) THEN json_extract(a.new_value, '$.expected_c') END FROM audit_logs a
    WHERE a.action = 'CHECK_RECORD' AND a.entity_type = 'payment_check' AND a.entity_id = payment_checks.id
      AND (CASE WHEN json_valid(a.new_value) THEN json_type(a.new_value, '$.expected_c') END) = 'integer'
    ORDER BY a.id LIMIT 1)
WHERE expected_c IS NULL;
UPDATE debit_memos SET paid_back_check_id = (
    SELECT c.id FROM payment_checks c JOIN payment_check_ofs o ON o.check_id = c.id JOIN purchase_orders p ON p.of_id = o.of_id
    WHERE p.id = debit_memos.po_id AND c.status = 'ACTIVE' AND c.recorded_at < debit_memos.cancelled_at
    ORDER BY c.recorded_at, c.id LIMIT 1)
WHERE status = 'CANCELLED' AND paid_back_check_id IS NULL AND cancelled_at IS NOT NULL;
CREATE TABLE IF NOT EXISTS dr_line_differences (
    id INTEGER PRIMARY KEY,
    dr_id INTEGER NOT NULL REFERENCES delivery_receipts(id),
    save_no INTEGER NOT NULL CHECK (save_no >= 1),
    po_item_id INTEGER,
    pieces_m INTEGER CHECK (pieces_m IS NULL OR pieces_m >= 0),
    unit_c INTEGER CHECK (unit_c IS NULL OR unit_c >= 0),
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    CHECK (po_item_id IS NOT NULL OR (pieces_m IS NULL AND unit_c IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_dr_line_differences_dr ON dr_line_differences(dr_id, save_no);
CREATE TRIGGER IF NOT EXISTS dr_line_differences_fixed BEFORE UPDATE ON dr_line_differences
BEGIN SELECT RAISE(ABORT, 'What a DR said, product by product, stays on record'); END;
CREATE TRIGGER IF NOT EXISTS dr_line_differences_no_delete BEFORE DELETE ON dr_line_differences
BEGIN SELECT RAISE(ABORT, 'What a DR said, product by product, stays on record'); END;
CREATE TRIGGER IF NOT EXISTS check_expected_fixed BEFORE UPDATE OF expected_c ON payment_checks
WHEN OLD.expected_c IS NOT NULL AND NEW.expected_c IS NOT OLD.expected_c
BEGIN SELECT RAISE(ABORT, 'What the program worked out for a check stays on record'); END;
CREATE TRIGGER IF NOT EXISTS debit_memo_paid_back_fixed BEFORE UPDATE OF paid_back_check_id ON debit_memos
WHEN NEW.paid_back_check_id IS NOT OLD.paid_back_check_id
 AND (OLD.paid_back_check_id IS NOT NULL OR OLD.status IS NOT 'ISSUED' OR NEW.status IS NOT 'CANCELLED')
BEGIN SELECT RAISE(ABORT, 'Which check a cancelled debit memo is paid back after stays on record'); END;
CREATE TRIGGER IF NOT EXISTS debit_memo_paid_back_new BEFORE INSERT ON debit_memos
WHEN NEW.paid_back_check_id IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'Which check a cancelled debit memo is paid back after stays on record'); END;
CREATE TRIGGER IF NOT EXISTS po_status_path BEFORE UPDATE OF status ON purchase_orders
WHEN NEW.status IS NOT OLD.status AND NOT EXISTS (SELECT 1 FROM po_write_guard) AND NOT (
    (OLD.status IN ('DRAFT', 'PENDING_APPROVAL') AND NEW.status = 'CANCELLED')
 OR (OLD.status = 'APPROVED' AND NEW.status IN ('ISSUED', 'PARTIALLY_DELIVERED', 'CANCELLED'))
 OR (OLD.status = 'ISSUED' AND NEW.status IN ('CLOSED', 'CANCELLED'))
 OR (OLD.status IN ('PARTIALLY_DELIVERED', 'FULLY_DELIVERED') AND NEW.status = 'CLOSED'))
BEGIN SELECT RAISE(ABORT, 'A PO status can only move the way the program moves it'); END;
CREATE TRIGGER IF NOT EXISTS of_status_path BEFORE UPDATE OF status ON order_forms
WHEN NEW.status IS NOT OLD.status AND NOT EXISTS (SELECT 1 FROM po_write_guard) AND NOT (
    (OLD.status = 'DRAFT' AND NEW.status IN ('PENDING_APPROVAL', 'APPROVED', 'CANCELLED'))
 OR (OLD.status = 'PENDING_APPROVAL' AND NEW.status IN ('DRAFT', 'APPROVED', 'CANCELLED'))
 OR (OLD.status = 'APPROVED' AND NEW.status = 'CANCELLED'))
BEGIN SELECT RAISE(ABORT, 'An OF status can only move the way the program moves it'); END;
CREATE TRIGGER IF NOT EXISTS po_approval_fixed BEFORE UPDATE OF approved_by, approval_date, auto_approved ON purchase_orders
WHEN NEW.approved_by IS NOT OLD.approved_by OR NEW.approval_date IS NOT OLD.approval_date OR NEW.auto_approved IS NOT OLD.auto_approved
BEGIN SELECT RAISE(ABORT, 'Who approved a PO, and when, stays on record'); END;
CREATE TRIGGER IF NOT EXISTS of_approval_fixed BEFORE UPDATE OF approved_by, approval_date, auto_approved ON order_forms
WHEN OLD.status NOT IN ('DRAFT', 'PENDING_APPROVAL')
 AND (NEW.approved_by IS NOT OLD.approved_by OR NEW.approval_date IS NOT OLD.approval_date OR NEW.auto_approved IS NOT OLD.auto_approved)
BEGIN SELECT RAISE(ABORT, 'Who approved an OF, and when, stays on record'); END;
CREATE TRIGGER IF NOT EXISTS po_pieces_locked BEFORE UPDATE OF total_pieces_m ON purchase_orders
WHEN OLD.status <> 'DRAFT' AND NEW.total_pieces_m IS NOT OLD.total_pieces_m AND NOT EXISTS (SELECT 1 FROM po_write_guard)
BEGIN SELECT RAISE(ABORT, 'A submitted PO can only change through an amendment'); END;
CREATE TRIGGER IF NOT EXISTS of_pieces_locked BEFORE UPDATE OF total_pieces_m ON order_forms
WHEN OLD.status <> 'DRAFT' AND NEW.total_pieces_m IS NOT OLD.total_pieces_m AND NOT EXISTS (SELECT 1 FROM po_write_guard)
BEGIN SELECT RAISE(ABORT, 'A submitted OF can only change through an amendment'); END;
CREATE TRIGGER IF NOT EXISTS po_payment_status_fixed BEFORE UPDATE OF payment_status ON purchase_orders
WHEN NEW.payment_status IS NOT OLD.payment_status
BEGIN SELECT RAISE(ABORT, 'The program does not change a PO payment status'); END;
CREATE TRIGGER IF NOT EXISTS po_payment_status_new BEFORE INSERT ON purchase_orders
WHEN NEW.payment_status IS NOT 'UNPAID'
BEGIN SELECT RAISE(ABORT, 'The program does not change a PO payment status'); END;
CREATE TRIGGER IF NOT EXISTS of_pos_generated_fixed BEFORE UPDATE OF pos_generated_at, pos_generated_by ON order_forms
WHEN OLD.pos_generated_at IS NOT NULL AND (NEW.pos_generated_at IS NOT OLD.pos_generated_at OR NEW.pos_generated_by IS NOT OLD.pos_generated_by)
BEGIN SELECT RAISE(ABORT, 'When the branch POs were made stays on record'); END;

CREATE TRIGGER IF NOT EXISTS debit_memo_items_after_issue BEFORE INSERT ON debit_memo_items
WHEN (SELECT length(pdf) > 0 OR status <> 'ISSUED' FROM debit_memos WHERE id = NEW.dm_id)
BEGIN SELECT RAISE(ABORT, 'An issued debit memo cannot change. Cancel it and issue another'); END;
CREATE TRIGGER IF NOT EXISTS debit_memo_cancel_fixed BEFORE UPDATE OF cancel_reason, cancelled_by, cancelled_at ON debit_memos
WHEN OLD.status = 'CANCELLED'
 AND (NEW.cancel_reason IS NOT OLD.cancel_reason OR NEW.cancelled_by IS NOT OLD.cancelled_by OR NEW.cancelled_at IS NOT OLD.cancelled_at)
BEGIN SELECT RAISE(ABORT, 'Why and by whom a debit memo was cancelled stays on record'); END;
CREATE TRIGGER IF NOT EXISTS claim_stub_items_after_approval BEFORE INSERT ON claim_stub_items
WHEN (SELECT length(pdf) > 0 OR status <> 'ISSUED' FROM claim_stubs WHERE id = NEW.cs_id)
BEGIN SELECT RAISE(ABORT, 'An approved claim stub cannot change. Cancel it and make another'); END;
CREATE TRIGGER IF NOT EXISTS claim_stub_not_back BEFORE UPDATE OF status ON claim_stubs
WHEN OLD.status = 'AT_WAREHOUSE' AND NEW.status = 'ISSUED'
BEGIN SELECT RAISE(ABORT, 'Goods that reached the warehouse stay recorded as there'); END;
CREATE TRIGGER IF NOT EXISTS item_pass_items_after_print BEFORE INSERT ON item_pass_items
WHEN (SELECT pdf IS NOT NULL OR status <> 'PRINTED' FROM item_passes WHERE id = NEW.ip_id)
BEGIN SELECT RAISE(ABORT, 'A printed item pass cannot change. Cancel it and print another'); END;
CREATE TRIGGER IF NOT EXISTS item_pass_not_back BEFORE UPDATE OF status ON item_passes
WHEN OLD.status = 'AT_WAREHOUSE' AND NEW.status = 'PRINTED'
BEGIN SELECT RAISE(ABORT, 'Goods that reached the warehouse stay recorded as there'); END;

CREATE TRIGGER IF NOT EXISTS branch_count_items_after_agreed BEFORE INSERT ON branch_count_items
WHEN EXISTS (SELECT 1 FROM branch_counts b WHERE b.id = NEW.count_id AND (b.status = 'MISMATCH' OR (b.status IN ('APPROVED', 'REPLACED')
  AND EXISTS (SELECT 1 FROM branch_received r WHERE r.po_id = b.po_id AND r.delivery_no = b.delivery_no AND r.attempt_no = b.attempt_no))))
BEGIN SELECT RAISE(ABORT, 'A count is permanent. Recount instead'); END;
CREATE TRIGGER IF NOT EXISTS branch_count_damage_after_agreed BEFORE INSERT ON branch_count_damage
WHEN EXISTS (SELECT 1 FROM branch_counts b WHERE b.id = NEW.count_id AND (b.status = 'MISMATCH' OR (b.status IN ('APPROVED', 'REPLACED')
  AND EXISTS (SELECT 1 FROM branch_received r WHERE r.po_id = b.po_id AND r.delivery_no = b.delivery_no AND r.attempt_no = b.attempt_no))))
BEGIN SELECT RAISE(ABORT, 'A count is permanent. Recount instead'); END;
CREATE TRIGGER IF NOT EXISTS branch_count_boxes_after_agreed BEFORE INSERT ON branch_count_boxes
WHEN EXISTS (SELECT 1 FROM branch_counts b WHERE b.id = NEW.count_id AND (b.status = 'MISMATCH' OR (b.status IN ('APPROVED', 'REPLACED')
  AND EXISTS (SELECT 1 FROM branch_received r WHERE r.po_id = b.po_id AND r.delivery_no = b.delivery_no AND r.attempt_no = b.attempt_no))))
BEGIN SELECT RAISE(ABORT, 'Counts are permanent'); END;
CREATE TRIGGER IF NOT EXISTS branch_count_agreed_by_new BEFORE INSERT ON branch_counts
WHEN NEW.status = 'APPROVED' AND NEW.approved_by IS NULL
BEGIN SELECT RAISE(ABORT, 'An agreed count names whose count it matched'); END;
CREATE TRIGGER IF NOT EXISTS branch_count_agreed_by BEFORE UPDATE OF status, approved_by, approved_at ON branch_counts
WHEN (NEW.status = 'APPROVED' AND NEW.approved_by IS NULL)
  OR (OLD.approved_by IS NOT NULL AND (NEW.approved_by IS NOT OLD.approved_by OR NEW.approved_at IS NOT OLD.approved_at))
BEGIN SELECT RAISE(ABORT, 'An agreed count names whose count it matched, and that stays on record'); END;

CREATE TRIGGER IF NOT EXISTS claim_request_items_after_decision BEFORE INSERT ON claim_request_items
WHEN (SELECT status FROM claim_requests WHERE id = NEW.request_id) <> 'PENDING'
BEGIN SELECT RAISE(ABORT, 'A damage report stays on record'); END;
-- claim_stubs writes a report's lines before it logs the report as sent, so once it is logged (and still waiting) it takes no more.
-- A report already there when the file was upgraded to 28 was logged the old way, so the upgrade note's highest report number covers it.
CREATE TRIGGER IF NOT EXISTS claim_request_items_once_sent BEFORE INSERT ON claim_request_items
WHEN EXISTS (SELECT 1 FROM audit_logs a WHERE a.action = 'CS_REQUEST' AND a.entity_id = NEW.request_id AND a.entity_type = 'claim_request')
  OR NEW.request_id <= (SELECT CASE WHEN json_valid(m.new_value) THEN json_extract(m.new_value, '$.last_claim_request') END
                        FROM audit_logs m WHERE m.action = 'MIGRATE_V28' ORDER BY m.id LIMIT 1)
BEGIN SELECT RAISE(ABORT, 'A damage report stays exactly as the branch sent it. Send a new report instead'); END;
CREATE TRIGGER IF NOT EXISTS late_damage_paid_before_known BEFORE INSERT ON late_damage
WHEN NEW.paid_before IS NOT NULL AND NOT EXISTS (SELECT 1 FROM payment_checks k WHERE k.id = NEW.paid_before)
BEGIN SELECT RAISE(ABORT, 'Damage found later must name a check that is on record'); END;
CREATE TRIGGER IF NOT EXISTS claim_request_approved_new BEFORE INSERT ON claim_requests
WHEN NEW.status = 'APPROVED' AND NEW.cs_id IS NULL
BEGIN SELECT RAISE(ABORT, 'An approved damage report has its claim stub'); END;
CREATE TRIGGER IF NOT EXISTS claim_request_approved_stub BEFORE UPDATE OF status, cs_id ON claim_requests
WHEN NEW.status = 'APPROVED' AND NEW.cs_id IS NULL
BEGIN SELECT RAISE(ABORT, 'An approved damage report has its claim stub'); END;

CREATE TRIGGER IF NOT EXISTS check_orders_not_cancelled BEFORE INSERT ON payment_check_ofs
WHEN (SELECT status FROM payment_checks WHERE id = NEW.check_id) = 'CANCELLED'
BEGIN SELECT RAISE(ABORT, 'What a check paid is permanent. Cancel the check instead'); END;
-- payments writes a check's orders before it logs the check, so once the check is logged (or claimed, or cleared) its orders are fixed.
CREATE TRIGGER IF NOT EXISTS check_orders_once_recorded BEFORE INSERT ON payment_check_ofs
WHEN EXISTS (SELECT 1 FROM audit_logs a WHERE a.action IN ('CHECK_RECORD', 'CHECK_CREDIT') AND a.entity_id = NEW.check_id AND a.entity_type = 'payment_check')
  OR EXISTS (SELECT 1 FROM check_claims k WHERE k.check_id = NEW.check_id)
  OR EXISTS (SELECT 1 FROM check_clears k WHERE k.check_id = NEW.check_id)
BEGIN SELECT RAISE(ABORT, 'What a check paid is permanent. Record a new check instead'); END;
CREATE TRIGGER IF NOT EXISTS check_orders_paid_once BEFORE INSERT ON payment_check_ofs
WHEN EXISTS (SELECT 1 FROM payment_check_ofs o JOIN payment_checks c ON c.id = o.check_id WHERE o.of_id = NEW.of_id AND o.check_id <> NEW.check_id AND c.status = 'ACTIVE')
BEGIN SELECT RAISE(ABORT, 'This order already has a live check. Cancel that check first'); END;
CREATE TRIGGER IF NOT EXISTS check_record_fixed BEFORE UPDATE OF source, difference_reason, recorded_by, recorded_at ON payment_checks
WHEN NEW.source IS NOT OLD.source OR NEW.difference_reason IS NOT OLD.difference_reason OR NEW.recorded_by IS NOT OLD.recorded_by
  OR NEW.recorded_at IS NOT OLD.recorded_at
BEGIN SELECT RAISE(ABORT, 'A recorded check cannot change. Cancel it and record it again'); END;
CREATE TRIGGER IF NOT EXISTS check_cancel_fixed BEFORE UPDATE OF cancel_reason, cancelled_by, cancelled_at ON payment_checks
WHEN OLD.status = 'CANCELLED'
 AND (NEW.cancel_reason IS NOT OLD.cancel_reason OR NEW.cancelled_by IS NOT OLD.cancelled_by OR NEW.cancelled_at IS NOT OLD.cancelled_at)
BEGIN SELECT RAISE(ABORT, 'Why and by whom a check was cancelled stays on record'); END;

CREATE TRIGGER IF NOT EXISTS po_amendment_decided_stays BEFORE UPDATE ON po_amendments
WHEN OLD.status <> 'PENDING_APPROVAL' OR NEW.po_id IS NOT OLD.po_id OR NEW.reason IS NOT OLD.reason OR NEW.before_json IS NOT OLD.before_json
  OR NEW.after_json IS NOT OLD.after_json OR NEW.requested_by IS NOT OLD.requested_by OR NEW.requested_at IS NOT OLD.requested_at
BEGIN SELECT RAISE(ABORT, 'An amendment stays on record as it was decided'); END;
CREATE TRIGGER IF NOT EXISTS po_amendment_decided_no_delete BEFORE DELETE ON po_amendments
WHEN OLD.status <> 'PENDING_APPROVAL'
BEGIN SELECT RAISE(ABORT, 'An amendment stays on record as it was decided'); END;
CREATE TRIGGER IF NOT EXISTS of_amendment_decided_stays BEFORE UPDATE ON of_amendments
WHEN OLD.status <> 'PENDING_APPROVAL' OR NEW.of_id IS NOT OLD.of_id OR NEW.reason IS NOT OLD.reason OR NEW.before_json IS NOT OLD.before_json
  OR NEW.after_json IS NOT OLD.after_json OR NEW.requested_by IS NOT OLD.requested_by OR NEW.requested_at IS NOT OLD.requested_at
BEGIN SELECT RAISE(ABORT, 'An amendment stays on record as it was decided'); END;
CREATE TRIGGER IF NOT EXISTS of_amendment_decided_no_delete BEFORE DELETE ON of_amendments
WHEN OLD.status <> 'PENDING_APPROVAL'
BEGIN SELECT RAISE(ABORT, 'An amendment stays on record as it was decided'); END;
-- The program never removes an attachment. Marking one removed, once, stays possible; the row itself stays.
CREATE TRIGGER IF NOT EXISTS po_attachment_fixed BEFORE UPDATE ON po_attachments
WHEN OLD.removed_at IS NOT NULL OR NEW.id IS NOT OLD.id OR NEW.po_id IS NOT OLD.po_id OR NEW.file_name IS NOT OLD.file_name
  OR NEW.stored_name IS NOT OLD.stored_name OR NEW.sha256 IS NOT OLD.sha256 OR NEW.size_bytes IS NOT OLD.size_bytes
  OR NEW.added_by IS NOT OLD.added_by OR NEW.added_at IS NOT OLD.added_at
BEGIN SELECT RAISE(ABORT, 'An attachment stays on record'); END;
CREATE TRIGGER IF NOT EXISTS po_attachment_no_delete BEFORE DELETE ON po_attachments
BEGIN SELECT RAISE(ABORT, 'An attachment stays on record'); END;

CREATE INDEX IF NOT EXISTS idx_claim_requests_status ON claim_requests(status);
CREATE INDEX IF NOT EXISTS idx_claim_requests_po ON claim_requests(po_id);
CREATE INDEX IF NOT EXISTS idx_claim_request_items_item ON claim_request_items(po_item_id);
CREATE INDEX IF NOT EXISTS idx_payment_checks_status ON payment_checks(status);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_warehouse_delivery_items_item ON warehouse_delivery_items(po_item_id);
CREATE INDEX IF NOT EXISTS idx_late_damage_check ON late_damage(paid_before);
CREATE INDEX IF NOT EXISTS idx_branch_counts_recount ON branch_counts(recount_of);
CREATE INDEX IF NOT EXISTS idx_claim_stub_items_item ON claim_stub_items(po_item_id);
CREATE INDEX IF NOT EXISTS idx_item_pass_items_item ON item_pass_items(po_item_id);
DROP INDEX IF EXISTS idx_items_po;
DROP INDEX IF EXISTS idx_of_items_of;
DROP INDEX IF EXISTS idx_amendments_po;
DROP INDEX IF EXISTS idx_of_amendments_of;
DROP INDEX IF EXISTS idx_dr_po;
DROP INDEX IF EXISTS idx_po_of;
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary, new_value)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V28', 'system',
        'Order statuses only move the way the program moves them, finished documents take no new lines, and faster lookups. '
        || (SELECT COUNT(*) FROM payment_checks WHERE expected_c IS NOT NULL) || ' checks keep what the program worked out for them, and '
        || (SELECT COUNT(*) FROM debit_memos WHERE paid_back_check_id IS NOT NULL) || ' debit memos cancelled after payment are paid back on the next check.',
        json_object('checks_expected', (SELECT COUNT(*) FROM payment_checks WHERE expected_c IS NOT NULL),
                    'memos_paid_back', (SELECT COUNT(*) FROM debit_memos WHERE paid_back_check_id IS NOT NULL),
                    'last_claim_request', (SELECT COALESCE(MAX(id), 0) FROM claim_requests)));
PRAGMA user_version = 28;
"""

# The guards V28 adds. An older migration that rebuilds a table one of them reads (V11 rebuilds branch_counts) can't run while they
# exist, so db._migrate removes them in front of any table rebuild, and V28 puts them back.
V28_TRIGGERS = tuple(line.split()[5] for line in MIGRATE_V28.splitlines() if line.startswith("CREATE TRIGGER IF NOT EXISTS "))
# The same guards as statements, so a later table rebuild on a file already at version 28 or newer can put them back itself.
V28_TRIGGER_SQL = "".join(m.group(0) + "\n" for m in re.finditer(r"^CREATE TRIGGER IF NOT EXISTS .*?\bEND;", MIGRATE_V28, re.S | re.M))


# ------------------------------------------------------------------ version 29: inventory, phase 1

# Stock that purchasing moves (agreed branch counts, claim stubs, item passes, forfeits) is read from the purchasing records themselves,
# so these tables hold only what inventory makes on its own: stock counts, adjustments and transfers. Location 0 is the Central Warehouse;
# any other location is a branch code. Every movement is permanent: a mistake is put right with another movement, never an edit.
MIGRATE_V29 = """
CREATE TABLE IF NOT EXISTS stock_number_sequences (
    kind TEXT NOT NULL CHECK (kind IN ('SC','SA','ST')),
    year INTEGER NOT NULL,
    last_number INTEGER NOT NULL CHECK (last_number >= 1),
    PRIMARY KEY (kind, year)
);
CREATE TRIGGER IF NOT EXISTS stock_sequence_never_backward BEFORE UPDATE ON stock_number_sequences
WHEN NEW.last_number <= OLD.last_number OR NEW.kind IS NOT OLD.kind OR NEW.year IS NOT OLD.year
BEGIN SELECT RAISE(ABORT, 'Inventory numbers cannot be reused'); END;
CREATE TRIGGER IF NOT EXISTS stock_sequence_no_delete BEFORE DELETE ON stock_number_sequences
BEGIN SELECT RAISE(ABORT, 'Inventory number history cannot be deleted'); END;

CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY,
    location_code INTEGER NOT NULL CHECK (location_code >= 0),
    product_id INTEGER NOT NULL REFERENCES products(id),
    bucket TEXT NOT NULL CHECK (bucket IN ('GOOD','DAMAGED')),
    qty_m INTEGER NOT NULL CHECK (qty_m <> 0),
    kind TEXT NOT NULL CHECK (kind IN ('COUNT','ADJUSTMENT','TRANSFER_OUT','TRANSFER_IN','TRANSFER_BACK')),
    ref_id INTEGER NOT NULL,
    moved_by INTEGER NOT NULL REFERENCES users(id),
    moved_at TEXT NOT NULL,
    UNIQUE (kind, ref_id, location_code, product_id, bucket)
);
CREATE INDEX IF NOT EXISTS idx_stock_movements_place ON stock_movements(location_code, product_id);
CREATE INDEX IF NOT EXISTS idx_stock_movements_product ON stock_movements(product_id);
CREATE TRIGGER IF NOT EXISTS stock_movements_no_update BEFORE UPDATE ON stock_movements
BEGIN SELECT RAISE(ABORT, 'A stock movement is permanent. Put it right with another one'); END;
CREATE TRIGGER IF NOT EXISTS stock_movements_no_delete BEFORE DELETE ON stock_movements
BEGIN SELECT RAISE(ABORT, 'A stock movement is permanent. Put it right with another one'); END;

CREATE TABLE IF NOT EXISTS stock_counts (
    id INTEGER PRIMARY KEY,
    count_number TEXT NOT NULL UNIQUE,
    location_code INTEGER NOT NULL CHECK (location_code >= 0),
    department_id INTEGER REFERENCES departments(id),
    status TEXT NOT NULL CHECK (status IN ('COUNTING','AGREED','POSTED','CANCELLED')),
    note TEXT NOT NULL DEFAULT '',
    opened_by INTEGER NOT NULL REFERENCES users(id),
    opened_at TEXT NOT NULL,
    closed_by INTEGER REFERENCES users(id),
    closed_at TEXT,
    cancel_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_stock_counts_place ON stock_counts(location_code, status);
CREATE TRIGGER IF NOT EXISTS stock_count_fixed BEFORE UPDATE OF count_number, location_code, department_id, note, opened_by, opened_at ON stock_counts
BEGIN SELECT RAISE(ABORT, 'A stock count cannot change. Cancel it and start another'); END;
CREATE TRIGGER IF NOT EXISTS stock_count_status_path BEFORE UPDATE OF status ON stock_counts
WHEN NOT ((OLD.status = 'COUNTING' AND NEW.status IN ('AGREED','CANCELLED')) OR (OLD.status = 'AGREED' AND NEW.status IN ('POSTED','CANCELLED')))
BEGIN SELECT RAISE(ABORT, 'A finished stock count stays as it is'); END;
CREATE TRIGGER IF NOT EXISTS stock_count_no_delete BEFORE DELETE ON stock_counts
BEGIN SELECT RAISE(ABORT, 'A stock count cannot be deleted. Cancel it instead'); END;

-- One row per product on the count. book_m is the good stock on the books when the count started; agreed_m is set once two people agree.
CREATE TABLE IF NOT EXISTS stock_count_lines (
    count_id INTEGER NOT NULL REFERENCES stock_counts(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    book_m INTEGER NOT NULL,
    agreed_m INTEGER CHECK (agreed_m >= 0),
    added_by INTEGER REFERENCES users(id),
    PRIMARY KEY (count_id, product_id)
);
CREATE TRIGGER IF NOT EXISTS stock_count_lines_fixed BEFORE UPDATE ON stock_count_lines
WHEN OLD.agreed_m IS NOT NULL OR NEW.count_id IS NOT OLD.count_id OR NEW.product_id IS NOT OLD.product_id OR NEW.book_m IS NOT OLD.book_m OR NEW.added_by IS NOT OLD.added_by
BEGIN SELECT RAISE(ABORT, 'An agreed count is permanent'); END;
CREATE TRIGGER IF NOT EXISTS stock_count_lines_no_delete BEFORE DELETE ON stock_count_lines
BEGIN SELECT RAISE(ABORT, 'An agreed count is permanent'); END;
CREATE TRIGGER IF NOT EXISTS stock_count_lines_while_counting BEFORE INSERT ON stock_count_lines
WHEN (SELECT status FROM stock_counts WHERE id = NEW.count_id) IS NOT 'COUNTING'
BEGIN SELECT RAISE(ABORT, 'This stock count is finished'); END;

-- What each person counted, round by round. Never changed.
CREATE TABLE IF NOT EXISTS stock_count_entries (
    count_id INTEGER NOT NULL REFERENCES stock_counts(id),
    round_no INTEGER NOT NULL CHECK (round_no >= 1),
    counted_by INTEGER NOT NULL REFERENCES users(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    qty_m INTEGER NOT NULL CHECK (qty_m >= 0),
    counted_at TEXT NOT NULL,
    PRIMARY KEY (count_id, round_no, counted_by, product_id)
);
CREATE TRIGGER IF NOT EXISTS stock_count_entries_no_update BEFORE UPDATE ON stock_count_entries
BEGIN SELECT RAISE(ABORT, 'A count is permanent. Count again instead'); END;
CREATE TRIGGER IF NOT EXISTS stock_count_entries_no_delete BEFORE DELETE ON stock_count_entries
BEGIN SELECT RAISE(ABORT, 'A count is permanent. Count again instead'); END;
CREATE TRIGGER IF NOT EXISTS stock_count_entries_while_counting BEFORE INSERT ON stock_count_entries
WHEN (SELECT status FROM stock_counts WHERE id = NEW.count_id) IS NOT 'COUNTING'
BEGIN SELECT RAISE(ABORT, 'This stock count is finished'); END;

CREATE TABLE IF NOT EXISTS stock_adjustments (
    id INTEGER PRIMARY KEY,
    adj_number TEXT NOT NULL UNIQUE,
    location_code INTEGER NOT NULL CHECK (location_code >= 0),
    reason TEXT NOT NULL CHECK (reason IN ('WRITE_OFF','DAMAGED','FOUND','INTERNAL_USE','RETURNED','OTHER')),
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('PENDING','APPROVED','REJECTED')),
    prepared_by INTEGER NOT NULL REFERENCES users(id),
    prepared_at TEXT NOT NULL,
    decided_by INTEGER REFERENCES users(id),
    decided_at TEXT,
    decision_note TEXT NOT NULL DEFAULT '',
    CHECK (decided_by IS NULL OR decided_by <> prepared_by)
);
CREATE TRIGGER IF NOT EXISTS stock_adjustment_fixed BEFORE UPDATE OF adj_number, location_code, reason, note, prepared_by, prepared_at ON stock_adjustments
BEGIN SELECT RAISE(ABORT, 'An adjustment cannot change. Reject it and make another'); END;
CREATE TRIGGER IF NOT EXISTS stock_adjustment_decided_stays BEFORE UPDATE OF status ON stock_adjustments
WHEN OLD.status <> 'PENDING'
BEGIN SELECT RAISE(ABORT, 'A decided adjustment stays as it is'); END;
CREATE TRIGGER IF NOT EXISTS stock_adjustment_no_delete BEFORE DELETE ON stock_adjustments
BEGIN SELECT RAISE(ABORT, 'An adjustment cannot be deleted. Reject it instead'); END;
CREATE TABLE IF NOT EXISTS stock_adjustment_lines (
    adj_id INTEGER NOT NULL REFERENCES stock_adjustments(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    bucket TEXT NOT NULL CHECK (bucket IN ('GOOD','DAMAGED')),
    qty_m INTEGER NOT NULL CHECK (qty_m <> 0),
    PRIMARY KEY (adj_id, product_id, bucket)
);
CREATE TRIGGER IF NOT EXISTS stock_adjustment_lines_no_update BEFORE UPDATE ON stock_adjustment_lines
BEGIN SELECT RAISE(ABORT, 'An adjustment cannot change. Reject it and make another'); END;
CREATE TRIGGER IF NOT EXISTS stock_adjustment_lines_no_delete BEFORE DELETE ON stock_adjustment_lines
BEGIN SELECT RAISE(ABORT, 'An adjustment cannot change. Reject it and make another'); END;
CREATE TRIGGER IF NOT EXISTS stock_adjustment_lines_while_pending BEFORE INSERT ON stock_adjustment_lines
WHEN (SELECT status FROM stock_adjustments WHERE id = NEW.adj_id) IS NOT 'PENDING'
BEGIN SELECT RAISE(ABORT, 'An adjustment cannot change. Reject it and make another'); END;

CREATE TABLE IF NOT EXISTS stock_transfers (
    id INTEGER PRIMARY KEY,
    transfer_number TEXT NOT NULL UNIQUE,
    from_code INTEGER NOT NULL CHECK (from_code >= 0),
    to_code INTEGER NOT NULL CHECK (to_code >= 0),
    status TEXT NOT NULL CHECK (status IN ('SENT','RECEIVED','CANCELLED')),
    note TEXT NOT NULL DEFAULT '',
    sent_by INTEGER NOT NULL REFERENCES users(id),
    sent_at TEXT NOT NULL,
    closed_by INTEGER REFERENCES users(id),
    closed_at TEXT,
    cancel_reason TEXT NOT NULL DEFAULT '',
    CHECK (from_code <> to_code),
    CHECK (closed_by IS NULL OR status <> 'RECEIVED' OR closed_by <> sent_by)
);
CREATE INDEX IF NOT EXISTS idx_stock_transfers_status ON stock_transfers(status);
CREATE TRIGGER IF NOT EXISTS stock_transfer_fixed BEFORE UPDATE OF transfer_number, from_code, to_code, note, sent_by, sent_at ON stock_transfers
BEGIN SELECT RAISE(ABORT, 'A transfer cannot change. Cancel it and send another'); END;
CREATE TRIGGER IF NOT EXISTS stock_transfer_finished_stays BEFORE UPDATE OF status ON stock_transfers
WHEN OLD.status <> 'SENT'
BEGIN SELECT RAISE(ABORT, 'A finished transfer stays as it is'); END;
CREATE TRIGGER IF NOT EXISTS stock_transfer_no_delete BEFORE DELETE ON stock_transfers
BEGIN SELECT RAISE(ABORT, 'A transfer cannot be deleted. Cancel it instead'); END;
CREATE TABLE IF NOT EXISTS stock_transfer_lines (
    transfer_id INTEGER NOT NULL REFERENCES stock_transfers(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    sent_m INTEGER NOT NULL CHECK (sent_m > 0),
    received_m INTEGER CHECK (received_m >= 0),
    PRIMARY KEY (transfer_id, product_id)
);
CREATE TRIGGER IF NOT EXISTS stock_transfer_lines_fixed BEFORE UPDATE ON stock_transfer_lines
WHEN OLD.received_m IS NOT NULL OR NEW.transfer_id IS NOT OLD.transfer_id OR NEW.product_id IS NOT OLD.product_id OR NEW.sent_m IS NOT OLD.sent_m
BEGIN SELECT RAISE(ABORT, 'A transfer cannot change. Cancel it and send another'); END;
CREATE TRIGGER IF NOT EXISTS stock_transfer_lines_no_delete BEFORE DELETE ON stock_transfer_lines
BEGIN SELECT RAISE(ABORT, 'A transfer cannot change. Cancel it and send another'); END;
CREATE TRIGGER IF NOT EXISTS stock_transfer_lines_while_sent BEFORE INSERT ON stock_transfer_lines
WHEN (SELECT status FROM stock_transfers WHERE id = NEW.transfer_id) IS NOT 'SENT'
  OR EXISTS (SELECT 1 FROM stock_movements WHERE kind = 'TRANSFER_OUT' AND ref_id = NEW.transfer_id)
BEGIN SELECT RAISE(ABORT, 'A transfer cannot change. Cancel it and send another'); END;

-- Purchasing history before this day is left out of the books. The first stock count at each place gives its opening stock.
INSERT OR IGNORE INTO system_settings(key, value, updated_at) VALUES ('inventory.live_since', date('now','localtime'), datetime('now','localtime'));
INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V29', 'system', 'Inventory: stock on hand, stock counts, adjustments and transfers.');
PRAGMA user_version = 29;
"""


# ------------------------------------------------------------------ version 30: sales, customer returns and reorder levels

# Sales take stock off the books the day they are recorded, typed in or imported from a POS file. Like everything else in inventory,
# the books read these papers directly. Reorder levels set by hand override the ones the program works out from sales.
MIGRATE_V30 = """
CREATE TABLE IF NOT EXISTS sale_number_sequences (
    kind TEXT NOT NULL CHECK (kind IN ('SALE','RETURN')),
    year INTEGER NOT NULL,
    last_number INTEGER NOT NULL CHECK (last_number >= 1),
    PRIMARY KEY (kind, year)
);
CREATE TRIGGER IF NOT EXISTS sale_sequence_never_backward BEFORE UPDATE ON sale_number_sequences
WHEN NEW.last_number <= OLD.last_number OR NEW.kind IS NOT OLD.kind OR NEW.year IS NOT OLD.year
BEGIN SELECT RAISE(ABORT, 'Sales numbers cannot be reused'); END;
CREATE TRIGGER IF NOT EXISTS sale_sequence_no_delete BEFORE DELETE ON sale_number_sequences
BEGIN SELECT RAISE(ABORT, 'Sales number history cannot be deleted'); END;

-- One paper per branch, day and source: what was sold, or what customers brought back. Quantities are in each product's stock unit.
CREATE TABLE IF NOT EXISTS stock_sales (
    id INTEGER PRIMARY KEY,
    sale_number TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('SALE','RETURN')),
    location_code INTEGER NOT NULL CHECK (location_code >= 0),
    sale_date TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('ENTRY','IMPORT')),
    file_name TEXT NOT NULL DEFAULT '',
    file_sha256 TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('POSTED','CANCELLED')),
    entered_by INTEGER NOT NULL REFERENCES users(id),
    entered_at TEXT NOT NULL,
    cancelled_by INTEGER REFERENCES users(id),
    cancelled_at TEXT,
    cancel_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_stock_sales_place ON stock_sales(location_code, sale_date);
CREATE INDEX IF NOT EXISTS idx_stock_sales_file ON stock_sales(file_sha256);
CREATE TRIGGER IF NOT EXISTS stock_sale_fixed BEFORE UPDATE OF sale_number, kind, location_code, sale_date, source, file_name, file_sha256, note, entered_by, entered_at ON stock_sales
BEGIN SELECT RAISE(ABORT, 'Recorded sales cannot change. Cancel them and record them again'); END;
CREATE TRIGGER IF NOT EXISTS stock_sale_stays_cancelled BEFORE UPDATE OF status ON stock_sales
WHEN OLD.status <> 'POSTED'
BEGIN SELECT RAISE(ABORT, 'Cancelled sales stay cancelled'); END;
CREATE TRIGGER IF NOT EXISTS stock_sale_no_delete BEFORE DELETE ON stock_sales
BEGIN SELECT RAISE(ABORT, 'Recorded sales cannot be deleted. Cancel them instead'); END;

-- price_c is what one stock unit sold for, cost_c what it cost then (last purchase cost), both kept as they were on the day.
CREATE TABLE IF NOT EXISTS stock_sale_lines (
    sale_id INTEGER NOT NULL REFERENCES stock_sales(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    bucket TEXT NOT NULL DEFAULT 'GOOD' CHECK (bucket IN ('GOOD','DAMAGED')),
    qty_m INTEGER NOT NULL CHECK (qty_m > 0),
    price_c INTEGER NOT NULL CHECK (price_c >= 0),
    amount_c INTEGER NOT NULL CHECK (amount_c >= 0),
    cost_c INTEGER CHECK (cost_c >= 0),
    PRIMARY KEY (sale_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_stock_sale_lines_product ON stock_sale_lines(product_id);
CREATE TRIGGER IF NOT EXISTS stock_sale_lines_no_update BEFORE UPDATE ON stock_sale_lines
BEGIN SELECT RAISE(ABORT, 'Recorded sales cannot change. Cancel them and record them again'); END;
CREATE TRIGGER IF NOT EXISTS stock_sale_lines_no_delete BEFORE DELETE ON stock_sale_lines
BEGIN SELECT RAISE(ABORT, 'Recorded sales cannot change. Cancel them and record them again'); END;
CREATE TRIGGER IF NOT EXISTS stock_sale_lines_while_posted BEFORE INSERT ON stock_sale_lines
WHEN (SELECT status FROM stock_sales WHERE id = NEW.sale_id) IS NOT 'POSTED'
BEGIN SELECT RAISE(ABORT, 'Recorded sales cannot change. Cancel them and record them again'); END;

-- Levels set by hand for one product at one branch. Without a row, the program works them out from recent sales.
CREATE TABLE IF NOT EXISTS reorder_levels (
    location_code INTEGER NOT NULL CHECK (location_code >= 0),
    product_id INTEGER NOT NULL REFERENCES products(id),
    min_m INTEGER NOT NULL CHECK (min_m >= 0),
    target_m INTEGER NOT NULL,
    set_by INTEGER NOT NULL REFERENCES users(id),
    set_at TEXT NOT NULL,
    PRIMARY KEY (location_code, product_id),
    CHECK (target_m >= min_m)
);

INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V30', 'system', 'Inventory: sales and customer returns, and reorder levels.');
PRAGMA user_version = 30;
"""


# ------------------------------------------------------------------ version 31: sales approval, and decided inventory papers locked

# A sale far below the catalog price, and a customer return to stock for sale, could hide missing stock, so they wait for management
# before they reach the books (sale_holds). Decided adjustments, finished counts and transfers, and cancelled sales can no longer have any
# column changed, and a stock movement must point at a paper in the right state.
MIGRATE_V31 = """
CREATE TABLE IF NOT EXISTS sale_holds (
    sale_id INTEGER PRIMARY KEY REFERENCES stock_sales(id),
    reason TEXT NOT NULL,
    decision TEXT CHECK (decision IN ('APPROVED','REJECTED')),
    decided_by INTEGER REFERENCES users(id),
    decided_at TEXT,
    decision_note TEXT NOT NULL DEFAULT '',
    CHECK ((decision IS NULL) = (decided_by IS NULL))
);
CREATE TRIGGER IF NOT EXISTS sale_holds_only_new BEFORE INSERT ON sale_holds
WHEN EXISTS (SELECT 1 FROM stock_sale_lines WHERE sale_id = NEW.sale_id)
BEGIN SELECT RAISE(ABORT, 'Only new sales can wait for approval'); END;
CREATE TRIGGER IF NOT EXISTS sale_holds_decided_once BEFORE UPDATE ON sale_holds
WHEN OLD.decision IS NOT NULL OR NEW.sale_id IS NOT OLD.sale_id OR NEW.reason IS NOT OLD.reason
  OR NEW.decided_by = (SELECT entered_by FROM stock_sales WHERE id = NEW.sale_id)
BEGIN SELECT RAISE(ABORT, 'Sales are approved once, by someone other than who recorded them'); END;
CREATE TRIGGER IF NOT EXISTS sale_holds_no_delete BEFORE DELETE ON sale_holds
BEGIN SELECT RAISE(ABORT, 'An approval record cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS stock_adjustment_decided_locked BEFORE UPDATE ON stock_adjustments
WHEN OLD.status <> 'PENDING'
BEGIN SELECT RAISE(ABORT, 'A decided adjustment stays as it is'); END;
CREATE TRIGGER IF NOT EXISTS stock_transfer_finished_locked BEFORE UPDATE ON stock_transfers
WHEN OLD.status <> 'SENT'
BEGIN SELECT RAISE(ABORT, 'A finished transfer stays as it is'); END;
CREATE TRIGGER IF NOT EXISTS stock_count_finished_locked BEFORE UPDATE ON stock_counts
WHEN OLD.status IN ('POSTED','CANCELLED')
BEGIN SELECT RAISE(ABORT, 'A finished stock count stays as it is'); END;
CREATE TRIGGER IF NOT EXISTS stock_sale_cancelled_locked BEFORE UPDATE ON stock_sales
WHEN OLD.status = 'CANCELLED'
BEGIN SELECT RAISE(ABORT, 'Cancelled sales stay cancelled'); END;
CREATE TRIGGER IF NOT EXISTS stock_movements_from_a_paper BEFORE INSERT ON stock_movements
WHEN NOT ((NEW.kind = 'ADJUSTMENT' AND (SELECT status FROM stock_adjustments WHERE id = NEW.ref_id) IS 'APPROVED')
       OR (NEW.kind = 'COUNT' AND (SELECT status FROM stock_counts WHERE id = NEW.ref_id) IS 'AGREED')
       OR (NEW.kind IN ('TRANSFER_OUT','TRANSFER_IN','TRANSFER_BACK') AND (SELECT status FROM stock_transfers WHERE id = NEW.ref_id) IS 'SENT'))
BEGIN SELECT RAISE(ABORT, 'A stock movement comes from an approved adjustment, an agreed count or a transfer on its way'); END;

INSERT INTO audit_logs(occurred_at, user_name, action, entity_type, summary)
VALUES (datetime('now','localtime'), 'system', 'MIGRATE_V31', 'system', 'Inventory: unusual sales wait for approval; decided inventory papers are locked.');
PRAGMA user_version = 31;
"""
