import Link from "next/link";
import { notFound } from "next/navigation";

import { CategoryBadge, StatusBadge, VerdictBadge, WatchBadge } from "@/components/badges";
import { ReanalyzeButton } from "@/components/reanalyze-button";
import { RevisionDiff } from "@/components/revision-diff";
import { ScoreRadar } from "@/components/score-radar";
import { WatchControls } from "@/components/watch-controls";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ApiError, apiGet } from "@/lib/api";
import type { AnalysisPayload, IdeaDetailResponse } from "@/lib/types";
import { formatDate, formatRelative } from "@/lib/utils";

export const revalidate = 0;
export const dynamic = "force-dynamic";

function ScoreList({ title, score, children }: { title: string; score: number; children: React.ReactNode }) {
  return (
    <div>
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">{title}</h3>
        <Badge variant="outline">{score}/5</Badge>
      </div>
      <div className="text-muted-foreground mt-1 space-y-1 text-sm">{children}</div>
    </div>
  );
}

function AnalysisBody({ payload }: { payload: AnalysisPayload }) {
  return (
    <div className="space-y-5">
      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <h3 className="text-sm font-semibold">Problema</h3>
          <p className="text-muted-foreground mt-1 text-sm">{payload.problem}</p>
        </div>
        <div>
          <h3 className="text-sm font-semibold">Cliente target</h3>
          <p className="text-muted-foreground mt-1 text-sm">{payload.target_customer}</p>
        </div>
        <div>
          <h3 className="text-sm font-semibold">Soluzione proposta</h3>
          <p className="text-muted-foreground mt-1 text-sm">{payload.proposed_solution}</p>
        </div>
        <div>
          <h3 className="text-sm font-semibold">Alternative esistenti</h3>
          <ul className="text-muted-foreground mt-1 list-disc space-y-0.5 pl-4 text-sm">
            {payload.current_alternatives.map((alt, i) => (
              <li key={i}>{alt}</li>
            ))}
          </ul>
        </div>
        <div>
          <h3 className="text-sm font-semibold">Scope dell&apos;MVP</h3>
          <ul className="text-muted-foreground mt-1 list-disc space-y-0.5 pl-4 text-sm">
            {payload.mvp_scope.map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        </div>
        <div>
          <h3 className="text-sm font-semibold">Differenziatori</h3>
          <ul className="text-muted-foreground mt-1 list-disc space-y-0.5 pl-4 text-sm">
            {payload.differentiators.map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        <ScoreList title="Fattibilità" score={payload.feasibility.score}>
          <p>{payload.feasibility.rationale}</p>
          {payload.feasibility.hard_blockers.length > 0 ? (
            <p><strong>Blocchi:</strong> {payload.feasibility.hard_blockers.join("; ")}</p>
          ) : null}
          {payload.feasibility.tech_stack_hint.length > 0 ? (
            <p><strong>Stack:</strong> {payload.feasibility.tech_stack_hint.join(", ")}</p>
          ) : null}
        </ScoreList>
        <ScoreList title="Economia" score={payload.economics.score}>
          <p>{payload.economics.rationale}</p>
          <p><strong>Segnale TAM:</strong> {payload.economics.tam_signal}</p>
        </ScoreList>
        <ScoreList title="Competizione" score={payload.competition.score}>
          <p>{payload.competition.rationale}</p>
          {payload.competition.named_players.length > 0 ? (
            <p><strong>Player:</strong> {payload.competition.named_players.join(", ")}</p>
          ) : null}
        </ScoreList>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <h3 className="text-sm font-semibold">Monetizzazione</h3>
          <p className="text-muted-foreground mt-1 text-sm">
            <Badge variant="secondary" className="mr-2">{payload.monetization.model}</Badge>
            {payload.monetization.price_hypothesis}
          </p>
          <p className="text-muted-foreground mt-1 text-sm">{payload.monetization.unit_economics_note}</p>
        </div>
        <div>
          <h3 className="text-sm font-semibold">Effort</h3>
          <p className="text-muted-foreground mt-1 text-sm">
            {payload.effort.weeks_to_mvp} settimane, team di {payload.effort.team_size} (confidenza{" "}
            {Math.round(payload.effort.confidence * 100)}%)
          </p>
        </div>
      </div>

      {payload.risks.length > 0 ? (
        <div>
          <h3 className="text-sm font-semibold">Rischi</h3>
          <ul className="mt-1 space-y-1 text-sm">
            {payload.risks.map((risk, i) => (
              <li key={i} className="text-muted-foreground">
                <Badge variant="outline" className="mr-2">sev {risk.severity}</Badge>
                <strong>{risk.risk}</strong> — {risk.mitigation}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {payload.notes ? (
        <div>
          <h3 className="text-sm font-semibold">Note</h3>
          <p className="text-muted-foreground mt-1 text-sm">{payload.notes}</p>
        </div>
      ) : null}
    </div>
  );
}

export default async function IdeaDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const ideaId = Number(id);
  if (!Number.isInteger(ideaId)) notFound();

  let data: IdeaDetailResponse;
  try {
    data = await apiGet<IdeaDetailResponse>(`/ideas/${ideaId}`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  }

  const { idea, current_analysis, revisions, items, timeline } = data;
  const urlByExternalId = new Map(items.map((item) => [item.external_id, item.url]));

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="space-y-2">
          <Link href="/ideas" className="text-muted-foreground text-xs hover:underline">
            ← Torna al catalogo
          </Link>
          <h1 className="text-2xl font-semibold tracking-tight">{idea.title}</h1>
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="secondary" className="font-mono">score {idea.opportunity_score ?? "—"}</Badge>
            <VerdictBadge verdict={idea.verdict} />
            <StatusBadge status={idea.status} />
            <CategoryBadge category={idea.category} />
            <WatchBadge enabled={idea.watch.enabled} mode={idea.watch.mode} />
            {idea.tags.map((tag) => (
              <Badge key={tag} variant="outline" className="font-normal">{tag}</Badge>
            ))}
          </div>
          <p className="text-muted-foreground max-w-3xl text-sm">{idea.canonical_summary}</p>
          <p className="text-muted-foreground text-xs">
            Prima osservazione {formatDate(idea.first_seen_at)} · ultima attività {formatRelative(idea.last_activity_at)} ·{" "}
            {idea.item_count} item
          </p>
        </div>
        <div className="space-y-3">
          <ReanalyzeButton ideaId={idea.id} />
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Watch</CardTitle>
          <CardDescription>Il watch ri-controlla il thread di origine e genera nuove revisioni.</CardDescription>
        </CardHeader>
        <CardContent>
          <WatchControls
            ideaId={idea.id}
            enabled={idea.watch.enabled}
            mode={idea.watch.mode}
            intervalS={idea.watch.interval_s}
          />
        </CardContent>
      </Card>

      {current_analysis ? (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Analisi corrente (rev {current_analysis.revision})</CardTitle>
              <CardDescription>
                {current_analysis.model_spec} · {formatDate(current_analysis.created_at)} · costo{" "}
                {current_analysis.cost_usd.toFixed(4)} USD
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="grid gap-6 lg:grid-cols-[280px_1fr]">
                <div>
                  <ScoreRadar
                    feasibility={current_analysis.feasibility_score}
                    economics={current_analysis.economics_score}
                    competition={current_analysis.competition_score}
                  />
                </div>
                <AnalysisBody payload={current_analysis.payload} />
              </div>
            </CardContent>
          </Card>

          {current_analysis.payload.evidence_quotes.length > 0 ? (
            <Card>
              <CardHeader>
                <CardTitle>Evidenze</CardTitle>
                <CardDescription>Citazioni validate, collegate al post originale.</CardDescription>
              </CardHeader>
              <CardContent>
                <ul className="space-y-2">
                  {current_analysis.payload.evidence_quotes.map((quote, i) => {
                    const url = urlByExternalId.get(quote.item_external_id);
                    return (
                      <li key={i} className="rounded-md border px-3 py-2 text-sm">
                        <p className="italic">«{quote.quote}»</p>
                        <p className="text-muted-foreground mt-1 text-xs">
                          <code className="font-mono">{quote.item_external_id}</code>
                          {url ? (
                            <>
                              {" · "}
                              <a href={url} target="_blank" rel="noreferrer" className="underline">
                                post originale
                              </a>
                            </>
                          ) : null}
                        </p>
                      </li>
                    );
                  })}
                </ul>
              </CardContent>
            </Card>
          ) : null}
        </>
      ) : (
        <Card>
          <CardContent className="text-muted-foreground py-6 text-sm">
            Nessuna analisi disponibile: l&apos;idea è in attesa di elaborazione.
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Diff fra revisioni</CardTitle>
          <CardDescription>Confronto dei campi dell&apos;analisi fra due revisioni.</CardDescription>
        </CardHeader>
        <CardContent>
          <RevisionDiff revisions={revisions} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Evidenze collegate ({items.length})</CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Tipo</TableHead>
                <TableHead>Titolo / estratto</TableHead>
                <TableHead className="text-right">Score</TableHead>
                <TableHead className="text-right">Commenti</TableHead>
                <TableHead>Ruolo</TableHead>
                <TableHead>Stato</TableHead>
                <TableHead>Link</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((item) => (
                <TableRow key={item.id}>
                  <TableCell className="text-xs">{item.kind}</TableCell>
                  <TableCell className="max-w-md">
                    {item.title ? <span className="font-medium">{item.title}</span> : null}
                    <p className="text-muted-foreground line-clamp-2 text-xs">{item.body}</p>
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs">{item.score ?? "—"}</TableCell>
                  <TableCell className="text-right font-mono text-xs">{item.num_comments ?? "—"}</TableCell>
                  <TableCell className="text-xs">{item.role}</TableCell>
                  <TableCell className="text-xs">{item.state}</TableCell>
                  <TableCell>
                    {item.url ? (
                      <a href={item.url} target="_blank" rel="noreferrer" className="text-xs underline">
                        apri
                      </a>
                    ) : (
                      "—"
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Timeline</CardTitle>
          <CardDescription>Aggiornamenti registrati in idea_updates.</CardDescription>
        </CardHeader>
        <CardContent>
          {timeline.length === 0 ? (
            <p className="text-muted-foreground text-sm">Nessun aggiornamento registrato.</p>
          ) : (
            <ol className="space-y-3">
              {timeline.map((update) => (
                <li key={update.id} className="border-l-2 pl-3">
                  <div className="flex items-center gap-2">
                    <Badge variant="outline" className="font-normal">{update.kind}</Badge>
                    <span className="text-muted-foreground text-xs">{formatDate(update.created_at)}</span>
                    {update.item_id ? (
                      <span className="text-muted-foreground text-xs">item #{update.item_id}</span>
                    ) : null}
                  </div>
                  <p className="mt-1 text-sm">{update.summary}</p>
                </li>
              ))}
            </ol>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
