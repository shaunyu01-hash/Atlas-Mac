import { z } from "zod";

/** Money is always whole centavos (₱1,234.50 = 123450), never a float. */
export const Centavos = z.number().int().safe();

export const Uuid = z.uuid();

export const Role = z.enum([
  "owner",
  "admin",
  "approver",
  "purchaser",
  "accounting",
  "warehouse",
  "receiver",
  "viewer",
]);
export type Role = z.infer<typeof Role>;

export const ApiError = z.object({
  error: z.string(),
  issues: z.array(z.object({ path: z.string(), message: z.string() })).optional(),
});
export type ApiError = z.infer<typeof ApiError>;
