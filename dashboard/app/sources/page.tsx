import { TargetForm } from "@/components/target-form";
import { TargetToggle } from "@/components/target-toggle";
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
import type { SourceInfo } from "@/lib/types";
import { formatCurrency, formatDate, formatRelative } from "@/lib/utils";

export const revalidate = 0;

function cursorSummary(cursor: Record<string, unknown> | null): string {
  if (!cursor || Object.keys(cursor).length === 0) return "—";
  return Object.entries(cursor)
    .map(([key, value]) => `${key}=${String(value)}`)
    .join(", ");
}

export default async function SourcesPage() {
  const sources = await apiGetSafe<SourceInfo[]>("/sources", []);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Sorgenti</h1>
        <p className="text-muted-foreground text-sm">
          Target monitorati, cursore di avanzamento, errori recenti e budget di rate limit consumato.
        </p>
      </div>

      {sources.length === 0 ? (
        <Card>
          <CardContent className="text-muted-foreground py-6 text-sm">
            Nessuna sorgente configurata.
          </CardContent>
        </Card>
      ) : null}

      {sources.map((source) => (
        <Card key={source.id}>
          <CardHeader>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <CardTitle className="flex items-center gap-2">
                  {source.name}
                  <Badge variant="outline" className="font-normal">{source.kind}</Badge>
                  <Badge variant={source.enabled ? "default" : "secondary"} className="font-normal">
                    {source.enabled ? "abilitata" : "disabilitata"}
                  </Badge>
                </CardTitle>
                <CardDescription>
                  Rate limit: {source.rate_limit.bucket} — {source.rate_limit.used ?? "—"}/
                  {source.rate_limit.limit ?? "—"} · chiamate oggi: {source.calls_today.calls} ·{" "}
                  {formatCurrency(source.calls_today.cost_usd)}
                </CardDescription>
              </div>
            </div>
          </CardHeader>
          <CardContent className="space-y-5">
            <TargetForm sourceId={source.id} />

            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Target</TableHead>
                  <TableHead>Tipo</TableHead>
                  <TableHead>Attivo</TableHead>
                  <TableHead>Cursore</TableHead>
                  <TableHead>Ultimo poll</TableHead>
                  <TableHead>Prossimo</TableHead>
                  <TableHead className="text-right">Intervallo</TableHead>
                  <TableHead className="text-right">Errori</TableHead>
                  <TableHead>Ultimo errore</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {source.targets.map((target) => (
                  <TableRow key={target.id}>
                    <TableCell className="font-medium">{target.target_ref}</TableCell>
                    <TableCell className="text-xs">{target.target_kind}</TableCell>
                    <TableCell>
                      <TargetToggle targetId={target.id} enabled={target.enabled} />
                    </TableCell>
                    <TableCell className="text-muted-foreground max-w-48 font-mono text-xs break-all">
                      {cursorSummary(target.cursor)}
                    </TableCell>
                    <TableCell className="text-muted-foreground text-xs">
                      {target.last_polled_at ? formatRelative(target.last_polled_at) : "—"}
                    </TableCell>
                    <TableCell className="text-muted-foreground text-xs">
                      {target.next_poll_at ? formatDate(target.next_poll_at) : "—"}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs">{target.poll_interval_s}s</TableCell>
                    <TableCell className="text-right">
                      {target.error_count > 0 ? (
                        <Badge variant="destructive">{target.error_count}</Badge>
                      ) : (
                        <span className="text-muted-foreground text-xs">0</span>
                      )}
                    </TableCell>
                    <TableCell className="text-muted-foreground max-w-48 truncate text-xs">
                      {target.last_error ?? "—"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
