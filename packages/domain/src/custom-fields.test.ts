import { describe, expect, it } from "vitest";
import { validateCustomValues } from "./custom-fields.ts";

const defs = [
  {
    key: "region",
    label: "Region",
    type: "select",
    required: true,
    options: ["NCR", "Luzon", "Visayas"],
    active: true,
  },
  { key: "credit_limit", label: "Credit limit", type: "money", required: false, options: null, active: true },
  { key: "accredited_on", label: "Accredited on", type: "date", required: false, options: null, active: true },
  { key: "old_code", label: "Old code", type: "text", required: true, options: null, active: false },
] as const;

describe("validateCustomValues", () => {
  it("accepts valid values and keeps retired fields as they are", () => {
    const r = validateCustomValues(defs as never, { region: "NCR", credit_limit: 500000, old_code: 42 });
    expect(r).toEqual({ ok: true, values: { region: "NCR", credit_limit: 500000, old_code: 42 } });
  });

  it("requires required active fields but not retired ones", () => {
    const r = validateCustomValues(defs as never, {});
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.issues.map((i) => i.path)).toEqual(["custom.region"]);
  });

  it("rejects wrong types, unknown options, fractional centavos and unknown keys", () => {
    const r = validateCustomValues(defs as never, {
      region: "Mindanao",
      credit_limit: 10.5,
      accredited_on: "next week",
      colour: "red",
    });
    expect(r.ok).toBe(false);
    if (!r.ok) {
      const paths = r.issues.map((i) => i.path);
      expect(paths).toEqual(
        expect.arrayContaining(["custom.region", "custom.credit_limit", "custom.accredited_on", "custom"]),
      );
    }
  });
});
