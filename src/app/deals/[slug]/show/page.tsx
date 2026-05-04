import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { BadgeCheck, CalendarDays, Store, Ticket } from "lucide-react";
import { OfferViewTracker } from "@/components/deal/tracked-actions";
import { PublicShell } from "@/components/layout/public-shell";
import { ButtonLink } from "@/components/ui/button";
import { getOfferBySlug, getPublicOffers } from "@/lib/deals";

type Params = Promise<{ slug: string }>;

export const metadata: Metadata = {
  title: "Show Offer",
};

export function generateStaticParams() {
  return getPublicOffers().map((offer) => ({ slug: offer.slug }));
}

export default async function ShowOfferPage({ params }: { params: Params }) {
  const { slug } = await params;
  const offer = getOfferBySlug(slug);
  if (!offer) notFound();

  return (
    <PublicShell>
      <OfferViewTracker offer={offer} eventType="show_offer_tap" />
      <main className="mx-auto flex min-h-[calc(100vh-80px)] max-w-2xl items-center px-4 py-8">
        <section className="w-full rounded-[2rem] border-2 border-dashed border-maple/30 bg-white p-6 text-center shadow-xl shadow-neutral-900/10">
          <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-3xl bg-maple text-white">
            <Ticket className="h-8 w-8" />
          </div>
          <p className="mt-5 text-sm font-black uppercase text-maple">Show this screen to cashier</p>
          <h1 className="mt-2 text-3xl font-black leading-tight">{offer.title}</h1>
          <div className="mx-auto mt-5 w-fit rounded-full bg-orange/15 px-5 py-2 text-2xl font-black text-[#8a3a0a]">
            {offer.discountValue}
          </div>

          <div className="mt-6 grid gap-3 rounded-3xl bg-cream p-4 text-left">
            <p className="flex items-center gap-2 text-sm font-bold">
              <Store className="h-4 w-4 text-maple" />
              {offer.business.name}
            </p>
            <p className="flex items-center gap-2 text-sm font-bold">
              <CalendarDays className="h-4 w-4 text-maple" />
              Expires {offer.endDate}
            </p>
            <p className="text-sm leading-6 text-muted">{offer.terms}</p>
          </div>

          {offer.promoCode ? (
            <div className="mt-5 rounded-3xl border border-border bg-white p-4">
              <p className="text-xs font-black uppercase text-muted">Promo code</p>
              <p className="mt-1 text-4xl font-black tracking-wider">{offer.promoCode}</p>
            </div>
          ) : null}

          <div className="mt-5 inline-flex items-center gap-2 rounded-full bg-emerald-50 px-3 py-2 text-sm font-black text-emerald-700">
            <BadgeCheck className="h-4 w-4" />
            Approved DealNear offer
          </div>
          <ButtonLink href={`/deals/${offer.slug}`} variant="outline" className="mt-6 w-full">
            Back to details
          </ButtonLink>
        </section>
      </main>
    </PublicShell>
  );
}

