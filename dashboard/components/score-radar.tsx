"use client";

import {
  PolarAngleAxis,
  PolarGrid,
  PolarRadiusAxis,
  Radar,
  RadarChart,
  ResponsiveContainer,
} from "recharts";

export interface ScoreRadarProps {
  feasibility: number | null;
  economics: number | null;
  competition: number | null;
}

export function ScoreRadar({ feasibility, economics, competition }: ScoreRadarProps) {
  const data = [
    { axis: "Fattibilità", value: feasibility ?? 0 },
    { axis: "Economia", value: economics ?? 0 },
    { axis: "Competizione", value: competition ?? 0 },
  ];
  return (
    <div className="h-64 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <RadarChart data={data} outerRadius="72%">
          <PolarGrid />
          <PolarAngleAxis dataKey="axis" tick={{ fontSize: 12 }} />
          <PolarRadiusAxis domain={[0, 5]} tickCount={6} tick={{ fontSize: 10 }} />
          <Radar name="Punteggi" dataKey="value" stroke="var(--primary)" fill="var(--primary)" fillOpacity={0.35} />
        </RadarChart>
      </ResponsiveContainer>
    </div>
  );
}
