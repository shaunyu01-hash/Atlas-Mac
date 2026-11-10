# Web app (`apps/web`)

React 19, Vite, TanStack Router (code-based routes in `main.tsx`), TanStack Query and Tailwind v4. It runs as a single-page app on :5173.

## Files

```
src/main.tsx                       routes, auth guard (requireSession), app shell and nav
src/lib/supabase.ts                Supabase client, used ONLY for sign-in
src/lib/api.ts                     api<T>(path, {method, body}): adds the bearer token, throws ApiRequestError
src/components/CustomFieldInputs   renders any custom-field definitions; draftToValues() converts typed text
src/pages/*Page.tsx                one file per screen; SuppliersPage is the reference example
src/styles.css                     theme tokens (@theme) plus .field / .btn / .label
```

## Conventions

- **Data always goes through `api()`**, never `supabase.from(...)`. Every table has Row-Level Security on with no policies, so a direct query returns nothing.
- Types come from `@atlas/contracts`. Validate before sending with the same schemas and `@atlas/domain` rules the API uses, then show the server's `issues` beside the fields they refer to.
- Money: the user types pesos, `parsePesos()` turns them into centavos for the API, and `formatPeso()` turns centavos back for display.
- Styling: use the theme tokens (`bg-surface`, `text-muted`, `border-line`, `text-accent`, `text-danger`), not raw colours. Dark mode follows the operating system. Keep layouts dense and keyboard friendly, per [PRODUCT.md](../../PRODUCT.md).
- A new screen means a page in `src/pages`, a `createRoute` under `appRoute` in `main.tsx`, and a nav `Link` in `Shell`.

## Run

- `.env.local` comes from `.env.example`. These values are public; never put a service-role key there.
- `pnpm --filter @atlas/web dev` · `pnpm --filter @atlas/web build`. Run the API at the same time for data.
