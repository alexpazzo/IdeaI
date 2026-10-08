"use client";

import * as React from "react";

import { Select } from "@/components/ui/select";
import { diffPayload, stringifyDiffValue, type DiffEntry } from "@/lib/diff";
import type { AnalysisRevision } from "@/lib/types";
import { cn } from "@/lib/utils";

const KIND_STYLE: Record<DiffEntry["kind"], string> = {
  added: "text-emerald-600",
  removed: "text-destructive",
  changed: "text-amber-600",
};

const KIND_LABEL: Record<DiffEntry["kind"], string> = {
  added: "aggiunto",
  removed: "rimosso",
  changed: "modificato",
};

export function RevisionDiff({ revisions }: { revisions: AnalysisRevision[] }) {
  const ordered = [...revisions].sort((a, b) => a.revision - b.revision);
  const [beforeRev, setBeforeRev] = React.useState(() => String(ordered[ordered.length - 2]?.revision ?? ""));
  const [afterRev, setAfterRev] = React.useState(() => String(ordered[ordered.length - 1]?.revision ?? ""));

  if (ordered.length < 2) {
    return (
      <p className="text-muted-foreground text-sm">
        Serve almeno una seconda revisione per mostrare il confronto.
      </p>
    );
  }

  const before = ordered.find((r) => String(r.revision) === beforeRev);
  const after = ordered.find((r) => String(r.revision) === afterRev);
  const entries = before && after ? diffPayload(before.payload, after.payload) : [];

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <span className="text-muted-foreground">Da</span>
        <Select value={beforeRev} onChange={(e) => setBeforeRev(e.target.value)} className="w-28">
          {ordered.map((r) => (
            <option key={r.revision} value={r.revision}>
              rev {r.revision}
            </option>
          ))}
        </Select>
        <span className="text-muted-foreground">a</span>
        <Select value={afterRev} onChange={(e) => setAfterRev(e.target.value)} className="w-28">
          {ordered.map((r) => (
            <option key={r.revision} value={r.revision}>
              rev {r.revision}
            </option>
          ))}
        </Select>
      </div>

      {entries.length === 0 ? (
        <p className="text-muted-foreground text-sm">Nessuna differenza fra le due revisioni selezionate.</p>
      ) : (
        <ul className="space-y-1.5 text-sm">
          {entries.map((entry) => (
            <li key={`${entry.kind}-${entry.path}`} className="rounded-md border px-3 py-2">
              <div className="flex items-center gap-2">
                <span className={cn("text-xs font-semibold uppercase", KIND_STYLE[entry.kind])}>
                  {KIND_LABEL[entry.kind]}
                </span>
                <code className="font-mono text-xs">{entry.path}</code>
              </div>
              <div className="text-muted-foreground mt-1 text-xs">
                {entry.kind === "added" ? (
                  <span>{stringifyDiffValue(entry.after)}</span>
                ) : entry.kind === "removed" ? (
                  <span>{stringifyDiffValue(entry.before)}</span>
                ) : (
                  <span>
                    <span className="line-through">{stringifyDiffValue(entry.before)}</span>
                    {" → "}
                    <span className="text-foreground">{stringifyDiffValue(entry.after)}</span>
                  </span>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
