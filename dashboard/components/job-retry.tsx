"use client";

import { useProxyMutation } from "@/components/use-proxy";
import { Button } from "@/components/ui/button";

export function JobRetry({ jobId }: { jobId: number }) {
  const mutation = useProxyMutation(`jobs/${jobId}/retry`, "POST");
  return (
    <div className="flex items-center gap-2">
      <Button
        variant="outline"
        size="sm"
        onClick={() => mutation.mutate(undefined)}
        disabled={mutation.isPending}
      >
        {mutation.isPending ? "…" : "Ritenta"}
      </Button>
      {mutation.isError ? <span className="text-destructive text-xs">{mutation.error.message}</span> : null}
    </div>
  );
}
