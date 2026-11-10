# Shared packages (`packages/`)

Internal packages export TypeScript source directly (`exports: ./src/index.ts`), so there is no build step. `domain` and `db` both depend on `contracts` (`db` uses its types only), and the apps depend on all three. Nothing depends on an app.

## contracts (`@atlas/contracts`)

Zod schemas and types for everything that crosses the API: `common.ts` (Centavos, Role, ApiError), `custom-fields.ts`, `suppliers.ts`.
- Use `XInput` for what a client sends and `X` for what the API returns.
- These schemas are also the format future AI-generated definitions must match. Keep them declarative: no code, no SQL.

## domain (`@atlas/domain`)

Pure business rules shared by the API and the web app. No I/O, no database.
- `money.ts`: `parsePesos`, `formatPeso` and `formatPlain`, ported from `apps/desktop/atlaspo/money.py` with identical rounding (half up).
- `custom-fields.ts`: `customValuesSchema(defs)` and `validateCustomValues(defs, values)`. Active fields are type-checked, retired fields keep their old values, and unknown keys are rejected.
- **Porting a desktop rule:** read the Python module and its tests first. Write the vitest cases from those tests, then the code. Keep `*.test.ts` next to the source and run `pnpm --filter @atlas/domain test`.

## db (`@atlas/db`)

Drizzle schema for Supabase Postgres (`src/schema.ts`) and `createDb(url)` (postgres-js, `prepare: false` for the pooler).
- Tables: `app_users` (linked to `auth.users`), `field_definitions`, `suppliers` (with a `custom jsonb` column and a GIN index), and `audit_logs`.
- Every table calls `.enableRLS()` and has no policies, so only the API can reach the data.
- To let users add fields to another kind of record: add a `custom jsonb` column, add the entity name to `CustomizableEntity` in contracts, and add API routes that call `validateCustomValues`.
- Migrations: edit the schema, run `pnpm db:generate` (offline), review the SQL in `drizzle/`, then run `pnpm db:migrate`, which needs `DIRECT_DATABASE_URL`. Never edit an already-applied migration.
