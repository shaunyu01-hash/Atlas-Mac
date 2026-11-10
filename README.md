# Atlas PO ERP

A Windows purchasing system for businesses that buy from suppliers for several branches through one central warehouse. It runs offline on one PC, or on several PCs sharing a database folder on the office network.

It covers the whole path of a purchase:

1. **Order Form (OF):** one supplier order across all branches, split into a PO per branch once approved.
2. **Approval:** a peso limit on the whole order, with separate preparer and approver.
3. **Printing:** the warehouse OF, the supplier POs, cut slips and delivery receipts, printed and issued together.
4. **Central Warehouse:** records each delivery (boxes per branch PO, stickered pieces) and ships to the branches.
5. **Branch receiving:** two receivers count each delivery blind. Only an agreed count is recorded.
6. **Differences:** debit memos for shortages, claim stubs for damaged goods, item passes for excess.
7. **Payments:** checks worked out from what the branches counted, then followed from printed to claimed to cleared, with cash flow.
8. **Selling prices** and reports.

Inventory is the next module (see [Next up](#next-up)).

## Start in 2 minutes

```
git clone https://github.com/shaunyu01-hash/Atlas-PO-ERP.git
cd Atlas-PO-ERP\po_app
pip install -r requirements.txt
python -E "Atlas PO ERP (Sample Data).pyw"
```

It runs on Python 3.14 and 64-bit Windows.

**Practice login:** `owner`, password `123`, the same for every sample login. Other roles: `admin`, `approver`, `purchaser`, `accounting`, `warehouse` and `viewer`. Each branch has two receivers: `nor1`/`nor2` (North), `sou1`/`sou2` (South), `eas1`/`eas2` (East) and `wes1`/`wes2` (West). The `123` password works in the sample data only.

The sample data lives in `Documents\Atlas PO ERP Sample`. Delete that folder to start fresh; it rebuilds itself the next time it opens.

| To do this | Run this in `po_app` |
|---|---|
| Open the sample program | `python -E "Atlas PO ERP (Sample Data).pyw"` |
| Open the real program | `python -E "Atlas PO ERP.pyw"` |
| Run the tests (about 12 minutes) | `python -E -m pytest tests -q` |
| Build the `.exe` | `Build EXE.cmd` |

**First use with real data:**
1. The program asks you to create an administrator.
2. Fill in the company name, address, TIN and logo under Settings, Company.
3. Set up branches and the warehouse under Branches. A new database starts with four example branches; rename them or add your own.
4. Add users and tick the approvers under Settings, Approval policy.
5. Add suppliers and products, or let products join the catalog as approved OFs bring them in.

The real database is `Documents\Atlas PO ERP\po.sqlite`. Set `PO_DATA_DIR` to put it elsewhere, for example a shared folder for several PCs.

## How to use it

The full guide (every screen, the rules the system enforces, roles, backups and a code map) is in [po_app/README.md](po_app/README.md).

## Next up

**Inventory, Phase 1:** stock on hand per branch and at the warehouse. It builds on the deliveries the program already records, and adds stock counts and approved adjustments.

## Repository

| Path | What it is |
|---|---|
| `po_app/` | The program: `atlaspo/` (code), `tests/`, launchers and `Build EXE.cmd` |
| `agents/`, `.claude/agents/` | Role-play test agents (purchaser, receivers, accounting, auditors) for scenario runs |
| `PRODUCT.md` | Who the users are and the design principles |
