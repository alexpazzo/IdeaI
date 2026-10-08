"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

interface TabsProps {
  tabs: { value: string; label: React.ReactNode }[];
  value: string;
  onValueChange: (value: string) => void;
  className?: string;
}

function Tabs({ tabs, value, onValueChange, className }: TabsProps) {
  return (
    <div
      data-slot="tabs"
      role="tablist"
      className={cn("bg-muted text-muted-foreground inline-flex h-9 items-center rounded-lg p-1", className)}
    >
      {tabs.map((tab) => (
        <button
          key={tab.value}
          role="tab"
          type="button"
          aria-selected={value === tab.value}
          onClick={() => onValueChange(tab.value)}
          className={cn(
            "inline-flex items-center justify-center rounded-md px-3 py-1 text-sm font-medium whitespace-nowrap transition-colors",
            value === tab.value ? "bg-background text-foreground shadow-sm" : "hover:text-foreground",
          )}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}

export { Tabs };
