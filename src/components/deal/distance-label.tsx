import { MapPin } from "lucide-react";
import { formatDistance } from "@/lib/deals";
import { cn } from "@/lib/utils";

export function DistanceLabel({ distanceKm, className }: { distanceKm?: number; className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-1 text-sm font-semibold text-muted", className)}>
      <MapPin className="h-4 w-4 text-maple" aria-hidden="true" />
      {formatDistance(distanceKm)}
    </span>
  );
}

