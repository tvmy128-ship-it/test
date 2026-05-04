import { cn } from "@/lib/utils";

export function LoadingSkeleton({ className }: { className?: string }) {
  return <div className={cn("animate-pulse rounded-2xl bg-neutral-200/70", className)} />;
}

export function DealCardSkeleton() {
  return (
    <div className="rounded-3xl bg-white p-3 shadow-sm">
      <LoadingSkeleton className="aspect-[4/3] w-full" />
      <LoadingSkeleton className="mt-4 h-4 w-24" />
      <LoadingSkeleton className="mt-3 h-6 w-4/5" />
      <LoadingSkeleton className="mt-3 h-4 w-2/3" />
      <LoadingSkeleton className="mt-4 h-11 w-full rounded-2xl" />
    </div>
  );
}

