import { type CustomFieldDefinition, type Supplier, SupplierInput } from "@atlas/contracts";
import { formatPeso, validateCustomValues } from "@atlas/domain";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { type CustomDraft, CustomFieldInputs, draftToValues } from "../components/CustomFieldInputs.tsx";
import { ApiRequestError, api } from "../lib/api.ts";

const empty = { name: "", tin: "", address: "", terms: "" };

function showValue(def: CustomFieldDefinition, value: unknown): string {
  if (value === undefined || value === null) return "";
  if (def.type === "money" && typeof value === "number") return formatPeso(value);
  if (def.type === "boolean") return value ? "Yes" : "No";
  return String(value);
}

export function SuppliersPage() {
  const queryClient = useQueryClient();
  const suppliers = useQuery({ queryKey: ["suppliers"], queryFn: () => api<Supplier[]>("/suppliers") });
  const fields = useQuery({
    queryKey: ["field-definitions", "supplier"],
    queryFn: () => api<CustomFieldDefinition[]>("/field-definitions?entity=supplier"),
  });
  const definitions = fields.data ?? [];
  const columns = definitions.filter((d) => d.active);

  const [core, setCore] = useState(empty);
  const [draft, setDraft] = useState<CustomDraft>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState("");

  const create = useMutation({
    mutationFn: (body: SupplierInput) => api<Supplier>("/suppliers", { method: "POST", body }),
    onSuccess: async () => {
      setCore(empty);
      setDraft({});
      await queryClient.invalidateQueries({ queryKey: ["suppliers"] });
    },
    onError: (e) => {
      if (e instanceof ApiRequestError) {
        setFormError(e.body.error);
        setErrors(Object.fromEntries((e.body.issues ?? []).map((i) => [i.path, i.message])));
      } else setFormError(e.message);
    },
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    setFormError("");
    // Same rules as the server, from the same packages, so most mistakes show before anything is sent.
    const converted = draftToValues(definitions, draft);
    const checked = validateCustomValues(definitions, converted.values);
    const parsed = SupplierInput.safeParse({
      ...Object.fromEntries(Object.entries(core).filter(([, v]) => v.trim())),
      custom: converted.values,
    });
    const issues = [
      ...converted.issues,
      ...(checked.ok ? [] : checked.issues),
      ...(parsed.success ? [] : parsed.error.issues.map((i) => ({ path: i.path.join("."), message: i.message }))),
    ];
    setErrors(Object.fromEntries(issues.map((i) => [i.path, i.message])));
    if (issues.length || !parsed.success) return;
    create.mutate(parsed.data);
  }

  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_22rem]">
      <section className="overflow-x-auto rounded border border-line bg-surface">
        <table className="w-full text-left">
          <thead className="border-b border-line text-xs text-muted">
            <tr>
              <th className="px-3 py-2 font-medium">Supplier</th>
              <th className="px-3 py-2 font-medium">TIN</th>
              <th className="px-3 py-2 font-medium">Terms</th>
              {columns.map((d) => (
                <th key={d.id} className="px-3 py-2 font-medium">
                  {d.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {suppliers.data?.map((s) => (
              <tr key={s.id} className="border-b border-line last:border-0">
                <td className="px-3 py-2">{s.name}</td>
                <td className="px-3 py-2 tabular-nums">{s.tin}</td>
                <td className="px-3 py-2">{s.terms}</td>
                {columns.map((d) => (
                  <td key={d.id} className="px-3 py-2">
                    {showValue(d, s.custom[d.key])}
                  </td>
                ))}
              </tr>
            ))}
            {suppliers.data?.length === 0 && (
              <tr>
                <td className="px-3 py-6 text-muted" colSpan={3 + columns.length}>
                  No suppliers yet. Add the first one on the right.
                </td>
              </tr>
            )}
          </tbody>
        </table>
        {suppliers.error && <p className="p-3 text-danger">{suppliers.error.message}</p>}
      </section>

      <form onSubmit={submit} className="space-y-3 rounded border border-line bg-surface p-4">
        <h2 className="font-semibold">New supplier</h2>
        {(["name", "tin", "address", "terms"] as const).map((key) => (
          <div key={key}>
            <label htmlFor={key} className="label">
              {{ name: "Name *", tin: "TIN", address: "Address", terms: "Terms" }[key]}
            </label>
            <input
              id={key}
              className="field"
              value={core[key]}
              onChange={(e) => setCore({ ...core, [key]: e.target.value })}
            />
            {errors[key] && <p className="mt-1 text-xs text-danger">{errors[key]}</p>}
          </div>
        ))}
        <CustomFieldInputs definitions={definitions} draft={draft} onChange={setDraft} errors={errors} />
        {formError && <p className="text-sm text-danger">{formError}</p>}
        <button type="submit" className="btn" disabled={create.isPending}>
          {create.isPending ? "Saving…" : "Add supplier"}
        </button>
      </form>
    </div>
  );
}
