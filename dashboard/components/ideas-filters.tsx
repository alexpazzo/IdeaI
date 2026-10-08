"use client";

import * as React from "react";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";

export interface IdeasFilterValues {
  q: string;
  min_score: string;
  verdict: string;
  status: string;
  category: string;
  tag: string;
  source_kind: string;
  sort: string;
}

const EMPTY: IdeasFilterValues = {
  q: "",
  min_score: "",
  verdict: "",
  status: "",
  category: "",
  tag: "",
  source_kind: "",
  sort: "opportunity_score",
};

export function IdeasFilters({ initial }: { initial: IdeasFilterValues }) {
  const router = useRouter();
  const [values, setValues] = React.useState<IdeasFilterValues>({ ...EMPTY, ...initial });

  const set = (key: keyof IdeasFilterValues) => (event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setValues((prev) => ({ ...prev, [key]: event.target.value }));

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(values)) {
      if (value && !(key === "sort" && value === "opportunity_score")) params.set(key, value);
    }
    router.push(params.size > 0 ? `/ideas?${params.toString()}` : "/ideas");
  };

  const reset = () => {
    setValues({ ...EMPTY });
    router.push("/ideas");
  };

  return (
    <form onSubmit={submit} className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex min-w-64 flex-1 flex-col gap-1">
          <span className="text-muted-foreground text-xs font-medium">Ricerca semantica (ibrida RRF)</span>
          <Input
            name="q"
            value={values.q}
            onChange={set("q")}
            placeholder="es. fatturazione elettronica artigiani"
          />
        </label>
        <label className="flex w-28 flex-col gap-1">
          <span className="text-muted-foreground text-xs font-medium">Score min</span>
          <Input name="min_score" type="number" min={0} max={100} value={values.min_score} onChange={set("min_score")} />
        </label>
        <label className="flex w-36 flex-col gap-1">
          <span className="text-muted-foreground text-xs font-medium">Verdict</span>
          <Select name="verdict" value={values.verdict} onChange={set("verdict")}>
            <option value="">tutti</option>
            <option value="strong">forte</option>
            <option value="promising">promettente</option>
            <option value="weak">debole</option>
            <option value="reject">scartata</option>
          </Select>
        </label>
        <label className="flex w-36 flex-col gap-1">
          <span className="text-muted-foreground text-xs font-medium">Stato</span>
          <Select name="status" value={values.status} onChange={set("status")}>
            <option value="">tutti</option>
            <option value="new">nuova</option>
            <option value="analyzed">analizzata</option>
            <option value="watching">in watch</option>
            <option value="rejected">rifiutata</option>
            <option value="archived">archiviata</option>
          </Select>
        </label>
        <label className="flex w-44 flex-col gap-1">
          <span className="text-muted-foreground text-xs font-medium">Categoria</span>
          <Select name="category" value={values.category} onChange={set("category")}>
            <option value="">tutte</option>
            <option value="product_idea">idea di prodotto</option>
            <option value="pain_point">pain point</option>
            <option value="market_signal">segnale di mercato</option>
          </Select>
        </label>
        <label className="flex w-36 flex-col gap-1">
          <span className="text-muted-foreground text-xs font-medium">Tag</span>
          <Input name="tag" value={values.tag} onChange={set("tag")} placeholder="es. pmi" />
        </label>
        <label className="flex w-40 flex-col gap-1">
          <span className="text-muted-foreground text-xs font-medium">Sorgente</span>
          <Input name="source_kind" value={values.source_kind} onChange={set("source_kind")} placeholder="es. reddit" />
        </label>
        <label className="flex w-44 flex-col gap-1">
          <span className="text-muted-foreground text-xs font-medium">Ordina per</span>
          <Select name="sort" value={values.sort} onChange={set("sort")}>
            <option value="opportunity_score">punteggio</option>
            <option value="last_activity_at">ultima attività</option>
            <option value="first_seen_at">prima osservazione</option>
          </Select>
        </label>
        <Button type="submit">Filtra</Button>
        <Button type="button" variant="ghost" onClick={reset}>
          Azzera
        </Button>
      </div>
    </form>
  );
}
