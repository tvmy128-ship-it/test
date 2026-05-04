import { cn } from "@/lib/utils";
import type { ApprovalStatus, OfferStatus } from "@/types/dealnear";

export function AdminStatusBadge({ status }: { status: ApprovalStatus | OfferStatus | string }) {
  const styles: Record<string, string> = {
    approved: "bg-emerald-50 text-emerald-700",
    active: "bg-emerald-50 text-emerald-700",
    pending: "bg-orange/10 text-[#8a3a0a]",
    open: "bg-orange/10 text-[#8a3a0a]",
    reviewed: "bg-blue-50 text-blue-700",
    resolved: "bg-emerald-50 text-emerald-700",
    paused: "bg-neutral-100 text-neutral-700",
    rejected: "bg-maple/10 text-maple",
    expired: "bg-neutral-200 text-neutral-600",
  };

  return (
    <span className={cn("inline-flex rounded-full px-2.5 py-1 text-xs font-black capitalize", styles[status] ?? "bg-neutral-100")}>
      {status.replace("_", " ")}
    </span>
  );
}
