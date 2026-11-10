import type { CustomFieldDefinition, CustomFieldType } from "@atlas/contracts";
import { z } from "zod";

type Definition = Pick<CustomFieldDefinition, "key" | "type" | "required" | "options" | "active" | "label">;

function valueSchema(type: CustomFieldType, options: string[] | null): z.ZodType {
  switch (type) {
    case "text":
      return z.string().trim().max(500);
    case "long_text":
      return z.string().trim().max(5000);
    case "number":
      return z.number().finite();
    case "money":
      return z.number().int().safe(); // centavos
    case "date":
      return z.iso.date();
    case "boolean":
      return z.boolean();
    case "select":
      if (!options?.length) throw new Error("A select field has no options");
      return z.enum(options as [string, ...string[]]);
  }
}

/**
 * Builds the validator for a record's `custom` values from the field definitions of its entity.
 * - Active fields are checked by type; required ones must be present.
 * - Values of retired (inactive) fields are kept untouched, so old records stay readable.
 * - Keys that no definition knows are rejected, so typos and stray data never reach the database.
 */
export function customValuesSchema(definitions: readonly Definition[]) {
  const shape: Record<string, z.ZodType> = {};
  for (const d of definitions) {
    if (!d.active) {
      shape[d.key] = z.unknown().optional();
      continue;
    }
    const schema = valueSchema(d.type, d.options);
    shape[d.key] = d.required ? schema : schema.optional();
  }
  return z.strictObject(shape);
}

export type CustomValuesResult =
  | { ok: true; values: Record<string, unknown> }
  | { ok: false; issues: { path: string; message: string }[] };

export function validateCustomValues(definitions: readonly Definition[], values: unknown): CustomValuesResult {
  const result = customValuesSchema(definitions).safeParse(values ?? {});
  if (result.success) return { ok: true, values: result.data };
  return {
    ok: false,
    issues: result.error.issues.map((i) => ({ path: ["custom", ...i.path].join("."), message: i.message })),
  };
}
