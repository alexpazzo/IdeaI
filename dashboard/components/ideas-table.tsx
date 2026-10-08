import Link from "next/link";

import { CategoryBadge, VerdictBadge, WatchBadge } from "@/components/badges";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { IdeaListItem } from "@/lib/types";
import { formatRelative } from "@/lib/utils";

export function IdeasTable({ items }: { items: IdeaListItem[] }) {
  if (items.length === 0) {
    return <p className="text-muted-foreground py-8 text-center text-sm">Nessuna idea corrisponde ai filtri.</p>;
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Titolo</TableHead>
          <TableHead className="text-right">Score</TableHead>
          <TableHead>Verdict</TableHead>
          <TableHead>Categoria</TableHead>
          <TableHead>Tag</TableHead>
          <TableHead>Sorgenti</TableHead>
          <TableHead>Ultima attività</TableHead>
          <TableHead>Watch</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {items.map((idea) => (
          <TableRow key={idea.id}>
            <TableCell className="max-w-md">
              <Link href={`/ideas/${idea.id}`} className="font-medium hover:underline">
                {idea.title}
              </Link>
              <p className="text-muted-foreground line-clamp-1 text-xs">{idea.canonical_summary}</p>
            </TableCell>
            <TableCell className="text-right font-mono tabular-nums">
              {idea.opportunity_score ?? "—"}
            </TableCell>
            <TableCell>
              <VerdictBadge verdict={idea.verdict} />
            </TableCell>
            <TableCell>
              <CategoryBadge category={idea.category} />
            </TableCell>
            <TableCell>
              <div className="flex flex-wrap gap-1">
                {idea.tags.slice(0, 4).map((tag) => (
                  <Badge key={tag} variant="secondary" className="font-normal">
                    {tag}
                  </Badge>
                ))}
              </div>
            </TableCell>
            <TableCell>
              <div className="flex flex-wrap gap-1">
                {idea.source_kinds.map((kind) => (
                  <Badge key={kind} variant="outline" className="font-normal">
                    {kind}
                  </Badge>
                ))}
              </div>
            </TableCell>
            <TableCell className="text-muted-foreground whitespace-nowrap text-sm">
              {formatRelative(idea.last_activity_at)}
            </TableCell>
            <TableCell>
              <WatchBadge enabled={idea.watch.enabled} mode={idea.watch.mode} />
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
