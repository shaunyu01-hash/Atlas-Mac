import { drizzle } from "drizzle-orm/postgres-js";
import postgres from "postgres";
import * as schema from "./schema.ts";

export * from "./schema.ts";
export { schema };

/**
 * Opens the database. Use Supabase's POOLED connection string (Supavisor, transaction mode, port 6543) for the API:
 * serverless functions each open their own connections, and the pooler keeps that within Supabase's limits.
 * Transaction mode doesn't support prepared statements, hence `prepare: false`.
 */
export function createDb(url: string, options: { max?: number } = {}) {
  const client = postgres(url, { prepare: false, max: options.max ?? 5 });
  return drizzle(client, { schema });
}

export type Db = ReturnType<typeof createDb>;
