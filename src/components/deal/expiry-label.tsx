import { Clock3 } from "lucide-react";
import { daysUntilExpiry } from "@/lib/deals";
import { cn } from "@/lib/utils";

export function ExpiryLabel({ endDate, className }: { endDate: string; className?: string }) {
  const days = daysUntilExpiry(endDate);
  const label =
    days < 0 ? "Expired" : days === 0 ? "Ends today" : days === 1 ? "Ends tomorrow" : `Ends in ${days} days`;

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full bg-cream px-2.5 py-1 text-xs font-semibold text-muted",
        days <= 3 && "bg-orange/15 text-[#9f3f09]",
        className,
      )}
    >
      <Clock3 className="h-3.5 w-3.5" aria-hidden="true" />
      {label}
    </span>
  );
}

