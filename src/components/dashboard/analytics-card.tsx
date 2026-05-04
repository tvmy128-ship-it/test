import { TrendingUp } from "lucide-react";
import type { AnalyticsMetric } from "@/types/dealnear";

export function AnalyticsCard({ metric }: { metric: AnalyticsMetric }) {
  return (
    <div className="rounded-3xl bg-white p-5 shadow-sm">
      <div className="flex items-center justify-between">
        <p className="text-sm font-bold text-muted">{metric.label}</p>
        <span className="inline-flex items-center gap-1 rounded-full bg-orange/10 px-2 py-1 text-xs font-black text-[#8a3a0a]">
          <TrendingUp className="h-3 w-3" />
          {metric.change}
        </span>
      </div>
      <p className="mt-4 text-3xl font-black">{metric.value.toLocaleString("en-CA")}</p>
    </div>
  );
}

