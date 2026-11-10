# Purchase Orders and Delivery Receipts

A PO is one branch's share of an approved Order Form (OF). `order_forms.generate_pos` creates it already **Approved**, and it inherits the OF's approval. From there this area covers the rest of the PO's life: issuing it to the supplier (all branch POs of an OF are printed and issued together from the Print page), keeping the exact PDF of every issued revision, amending it (which makes a new revision that has to be issued again), cancelling it, closing it, and reprints marked as copies. It also covers the **Delivery Receipt (DR)**: a priceless form numbered after the PO that the supplier fills in and hands over with the goods. Place in the flow: OF approval → **PO issue + DR** → warehouse → blind branch counts (receiving sets Partially/Fully delivered) → differences (DM/CS/IP) → supplier checks (payments compares DRs). POs made before OFs existed ("legacy") keep their old numbers and can still be amended, issued, cancelled and closed.

## Files

| File | Role | Key public functions |
|---|---|---|
| `atlaspo/orders.py` | PO lifecycle service (no Qt) | `issue`, `issue_batch`, `issued_versions`, `issued_pdf`, `cancel`, `close`, `can_cancel`, `can_close`, `receive_outside`, `can_receive_outside`, `amend`, `decide_amendment`, `set_legacy_number`, `write_guard`, `sync_parent_of`, `agreed_discount_shares`, `search`, `dashboard_counts`, `pending_amendments`, `add_attachment`, `list_attachments`, `next_po_number`, `get_po`, `unsent`, `to_issue`, `paid_by`, `live_charges` |
| `atlaspo/delivery_receipts.py` | DR numbering, channel, email tracking | `ensure_first`, `_ensure_first`, `another_delivery`, `get`, `search`, `first_office_ids`, `needs_email`, `waiting_to_email`, `email_text`, `mark_emailed`, `format_number` |
| `atlaspo/ui/po_page.py` | `POPage`: one PO, read-only until "Amend" | `open_po`, `issue_po`, `cancel_po`, `close_po`, `receive_outside`, `save_amendment`, `print_pdf`, `save_pdf`, `open_version`, `write_print_file` |
| `atlaspo/pdf.py` (entry points only) | Rendering and print recording | `render_supplier_po`, `stamp_copy`, `print_order_form`, `reprint_order_form`, `_print_set`, `delivery_receipt`, `office_receipts`, `receipts_for_email`, `blind_copy`, `PRINTABLE` |
| `atlaspo/ui/pages.py` `PrintPage` | Print tab: To print, Printed, DRs to email, All DRs | `print_selected`, `print_receipt`, `another_delivery`, `start_email`, `mark_sent` |
| `atlaspo/ui/common.py` | `print_order_forms()` runs `pdf.print_order_form` for each ticked OF | |

## Data model

Tables (from `schema.py`, current `SCHEMA_VERSION = 28`):
- `purchase_orders` (rebuilt in MIGRATE_V2): `po_number/po_year/po_seq` (all NULL or all set), `of_id`, `supplier_snapshot` (JSON of the supplier when the PO was made), `branch_code/name/address`, the header terms, the totals `*_c`, `total_qty_m`, `total_pieces_m`, `revision`, `issued_revision`, `issued_at/by`, `cancelled_*`, `closed_*`, `approved_by`, `auto_approved`.
- `purchase_order_items`: `qty_m` (thousandths), `received_qty_m`, the generated column `remaining_qty_m`, `of_item_id`, `of_allocation_id`, line money columns, `UNIQUE(po_id, line_no)`.
- `po_number_sequences(year, branch_code, last_number)`: triggers stop the number going backwards and stop rows being deleted.
- `po_write_guard(token)`: holds a row only while a guarded write is running (see Gotchas).
- `po_issued_versions(po_id, revision, snapshot_json, pdf BLOB, sha256)`, `UNIQUE(po_id, revision)`. No update and no delete (triggers).
- `po_amendments` (`PENDING_APPROVAL|APPLIED|REJECTED`, `before_json`, `after_json`). A decided row can't change or be deleted (`po_amendment_decided_stays`, MIGRATE_V28 block).
- `po_approvals`: append-only (`approvals_no_update/no_delete`). Decisions are `SUBMITTED|APPROVED|AUTO_APPROVED|REJECTED`.
- `po_attachments`: rows are never deleted. `removed_at` may be set once.
- `outside_receipts` (MIGRATE_V22): one per PO, append-only.
- `delivery_receipts` (MIGRATE_V16): `dr_number UNIQUE`, `po_id`, `delivery_no`, `channel OFFICE|EMAIL`, `emailed_at/by/revision`, `UNIQUE(po_id, delivery_no)`. `dr_number_fixed` and `dr_no_delete` triggers. Payments writes `dr_entries` and `dr_line_differences` against DRs; that is not this area.

Triggers on POs: `po_number_fixed`, `po_parent_fixed` (of_id, branch and supplier_snapshot; can be bypassed by write_guard), `po_requires_approved_of`, `po_no_delete_numbered`, `po_terms_locked` (terms, totals and `revision` once status <> DRAFT, unless write_guard is held), `items_locked_insert/update/delete`. Unique indexes `ux_po_of_branch` (one PO per branch per OF) and `ux_po_year_branch_seq` both apply only where `of_id IS NOT NULL`. **Not locked by trigger:** `status`, `remarks`, `internal_notes`, `legacy_po_number`, and the issued, closed and cancelled columns.

Statuses (`orders.py:26-36`). DRAFT and PENDING_APPROVAL exist only on legacy POs.
```
APPROVED --issue--> ISSUED --(receiving)--> PARTIALLY_DELIVERED --> FULLY_DELIVERED
   ^                  |  \__ amend applied: back to APPROVED (revision+1, issued_revision unchanged)
   |__________________|
ISSUED/PARTIAL/FULL --close / receive_outside--> CLOSED
DRAFT/PENDING/APPROVED/ISSUED --cancel--> CANCELLED
```
A PARTIAL or FULL PO that is amended keeps its status, worked out again by `_refresh_delivery`. In that case `unsent(po)` (`revision > issued_revision`) marks it as needing to be issued again. Register tabs (`PO_TAB_STATUSES`): Open = DRAFT/PENDING/APPROVED, Completed = ISSUED/PARTIAL/FULL/CLOSED, Cancelled.

## Rules enforced

Permissions (`auth.PERMISSIONS`): `po.issue`, `po.cancel`, `po.amend`, `po.close` and `po.edit` belong to ADMIN and BUYER. `po.approve` belongs to ADMIN and APPROVER. `po.receive_outside` is ADMIN only. `payment.edit` belongs to ADMIN and ACCOUNTING.

1. **Issuing:** only a PO where `to_issue()` is true (APPROVED, or ISSUED/PARTIAL with an unsent revision), and never while an amendment is pending. `issue()` keeps the rendered PDF, a JSON snapshot and the SHA-256, records issued_*, and makes the first DR. Requires `po.issue`.
2. **All branch POs of an OF go out together or not at all.** `pdf.print_order_form` generates any missing POs, issues every PO waiting to be issued, and builds the print set in one `tx`. If any one fails, the whole transaction rolls back. `issue_batch` (one PO at a time, best effort) is used only by tests. POPage shows "Print and issue" only for a legacy PO with no OF (`po_page.py:571`).
3. **Reprints are copies.** Asking for `issued_pdf(user!=None)` records `PO_REPRINT` and stamps "COPY n" with n = earlier PO_REPRINTs + 2, counted inside the same tx. Passing `user=None` returns the original bytes without recording anything. RECEIVER and WAREHOUSE are refused (`orders.py:411`). The first print of a DR is the original and needs `po.issue`. Later DR prints are stamped copies, allowed to anyone except receivers (`pdf.delivery_receipt`).
4. **Cancel** (`po.cancel`, reason required): status must be DRAFT, PENDING, APPROVED or ISSUED. It is refused when an amendment is pending, when any branch count exists, when there are live DM/CS/IP (`live_charges`), when an ACTIVE check pays the OF (`refuse_if_paid`), or when anything was received. A peso overall discount is shared again across the sibling POs, then `sync_parent_of`.
5. **Close** (`po.close`, reason required): status must be ISSUED, PARTIAL or FULL, with no pending amendment. A PO issued after `receiving.live_since` (the first MIGRATE_V9 audit row) needs an APPROVED branch count, and no count may be in progress.
6. **Receive outside** (`po.receive_outside`, ADMIN): status must be ISSUED, PARTIAL or FULL, with no branch count and no pending amendment. It sets `received_qty_m = qty_m` under write_guard, closes the PO, and inserts `outside_receipts`.
7. **Amend** (`po.amend`, reason required): status must be APPROVED, ISSUED or PARTIAL, the OF must not be paid, and only one pending amendment is allowed. Only `AMENDABLE_HEADER` fields can change. Supplier, branch and PO date can't. On an OF-linked PO, `ORDER_LEVEL_HEADER` (VAT, overall discount, other charges) and `ORDER_LEVEL_LINE` (price, product, dept, discounts) are refused, and new rows are refused. A row's qty can't go below `received_qty_m`, and can't drop while a DM/CS charges it as SHORT or DAMAGED. A row can't be removed if it was received, charged, or counted or stickered at the warehouse. "Nothing changed" is refused.
8. **When an amendment needs approval:** when the policy is enabled and qty per piece rises, a row is new, it costs more per piece, a discount gets smaller, a money header field changes (payment_terms, discount, VAT, charges), or the grand total rises. Otherwise it is applied at once and recorded as `AUTO_APPROVED`. A notes-only change (`UNPRINTED_HEADER`: internal_notes, legacy_po_number) makes no revision and no amendment row (`PO_NOTES`).
9. **Decide amendment:** requires `po.approve`, the user must be in `approval.approver_user_ids`, and the requester can't approve their own unless `allow_self_approval` is on. A rejection needs a reason. On approve, `_recheck_waiting` checks the rows again against today's PO, because counts or DMs may have been made while it waited.
10. **External PO number** (`set_legacy_number`, `po.edit`): it can be set at any status without an amendment, and must be unique per branch among non-cancelled POs.
11. **Attachments:** `po.edit`. Program file types are refused (`PROGRAM_SUFFIXES`, trailing dots and spaces ignored), as are empty files and files over 200 MB. RECEIVER and WAREHOUSE can't list attachments.
12. **DR:** `ensure_first` needs `po.issue` or `payment.edit`. `another_delivery` (`po.issue`) is allowed only while the PO is ISSUED or PARTIAL. `mark_emailed` needs `po.issue`.

## Main flows

- **Print and issue an OF** (Print page, To print tab): `common.print_order_forms` → `pdf.print_order_form` → `order_forms.generate_pos` → `tx{ orders.issue(each) → _print_set(warehouse copy [OF_PRINT_WAREHOUSE], optional 2 blind sheets per PO, cut slips, issued PDFs, office DRs [DR_PRINT]) }`. Audit actions: `PO_ISSUE`, `DR_CREATE`, `DR_PRINT`.
- **`issue(po_id)`**: checks → `pdf.render_supplier_po(preview=False)` → INSERT `po_issued_versions` → UPDATE status to ISSUED (to PARTIAL if something was received; ISSUED/PARTIAL keep their status) and set `issued_revision = revision` → audit `PO_ISSUE` → `delivery_receipts.ensure_first`.
- **Reprint an OF** (Printed tab): `pdf.reprint_order_form` → in one tx: `OF_REPRINT`, `PO_REPRINT` per live PO, `_ensure_first` DR (no permission check, deliberately left as is), `_print_set(reprint=True)`, and the whole file stamped `COPY n`.
- **Amend:** `POPage.save_amendment` → `orders.amend` → INSERT `po_amendments` (PENDING). Then either `po_approvals SUBMITTED` + `PO_AMEND_REQUEST` and stop, or `_apply_amendment` + `AUTO_APPROVED`. `_apply_amendment` (under write_guard): update the header, delete dropped rows, shift line_no by +100000 and then renumber, update or insert rows, `revision+1`, `order_forms.revision+1`, store totals, `_reshare_siblings` (`PO_DISCOUNT_SHARE` per sibling, which also go back to APPROVED if ISSUED), ISSUED→APPROVED or `_refresh_delivery`, amendment APPLIED, audit `PO_AMEND`, then `sync_parent_of` (`OF_SYNC`).
- **Decide:** Approvals page → `orders.decide_amendment` → approve: `_recheck_waiting` → `_apply_amendment` → `po_approvals APPROVED`. Reject: amendment REJECTED, `po_approvals REJECTED`, `PO_AMEND_REJECT`.
- **Cancel:** checks → `agreed_discount_shares(leaving={po})` (before any write) → CANCELLED + `PO_CANCEL` → `_reshare_siblings` → `sync_parent_of`. `order_forms` cancelling a whole OF calls `cancel(..., sync_of=False)` so the OF keeps showing what was ordered.
- **Close** → `PO_CLOSE`. **Receive outside** → `PO_RECEIVED_OUTSIDE`. **Legacy number** → `PO_LEGACY_NUMBER`. **Attach** → `PO_ATTACH`.
- **DR email:** DRs to email tab → `waiting_to_email` (one group per OF; needs email when channel=EMAIL, PO is ISSUED/PARTIAL, and it was never emailed or `emailed_revision < revision`) → `pdf.receipts_for_email` writes `data/email/DR <ref> <code>.pdf` (`DR_SAVED_FOR_EMAIL`) → the person sends it → `mark_emailed` (`DR_EMAILED`).

## Screens

**POPage** (`po_page.py`). Read-only: a fact line instead of input boxes. The tabs are Items, History (`audit_trail`), Attachments, and Issued versions (double-click asks to confirm a reprint, then `issued_pdf` with the user). Notices: cancelled or closed reason; "Revision N hasn't gone to the supplier yet"; legacy DRAFT/PENDING "can only be cancelled"; `payments.dr_differs` ("DR differs"); pending amendment. Buttons (`_build_actions`):
- "Open parent OF".
- "Amend" (`po.amend`, APPROVED/ISSUED/PARTIAL, not paid).
- "Preview" (APPROVED or unsent; renders a marked preview, free) or "Print again" (asks `confirm_reprint`).
- "Print and issue": legacy PO without OF only.
- Under "More": Cancel PO (`can_cancel`), Close PO (`can_close`), Received in full outside the program, Save external number, Save PDF.

While amending, on a linked PO only the Pieces column is editable and add-row is hidden. The OF-level money controls are disabled when linked. Keyboard: Esc goes back (asks if amending), Ctrl+S saves the amendment, Ctrl+N adds a row (legacy only), Enter on the last row in Disc 2/Amount/Remarks adds a row. Print files go to `data/print/` with a millisecond timestamp and the PC name, and are never overwritten (`write_print_file`).

**PrintPage** (`pages.py:455`), Purchasing sidebar. It is hidden from receiver-only, accounting-only and warehouse-only users, so VIEWER, APPROVER, BUYER and ADMIN see it. To print needs `po.issue`. Reprints on the Printed tab are open to anyone who reaches the page. The "Add paper count forms" checkbox adds 2 blind sheets per PO. All DRs: Print (the first print needs `po.issue`) and "Another DR" (`po.issue`; office-channel DRs open for printing right away).

## Money and numbers

- PO number `format_po_number`: `{year}-{branch_code}-{seq:05d}`, counted per (year, branch) in `po_number_sequences`. `next_po_number` must run inside `tx` (BEGIN IMMEDIATE) so two PCs can't take the same number.
- DR number: `{year}-{branch}-DR-{seq:05d}`, and `-{n}` for delivery n>1 (`delivery_receipts.format_number`). It reuses the PO's `po_year/po_seq`.
- Line amounts come from `money.compute_line`; totals come from `money.compute_totals` (`_totals`) and are stored by `store_totals`. Quantities are `_m` (×1000), with `pieces = qty_m * pieces_per_unit`. Safety cap: 10^9 pieces per row.
- "Costs more" is compared per piece: `unit_cost * prior_ppu > prior_cost * ppu` (`orders.py:691-693`), so changing the unit can't hide a price rise.
- Agreed OF overall discount (`agreed_discount_shares`): a percent OF gives each changed PO `round_centavos(merchandise * bp / 10000)`. A peso OF shares the agreed amount across the live POs by merchandise using `split_by_weight` (largest remainder, so it adds back exactly). It is refused when the discount is ≥ the merchandise left, or would bring a PO to ₱0. Each PO stores its share as an `AMOUNT` discount in pesos.
- The DR shows no prices (it has Pieces ordered and blank Pieces delivered, Unit price, Total). Channel: OFFICE if the PO's supplier snapshot has `sticker_rate_c` > 0, otherwise EMAIL.

## Interactions with other modules

- Called by: `order_forms` (`generate_pos` uses `next_po_number`, `insert_items`, `store_totals`; OF cancel calls `cancel(sync_of=False)`; OF amendments use `write_guard`), `pdf` (`issue`, `issued_pdf`, `to_issue`), `payments` (`receipts_to_enter` → `ensure_first`; `paid_by` checks), `receiving` (`write_guard`, sets received_qty and PARTIAL/FULL via `_apply_totals`), `ui/pages.py` (Approvals, Print), `sample_data`.
- Calls: `receiving.dm_pieces`, `progress`, `live_since`, `_apply_totals`; `pdf.render_supplier_po`, `stamp_copy`; `masters.clean_terms`; `money.*`; `delivery_receipts.ensure_first`.
- `order_forms` has its own `decide_amendment` and `pending_amendments` for OF amendments; the Approvals page shows both lists.

## Tests

`tests/test_fix_orders.py` (amendment recheck, delivery status, gates, write_guard, issued_pdf), `tests/test_fix2_po-amend-labels.py`, `tests/test_delivery_receipts.py`, `tests/test_reprints.py`, `tests/test_rules_discount.py` (agreed discount shares), `tests/test_fix_attachments.py`, `tests/test_rules_paging_orders.py`, `tests/test_business_rules.py`. DR line differences on Payments are in `tests/test_rules_dr_lines.py` (that belongs to the payments area). From `apps/desktop`:
`python -E -m pytest tests/test_fix_orders.py tests/test_delivery_receipts.py tests/test_reprints.py -q -p no:cacheprovider`

## Gotchas for changing this module

- **write_guard** (`orders.py:70`): inserts a random token into `po_write_guard` and deletes it in `finally`. It raises `RuntimeError` outside a transaction, because a token committed on its own would unlock every document. Any write to terms, totals, `revision`, items or the parent/branch of a past-DRAFT PO (and of a past-DRAFT OF) must sit inside it, within `tx`. Never add a write path that skips it, and never weaken the triggers.
- `db.tx` nests by joining the outer transaction (`if conn.in_transaction: yield`). That is why `issue()` can be called inside `print_order_form`'s tx, and why one failing PO rolls back the whole OF print. `issue_batch` relies on each `issue` having its own tx, so calling it inside an outer tx would undo that.
- `status` changes don't need the guard, because `po_terms_locked` doesn't cover status. `revision` changes do.
- Append-only: `po_issued_versions`, `po_approvals`, decided `po_amendments`, `outside_receipts`, `delivery_receipts` (number, po and delivery_no fixed; no delete), and `po_attachments`. An amendment never edits an issued version; it adds a revision.
- An amendment returns the PO to **APPROVED** (or keeps PARTIAL/FULL with `unsent`), so a linked PO must be reissued from the Print page. Note that `print_order_form` reissues every PO of the OF that is `to_issue`. A peso-discount amendment can also send **sibling** POs back to APPROVED (`_reshare_siblings`).
- `issued_pdf(None, …)` is the system fetch and records nothing. Passing a user records a reprint. Keep that distinction. `pypdf` is pinned below 7 because of `stamp_copy`.
- `sync_parent_of` checks `po.cancel`, `po.amend` or `po.approve` itself, so call it only from those flows. It rewrites the OF's allocations and totals under write_guard.
- Things that look like bugs but are deliberate: a PO linked to an OF can't change price or product (that needs a new OF); a POPage notes-only save makes no revision; `close` refuses a post-live_since PO with nothing counted (cancel it instead); `can_cancel` and `can_close` repeat the service checks so buttons appear only when the action would work.
- `amend` and `_apply_amendment` both compute discount shares. The second time is on purpose, so an amendment approved later uses today's figures.
- Possible issues found while writing this doc (not changed):
  - `decide_amendment` checks `refuse_if_paid` and the status before `require(user, "po.approve")` (`orders.py:826-830`).
  - `reprint_order_form` has no permission check beyond "not a receiver" and can create DRs (`_ensure_first`).
  - `cancel` calls `live_charges` twice.
  - `POPage.save_blind_sheet` is not wired to any button.
  - `issue_batch` has no production caller.
