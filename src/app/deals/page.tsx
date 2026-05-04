import type { Metadata } from "next";
import { DealCard } from "@/components/deal/deal-card";
import { FilterSheet } from "@/components/deal/filter-sheet";
import { SearchBar } from "@/components/deal/search-bar";
import { PublicShell } from "@/components/layout/public-shell";
import { EmptyState } from "@/components/ui/empty-state";
import { searchOffers } from "@/lib/deals";

export const metadata: Metadata = {
  title: "Deals",
  description: "Search nearby active local offers on DealNear.",
};

type SearchParams = Promise<Record<string, string | string[] | undefined>>;

export default async function DealsPage({ searchParams }: { searchParams: SearchParams }) {
  const params = await searchParams;
  const q = value(params.q);
  const category = value(params.category);
  const distance = value(params.distance);
  const sort = value(params.sort) as "nearby" | "newest" | "expiring" | "discount" | undefined;
  const openNow = value(params.openNow);
  const featured = value(params.featured);

  const deals = searchOffers({
    query: q,
    category,
    distanceKm: distance ? Number(distance) : undefined,
    sort,
    openNow: openNow === "true",
    featured: featured === "true",
  });

  return (
    <PublicShell>
      <main className="mx-auto max-w-7xl px-4 py-6">
        <div className="mb-6">
          <p className="text-xs font-black uppercase text-maple">Search</p>
          <h1 className="text-3xl font-black md:text-5xl">Find local offers</h1>
          <p className="mt-2 max-w-2xl text-muted">Search by offer title, business, category, distance, discount, and expiry.</p>
        </div>
        <form className="grid gap-4">
          <SearchBar defaultValue={q} />
          <FilterSheet category={category} distance={distance} sort={sort} openNow={openNow} />
        </form>

        <div className="mt-6 flex items-center justify-between">
          <h2 className="text-xl font-black">{deals.length} active offers</h2>
          <span className="rounded-full bg-white px-3 py-1 text-sm font-bold text-muted">Approved only</span>
        </div>

        {deals.length ? (
          <div className="mt-4 grid grid-cols-[repeat(auto-fill,minmax(260px,1fr))] gap-4">
            {deals.map((offer) => (
              <DealCard key={offer.id} offer={offer} />
            ))}
          </div>
        ) : (
          <div className="mt-6">
            <EmptyState title="No offers found" description="Try another category, broader distance, or remove open-now filtering." href="/deals" action="Reset search" />
          </div>
        )}
      </main>
    </PublicShell>
  );
}

function value(input: string | string[] | undefined) {
  return Array.isArray(input) ? input[0] ?? "" : input ?? "";
}
