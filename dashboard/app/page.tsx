import Link from "next/link";

import { IdeasTable } from "@/components/ideas-table";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { apiGetSafe } from "@/lib/api";
import type { IdeaListResponse, StatsResponse } from "@/lib/types";
import { formatCurrency } from "@/lib/utils";

export const revalidate = 0;

const EMPTY_STATS: StatsResponse = {
  ideas: { total: 0, new: 0, analyzed: 0, watching: 0, rejected: 0, archived: 0 },
  analyses: 0,
  watch_active: 0,
  llm_cost_7d: [],
  source_cost_7d: [],
  queue: [],
  failed_llm_calls: [],
};

function Kpi({ label, value, hint }: { label: string; value: string | number; hint?: string }) {
  return (
    <Card>
      <CardHeader>
        <CardDescription>{label}</CardDescription>
        <CardTitle className="text-3xl tabular-nums">{value}</CardTitle>
      </CardHeader>
      {hint ? <CardContent className="text-muted-foreground text-xs">{hint}</CardContent> : null}
    </Card>
  );
}

export default async function HomePage() {
  const [stats, top] = await Promise.all([
    apiGetSafe<StatsResponse>("/stats", EMPTY_STATS),
    apiGetSafe<IdeaListResponse>("/ideas", { total: 0, items: [] }, { sort: "opportunity_score", limit: 20 }),
  ]);

  const llmCost7d = stats.llm_cost_7d.reduce((sum, row) => sum + row.cost_usd, 0);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Panoramica</h1>
        <p className="text-muted-foreground text-sm">Idee catalogate e ordinate per punteggio di opportunità.</p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Kpi label="Idee catalogate" value={stats.ideas.total} hint={`${stats.ideas.new} in attesa di analisi`} />
        <Kpi label="Analizzate" value={stats.ideas.analyzed} hint={`${stats.analyses} revisioni di analisi`} />
        <Kpi label="Watch attivi" value={stats.watch_active} />
        <Kpi label="Costo LLM (7 g)" value={formatCurrency(llmCost7d)} hint="somma dei costi registrati" />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Top 20 per opportunità</CardTitle>
          <CardDescription>
            Vista <Link href="/ideas" className="underline">catalogo completo</Link> con filtri e ricerca semantica.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <IdeasTable items={top.items} />
        </CardContent>
      </Card>
    </div>
  );
}
