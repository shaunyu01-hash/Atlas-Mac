import { describe, expect, it } from "vitest";
import { app } from "./app.ts";

describe("api", () => {
  it("answers the health check without a database", async () => {
    const res = await app.request("/health");
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true });
  });

  it("publishes an OpenAPI document with every route", async () => {
    const res = await app.request("/openapi.json");
    expect(res.status).toBe(200);
    const doc = (await res.json()) as { paths: Record<string, unknown> };
    expect(Object.keys(doc.paths)).toEqual(expect.arrayContaining(["/health", "/field-definitions", "/suppliers"]));
  });

  it("refuses data routes without a sign-in", async () => {
    for (const path of ["/suppliers", "/field-definitions?entity=supplier"]) {
      const res = await app.request(path);
      expect(res.status).toBe(401);
      expect(await res.json()).toEqual({ error: "Sign in first" });
    }
  });

  it("returns errors as JSON", async () => {
    const res = await app.request("/nope");
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ error: "Not found" });
  });
});
