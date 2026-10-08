import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

type BadgeVariant = "default" | "secondary" | "destructive" | "outline";

const VERDICT_VARIANT: Record<string, BadgeVariant> = {
  strong: "default",
  promising: "secondary",
  weak: "outline",
  reject: "destructive",
};

const VERDICT_LABEL: Record<string, string> = {
  strong: "forte",
  promising: "promettente",
  weak: "debole",
  reject: "scartata",
};

const STATUS_LABEL: Record<string, string> = {
  new: "nuova",
  analyzed: "analizzata",
  watching: "in watch",
  archived: "archiviata",
  rejected: "rifiutata",
};

const CATEGORY_LABEL: Record<string, string> = {
  product_idea: "idea di prodotto",
  pain_point: "pain point",
  market_signal: "segnale di mercato",
  question: "domanda",
  announcement: "annuncio",
  spam: "spam",
  off_topic: "fuori tema",
};

export function VerdictBadge({ verdict }: { verdict: string | null }) {
  if (!verdict) return <span className="text-muted-foreground">—</span>;
  return <Badge variant={VERDICT_VARIANT[verdict] ?? "outline"}>{VERDICT_LABEL[verdict] ?? verdict}</Badge>;
}

export function StatusBadge({ status }: { status: string }) {
  const variant: BadgeVariant = status === "watching" ? "default" : status === "rejected" ? "destructive" : "secondary";
  return <Badge variant={variant}>{STATUS_LABEL[status] ?? status}</Badge>;
}

export function CategoryBadge({ category }: { category: string | null }) {
  if (!category) return <span className="text-muted-foreground">—</span>;
  return <Badge variant="outline">{CATEGORY_LABEL[category] ?? category}</Badge>;
}

export function WatchBadge({ enabled, mode }: { enabled: boolean; mode: string }) {
  if (!enabled) return <Badge variant="outline">non attivo</Badge>;
  return (
    <Badge variant="secondary" className={cn("gap-1")}>
      <span className="bg-primary inline-block size-1.5 rounded-full" />
      {mode === "thread_full" ? "thread" : "commenti"}
    </Badge>
  );
}

export { STATUS_LABEL, CATEGORY_LABEL, VERDICT_LABEL };
