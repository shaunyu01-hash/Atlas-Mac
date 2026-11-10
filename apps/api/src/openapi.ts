import { ApiError } from "@atlas/contracts";
import { OpenAPIHono } from "@hono/zod-openapi";
import type { Env } from "hono";

/** An OpenAPIHono whose request-validation failures come back in the same ApiError shape as every other error. */
export function router<E extends Env>() {
  return new OpenAPIHono<E>({
    defaultHook: (result, c) => {
      if (!result.success) {
        const body: ApiError = {
          error: "Some details need fixing",
          issues: result.error.issues.map((i) => ({ path: i.path.join("."), message: i.message })),
        };
        return c.json(body, 400);
      }
    },
  });
}

export const errorResponses = {
  400: { description: "Invalid input", content: { "application/json": { schema: ApiError } } },
  401: { description: "Not signed in", content: { "application/json": { schema: ApiError } } },
  403: { description: "Not allowed", content: { "application/json": { schema: ApiError } } },
};

export const bearer = [{ bearer: [] }];
