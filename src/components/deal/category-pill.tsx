import Link from "next/link";
import { CategoryIcon } from "@/components/ui/category-icon";
import type { Category } from "@/types/dealnear";
import { cn } from "@/lib/utils";

export function CategoryPill({ category, className }: { category: Category; className?: string }) {
  return (
    <Link
      href={`/deals?category=${category.slug}`}
      className={cn(
        "inline-flex items-center gap-2 rounded-full border border-border bg-white px-3 py-2 text-sm font-semibold text-foreground shadow-sm transition hover:border-maple/40 hover:text-maple",
        className,
      )}
    >
      <CategoryIcon name={category.icon} className="h-4 w-4 text-maple" />
      {category.name}
    </Link>
  );
}

