"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

const LINKS = [
  { href: "/", label: "Panoramica" },
  { href: "/ideas", label: "Idee" },
  { href: "/sources", label: "Sorgenti" },
  { href: "/ops", label: "Operazioni" },
];

export function Nav() {
  const pathname = usePathname();
  return (
    <header className="bg-card/80 sticky top-0 z-10 border-b backdrop-blur">
      <div className="mx-auto flex h-14 max-w-7xl items-center gap-6 px-4">
        <Link href="/" className="text-base font-semibold tracking-tight">
          IdeaI
        </Link>
        <nav className="flex items-center gap-1">
          {LINKS.map((link) => {
            const active = link.href === "/" ? pathname === "/" : pathname.startsWith(link.href);
            return (
              <Link
                key={link.href}
                href={link.href}
                className={cn(
                  "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                  active ? "bg-secondary text-secondary-foreground" : "text-muted-foreground hover:text-foreground",
                )}
              >
                {link.label}
              </Link>
            );
          })}
        </nav>
      </div>
    </header>
  );
}
