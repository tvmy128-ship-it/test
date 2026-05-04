import Image from "next/image";
import { LocateFixed, MapPin, Sparkles } from "lucide-react";
import { DealCard } from "@/components/deal/deal-card";
import { CategoryPill } from "@/components/deal/category-pill";
import { SearchBar } from "@/components/deal/search-bar";
import { PublicShell } from "@/components/layout/public-shell";
import { SectionHeader } from "@/components/layout/section-header";
import { ButtonLink } from "@/components/ui/button";
import { getCategories, getHomeSections, getPublicOffers } from "@/lib/deals";

export default function HomePage() {
  const [heroOffer] = getPublicOffers({ sort: "nearby" });
  const sections = getHomeSections();
  const categories = getCategories();

  return (
    <PublicShell>
      <main>
        <section className="mx-auto grid max-w-7xl gap-6 px-4 py-6 md:grid-cols-[1.05fr_0.95fr] md:py-10">
          <div className="flex flex-col justify-center">
            <div className="inline-flex w-fit items-center gap-2 rounded-full bg-white px-3 py-2 text-sm font-black text-maple shadow-sm">
              <LocateFixed className="h-4 w-4" aria-hidden="true" />
              Toronto, Ontario
            </div>
            <h1 className="mt-5 max-w-2xl text-4xl font-black tracking-normal md:text-6xl">
              What offers are near me right now?
            </h1>
            <p className="mt-4 max-w-xl text-base leading-7 text-muted md:text-lg">
              Browse nearby food, grocery, cafe, bakery, and shop offers without signing up.
            </p>
            <form action="/deals" className="mt-6 max-w-2xl">
              <SearchBar />
            </form>
            <div className="mt-5 flex flex-wrap gap-2">
              {categories.slice(0, 5).map((category) => (
                <CategoryPill key={category.id} category={category} />
              ))}
            </div>
          </div>

          {heroOffer ? (
            <div className="relative overflow-hidden rounded-[2rem] bg-white p-3 shadow-xl shadow-neutral-900/10">
              <div className="relative min-h-[430px] overflow-hidden rounded-[1.55rem]">
                <Image src={heroOffer.imageUrl} alt={heroOffer.title} fill sizes="(max-width: 768px) 92vw, 520px" className="object-cover" priority />
                <div className="absolute inset-0 bg-gradient-to-t from-black/75 via-black/10 to-transparent" />
                <div className="absolute left-4 right-4 top-4 flex justify-between gap-3">
                  <span className="inline-flex items-center gap-2 rounded-full bg-white px-3 py-2 text-sm font-black text-maple">
                    <Sparkles className="h-4 w-4" />
                    Featured
                  </span>
                  <span className="rounded-full bg-maple px-3 py-2 text-sm font-black text-white">{heroOffer.discountValue}</span>
                </div>
                <div className="absolute bottom-4 left-4 right-4 text-white">
                  <p className="flex items-center gap-2 text-sm font-bold text-white/85">
                    <MapPin className="h-4 w-4" />
                    {heroOffer.business.name} · {heroOffer.category.name}
                  </p>
                  <h2 className="mt-2 text-3xl font-black leading-tight">{heroOffer.title}</h2>
                  <ButtonLink href={`/deals/${heroOffer.slug}`} className="mt-4" variant="secondary">
                    Show Offer
                  </ButtonLink>
                </div>
              </div>
            </div>
          ) : null}
        </section>

        <section className="mx-auto max-w-7xl px-4 pb-12">
          <div className="grid gap-8">
            {sections.map((section) =>
              section.offers.length ? (
                <section key={section.title}>
                  <SectionHeader title={section.title} href={section.href} />
                  <div className="grid grid-cols-[repeat(auto-fill,minmax(260px,1fr))] gap-4">
                    {section.offers.map((offer) => (
                      <DealCard key={offer.id} offer={offer} />
                    ))}
                  </div>
                </section>
              ) : null,
            )}
          </div>
        </section>
      </main>
    </PublicShell>
  );
}

