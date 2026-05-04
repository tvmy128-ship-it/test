import type { Metadata } from "next";
import { LockKeyhole } from "lucide-react";
import { DealCard } from "@/components/deal/deal-card";
import { PublicShell } from "@/components/layout/public-shell";
import { ButtonLink } from "@/components/ui/button";
import { getPublicOffers } from "@/lib/deals";

export const metadata: Metadata = {
  title: "Saved offers",
};

export default function SavedPage() {
  const preview = getPublicOffers().slice(0, 3);

  return (
    <PublicShell>
      <main className="mx-auto max-w-7xl px-4 py-6">
        <section className="rounded-[2rem] bg-white p-6 shadow-sm">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-cream text-maple">
            <LockKeyhole className="h-6 w-6" />
          </div>
          <h1 className="mt-4 text-3xl font-black">Saved offers require an account</h1>
          <p className="mt-2 max-w-2xl text-muted">Browsing is open to everyone. Sign in only when you want to save offers across sessions.</p>
          <ButtonLink href="/login" className="mt-5">
            Login to save offers
          </ButtonLink>
        </section>
        <section className="mt-8">
          <h2 className="text-2xl font-black">Popular right now</h2>
          <div className="mt-4 grid grid-cols-[repeat(auto-fill,minmax(260px,1fr))] gap-4">
            {preview.map((offer) => (
              <DealCard key={offer.id} offer={offer} compact />
            ))}
          </div>
        </section>
      </main>
    </PublicShell>
  );
}

