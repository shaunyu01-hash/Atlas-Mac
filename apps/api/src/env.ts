import { createDb, type Db } from "@atlas/db";

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is not set. Copy apps/api/.env.example to apps/api/.env and fill it in.`);
  return value;
}

let db: Db | undefined;

/** Opened on first use, so routes that don't touch the database (health, OpenAPI) work without credentials. */
export function getDb(): Db {
  db ??= createDb(required("DATABASE_URL"));
  return db;
}

export function supabaseUrl(): string {
  return required("SUPABASE_URL").replace(/\/$/, "");
}

export function corsOrigins(): string[] {
  return (process.env.CORS_ORIGINS ?? "http://localhost:5173")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}
