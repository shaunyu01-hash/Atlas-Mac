import { ApiError, Supplier, SupplierInput } from "@atlas/contracts";
import { auditLogs, fieldDefinitions, suppliers } from "@atlas/db";
import { validateCustomValues } from "@atlas/domain";
import { createRoute, z } from "@hono/zod-openapi";
import { asc, eq } from "drizzle-orm";
import { type AuthEnv, requireRole, requireUser } from "../auth.ts";
import { getDb } from "../env.ts";
import { pgCode, UNIQUE_VIOLATION } from "../errors.ts";
import { bearer, errorResponses, router } from "../openapi.ts";

export const supplierRoutes = router<AuthEnv>();
supplierRoutes.use("*", requireUser);

function toDto(row: typeof suppliers.$inferSelect): Supplier {
  return Supplier.parse(row);
}

const list = createRoute({
  method: "get",
  path: "/",
  tags: ["Suppliers"],
  summary: "All suppliers, by name",
  security: bearer,
  responses: {
    200: { description: "Suppliers", content: { "application/json": { schema: z.array(Supplier) } } },
    ...errorResponses,
  },
});

supplierRoutes.openapi(list, async (c) => {
  const rows = await getDb().select().from(suppliers).orderBy(asc(suppliers.name));
  return c.json(rows.map(toDto), 200);
});

const create = createRoute({
  method: "post",
  path: "/",
  tags: ["Suppliers"],
  summary: "Add a supplier (owner, admin or purchaser)",
  security: bearer,
  middleware: [requireRole("owner", "admin", "purchaser")] as const,
  request: { body: { required: true, content: { "application/json": { schema: SupplierInput } } } },
  responses: {
    201: { description: "Created", content: { "application/json": { schema: Supplier } } },
    409: { description: "Name already used", content: { "application/json": { schema: ApiError } } },
    ...errorResponses,
  },
});

supplierRoutes.openapi(create, async (c) => {
  const input = c.req.valid("json");
  const user = c.var.user;
  const db = getDb();

  const definitions = await db.select().from(fieldDefinitions).where(eq(fieldDefinitions.entity, "supplier"));
  const custom = validateCustomValues(definitions, input.custom);
  if (!custom.ok) return c.json({ error: "Some details need fixing", issues: custom.issues }, 400);

  try {
    const row = await db.transaction(async (tx) => {
      const [inserted] = await tx
        .insert(suppliers)
        .values({
          name: input.name,
          tin: input.tin ?? null,
          address: input.address ?? null,
          terms: input.terms ?? null,
          custom: custom.values,
        })
        .returning();
      if (!inserted) throw new Error("Insert returned nothing");
      await tx.insert(auditLogs).values({
        userId: user.id,
        action: "supplier.create",
        entity: "supplier",
        entityId: inserted.id,
        details: { name: input.name },
      });
      return inserted;
    });
    return c.json(toDto(row), 201);
  } catch (err) {
    if (pgCode(err) === UNIQUE_VIOLATION)
      return c.json({ error: `A supplier named "${input.name}" already exists` }, 409);
    throw err;
  }
});
