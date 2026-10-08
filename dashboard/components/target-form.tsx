"use client";

import * as React from "react";

import { useProxyMutation } from "@/components/use-proxy";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";

const TARGET_KINDS = ["subreddit", "thread", "feed", "query"] as const;

export function TargetForm({ sourceId }: { sourceId: number }) {
  const [targetRef, setTargetRef] = React.useState("");
  const [targetKind, setTargetKind] = React.useState<string>("subreddit");
  const [pollInterval, setPollInterval] = React.useState("");
  const mutation = useProxyMutation(`sources/${sourceId}/targets`, "POST");

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const body: Record<string, unknown> = { target_ref: targetRef, target_kind: targetKind };
    if (pollInterval) body.poll_interval_s = Number(pollInterval);
    mutation.mutate(body, { onSuccess: () => setTargetRef("") });
  };

  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-3">
      <label className="flex min-w-56 flex-1 flex-col gap-1">
        <span className="text-muted-foreground text-xs font-medium">Riferimento target</span>
        <Input
          value={targetRef}
          onChange={(e) => setTargetRef(e.target.value)}
          placeholder="es. r/ItaliaPersonalFinance"
          required
        />
      </label>
      <label className="flex w-36 flex-col gap-1">
        <span className="text-muted-foreground text-xs font-medium">Tipo</span>
        <Select value={targetKind} onChange={(e) => setTargetKind(e.target.value)}>
          {TARGET_KINDS.map((kind) => (
            <option key={kind} value={kind}>
              {kind}
            </option>
          ))}
        </Select>
      </label>
      <label className="flex w-36 flex-col gap-1">
        <span className="text-muted-foreground text-xs font-medium">Intervallo (s)</span>
        <Input
          type="number"
          min={30}
          value={pollInterval}
          onChange={(e) => setPollInterval(e.target.value)}
          placeholder="default"
        />
      </label>
      <Button type="submit" disabled={mutation.isPending || targetRef.length === 0}>
        {mutation.isPending ? "Aggiunta…" : "Aggiungi target"}
      </Button>
      {mutation.isError ? <span className="text-destructive text-sm">{mutation.error.message}</span> : null}
      {mutation.isSuccess ? <span className="text-muted-foreground text-sm">Target aggiunto.</span> : null}
    </form>
  );
}
