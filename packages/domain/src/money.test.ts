import { describe, expect, it } from "vitest";
import { formatPeso, formatPlain, parsePesos } from "./money.ts";

describe("parsePesos (same results as atlaspo/money.py parse_pesos)", () => {
  it.each([
    ["1,234.50", 123450],
    ["₱1,234.50", 123450],
    ["PHP 1234.5", 123450],
    ["0.005", 1],
    ["0.004", 0],
    ["-0.005", -1],
    ["12", 1200],
    [".5", 50],
    ["", 0],
    ["   ", 0],
    [null, 0],
    [19.99, 1999],
  ] as const)("%s -> %i", (input, expected) => {
    expect(parsePesos(input)).toBe(expected);
  });

  it.each(["abc", "1.2.3", ".", "1,00x"])("rejects %s", (input) => {
    expect(() => parsePesos(input)).toThrow(/not a valid amount/);
  });
});

describe("formatting", () => {
  it("formats centavos", () => {
    expect(formatPlain(123450)).toBe("1,234.50");
    expect(formatPlain(5)).toBe("0.05");
    expect(formatPeso(123450)).toBe("₱1,234.50");
    expect(formatPeso(-500)).toBe("-₱5.00");
  });

  it("refuses fractional centavos", () => {
    expect(() => formatPlain(1.5)).toThrow();
  });
});
