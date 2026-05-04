import { Sparkles } from "lucide-react";
import { DashboardShell } from "@/components/layout/dashboard-shell";
import { Button } from "@/components/ui/button";
import { getPublicOffers } from "@/lib/deals";

export default function AdminFeaturedPage() {
  const offers = getPublicOffers().filter((offer) => offer.isFeatured || offer.isBoosted);

  return (
    <DashboardShell area="admin" title="Featured placements" description="Manage homepage, category, featured, and promoted map pin placements.">
      <section className="rounded-3xl bg-white p-5 shadow-sm">
        <div className="flex items-center gap-2">
          <Sparkles className="h-5 w-5 text-maple" />
          <h2 className="text-xl font-black">Live placements</h2>
        </div>
        <div className="mt-4 grid gap-3">
          {offers.map((offer) => (
            <div key={offer.id} className="flex flex-col gap-2 rounded-2xl bg-cream p-4 md:flex-row md:items-center md:justify-between">
              <div>
                <p className="font-bold">{offer.title}</p>
                <p className="text-sm text-muted">{offer.isFeatured ? "Featured homepage" : "Promoted map pin"}</p>
              </div>
              <Button type="button" variant="outline" size="sm">Manage</Button>
            </div>
          ))}
        </div>
      </section>
    </DashboardShell>
  );
}

