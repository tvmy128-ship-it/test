import type { Metadata } from "next";
import { Heart, UserRound } from "lucide-react";
import { DealCard } from "@/components/deal/deal-card";
import { PublicShell } from "@/components/layout/public-shell";
import { getPublicOffers } from "@/lib/deals";

export const metadata: Metadata = {
  title: "Profile",
};

export default function ProfilePage() {
  const savedPreview = getPublicOffers().slice(0, 2);

  return (
    <PublicShell>
      <main className="mx-auto max-w-7xl px-4 py-6">
        <section className="rounded-[2rem] bg-white p-6 shadow-sm">
          <div className="flex h-14 w-14 items-center justify-center rounded-3xl bg-maple text-white">
            <UserRound className="h-7 w-7" />
          </div>
          <h1 className="mt-4 text-3xl font-black">Customer profile</h1>
          <p className="mt-2 text-muted">Manage saved offers and account details after signing in.</p>
        </section>
        <section className="mt-8">
          <div className="flex items-center gap-2">
            <Heart className="h-5 w-5 text-maple" />
            <h2 className="text-2xl font-black">Saved offers</h2>
          </div>
          <div className="mt-4 grid grid-cols-[repeat(auto-fill,minmax(260px,1fr))] gap-4">
            {savedPreview.map((offer) => (
              <DealCard key={offer.id} offer={offer} compact />
            ))}
          </div>
        </section>
      </main>
    </PublicShell>
  );
}
