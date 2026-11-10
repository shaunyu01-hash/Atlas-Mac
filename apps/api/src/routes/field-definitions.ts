import { ApiError, CustomFieldDefinition, CustomFieldDefinitionInput, CustomizableEntity } from "@atlas/contracts";
import { auditLogs, fieldDefinitions } from "@atlas/db";
import { createRoute, z } from "@hono/zod-openapi";
import { asc, eq, sql } from "drizzle-orm";
import { type AuthEnv, requireRole, requireUser } from "../auth.ts";
import { getDb } from "../env.ts";
import { pgCode, UNIQUE_VIOLATION } from "../errors.ts";
import { bearer, errorResponses, router } from "../openapi.ts";

export const fieldDefinitionRoutes = router<AuthEnv>();
fieldDefinitionRoutes.use("*", requireUser);

function toDto(row: typeof fieldDefinitions.$inferSelect): CustomFieldDefinition {
  return CustomFieldDefinition.parse({ ...row, options: row.options ?? null });
}

const list = createRoute({
  method: "get",
  path: "/",
  tags: ["Custom fields"],
  summary: "Field definitions for one kind of record, in display order",
  security: bearer,
  request: { query: z.object({ entity: CustomizableEntity }) },
  responses: {
    200: { description: "Definitions", content: { "application/json": { schema: z.array(CustomFieldDefinition) } } },
    ...errorResponses,
  },
});

fieldDefinitionRoutes.openapi(list, async (c) => {
  const { entity } = c.req.valid("query");
  const rows = await getDb()
    .select()
    .from(fieldDefinitions)
    .where(eq(fieldDefinitions.entity, entity))
    .orderBy(asc(fieldDefinitions.sortOrder), asc(fieldDefinitions.createdAt));
  return c.json(rows.map(toDto), 200);
});

const create = createRoute({
  method: "post",
  path: "/",
  tags: ["Custom fields"],
  summary: "Add a custom field (owner or admin)",
  security: bearer,
  middleware: [requireRole("owner", "admin")] as const,
  request: { body: { required: true, content: { "application/json": { schema: CustomFieldDefinitionInput } } } },
  responses: {
    201: { description: "Created", content: { "application/json": { schema: CustomFieldDefinition } } },
    409: { description: "Key already used", content: { "application/json": { schema: ApiError } } },
    ...errorResponses,
  },
});

fieldDefinitionRoutes.openapi(create, async (c) => {
  const input = c.req.valid("json");
  const user = c.var.user;
  try {
    const row = await getDb().transaction(async (tx) => {
      const [inserted] = await tx
        .insert(fieldDefinitions)
        .values({
          entity: input.entity,
          key: input.key,
          label: input.label,
          type: input.type,
          required: input.required,
          helpText: input.helpText ?? null,
          options: input.options ?? null,
          createdBy: user.id,
          sortOrder: sql`(select coalesce(max(${fieldDefinitions.sortOrder}), 0) + 1 from ${fieldDefinitions} where ${fieldDefinitions.entity} = ${input.entity})`,
        })
        .returning();
      if (!inserted) throw new Error("Insert returned nothing");
      await tx.insert(auditLogs).values({
        userId: user.id,
        action: "field_definition.create",
        entity: "field_definition",
        entityId: inserted.id,
        details: { entity: input.entity, key: input.key, type: input.type },
      });
      return inserted;
    });
    return c.json(toDto(row), 201);
  } catch (err) {
    if (pgCode(err) === UNIQUE_VIOLATION) {
      return c.json({ error: `A ${input.entity} field with the key "${input.key}" already exists` }, 409);
    }
    throw err;
  }
});
