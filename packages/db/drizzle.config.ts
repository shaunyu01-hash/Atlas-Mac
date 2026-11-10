import { defineConfig } from "drizzle-kit";

// Migrations use Supabase's DIRECT connection (port 5432), not the pooler.
// DATABASE_URL is only needed for `migrate` and `studio`; `generate` works offline.
export default defineConfig({
  dialect: "postgresql",
  schema: "./src/schema.ts",
  out: "./drizzle",
  schemaFilter: ["public"],
  dbCredentials: { url: process.env.DIRECT_DATABASE_URL ?? "" },
  strict: true,
  verbose: true,
});
