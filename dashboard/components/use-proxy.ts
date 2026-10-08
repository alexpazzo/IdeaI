"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";

type Method = "POST" | "PATCH" | "DELETE";

async function proxyRequest(path: string, method: Method, body?: unknown): Promise<unknown> {
  const res = await fetch(`/api/proxy/${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `Richiesta fallita (${res.status})`;
    try {
      const data = (await res.json()) as { detail?: string };
      if (data.detail) detail = data.detail;
    } catch {
      // corpo non JSON: si mantiene il messaggio generico
    }
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

/** Mutazione verso l'API che passa dal proxy server-side (la chiave non esce mai dal server). */
export function useProxyMutation(path: string, method: Method) {
  const router = useRouter();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body?: unknown) => proxyRequest(path, method, body),
    onSuccess: () => {
      queryClient.invalidateQueries();
      router.refresh();
    },
  });
}
