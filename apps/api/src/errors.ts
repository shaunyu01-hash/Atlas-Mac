/** Postgres error code from a driver error, including when Drizzle wraps it in `cause`. */
export function pgCode(err: unknown): string | undefined {
  for (let e: unknown = err; e && typeof e === "object"; e = (e as { cause?: unknown }).cause) {
    const code = (e as { code?: unknown }).code;
    if (typeof code === "string") return code;
  }
  return undefined;
}

export const UNIQUE_VIOLATION = "23505";
