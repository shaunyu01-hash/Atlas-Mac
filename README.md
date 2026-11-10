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
9. **Inventory:** stock on hand at every branch and the warehouse, blind stock counts, approved adjustments and transfers. The Purchasing | Inventory switch at the top of the sidebar opens it.

## Start in 2 minutes

```
git clone https://github.com/shaunyu01-hash/Atlas-PO-ERP.git
cd Atlas-PO-ERP\apps\desktop
pip install -r requirements.txt
python -E "Atlas PO ERP (Sample Data).pyw"
```

It runs on Python 3.14 and 64-bit Windows.

**Practice login:** `owner`, password `123`, the same for every sample login. Other roles: `admin`, `approver`, `purchaser`, `accounting`, `warehouse` and `viewer`. Each branch has two receivers: `nor1`/`nor2` (North), `sou1`/`sou2` (South), `eas1`/`eas2` (East) and `wes1`/`wes2` (West). The `123` password works in the sample data only.

The sample data lives in `Documents\Atlas PO ERP Sample`. Delete that folder to start fresh; it rebuilds itself the next time it opens.

| To do this | Run this in `apps/desktop` |
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

The full guide (every screen, the rules the system enforces, roles, backups and a code map) is in [apps/desktop/README.md](apps/desktop/README.md).

## Next up

**Inventory, phase 2:** sales or POS capture, reorder points and costing methods. Phase 1 is in [apps/desktop/docs/inventory.md](apps/desktop/docs/inventory.md).

**Web app and API (in progress):** a TypeScript API and a browser app on Supabase, alongside the desktop program. So far they cover sign-in, suppliers, and custom fields that admins can add without code changes. That custom-field groundwork is what later AI features (fields, forms and processes made from a prompt) will build on.

## Web app and API

Needs Node 22+, pnpm and a Supabase project (Singapore region).

```
pnpm install
copy apps\api\.env.example apps\api\.env          (fill in from Supabase)
copy apps\web\.env.example apps\web\.env.local    (fill in from Supabase)
pnpm db:migrate
pnpm dev
```

The web app runs on http://localhost:5173 and the API on http://localhost:8787. The API's contract is at http://localhost:8787/openapi.json.

To sign in, create the user under Supabase Authentication, then give them an Atlas profile in the SQL editor:

```sql
insert into app_users (id, display_name, role)
select id, 'Owner', 'owner' from auth.users where email = 'you@example.com';
```

| To do this | Run this at the repository root |
|---|---|
| Start the API and web app | `pnpm dev` |
| Type-check everything | `pnpm typecheck` |
| Run the tests | `pnpm test` |
| Lint and format | `pnpm lint` / `pnpm format` |
| Make a migration after editing `packages/db/src/schema.ts` | `pnpm db:generate` |
| Apply migrations to Supabase | `pnpm db:migrate` |

**Ground rules:**
- Money is whole centavos everywhere, never a float.
- Every rule lives in `packages/domain` and is shared by the API and the web app.
- The browser never reads Supabase tables directly. Every table has Row-Level Security on with no policies, and all data goes through the API.

## Repository

| Path | What it is |
|---|---|
| `apps/desktop/` | The desktop program (Python, PySide6, SQLite): `atlaspo/` (code), `tests/`, launchers and `Build EXE.cmd` |
| `apps/api/` | The API (Hono, OpenAPI). `src/index.ts` is the serverless entry and `src/server.ts` runs it locally |
| `apps/web/` | The browser app (React, Vite, TanStack Router and Query, Tailwind) |
| `packages/contracts/` | Zod schemas shared by the API and the web app. They're also the format future AI output must match |
| `packages/domain/` | Business rules in TypeScript: money (ported from `atlaspo/money.py`) and custom-field checks |
| `packages/db/` | Drizzle schema and migrations for Supabase Postgres |
| `agents/`, `.claude/agents/` | Role-play test agents (purchaser, receivers, accounting, auditors) for scenario runs |
| `PRODUCT.md` | Who the users are and the design principles |
