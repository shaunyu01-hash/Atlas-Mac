import { z } from "zod";
import { CustomValues } from "./custom-fields.ts";

export const SupplierInput = z.object({
  name: z.string().trim().min(1).max(160),
  tin: z.string().trim().max(20).optional(),
  address: z.string().trim().max(400).optional(),
  terms: z.string().trim().max(80).optional(),
  custom: CustomValues.default({}),
});
export type SupplierInput = z.infer<typeof SupplierInput>;

export const Supplier = z.object({
  id: z.uuid(),
  name: z.string(),
  tin: z.string().nullable(),
  address: z.string().nullable(),
  terms: z.string().nullable(),
  custom: CustomValues,
  createdAt: z.string(),
});
export type Supplier = z.infer<typeof Supplier>;
