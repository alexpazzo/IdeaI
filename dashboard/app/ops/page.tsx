import { JobRetry } from "@/components/job-retry";
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
import { apiGetSafe } from "@/lib/api";
import type { JobListResponse, StatsResponse } from "@/lib/types";
import { formatCurrency, formatDate, formatRelative } from "@/lib/utils";

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

const DAY_FORMAT = new Intl.DateTimeFormat("it-IT", { day: "2-digit", month: "2-digit" });

export default async function OpsPage() {
  const [stats, deadJobs] = await Promise.all([
    apiGetSafe<StatsResponse>("/stats", EMPTY_STATS),
    apiGetSafe<JobListResponse>("/jobs", { total: 0, oldest_pending: null, items: [] }, { state: "dead", limit: 100 }),
  ]);

  const llmByRole = new Map<string, { calls: number; tokens_in: number; tokens_out: number; cost: number }>();
  for (const row of stats.llm_cost_7d) {
    const acc = llmByRole.get(row.role) ?? { calls: 0, tokens_in: 0, tokens_out: 0, cost: 0 };
    acc.calls += row.calls;
    acc.tokens_in += row.tokens_in;
    acc.tokens_out += row.tokens_out;
    acc.cost += row.cost_usd;
    llmByRole.set(row.role, acc);
  }

  const sourceCost = new Map<number, { calls: number; cost: number }>();
  for (const row of stats.source_cost_7d) {
    const acc = sourceCost.get(row.source_id) ?? { calls: 0, cost: 0 };
    acc.calls += row.calls;
    acc.cost += row.cost_usd;
    sourceCost.set(row.source_id, acc);
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Operazioni</h1>
        <p className="text-muted-foreground text-sm">Salute della coda, job falliti, costo LLM e delle sorgenti.</p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Coda per topic e stato</CardTitle>
          <CardDescription>Vista v_queue_health, con età del più vecchio job pending.</CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Topic</TableHead>
                <TableHead>Stato</TableHead>
                <TableHead className="text-right">Job</TableHead>
                <TableHead>Più vecchio pending</TableHead>
                <TableHead>Ultimo completato</TableHead>
                <TableHead className="text-right">Dead</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {stats.queue.map((row) => (
                <TableRow key={`${row.topic}-${row.state}`}>
                  <TableCell className="font-mono text-xs">{row.topic}</TableCell>
                  <TableCell>
                    <Badge variant={row.state === "dead" ? "destructive" : "secondary"} className="font-normal">
                      {row.state}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs">{row.jobs}</TableCell>
                  <TableCell className="text-muted-foreground text-xs">
                    {row.oldest_pending ? formatRelative(row.oldest_pending) : "—"}
                  </TableCell>
                  <TableCell className="text-muted-foreground text-xs">{formatDate(row.last_done)}</TableCell>
                  <TableCell className="text-right font-mono text-xs">{row.dead}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Job in stato dead ({deadJobs.total})</CardTitle>
          <CardDescription>Un retry riporta il job in pending con attempts azzerato.</CardDescription>
        </CardHeader>
        <CardContent>
          {deadJobs.items.length === 0 ? (
            <p className="text-muted-foreground text-sm">Nessun job fallito.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>ID</TableHead>
                  <TableHead>Topic</TableHead>
                  <TableHead className="text-right">Tentativi</TableHead>
                  <TableHead>Ultimo errore</TableHead>
                  <TableHead>Terminato</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {deadJobs.items.map((job) => (
                  <TableRow key={job.id}>
                    <TableCell className="font-mono text-xs">#{job.id}</TableCell>
                    <TableCell className="font-mono text-xs">{job.topic}</TableCell>
                    <TableCell className="text-right font-mono text-xs">
                      {job.attempts}/{job.max_attempts}
                    </TableCell>
                    <TableCell className="text-muted-foreground max-w-80 truncate text-xs">
                      {job.last_error ?? "—"}
                    </TableCell>
                    <TableCell className="text-muted-foreground text-xs">{formatDate(job.finished_at)}</TableCell>
                    <TableCell>
                      <JobRetry jobId={job.id} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Costo LLM ultimi 7 giorni per ruolo</CardTitle>
          </CardHeader>
          <CardContent>
            {llmByRole.size === 0 ? (
              <p className="text-muted-foreground text-sm">Nessuna chiamata registrata.</p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Ruolo</TableHead>
                    <TableHead className="text-right">Chiamate</TableHead>
                    <TableHead className="text-right">Token in</TableHead>
                    <TableHead className="text-right">Token out</TableHead>
                    <TableHead className="text-right">Costo</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {[...llmByRole.entries()].map(([role, acc]) => (
                    <TableRow key={role}>
                      <TableCell>{role}</TableCell>
                      <TableCell className="text-right font-mono text-xs">{acc.calls}</TableCell>
                      <TableCell className="text-right font-mono text-xs">{acc.tokens_in}</TableCell>
                      <TableCell className="text-right font-mono text-xs">{acc.tokens_out}</TableCell>
                      <TableCell className="text-right font-mono text-xs">{formatCurrency(acc.cost)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Costo sorgenti ultimi 7 giorni</CardTitle>
            <CardDescription>Da source_calls_daily.</CardDescription>
          </CardHeader>
          <CardContent>
            {sourceCost.size === 0 ? (
              <p className="text-muted-foreground text-sm">Nessuna chiamata registrata.</p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Sorgente</TableHead>
                    <TableHead className="text-right">Chiamate</TableHead>
                    <TableHead className="text-right">Costo</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {[...sourceCost.entries()].map(([sourceId, acc]) => (
                    <TableRow key={sourceId}>
                      <TableCell>#{sourceId}</TableCell>
                      <TableCell className="text-right font-mono text-xs">{acc.calls}</TableCell>
                      <TableCell className="text-right font-mono text-xs">{formatCurrency(acc.cost)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Ultime chiamate LLM fallite ({stats.failed_llm_calls.length})</CardTitle>
        </CardHeader>
        <CardContent>
          {stats.failed_llm_calls.length === 0 ? (
            <p className="text-muted-foreground text-sm">Nessuna chiamata fallita.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>ID</TableHead>
                  <TableHead>Ruolo</TableHead>
                  <TableHead>Modello</TableHead>
                  <TableHead>Stato</TableHead>
                  <TableHead>Errore</TableHead>
                  <TableHead>Quando</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {stats.failed_llm_calls.map((call) => (
                  <TableRow key={call.id}>
                    <TableCell className="font-mono text-xs">#{call.id}</TableCell>
                    <TableCell className="text-xs">{call.role}</TableCell>
                    <TableCell className="font-mono text-xs">{call.model}</TableCell>
                    <TableCell>
                      <Badge variant="destructive" className="font-normal">{call.status}</Badge>
                    </TableCell>
                    <TableCell className="text-muted-foreground max-w-80 truncate text-xs">
                      {call.error ?? "—"}
                    </TableCell>
                    <TableCell className="text-muted-foreground text-xs">
                      {DAY_FORMAT.format(new Date(call.created_at))}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
