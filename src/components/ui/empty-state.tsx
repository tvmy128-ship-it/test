import { SearchX } from "lucide-react";
import { ButtonLink } from "@/components/ui/button";

export function EmptyState({
  title,
  description,
  href,
  action,
}: {
  title: string;
  description: string;
  href?: string;
  action?: string;
}) {
  return (
    <div className="rounded-3xl border border-dashed border-border bg-white p-8 text-center shadow-sm">
      <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-cream text-maple">
        <SearchX className="h-6 w-6" aria-hidden="true" />
      </div>
      <h2 className="mt-4 text-xl font-bold">{title}</h2>
      <p className="mx-auto mt-2 max-w-md text-sm leading-6 text-muted">{description}</p>
      {href && action ? (
        <ButtonLink href={href} className="mt-5" variant="outline">
          {action}
        </ButtonLink>
      ) : null}
    </div>
  );
}

