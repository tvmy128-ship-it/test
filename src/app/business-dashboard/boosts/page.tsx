import { Megaphone } from "lucide-react";
import { AdminStatusBadge } from "@/components/dashboard/admin-status-badge";
import { DashboardShell } from "@/components/layout/dashboard-shell";
import { Button } from "@/components/ui/button";
import { getPublicOffers } from "@/lib/deals";

export default function BusinessBoostsPage() {
  const offers = getPublicOffers().slice(0, 4);

  return (
    <DashboardShell area="business" title="Boost requests" description="Request admin-approved promotional placements for homepage, category, featured, and map visibility.">
      <section className="rounded-3xl bg-white p-5 shadow-sm">
        <div className="flex items-center gap-2">
          <Megaphone className="h-5 w-5 text-maple" />
          <h2 className="text-xl font-black">Request placement</h2>
        </div>
        <form className="mt-5 grid gap-4 md:grid-cols-2">
          <label className="grid gap-2 text-sm font-bold">
            Offer
            <select className="form-input">
              {offers.map((offer) => (
                <option key={offer.id}>{offer.title}</option>
              ))}
            </select>
          </label>
          <label className="grid gap-2 text-sm font-bold">
            Placement
            <select className="form-input">
              <option>Homepage</option>
              <option>Category</option>
              <option>Map pin</option>
              <option>Featured</option>
              <option>Push notification later</option>
            </select>
          </label>
          <label className="grid gap-2 text-sm font-bold">
            Start date
            <input type="date" className="form-input" defaultValue="2026-05-10" />
          </label>
          <label className="grid gap-2 text-sm font-bold">
            End date
            <input type="date" className="form-input" defaultValue="2026-05-17" />
          </label>
          <Button type="button" className="md:w-fit">
            Submit request
          </Button>
        </form>
      </section>
      <section className="mt-6 rounded-3xl bg-white p-5 shadow-sm">
        <h2 className="text-xl font-black">Current requests</h2>
        <div className="mt-4 grid gap-3">
          {offers.map((offer, index) => (
            <div key={offer.id} className="flex flex-col gap-2 rounded-2xl bg-cream p-4 md:flex-row md:items-center md:justify-between">
              <div>
                <p className="font-bold">{offer.title}</p>
                <p className="text-sm text-muted">{index % 2 ? "Category placement" : "Promoted map pin"}</p>
              </div>
              <AdminStatusBadge status={index === 0 ? "approved" : "pending"} />
            </div>
          ))}
        </div>
      </section>
    </DashboardShell>
  );
}
