import type { ApiError } from "@atlas/contracts";
import { supabase } from "./supabase.ts";

const base = (import.meta.env.VITE_API_URL ?? "http://localhost:8787").replace(/\/$/, "");

export class ApiRequestError extends Error {
  constructor(
    readonly status: number,
    readonly body: ApiError,
  ) {
    super(body.error);
  }
}

/** Calls the Atlas API with the signed-in user's token. Errors come back as ApiRequestError with the server's message. */
export async function api<T>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const { data } = await supabase.auth.getSession();
  const headers: Record<string, string> = {};
  if (data.session) headers.Authorization = `Bearer ${data.session.access_token}`;
  if (init.body !== undefined) headers["Content-Type"] = "application/json";

  const res = await fetch(`${base}${path}`, {
    method: init.method ?? "GET",
    headers,
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
  });
  const json = await res.json().catch(() => ({ error: `The server answered ${res.status}` }));
  if (!res.ok) throw new ApiRequestError(res.status, json as ApiError);
  return json as T;
}
