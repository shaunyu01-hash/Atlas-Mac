# Master data, printed papers, reports and sample data

Four supporting modules. `masters.py` owns suppliers, branches and products; departments are fixed seed data. `pdf.py` renders every printed paper with ReportLab and marks reprints with pypdf. Most documents are rendered once, and the exact bytes are kept in the database (issued POs, DMs, CSs, IPs). A later print hands back the kept file stamped "COPY n". `reports.py` holds 13 date-filtered purchasing reports plus an Excel export. It has **no permission checks of its own**; visibility is decided by the sidebar. `sample_data.py` builds a separate practice database by driving the real services under a back-dated clock.

## Files

| File | Role | Key public names |
|---|---|---|
| `atlaspo/masters.py` | Supplier/branch/product create, update, search; SKU ownership | `save_supplier`, `DuplicateSupplier`, `find_similar_suppliers`, `search_suppliers`, `supplier_history`, `save_branch`, `list_branches`, `list_departments`, `save_product`, `record_products`, `check_skus`, `search_products`, `last_purchase_cost`, helpers `whole_number`, `code_key`, `clean_terms`, `check_length` |
| `atlaspo/pdf.py` | Every printed document, print recording, copy stamping | `render_*` (10 renderers), `stamp_copy`, `combine`, `reprint_counts`, `print_order_form`, `reprint_order_form`, `warehouse_of`, `office_copy`/`office_of`, `delivery_receipt`, `office_receipts`, `receipts_for_email`, `blind_copy`, `warehouse_printed`, `office_ready`, `office_printed_at` |
| `atlaspo/reports.py` | Reports and Excel | `REPORTS` (list of `Definition`), `Report`, `Column`, `totals_row`, `display`, `to_excel`, `po_register_excel`, `of_register_excel` |
| `atlaspo/sample_data.py` | Practice database builder | `ensure(folder)`, `build(conn)`, `is_sample(conn)`, `FOLDER`, `PASSWORD`, `FLAG` |
| `Atlas PO ERP (Sample Data).pyw` | Launcher: `sample_data.ensure()` then `atlaspo.__main__.main()` | 5 lines |
| `atlaspo/ui/pages.py` | `SuppliersPage` (:801), `ProductsPage` (:1052), `BranchesPage` (:1228), `ReportsPage` (:1319) | |
| `atlaspo/ui/settings_page.py` | `company.*` settings and the logo that every PDF header reads | |

## Master data

**Departments** (`schema.py:28`, seed `schema.DEPARTMENTS` at `schema.py:327`, inserted in `db.py:218`): ids 1–9 (Grocery … General Merchandise), and the CHECK allows ids 1–99. Triggers block changing an id or deleting a row. The code has no service and no UI for editing them. `list_departments` is read-only.

**Suppliers**: `save_supplier(conn, user, data, supplier_id=None, confirm_duplicate=False)`, `masters.py:84`.
- Permission `supplier.edit` = **ADMIN only** (`auth.py:34`). Purchasers can look suppliers up but not change them, because terms, discounts, stickers and VAT drive every payment. The UI hides editing via `self.editable = user.can("supplier.edit")` (`pages.py:811`).
- Fields: `SUPPLIER_FIELDS` (`masters.py:12`). The schema also has `source` (MANUAL/LEGACY), `created_at` and `updated_at`. `sticker_rate_c`, `term_discount_bp` and `term_discount_days` were added by migrations (`schema.py:842`, `:1246`).
- Validation: code is uppercased and must match `[A-Z0-9]{1,4}`. Name is required, and name and registered name are at most 200 characters (`MAX_TEXT`). `default_discount_bp` is 0–10000. `sticker_rate_c` is 0–100000 (₱1,000/pc). `term_discount_bp` is ≤10000 and `term_discount_days` ≤3650. `vat_mode` must be in `money.VAT_MODES`; the service defaults it to `NONE`, the schema to `INCLUSIVE`. Status is ACTIVE/INACTIVE. Numbers go through `whole_number()`, which accepts an int, a whole float or a digit string, treats blank as 0, and raises `POError` for anything else.
- `clean_terms`: `120`, `120 d` and `120 days` all become `120D` (cap 3650 days). Blank becomes `"None"`. Any other text is custom and capped at 60 characters. The UI offers `PAYMENT_TERMS`.
- Duplicate guard: the same code raises a hard `POError`. A similar name (difflib ratio ≥ 0.88 on alphanumerics) or the same TIN raises `DuplicateSupplier` (a `POError` subclass carrying `.matches`) unless `confirm_duplicate=True`. The UI asks "Save anyway?" and retries (`pages.py:1043`). The check runs on every edit, not only on create.
- Audit: `SUPPLIER_CREATE` (full new dict) and `SUPPLIER_UPDATE` (only the changed fields, compared as `str()`, with previous and new values).
- No delete exists in code (no service, no trigger, no UI). Deactivate with `status='INACTIVE'`. `search_suppliers(include_inactive=True)` is the default.

**Branches**: `save_branch(conn, user, data, code=None)`, `masters.py:176`.
- Permission `branch.manage` = ADMIN only. The UI shows New/Save only with that permission (`pages.py:1239`, `:1262`).
- `code` is an INTEGER primary key, typed on create, from 1 to 999,999,999. **It is permanent:** trigger `branch_code_fixed` blocks changes, and a used code is never reused. `kind` is BRANCH or WAREHOUSE. Name is required. `short_code` is uppercased. Nothing checks the length of name, short code or address (unlike suppliers and products).
- Deleting is blocked by trigger `branch_no_delete` ("Make the branch inactive instead"). Set `active` to 0 or 1.
- Audit: `BRANCH_CREATE`; on update, `BRANCH_UPDATE`, or `BRANCH_DEACTIVATE`/`BRANCH_REACTIVATE` when `active` changed.
- Seed branches 2 North, 4 South, 6 East, 8 West (`schema.DESTINATIONS`, migrated into `branches` at `schema.py:393`).

**Products**: `save_product(conn, user, data, product_id=None)`, `masters.py:234`.
- Permission `master.edit` = ADMIN, BUYER (`ProductsPage` uses `user.can("master.edit")`, `pages.py:1060`).
- `_check_product` validates screen input: department and supplier must exist; `active` must be one of None, True, False, 0, 1; item code, description and supplier item code are at most 200 characters; `pieces_per_unit` ≥ 1; `last_cost_c` and `selling_c` are from 0 to `MAX_AMOUNT_C` (10^13). `_store_product` requires item code, description and department, and defaults to `uom='PC'`, `pieces_per_unit=1`, `active=1`.
- Item code is unique **case-insensitively in any language**. `_same_code` uses `code_key()` (NFC + casefold), because the schema's `COLLATE NOCASE` only folds A–Z. For ASCII codes it pre-filters in SQL with `upper()` plus a GLOB for non-ASCII rows.
- Audit: `PRODUCT_CREATE`; `PRODUCT_COST_CHANGE` when `last_cost_c` changed, otherwise `PRODUCT_UPDATE`. Comparison uses raw `!=`, not `str()` as for suppliers.
- No delete exists in code. Deactivate with `active=0`. `search_products(active_only=True)` is the default. `supplier_id=X` also returns products with no supplier; `only_supplier=X` returns only that supplier's.
- **SKU ownership**: `check_skus` refuses an item code that belongs to another supplier. It runs when an OF is saved. `record_products` runs on approval: it adds each new SKU to the catalog under the OF's supplier and links rows to existing products. It never overwrites an existing product's details, except its cost: `_approved_cost` sets `last_cost_c` to the approved line's net unit cost (`net_unit_c`, converted to the product's own pieces per unit; a cost-0 line is ignored; the first line wins when a product is on the order twice), audited `PRODUCT_COST_CHANGE`. `recost_after_cancel` (called by `order_forms.cancel` for an APPROVED OF) takes each product back to its latest approved line still standing. `save_product` refuses to change `uom` or `pieces_per_unit` once `_has_stock` (received, or on any inventory paper); see [inventory.md](inventory.md). It skips `_check_product` and needs any one of `master.edit`, `of.edit`, `of.amend` or `of.approve`, so an APPROVER can add catalog rows this way.
- `last_purchase_cost`: latest `unit_cost_c` on a non-DRAFT, non-CANCELLED PO, else `products.last_cost_c`.
- Selling prices are also edited in `selling.py` under `price.edit` (ADMIN, BUYER), outside this module.

## Printed papers (pdf.py)

All are A4 with 12 mm margins, built with `SimpleDocTemplate`. `_footer` writes "Page n of N", repeats the document id at the top of every page after the first, and can add "Supplier initials" on every page but the last. Fonts are Segoe UI or Arial embedded from `C:\Windows\Fonts` (needed for the ₱ sign), falling back to Helvetica with "PHP " amounts (`Styles.peso`). The header comes from `company.*` settings and the logo from `data_dir()/company.logo_file`. All user text passes through `esc()`.

| Renderer | Prints | Called from | Content rules |
|---|---|---|---|
| `render_supplier_po(po_id, preview)` :228 | One branch's PO for the supplier | `orders.issue` (kept in `po_issued_versions.pdf` with sha256); `po_page.py:787` (preview) | Status must be in `PRINTABLE`. **Never prints other branches, internal or warehouse notes, approval remarks, selling price or FX.** Says DELIVER TO the Central Warehouse, FOR BRANCH x. A notice gives the DR number. Lines are grouped by department with subtotals. Marked "PREVIEW. NOT YET ISSUED" when APPROVED or amended since issue; "CANCELLED" in red. |
| `render_warehouse_of(of_id)` :440 | Whole OF, every branch's pieces, costs, sticker and term deductions | `_warehouse_copy`, `_print_set` | OF must be APPROVED or CANCELLED and numbered. Blank grid columns for Received/Short/Excess/Damaged. Five signatures, including "Supplier agrees to costs". Initials on every page. One original per revision (`warehouse_printed`). |
| `render_office_of(of_id)` :814 | Accounting's final copy: per-piece cost, settlement block from `payments.settlement` (DMs, CSs, unclaimed short, sticker and term adjustments, lower DR, memos paid back) | `office_copy` | Prints only when `office_ready` (delivery complete, or OF cancelled). One "To pay after …" final figure. Internal notes appear here only. |
| `render_delivery_receipt(dr_id)` :972 | DR: pieces ordered, blanks for delivered, unit price and total | `delivery_receipt`, `office_receipts`, `receipts_for_email` | **No prices.** Refused for a cancelled PO. |
| `render_blind_sheet(po_id)` :590 | Paper backup count form | `blind_copy`, `_print_set(paper_forms=True)` (two per PO) | **No quantities.** Never stamped as a copy, but logged as `BLIND_PRINT`. |
| `render_cut_slips(po_ids)` :644 | Four branch delivery slips per page | `_print_set` | Branch, PO number, supplier, OF, expected date; nothing about items. |
| `render_debit_memo(dm_id)` :675 | DM lines (Short/Damaged), cost per piece, total | `debit_memos.issue`, kept in `debit_memos.pdf` | "Approved by" shows the **preparer's** name. |
| `render_claim_stub(cs_id)` :715 | CS lines and total; collect from the warehouse within 90 days | `claim_stubs` approval, kept in `claim_stubs.pdf` | |
| `render_item_pass(ip_id)` :757 | Excess pieces back to the warehouse, ORIGINAL + DUPLICATE | `item_passes` print, kept in `item_passes.pdf` | No money ("excess is never paid for"). Via `_goods_pass`. |
| `render_claim_stub_pass(cs_id, printed_by)` :766 | Damaged goods pass from a branch, ORIGINAL + DUPLICATE | `claim_stubs.print_pass` (RECEIVER, own branch) | Pieces only, no costs. Dated `today()`, not the CS date. |

**Orchestrators.**
- `print_order_form` (:1092) is the first print from the Print page (`ui/common.py:935`). It runs `generate_pos` and issues every PO that `to_issue` selects **in one transaction** (all branch POs or none), then `_print_set`. It returns `(bytes|None, problems)`.
- `_print_set` combines the warehouse OF, optional blind sheets ×2 per PO, cut slips, the issued PO PDFs and the office-printed DRs (`delivery_receipts.first_office_ids`).
- `reprint_order_form` (:1131) logs `OF_REPRINT` plus a `PO_REPRINT` for each live PO and calls `delivery_receipts._ensure_first`, which can create a DR on a view-only reprint. The comment marks this as undecided. The whole set is stamped.

**Who may print.**
- `_no_receivers(user)` rejects any user holding RECEIVER, even alongside other roles, then requires `receiving.review`. It guards the warehouse OF, office OF, DRs, blind sheet and reprint.
- The first Office OF needs `payment.edit`.
- The first DR print needs `po.issue`, and so does `receipts_for_email`, which writes `data_dir()/email/DR <ref> <supplier>.pdf`, unstamped, logged as `DR_SAVED_FOR_EMAIL`.
- `orders.issued_pdf` refuses RECEIVER and WAREHOUSE.

**Reprint stamping.**
- `stamp_copy(data, copy, who)` (:43) overlays a red line on every page: "COPY n. PRINTED AGAIN <DATE> BY <NAME>". It renders a ReportLab overlay per page and uses `page.merge_page`. The kept bytes are never modified.
- Copy numbers come from counting audit rows **inside the same `tx` that records the print**, so two PCs can't both get the original. The original counts as copy 1, so the second print is "COPY 2":

| Document | Audit row counted | Copy number |
|---|---|---|
| PO (`orders.issued_pdf`) | `PO_REPRINT` | `earlier+2` |
| OF reprint and warehouse copy | `OF_REPRINT` | `earlier+2` |
| Office OF, DR, DM, CS, CS pass | first-print rows (`OF_PRINT_OFFICE`, `DR_PRINT`, `DM_PRINT`, `CS_PRINT`, `CS_PASS_PRINT`) | `earlier+1` when `earlier>0` |

- `reprint_counts` feeds the "printed more than once" flags in the lists (`of_page.py:1867`, `pages.py:564`).

**pypdf pin.** `requirements.txt:3` is `pypdf>=6.0,<7`. Version 7.0 removed `PageObject.replace_contents()` on unattached pages, which `merge_page` in `stamp_copy` depends on. Do not lift the pin without rewriting `stamp_copy` and running `test_reprints.py`.

## Reports

`REPORTS` (`reports.py:247`) drives `ReportsPage`. Every report function takes `(conn, date_from, date_to, supplier_id=None, item_text=None)`. `Definition.needs` shows the Supplier or Product filter. "Bought" means `ORDERED`: PO status not DRAFT, PENDING_APPROVAL or CANCELLED.

| Report (key) | Shows | Function | Rows / totals |
|---|---|---|---|
| OF Register (`of_register`) | Numbered OFs via `order_forms.search` | `of_register` | Cancelled and unapproved rows are listed, but `skip_in_totals` leaves them out of the total |
| PO Register (`register`) | Numbered POs with gross, discounts, VAT, total, issued, expected, external PO | `po_register` | Same skip rule |
| Purchases by Supplier / Branch / Department | Grouped sums over `ORDERED` | `by_supplier`, `by_branch`, `by_department` | Department totals are per line, before overall discount, VAT and charges |
| Monthly / Annual Purchasing | Grouped by `substr(po_date)` | `monthly`, `annual` | |
| Pending OF Approvals | OFs in PENDING_APPROVAL, filtered on `of_date` | `pending_approval` | |
| Outstanding POs | Pieces still to come = ordered − max(agreed branch received, warehouse counted) | `outstanding` | Approved, issued or partly delivered POs with remaining > 0 |
| Cancelled OFs and POs | UNION of both | `cancelled` | `totals=False` |
| Supplier Purchase History | All POs, optionally one supplier | `supplier_history` (needs supplier) | Skips the non-bought rows in the total |
| Product Purchase History | PO lines matching SKU or description | `item_history` (needs item) | LIMIT 20000, no totals |
| Purchase Cost Comparison | SKUs bought more than once; min, max and latest net in the window | `cost_comparison` (needs item) | One OF counts as one purchase |

**Visibility.** The report functions check no permissions. `main_window.py:213` always registers `ReportsPage`, but receiver-only, accounting-only and warehouse-only users get a sidebar without the Reports group (`main_window.py:233-240`). In practice ADMIN, BUYER, APPROVER and VIEWER (and anyone combining roles) see every report, costs included.

**Totals and Excel.**
- `totals_row` sums MONEY, INT and QTY columns except those **titled** "Dept", "Draft OF #" or "Code".
- `to_excel` writes money as pesos (`/100`) and quantities as `/1000`, and forces strings to text type, so "=..." never becomes a formula.
- `po_register_excel` and `of_register_excel` export the OF and PO list screens (`of_page.py:1999`).

## Sample data

- **Launcher.** `"Atlas PO ERP (Sample Data).pyw"` calls `sample_data.ensure()`. `ensure` sets `os.environ["PO_DATA_DIR"]` to `FOLDER` for the rest of the process. `FOLDER` is `PO_SAMPLE_DIR`, or `~/Documents/Atlas PO ERP Sample`; it is read **at import time**. Setting `PO_DATA_DIR` also overrides the `data-folder.txt` pointer (`db.data_dir`).
- **Build only once.** If `po.sqlite` exists, `ensure` returns without rebuilding; delete the folder to rebuild. Otherwise it builds `po.building.sqlite` and `os.replace`s it in, so a failed build leaves nothing behind.
- **Refusals.** `build()` refuses any database that has users. `is_sample` checks the setting `sample.data = "1"`, which makes the window title and login hint say "sample data".
- **Clock.** `_clock`/`_at` monkeypatch `db.now` and `order_forms.now` to back-date work from Aug 2025 to 2 Nov 2026, then restore them.
- **Logins** (all password `123`, forced by `_easy_logins`, which updates every hash directly):

| Login | Role | Notes |
|---|---|---|
| `owner` | ADMIN + APPROVER | Self-approval allowed |
| `admin` | ADMIN | |
| `approver` | APPROVER | |
| `purchaser`, `purchaser2` | BUYER | |
| `viewer` | VIEWER | |
| `accounting` | ACCOUNTING | |
| `warehouse`, `warehouse2` | WAREHOUSE | |
| `nor1/2`, `sou1/2`, `eas1/2`, `wes1/2` | RECEIVER | Branches 2, 4, 6, 8 |
| `former` | BUYER | Turned off |
| `old1/2` | RECEIVER | Branch 10, turned off |

- **Settings changed.** `receiving.after_shipping=0`, so receivers can count before shipping. Approvers are owner and approver. `company.name` is "ATLAS PO ERP (SAMPLE DATA)".
- **Masters.** Branches 2/4/6/8 get sample addresses. Branch 10 "Closed Branch (Sample)" trades until it is deactivated on 31 Dec 2025. Suppliers AAA, BBB and CCC (AAA has a 3%/14-day term discount and a ₱0.50 sticker rate only from 30 Jun to 1 Aug 2026). 36 products A001–A025, B001–B006 and C001–C005; A025 is deactivated at the end.
- **Documents.**
  - `_history`: closed POs from 2025 to mid-2026, including one short and closed.
  - `_open_work`: OFs in every state: draft, pending, rejected, approved without POs, amended, amendment pending or rejected, exactly at the limit and one peso over, cancelled after POs, a cancelled draft, issued/overdue with external numbers and an attachment, and an FX order.
  - `_receiving`: every count situation, plus DMs, CSs, IPs, late damage, emailed DRs and selling prices.
  - `_payment_stages`: checks at every stage, including one waiting for sign-off.
  - `_warehouse_stages` and `_other_suppliers` add warehouse history and BBB/CCC business.
  - `build` ends with `backup.manual_backup`.

## Interactions with other modules

- `order_forms` calls `masters.check_skus` on save and `record_products` on approval. `pdf.print_order_form` calls `order_forms.generate_pos` and `orders.issue`.
- `orders.issue` renders and keeps the PO PDF, then calls `delivery_receipts.ensure_first`. `orders.issued_pdf` stamps PO reprints.
- `debit_memos`, `claim_stubs` and `item_passes` render once, keep the PDF and sha256, and handle their own reprint stamping (`document()`, `print_pass`). `pdf.office_copy` appends each issued DM through `debit_memos.document`, so each one logs a `DM_PRINT`.
- `pdf.render_office_of` depends on `payments.settlement` and `receiving.of_delivery_complete`. A settlement change changes the printed final figure.
- `reports` imports `orders.STATUS_LABELS` and `order_forms.search`.
- `sample_data` touches nearly every service. A rule change that breaks it shows up in `test_sample_data.py`.

## Tests

Run from `apps/desktop`: `python -E -m pytest tests/test_x.py -q -p no:cacheprovider`.

| Test file | Covers |
|---|---|
| `test_fix_masters_auth.py` (25) | Supplier, product and branch validation; ñ case folding; SKU ownership; `record_products` refusing a viewer; some login rules |
| `test_fix_pdf_reports.py` (13) | A failed print set issues nothing; receivers get no priced papers; DR permissions; the transaction around print recording; register totals; Excel "=" text; outstanding; cost comparison |
| `test_reprints.py` (12) | Copy stamping for each document type and the reprint flags on screens |
| `test_sample_data.py` (30) | Every report has rows; every status, screen and role is present; refuses a database with users; the clock is restored. It builds the sample once per module, so it is slow. |
| `test_fix_attachments.py` (17) | PO attachments (used by the sample), not PDFs |

Other tests also touch these modules: `test_delivery_receipts.py`, `test_fix_of_print_approvals.py`, `test_fix2_approvals-print.py`, `test_debit_memos.py`.

## Gotchas for changing these modules

- **Never weaken the content rules on the supplier PO, DR and blind sheet.** The supplier PO shows only its own branch, with no internal notes, selling prices or FX. The DR has no prices. The blind sheet has no quantities. Tests assert these rules.
- **Kept PDFs are evidence.** Never re-render an issued PO, DM, CS or IP to reprint it. Fetch the kept bytes and `stamp_copy` them. A change to a renderer affects only future documents.
- **Count and record a print in one `tx`.** Otherwise two PCs can both get the original. `db.tx` joins an outer transaction, with no savepoint. `debit_memos.document` counts `earlier` **before** opening its `tx`, unlike `claim_stubs.document`, so it may be exposed to that race.
- **The pypdf pin stays below 7.** See the pypdf pin above.
- **Fonts.** `pdf._fonts()` takes the first pair in `_FONT_FILES` (Windows Segoe UI or Arial, Mac Arial, Linux DejaVu Sans) whose regular and bold both have ₱ (U+20B1). With none, PDFs print "PHP" instead of ₱ in Helvetica, and that changes layout widths.
- **Reports have no `require()`.** Adding a report exposes costs to every role that sees the sidebar group. Adding a count column titled "Code", "Dept" or "Draft OF #" leaves it out of the totals. Any other title gets summed.
- **Master records are never deleted.** Branch and department deletion is blocked by triggers. Suppliers and products have no delete in code. Deactivate them instead.
- **Supplier code format.** `save_supplier` caps codes at 4 characters (`[A-Z0-9]{1,4}`). Other code, such as file names and labels, prints the code. Grep for `supplier_code` before widening it.
- **Sample data isolation.** Scripts must set `PO_SAMPLE_DIR` **and** `PO_DATA_DIR` before importing `atlaspo`; `FOLDER` falls back to the real `Documents` folder. Changing the sample's dates or amounts can break the exact assertions in `test_sample_data.py`.
- **Stale comments.**
  - `pdf.office_print_count` claims "audit scripts under po_app/audit" import it. That folder no longer exists, and no caller remains in the repository.
  - The comment at `schema.DESTINATIONS` mentions a "Destinations" screen; today it is Branches.
  - `masters.supplier_history` (rows for the supplier screen) and `reports.supplier_history` (a Report) share a name but are different functions.
