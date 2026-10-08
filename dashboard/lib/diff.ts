// Diff strutturale del payload di analisi fra due revisioni.
// Il payload viene appiattito in percorsi puntati (`feasibility.score`,
// `risks.0.risk`, …) e confrontato valore per valore.

export type DiffKind = "added" | "removed" | "changed";

export interface DiffEntry {
  path: string;
  kind: DiffKind;
  before?: unknown;
  after?: unknown;
}

export function flatten(value: unknown, prefix = ""): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (Array.isArray(value)) {
    value.forEach((v, i) => {
      Object.assign(out, flatten(v, `${prefix}.${i}`));
    });
  } else if (value !== null && typeof value === "object") {
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      Object.assign(out, flatten(v, prefix ? `${prefix}.${k}` : k));
    }
  } else {
    const key = prefix === "" ? "value" : prefix.replace(/^\./, "");
    out[key] = value;
  }
  return out;
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === "object";
}

function equal(a: unknown, b: unknown): boolean {
  if (isPlainObject(a) || isPlainObject(b)) {
    return JSON.stringify(flatten(a)) === JSON.stringify(flatten(b));
  }
  return a === b;
}

/** Diff a livello di nodo: foglie e rami interi aggiunti/rimossi/modificati. */
export function diffPayload(before: unknown, after: unknown, prefix = ""): DiffEntry[] {
  const entries: DiffEntry[] = [];
  const bObj = isPlainObject(before) ? before : undefined;
  const aObj = isPlainObject(after) ? after : undefined;

  if (bObj && aObj) {
    const keys = new Set([...Object.keys(bObj), ...Object.keys(aObj)]);
    for (const key of keys) {
      const path = prefix ? `${prefix}.${key}` : key;
      const hasB = key in bObj;
      const hasA = key in aObj;
      if (hasB && !hasA) entries.push({ path, kind: "removed", before: bObj[key] });
      else if (!hasB && hasA) entries.push({ path, kind: "added", after: aObj[key] });
      else if (!equal(bObj[key], aObj[key])) {
        const nested = diffPayload(bObj[key], aObj[key], path);
        if (nested.length > 0) entries.push(...nested);
        else entries.push({ path, kind: "changed", before: bObj[key], after: aObj[key] });
      }
    }
    return entries;
  }

  if (!equal(before, after)) {
    if (before === undefined) entries.push({ path: prefix || "value", kind: "added", after });
    else if (after === undefined) entries.push({ path: prefix || "value", kind: "removed", before });
    else entries.push({ path: prefix || "value", kind: "changed", before, after });
  }
  return entries;
}

export function stringifyDiffValue(value: unknown): string {
  if (value === undefined) return "—";
  if (value === null) return "null";
  if (typeof value === "string") return value;
  if (Array.isArray(value) || isPlainObject(value)) {
    const flat = flatten(value);
    const values = Object.values(flat);
    if (values.length > 0 && values.every((v) => typeof v !== "object")) {
      return values.map((v) => String(v)).join(", ");
    }
    return JSON.stringify(value);
  }
  return String(value);
}
