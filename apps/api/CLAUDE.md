# API (`apps/api`)

Hono with `@hono/zod-openapi`. It runs on Node locally and serverless on Vercel. It is the **only** way any client reaches data: the web app now, the desktop app later through a generated OpenAPI client.

## Files

```
src/app.ts          builds the app: CORS, error handler, routes, /health, /openapi.json
src/index.ts        serverless entry (default export). src/server.ts runs it locally on :8787
src/env.ts          getDb() opens the database on first use (so tests need no database); env lookups
src/auth.ts         requireUser (Supabase token → app_users profile) and requireRole(...)
src/openapi.ts      router() factory with ApiError validation hook, errorResponses, bearer
src/errors.ts       pgCode(err), UNIQUE_VIOLATION
src/routes/*.ts     one file per resource; suppliers.ts is the reference example
```

## Adding a route

1. Put request and response schemas in `packages/contracts`, never inline in the route.
2. Use `router<AuthEnv>()`, then `.use("*", requireUser)`. For writes, add `middleware: [requireRole(...)] as const` on the route.
3. Write with `getDb().transaction(...)`, and insert an `auditLogs` row in the same transaction.
4. Put business rules in `packages/domain`, not in the handler. The handler validates, calls the rules, writes, and responds.
5. Return `{ error, issues? }` (ApiError) for failures. Map `UNIQUE_VIOLATION` to 409.
6. Mount the router in `app.ts`. The route then appears in `/openapi.json` automatically. Add its path to the list in `app.test.ts`.

## Auth model

The Supabase JWT is verified against the project's JWKS (`SUPABASE_URL/auth/v1/.well-known/jwks.json`). The user's role, branch and approver flag then come from the `app_users` table. Supabase proves who someone is; `app_users` decides what they may do.

## Run and test

- `.env` comes from `.env.example`. `DATABASE_URL` is the pooled Supabase connection (port 6543, `prepare: false`). `DIRECT_DATABASE_URL` is used only for migrations.
- `pnpm --filter @atlas/api dev` · `pnpm --filter @atlas/api test`. Tests use `app.request()`, and the current ones need no database.
