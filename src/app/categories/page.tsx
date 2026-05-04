import type { Metadata } from "next";
import { CategoryIcon } from "@/components/ui/category-icon";
import { PublicShell } from "@/components/layout/public-shell";
import { getCategories, getPublicOffers } from "@/lib/deals";

export const metadata: Metadata = {
  title: "Categories",
};

export default function CategoriesPage() {
  const categories = getCategories();
  const offers = getPublicOffers();

  return (
    <PublicShell>
      <main className="mx-auto max-w-7xl px-4 py-6">
        <p className="text-xs font-black uppercase text-maple">Categories</p>
        <h1 className="text-3xl font-black md:text-5xl">Browse by local need</h1>
        <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {categories.map((category) => {
            const count = offers.filter((offer) => offer.categoryId === category.id).length;
            return (
              <a key={category.id} href={`/deals?category=${category.slug}`} className="rounded-3xl bg-white p-5 shadow-sm transition hover:-translate-y-0.5 hover:shadow-xl hover:shadow-neutral-900/10">
                <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-cream text-maple">
                  <CategoryIcon name={category.icon} className="h-6 w-6" />
                </div>
                <h2 className="mt-4 text-xl font-black">{category.name}</h2>
                <p className="mt-1 text-sm font-semibold text-muted">{count} active offers</p>
              </a>
            );
          })}
        </div>
      </main>
    </PublicShell>
  );
}

