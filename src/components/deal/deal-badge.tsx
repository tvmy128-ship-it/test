import { BadgePercent, Sparkles } from "lucide-react";
import { cn } from "@/lib/utils";

export function DealBadge({
  value,
  featured,
  className,
}: {
  value: string;
  featured?: boolean;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full bg-maple px-3 py-1 text-xs font-bold text-white shadow-sm",
        featured && "bg-foreground",
        className,
      )}
    >
      {featured ? <Sparkles className="h-3.5 w-3.5" /> : <BadgePercent className="h-3.5 w-3.5" />}
      {featured ? "Featured" : value}
    </div>
  );
}

