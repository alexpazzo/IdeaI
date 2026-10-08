import { IdeasFilters, type IdeasFilterValues } from "@/components/ideas-filters";
import { IdeasTable } from "@/components/ideas-table";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { apiGetSafe } from "@/lib/api";
import type { IdeaListResponse } from "@/lib/types";

export const revalidate = 0;

const EMPTY: IdeaListResponse = { total: 0, items: [] };

function first(value: string | string[] | undefined): string {
  if (Array.isArray(value)) return value[0] ?? "";
  return value ?? "";
}

export default async function IdeasPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const sp = await searchParams;
  const filters: IdeasFilterValues = {
    q: first(sp.q),
    min_score: first(sp.min_score),
    verdict: first(sp.verdict),
    status: first(sp.status),
    category: first(sp.category),
    tag: first(sp.tag),
    source_kind: first(sp.source_kind),
    sort: first(sp.sort) || "opportunity_score",
  };

  const data = filters.q
    ? await apiGetSafe<IdeaListResponse>("/ideas/search", EMPTY, { q: filters.q, limit: 100 })
    : await apiGetSafe<IdeaListResponse>("/ideas", EMPTY, {
        min_score: filters.min_score,
        verdict: filters.verdict,
        status: filters.status,
        category: filters.category,
        tag: filters.tag,
        source_kind: filters.source_kind,
        sort: filters.sort,
        limit: 100,
      });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Idee</h1>
        <p className="text-muted-foreground text-sm">
          Catalogo filtrabile e ordinabile; la ricerca semantica usa l&apos;endpoint ibrido <code>/ideas/search</code>.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Filtri</CardTitle>
          <CardDescription>
            {filters.q ? `Ricerca per «${filters.q}»` : "Filtri applicati alla vista v_idea_ranking."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <IdeasFilters initial={filters} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{data.total} risultato(i)</CardTitle>
        </CardHeader>
        <CardContent>
          <IdeasTable items={data.items} />
        </CardContent>
      </Card>
    </div>
  );
}
