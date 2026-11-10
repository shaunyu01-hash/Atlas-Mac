import type { Role } from "@atlas/contracts";
import { appUsers } from "@atlas/db";
import { eq } from "drizzle-orm";
import { createMiddleware } from "hono/factory";
import { HTTPException } from "hono/http-exception";
import { createRemoteJWKSet, jwtVerify } from "jose";
import { getDb, supabaseUrl } from "./env.ts";

export type AuthUser = {
  id: string;
  displayName: string;
  role: Role;
  branchId: string | null;
  isApprover: boolean;
};

export type AuthEnv = { Variables: { user: AuthUser } };

let jwks: ReturnType<typeof createRemoteJWKSet> | undefined;

/** Checks a Supabase Auth access token against the project's published signing keys. Returns the user id. */
async function verifySupabaseToken(token: string): Promise<string> {
  const base = supabaseUrl();
  jwks ??= createRemoteJWKSet(new URL(`${base}/auth/v1/.well-known/jwks.json`));
  const { payload } = await jwtVerify(token, jwks, { issuer: `${base}/auth/v1`, audience: "authenticated" });
  if (!payload.sub) throw new Error("Token has no subject");
  return payload.sub;
}

/**
 * Requires a signed-in Supabase user who also has an active Atlas profile (app_users).
 * Supabase proves who someone is; the Atlas profile decides what they may do.
 */
export const requireUser = createMiddleware<AuthEnv>(async (c, next) => {
  const header = c.req.header("authorization") ?? "";
  const token = header.startsWith("Bearer ") ? header.slice(7) : "";
  if (!token) throw new HTTPException(401, { message: "Sign in first" });

  let userId: string;
  try {
    userId = await verifySupabaseToken(token);
  } catch {
    throw new HTTPException(401, { message: "Your sign-in has expired. Sign in again." });
  }

  const [profile] = await getDb().select().from(appUsers).where(eq(appUsers.id, userId)).limit(1);
  if (!profile?.active) throw new HTTPException(403, { message: "This login has no active Atlas access" });

  c.set("user", {
    id: profile.id,
    displayName: profile.displayName,
    role: profile.role,
    branchId: profile.branchId,
    isApprover: profile.isApprover,
  });
  await next();
});

export function requireRole(...roles: Role[]) {
  return createMiddleware<AuthEnv>(async (c, next) => {
    if (!roles.includes(c.var.user.role)) {
      throw new HTTPException(403, { message: "You don't have permission to do this" });
    }
    await next();
  });
}
