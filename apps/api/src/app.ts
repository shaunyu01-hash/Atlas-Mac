import type { ApiError } from "@atlas/contracts";
import { createRoute, z } from "@hono/zod-openapi";
import { cors } from "hono/cors";
import { HTTPException } from "hono/http-exception";
import { logger } from "hono/logger";
import { corsOrigins } from "./env.ts";
import { router } from "./openapi.ts";
import { fieldDefinitionRoutes } from "./routes/field-definitions.ts";
import { supplierRoutes } from "./routes/suppliers.ts";

export const app = router();

if (!process.env.VITEST) app.use("*", logger());
app.use("*", cors({ origin: corsOrigins(), allowHeaders: ["Authorization", "Content-Type"], maxAge: 600 }));

app.onError((err, c) => {
  if (err instanceof HTTPException) return c.json<ApiError>({ error: err.message }, err.status);
  console.error(err);
  return c.json<ApiError>({ error: "Something went wrong on the server. Try again, or tell your administrator." }, 500);
});

app.notFound((c) => c.json<ApiError>({ error: "Not found" }, 404));

app.openapi(
  createRoute({
    method: "get",
    path: "/health",
    tags: ["System"],
    summary: "Liveness check",
    responses: {
      200: { description: "OK", content: { "application/json": { schema: z.object({ ok: z.literal(true) }) } } },
    },
  }),
  (c) => c.json({ ok: true as const }, 200),
);

app.route("/field-definitions", fieldDefinitionRoutes);
app.route("/suppliers", supplierRoutes);

app.openAPIRegistry.registerComponent("securitySchemes", "bearer", {
  type: "http",
  scheme: "bearer",
  bearerFormat: "JWT",
  description: "Supabase Auth access token",
});

// The contract for every client: the web app, and a generated Python client for the desktop app.
app.doc31("/openapi.json", {
  openapi: "3.1.0",
  info: { title: "Atlas PO ERP API", version: "0.1.0" },
});
