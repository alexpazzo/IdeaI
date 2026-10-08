"use client";

import { useProxyMutation } from "@/components/use-proxy";
import { Button } from "@/components/ui/button";

export function ReanalyzeButton({ ideaId }: { ideaId: number }) {
  const mutation = useProxyMutation(`ideas/${ideaId}/reanalyze`, "POST");
  return (
    <div className="flex items-center gap-3">
      <Button variant="secondary" onClick={() => mutation.mutate(undefined)} disabled={mutation.isPending}>
        {mutation.isPending ? "Accodamento…" : "Ri-analizza"}
      </Button>
      {mutation.isSuccess ? <span className="text-muted-foreground text-sm">Job accodato.</span> : null}
      {mutation.isError ? <span className="text-destructive text-sm">{mutation.error.message}</span> : null}
    </div>
  );
}
