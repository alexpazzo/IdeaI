"use client";

import { useProxyMutation } from "@/components/use-proxy";
import { Switch } from "@/components/ui/switch";

export function TargetToggle({ targetId, enabled }: { targetId: number; enabled: boolean }) {
  const mutation = useProxyMutation(`targets/${targetId}`, "PATCH");
  return (
    <Switch
      checked={enabled}
      disabled={mutation.isPending}
      onCheckedChange={(checked) => mutation.mutate({ enabled: checked })}
      aria-label="Abilita o disabilita il target"
    />
  );
}
