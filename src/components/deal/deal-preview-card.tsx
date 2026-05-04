import Image from "next/image";
import { ArrowRight } from "lucide-react";
import { ButtonLink } from "@/components/ui/button";
import { DealBadge } from "@/components/deal/deal-badge";
import { DistanceLabel } from "@/components/deal/distance-label";
import type { EnrichedOffer } from "@/types/dealnear";

export function DealPreviewCard({ offer }: { offer: EnrichedOffer }) {
  return (
    <div className="w-[280px] rounded-3xl bg-white p-3">
      <div className="relative h-32 overflow-hidden rounded-2xl">
        <Image src={offer.imageUrl} alt={offer.title} fill sizes="280px" className="object-cover" />
        <DealBadge value={offer.discountValue} className="absolute left-2 top-2" />
      </div>
      <div className="mt-3">
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs font-bold text-maple">{offer.category.name}</span>
          <DistanceLabel distanceKm={offer.distanceKm} className="text-xs" />
        </div>
        <h3 className="mt-1 line-clamp-2 text-base font-extrabold leading-snug">{offer.title}</h3>
        <p className="mt-1 truncate text-sm text-muted">{offer.business.name}</p>
        <ButtonLink href={`/deals/${offer.slug}`} size="sm" className="mt-3 w-full">
          Deal details
          <ArrowRight className="h-4 w-4" aria-hidden="true" />
        </ButtonLink>
      </div>
    </div>
  );
}

