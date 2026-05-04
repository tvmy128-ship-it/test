import type { Metadata } from "next";
import Image from "next/image";
import { notFound } from "next/navigation";
import { CalendarDays, MapPinned, MessageCircle, Phone, Ticket } from "lucide-react";
import { BusinessHeader } from "@/components/business/business-header";
import { DealCard } from "@/components/deal/deal-card";
import { DealBadge } from "@/components/deal/deal-badge";
import { ExpiryLabel } from "@/components/deal/expiry-label";
import { OfferViewTracker, ReportIssueForm, SaveOfferButton, TrackedLink } from "@/components/deal/tracked-actions";
import { PublicShell } from "@/components/layout/public-shell";
import { ButtonLink } from "@/components/ui/button";
import { getOfferBySlug, getOffersForBusiness, getPublicOffers } from "@/lib/deals";
import { formatCurrency } from "@/lib/utils";

type Params = Promise<{ slug: string }>;

export function generateStaticParams() {
  return getPublicOffers().map((offer) => ({ slug: offer.slug }));
}

export async function generateMetadata({ params }: { params: Params }): Promise<Metadata> {
  const { slug } = await params;
  const offer = getOfferBySlug(slug);

  if (!offer) {
    return { title: "Deal not found" };
  }

  return {
    title: offer.title,
    description: offer.description,
    openGraph: {
      title: offer.title,
      description: offer.description,
      images: [offer.imageUrl],
    },
  };
}

export default async function DealDetailsPage({ params }: { params: Params }) {
  const { slug } = await params;
  const offer = getOfferBySlug(slug);
  if (!offer) notFound();

  const related = getOffersForBusiness(offer.businessId).filter((item) => item.id !== offer.id).slice(0, 3);
  const directions = `https://www.google.com/maps/dir/?api=1&destination=${offer.business.latitude},${offer.business.longitude}`;
  const whatsapp = `https://wa.me/${offer.business.whatsapp.replace(/[^0-9]/g, "")}?text=${encodeURIComponent(`Hi, I saw "${offer.title}" on DealNear.`)}`;

  return (
    <PublicShell>
      <OfferViewTracker offer={offer} />
      <main className="mx-auto max-w-7xl px-4 py-6">
        <div className="grid gap-6 lg:grid-cols-[1.05fr_0.95fr]">
          <section className="overflow-hidden rounded-[2rem] bg-white shadow-sm">
            <div className="relative h-72 md:aspect-[4/3] md:h-auto md:min-h-[360px]">
              <Image src={offer.imageUrl} alt={offer.title} fill sizes="(max-width: 1024px) 100vw, 640px" className="object-cover" priority />
              <div className="absolute left-4 top-4 flex flex-wrap gap-2">
                <DealBadge value={offer.discountValue} />
                {offer.isFeatured ? <DealBadge value={offer.discountValue} featured /> : null}
              </div>
            </div>
            {offer.extraImageUrls?.length ? (
              <div className="hidden grid-cols-2 gap-2 p-3 md:grid">
                {offer.extraImageUrls.map((image) => (
                  <div key={image} className="relative h-32 overflow-hidden rounded-2xl">
                    <Image src={image} alt={`${offer.title} photo`} fill sizes="260px" className="object-cover" />
                  </div>
                ))}
              </div>
            ) : null}
          </section>

          <section className="rounded-[2rem] bg-white p-5 shadow-sm">
            <div className="flex flex-wrap items-center gap-2">
              <span className="rounded-full bg-cream px-3 py-1 text-sm font-black text-maple">{offer.category.name}</span>
              <ExpiryLabel endDate={offer.endDate} />
            </div>
            <h1 className="mt-4 text-3xl font-black leading-tight md:text-5xl">{offer.title}</h1>
            <p className="mt-4 text-base leading-7 text-muted">{offer.description}</p>

            <div className="mt-5 grid gap-3">
              <ButtonLink href={`/deals/${offer.slug}/show`} className="w-full" variant="primary">
                <Ticket className="h-4 w-4" />
                Show Offer
              </ButtonLink>
              <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
                <TrackedLink offer={offer} eventType="call_tap" href={`tel:${offer.business.phone}`}>
                  <Phone className="h-4 w-4" />
                  Call
                </TrackedLink>
                <TrackedLink offer={offer} eventType="whatsapp_tap" href={whatsapp}>
                  <MessageCircle className="h-4 w-4" />
                  WhatsApp
                </TrackedLink>
                <TrackedLink offer={offer} eventType="direction_tap" href={directions}>
                  <MapPinned className="h-4 w-4" />
                  Directions
                </TrackedLink>
                <SaveOfferButton offer={offer} />
              </div>
            </div>

            <div className="mt-5 grid grid-cols-2 gap-3">
              <div className="rounded-3xl bg-cream p-4">
                <p className="text-xs font-black uppercase text-muted">Discount</p>
                <p className="mt-1 text-2xl font-black text-maple">{offer.discountValue}</p>
              </div>
              <div className="rounded-3xl bg-cream p-4">
                <p className="text-xs font-black uppercase text-muted">Price</p>
                <p className="mt-1 text-2xl font-black">{offer.dealPrice ? formatCurrency(offer.dealPrice) : "Show at store"}</p>
              </div>
            </div>

            <div className="mt-6 grid gap-4 rounded-3xl bg-cream p-4">
              <div>
                <h2 className="text-sm font-black uppercase text-muted">Terms and conditions</h2>
                <p className="mt-2 text-sm leading-6 text-foreground">{offer.terms}</p>
              </div>
              <div className="flex items-center gap-2 text-sm font-bold text-muted">
                <CalendarDays className="h-4 w-4 text-maple" />
                Valid from {offer.startDate} to {offer.endDate}
              </div>
              {offer.promoCode ? (
                <div className="rounded-2xl border border-dashed border-maple/30 bg-white px-4 py-3">
                  <p className="text-xs font-black uppercase text-maple">Optional promo code</p>
                  <p className="text-2xl font-black tracking-wider">{offer.promoCode}</p>
                </div>
              ) : null}
            </div>
          </section>
        </div>

        <div className="mt-6">
          <BusinessHeader business={offer.business} />
        </div>

        <div className="mt-6 grid gap-6 lg:grid-cols-[1fr_360px]">
          <section>
            <h2 className="text-2xl font-black">More from {offer.business.name}</h2>
            {related.length ? (
              <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {related.map((item) => (
                  <DealCard key={item.id} offer={item} compact />
                ))}
              </div>
            ) : (
              <p className="mt-2 text-muted">This business has one active approved offer right now.</p>
            )}
          </section>
          <ReportIssueForm offer={offer} />
        </div>
      </main>
    </PublicShell>
  );
}
