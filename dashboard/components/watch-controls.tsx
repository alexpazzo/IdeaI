"use client";

import * as React from "react";

import { useProxyMutation } from "@/components/use-proxy";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";

export function WatchControls({
  ideaId,
  enabled,
  mode,
  intervalS,
}: {
  ideaId: number;
  enabled: boolean;
  mode: string;
  intervalS: number;
}) {
  const [active, setActive] = React.useState(enabled);
  const [watchMode, setWatchMode] = React.useState(mode === "comments_only" ? "comments_only" : "thread_full");
  const [interval, setIntervalS] = React.useState(String(intervalS));
  const mutation = useProxyMutation(`ideas/${ideaId}/watch`, "POST");

  const save = () => {
    mutation.mutate({ enabled: active, mode: watchMode, interval_s: Number(interval) || intervalS });
  };

  return (
    <div className="flex flex-wrap items-center gap-3">
      <div className="flex items-center gap-2">
        <Switch checked={active} onCheckedChange={setActive} aria-label="Watch attivo" />
        <span className="text-sm">{active ? "watch attivo" : "watch spento"}</span>
      </div>
      <Select
        value={watchMode}
        onChange={(e) => setWatchMode(e.target.value as typeof watchMode)}
        className="w-40"
        disabled={!active}
      >
        <option value="thread_full">intero thread</option>
        <option value="comments_only">solo commenti</option>
      </Select>
      <Input
        type="number"
        min={60}
        value={interval}
        onChange={(e) => setIntervalS(e.target.value)}
        className="w-28"
        disabled={!active}
        aria-label="Intervallo di controllo (secondi)"
      />
      <Button onClick={save} disabled={mutation.isPending}>
        {mutation.isPending ? "Salvataggio…" : "Salva watch"}
      </Button>
      {mutation.isError ? <span className="text-destructive text-sm">{mutation.error.message}</span> : null}
    </div>
  );
}
