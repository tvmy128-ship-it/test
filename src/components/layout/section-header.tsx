import { ArrowRight } from "lucide-react";
import { ButtonLink } from "@/components/ui/button";

export function SectionHeader({ title, href }: { title: string; href?: string }) {
  return (
    <div className="mb-4 flex items-end justify-between gap-3">
      <div>
        <p className="text-xs font-black uppercase text-maple">DealNear</p>
        <h2 className="text-2xl font-black tracking-normal md:text-3xl">{title}</h2>
      </div>
      {href ? (
        <ButtonLink href={href} variant="ghost" size="sm" className="hidden md:inline-flex">
          View all
          <ArrowRight className="h-4 w-4" />
        </ButtonLink>
      ) : null}
    </div>
  );
}

