/**
 * Money helpers, ported from apps/desktop/atlaspo/money.py.
 * Amounts are whole centavos everywhere outside these parsing helpers. No floats ever touch an amount.
 */

export const PESO = "₱";

const AMOUNT = /^([+-])?(\d*)(?:\.(\d*))?$/;

/** "1,234.50", "₱1,234.50" or "PHP 1234.5" -> 123450. Blank is 0. Rounds a fraction of a centavo half up, like the desktop app. */
export function parsePesos(text: string | number | null | undefined): number {
  if (text === null || text === undefined) return 0;
  if (typeof text === "number") {
    if (!Number.isFinite(text)) throw new Error(`'${text}' is not a valid amount`);
    text = String(text);
  }
  const s = text.replaceAll(",", "").replaceAll(PESO, "").replaceAll("PHP", "").trim();
  if (!s) return 0;
  const m = AMOUNT.exec(s);
  const whole = m?.[2] ?? "";
  const frac = m?.[3] ?? "";
  if (!m || (whole === "" && frac === "")) throw new Error(`'${text}' is not a valid amount`);

  const padded = `${frac}000`;
  let centavos = BigInt(whole || "0") * 100n + BigInt(padded.slice(0, 2));
  if (Number(padded[2]) >= 5) centavos += 1n; // half up, away from zero (applied before the sign)
  if (m[1] === "-") centavos = -centavos;
  if (centavos > BigInt(Number.MAX_SAFE_INTEGER) || centavos < BigInt(Number.MIN_SAFE_INTEGER)) {
    throw new Error(`'${text}' is too large`);
  }
  return Number(centavos);
}

/** 123450 -> "1,234.50" */
export function formatPlain(centavos: number): string {
  if (!Number.isSafeInteger(centavos)) throw new Error(`${centavos} is not a whole number of centavos`);
  const negative = centavos < 0;
  const abs = Math.abs(centavos);
  const pesos = Math.trunc(abs / 100).toLocaleString("en-US");
  const cents = String(abs % 100).padStart(2, "0");
  return `${negative ? "-" : ""}${pesos}.${cents}`;
}

/** 123450 -> "₱1,234.50", -500 -> "-₱5.00" */
export function formatPeso(centavos: number): string {
  const plain = formatPlain(centavos);
  return plain.startsWith("-") ? `-${PESO}${plain.slice(1)}` : `${PESO}${plain}`;
}
