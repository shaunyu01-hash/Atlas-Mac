import { z } from "zod";

/**
 * Records that users can add their own fields to. Each one has a `custom jsonb` column.
 * Add an entity here only together with that column and its API routes.
 */
export const CustomizableEntity = z.enum(["supplier"]);
export type CustomizableEntity = z.infer<typeof CustomizableEntity>;

export const CustomFieldType = z.enum(["text", "long_text", "number", "money", "date", "boolean", "select"]);
export type CustomFieldType = z.infer<typeof CustomFieldType>;

/** Lower snake case, starts with a letter. It is the key inside `custom`, so it never changes once published. */
export const CustomFieldKey = z
  .string()
  .regex(/^[a-z][a-z0-9_]{0,47}$/, "Use lower-case letters, digits and underscores, starting with a letter");

/**
 * What an admin (or, later, the AI) writes to define a field. This is the format AI output must match,
 * so it stays declarative: no code, no SQL, nothing that changes the database structure.
 */
export const CustomFieldDefinitionInput = z
  .object({
    entity: CustomizableEntity,
    key: CustomFieldKey,
    label: z.string().trim().min(1).max(80),
    type: CustomFieldType,
    required: z.boolean().default(false),
    helpText: z.string().trim().max(200).optional(),
    options: z.array(z.string().trim().min(1).max(80)).min(1).max(100).optional(),
  })
  .refine((d) => (d.type === "select") === (d.options !== undefined), {
    message: "Select fields need options, and only select fields may have them",
    path: ["options"],
  });
export type CustomFieldDefinitionInput = z.infer<typeof CustomFieldDefinitionInput>;

export const CustomFieldDefinition = z.object({
  id: z.uuid(),
  entity: CustomizableEntity,
  key: CustomFieldKey,
  label: z.string(),
  type: CustomFieldType,
  required: z.boolean(),
  helpText: z.string().nullable(),
  options: z.array(z.string()).nullable(),
  sortOrder: z.number().int(),
  active: z.boolean(),
});
export type CustomFieldDefinition = z.infer<typeof CustomFieldDefinition>;

/** Values stored in a record's `custom` column, keyed by field key. Checked against the definitions by @atlas/domain. */
export const CustomValues = z.record(z.string(), z.unknown());
export type CustomValues = z.infer<typeof CustomValues>;
