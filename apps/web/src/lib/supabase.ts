import { createClient } from "@supabase/supabase-js";

const url = import.meta.env.VITE_SUPABASE_URL;
const key = import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY;
if (!url || !key) throw new Error("Copy apps/web/.env.example to apps/web/.env.local and fill in the Supabase values.");

/** Used only for signing in. All data goes through the Atlas API, never straight to Supabase tables. */
export const supabase = createClient(url, key);
