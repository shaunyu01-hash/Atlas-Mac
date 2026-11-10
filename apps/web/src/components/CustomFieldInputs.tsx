import type { CustomFieldDefinition } from "@atlas/contracts";
import { parsePesos } from "@atlas/domain";

/** What the inputs hold while typing: text for everything except checkboxes. */
export type CustomDraft = Record<string, string | boolean>;

/** Turns typed text into stored values (pesos -> centavos, text -> number). Blank optional fields are left out. */
export function draftToValues(definitions: CustomFieldDefinition[], draft: CustomDraft) {
  const values: Record<string, unknown> = {};
  const issues: { path: string; message: string }[] = [];
  for (const d of definitions.filter((d) => d.active)) {
    const raw = draft[d.key];
    if (d.type === "boolean") {
      values[d.key] = raw === true;
      continue;
    }
    const text = typeof raw === "string" ? raw.trim() : "";
    if (!text) continue;
    try {
      if (d.type === "money") values[d.key] = parsePesos(text);
      else if (d.type === "number") {
        const n = Number(text.replaceAll(",", ""));
        if (!Number.isFinite(n)) throw new Error(`'${text}' is not a number`);
        values[d.key] = n;
      } else values[d.key] = text;
    } catch (e) {
      issues.push({ path: `custom.${d.key}`, message: (e as Error).message });
    }
  }
  return { values, issues };
}

/** Renders inputs for whatever custom fields are defined. The same renderer will draw AI-defined forms later. */
export function CustomFieldInputs(props: {
  definitions: CustomFieldDefinition[];
  draft: CustomDraft;
  onChange: (draft: CustomDraft) => void;
  errors: Record<string, string>;
}) {
  const set = (key: string, value: string | boolean) => props.onChange({ ...props.draft, [key]: value });

  return (
    <>
      {props.definitions
        .filter((d) => d.active)
        .map((d) => {
          const id = `custom-${d.key}`;
          const error = props.errors[`custom.${d.key}`];
          const text = typeof props.draft[d.key] === "string" ? (props.draft[d.key] as string) : "";
          return (
            <div key={d.id}>
              {d.type === "boolean" ? (
                <label className="flex items-center gap-2 pt-5">
                  <input
                    id={id}
                    type="checkbox"
                    checked={props.draft[d.key] === true}
                    onChange={(e) => set(d.key, e.target.checked)}
                  />
                  {d.label}
                </label>
              ) : (
                <>
                  <label htmlFor={id} className="label">
                    {d.label}
                    {d.required && " *"}
                  </label>
                  {d.type === "select" ? (
                    <select id={id} className="field" value={text} onChange={(e) => set(d.key, e.target.value)}>
                      <option value="">—</option>
                      {d.options?.map((o) => (
                        <option key={o}>{o}</option>
                      ))}
                    </select>
                  ) : d.type === "long_text" ? (
                    <textarea
                      id={id}
                      className="field"
                      rows={3}
                      value={text}
                      onChange={(e) => set(d.key, e.target.value)}
                    />
                  ) : (
                    <input
                      id={id}
                      className="field"
                      type={d.type === "date" ? "date" : "text"}
                      inputMode={d.type === "money" || d.type === "number" ? "decimal" : undefined}
                      placeholder={d.type === "money" ? "0.00" : undefined}
                      value={text}
                      onChange={(e) => set(d.key, e.target.value)}
                    />
                  )}
                </>
              )}
              {d.helpText && <p className="mt-1 text-xs text-muted">{d.helpText}</p>}
              {error && <p className="mt-1 text-xs text-danger">{error}</p>}
            </div>
          );
        })}
    </>
  );
}
