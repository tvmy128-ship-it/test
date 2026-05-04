import type { Metadata } from "next";
import Image from "next/image";
import { notFound } from "next/navigation";
import { DealCard } from "@/components/deal/deal-card";
import { BusinessHeader } from "@/components/business/business-header";
import { PublicShell } from "@/components/layout/public-shell";
import { getBusinessBySlug, getOffersForBusiness, getPublicBusinesses } from "@/lib/deals";

type Params = Promise<{ slug: string }>;

export function generateStaticParams() {
  return getPublicBusinesses().map((business) => ({ slug: business.slug }));
}

export async function generateMetadata({ params }: { params: Params }): Promise<Metadata> {
  const { slug } = await params;
  const business = getBusinessBySlug(slug);
  return {
    title: business?.name ?? "Business",
    description: business?.description,
  };
}

export default async function BusinessProfilePage({ params }: { params: Params }) {
  const { slug } = await params;
  const business = getBusinessBySlug(slug);
  if (!business) notFound();

  const offers = getOffersForBusiness(business.id);

  return (
    <PublicShell>
      <main className="mx-auto max-w-7xl px-4 py-6">
        <BusinessHeader business={business} />
        <section className="mt-6">
          <h2 className="text-2xl font-black">Gallery</h2>
          <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-3">
            {business.galleryUrls.map((image) => (
              <div key={image} className="relative aspect-[4/3] overflow-hidden rounded-3xl bg-white shadow-sm">
                <Image src={image} alt={`${business.name} gallery`} fill sizes="(max-width: 768px) 44vw, 320px" className="object-cover" />
              </div>
            ))}
          </div>
        </section>
        <section className="mt-8">
          <h2 className="text-2xl font-black">Active offers</h2>
          <div className="mt-4 grid grid-cols-[repeat(auto-fill,minmax(260px,1fr))] gap-4">
            {offers.map((offer) => (
              <DealCard key={offer.id} offer={offer} />
            ))}
          </div>
        </section>
      </main>
    </PublicShell>
  );
}

