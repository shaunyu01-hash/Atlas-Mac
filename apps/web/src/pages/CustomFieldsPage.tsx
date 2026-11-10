import {
  type CustomFieldDefinition,
  CustomFieldDefinitionInput,
  CustomFieldType,
  CustomizableEntity,
} from "@atlas/contracts";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { ApiRequestError, api } from "../lib/api.ts";

const typeLabels: Record<CustomFieldType, string> = {
  text: "Text",
  long_text: "Long text",
  number: "Number",
  money: "Amount (₱)",
  date: "Date",
  boolean: "Yes / No",
  select: "Choice list",
};

const blank = { key: "", label: "", type: "text" as CustomFieldType, required: false, helpText: "", options: "" };

/**
 * Admins add fields by hand today. The planned AI feature fills this same form from a prompt,
 * and the admin reviews it before saving, so nothing reaches the database unchecked.
 */
export function CustomFieldsPage() {
  const queryClient = useQueryClient();
  const [entity, setEntity] = useState<CustomizableEntity>("supplier");
  const [form, setForm] = useState(blank);
  const [error, setError] = useState("");

  const fields = useQuery({
    queryKey: ["field-definitions", entity],
    queryFn: () => api<CustomFieldDefinition[]>(`/field-definitions?entity=${entity}`),
  });

  const create = useMutation({
    mutationFn: (body: CustomFieldDefinitionInput) =>
      api<CustomFieldDefinition>("/field-definitions", { method: "POST", body }),
    onSuccess: async () => {
      setForm(blank);
      await queryClient.invalidateQueries({ queryKey: ["field-definitions", entity] });
    },
    onError: (e) => setError(e instanceof ApiRequestError ? e.body.error : e.message),
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    const options = form.options
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
    const parsed = CustomFieldDefinitionInput.safeParse({
      entity,
      key: form.key,
      label: form.label,
      type: form.type,
      required: form.required,
      helpText: form.helpText.trim() || undefined,
      options: form.type === "select" ? options : undefined,
    });
    if (!parsed.success)
      return setError(parsed.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`).join("; "));
    create.mutate(parsed.data);
  }

  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_22rem]">
      <section className="rounded border border-line bg-surface">
        <div className="flex items-center gap-2 border-b border-line p-3">
          <label htmlFor="entity" className="text-xs text-muted">
            Fields for
          </label>
          <select
            id="entity"
            className="field w-auto"
            value={entity}
            onChange={(e) => setEntity(e.target.value as CustomizableEntity)}
          >
            {CustomizableEntity.options.map((o) => (
              <option key={o} value={o}>
                {o[0]?.toUpperCase() + o.slice(1)}s
              </option>
            ))}
          </select>
        </div>
        <ul>
          {fields.data?.map((f) => (
            <li key={f.id} className="flex justify-between border-b border-line px-3 py-2 last:border-0">
              <span>
                {f.label} <span className="text-xs text-muted">({f.key})</span>
              </span>
              <span className="text-xs text-muted">
                {typeLabels[f.type]}
                {f.required && " · required"}
                {!f.active && " · retired"}
              </span>
            </li>
          ))}
          {fields.data?.length === 0 && <li className="px-3 py-6 text-muted">No custom fields yet.</li>}
        </ul>
      </section>

      <form onSubmit={submit} className="space-y-3 rounded border border-line bg-surface p-4">
        <h2 className="font-semibold">New field</h2>
        <div>
          <label htmlFor="f-label" className="label">
            Label *
          </label>
          <input
            id="f-label"
            className="field"
            value={form.label}
            onChange={(e) => {
              // Keep suggesting a key from the label until the admin types their own.
              const label = e.target.value;
              const keyIsOwn = form.key !== "" && form.key !== suggestKey(form.label);
              setForm({ ...form, label, key: keyIsOwn ? form.key : suggestKey(label) });
            }}
          />
        </div>
        <div>
          <label htmlFor="f-key" className="label">
            Key * (can't change later)
          </label>
          <input
            id="f-key"
            className="field font-mono"
            value={form.key}
            onChange={(e) => setForm({ ...form, key: e.target.value })}
          />
        </div>
        <div>
          <label htmlFor="f-type" className="label">
            Type
          </label>
          <select
            id="f-type"
            className="field"
            value={form.type}
            onChange={(e) => setForm({ ...form, type: e.target.value as CustomFieldType })}
          >
            {CustomFieldType.options.map((t) => (
              <option key={t} value={t}>
                {typeLabels[t]}
              </option>
            ))}
          </select>
        </div>
        {form.type === "select" && (
          <div>
            <label htmlFor="f-options" className="label">
              Choices (one per line) *
            </label>
            <textarea
              id="f-options"
              className="field"
              rows={4}
              value={form.options}
              onChange={(e) => setForm({ ...form, options: e.target.value })}
            />
          </div>
        )}
        <div>
          <label htmlFor="f-help" className="label">
            Help text
          </label>
          <input
            id="f-help"
            className="field"
            value={form.helpText}
            onChange={(e) => setForm({ ...form, helpText: e.target.value })}
          />
        </div>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={form.required}
            onChange={(e) => setForm({ ...form, required: e.target.checked })}
          />
          Required
        </label>
        {error && <p className="text-sm text-danger">{error}</p>}
        <button type="submit" className="btn" disabled={create.isPending}>
          {create.isPending ? "Saving…" : "Add field"}
        </button>
      </form>
    </div>
  );
}

function suggestKey(label: string): string {
  return label
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .replace(/^(\d)/, "f_$1");
}
