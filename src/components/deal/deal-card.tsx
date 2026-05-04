import Image from "next/image";
import Link from "next/link";
import { ArrowRight, Building2 } from "lucide-react";
import { ButtonLink } from "@/components/ui/button";
import { DealBadge } from "@/components/deal/deal-badge";
import { DistanceLabel } from "@/components/deal/distance-label";
import { ExpiryLabel } from "@/components/deal/expiry-label";
import type { EnrichedOffer } from "@/types/dealnear";
import { formatCurrency } from "@/lib/utils";

export function DealCard({ offer, compact = false }: { offer: EnrichedOffer; compact?: boolean }) {
  return (
    <article className="group overflow-hidden rounded-3xl bg-white shadow-sm ring-1 ring-black/[0.03] transition hover:-translate-y-0.5 hover:shadow-xl hover:shadow-neutral-900/10">
      <Link href={`/deals/${offer.slug}`} className="relative block aspect-[4/3] overflow-hidden bg-neutral-100">
        <Image
          src={offer.imageUrl}
          alt={offer.title}
          fill
          sizes="(max-width: 768px) 92vw, 320px"
          className="object-cover transition duration-500 group-hover:scale-105"
        />
        <div className="absolute left-3 top-3 flex flex-wrap gap-2">
          <DealBadge value={offer.discountValue} />
          {offer.isFeatured ? <DealBadge value={offer.discountValue} featured /> : null}
        </div>
      </Link>

      <div className="p-4">
        <div className="flex items-center justify-between gap-3">
          <span className="rounded-full bg-cream px-2.5 py-1 text-xs font-bold text-maple">{offer.category.name}</span>
          <DistanceLabel distanceKm={offer.distanceKm} />
        </div>

        <Link href={`/deals/${offer.slug}`}>
          <h3 className="mt-3 line-clamp-2 text-lg font-extrabold leading-snug tracking-normal text-foreground">
            {offer.title}
          </h3>
        </Link>

        <div className="mt-2 flex items-center gap-2 text-sm font-medium text-muted">
          <Building2 className="h-4 w-4" aria-hidden="true" />
          <span className="truncate">{offer.business.name}</span>
        </div>

        {!compact ? (
          <p className="mt-2 line-clamp-2 text-sm leading-6 text-muted">{offer.description}</p>
        ) : null}

        <div className="mt-4 flex items-center justify-between gap-3">
          <ExpiryLabel endDate={offer.endDate} />
          {offer.dealPrice ? (
            <span className="text-sm font-bold text-foreground">{formatCurrency(offer.dealPrice)}</span>
          ) : null}
        </div>

        <ButtonLink href={`/deals/${offer.slug}`} className="mt-4 w-full" variant="primary">
          Show Offer
          <ArrowRight className="h-4 w-4" aria-hidden="true" />
        </ButtonLink>
      </div>
    </article>
  );
}

