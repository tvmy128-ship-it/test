import { Plus, Store } from "lucide-react";
import { AnalyticsCard } from "@/components/dashboard/analytics-card";
import { AdminStatusBadge } from "@/components/dashboard/admin-status-badge";
import { DashboardShell } from "@/components/layout/dashboard-shell";
import { ButtonLink } from "@/components/ui/button";
import { getBusinessMetrics, getPublicOffers } from "@/lib/deals";

export default function BusinessDashboardPage() {
  const metrics = getBusinessMetrics();
  const offers = getPublicOffers().slice(0, 5);

  return (
    <DashboardShell area="business" title="Business overview" description="Manage your profile, active offers, boost requests, and simple analytics from one web dashboard.">
      <div className="grid gap-4 md:grid-cols-4">
        {metrics.map((metric) => (
          <AnalyticsCard key={metric.label} metric={metric} />
        ))}
      </div>
      <section className="mt-6 rounded-3xl bg-white p-5 shadow-sm">
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <h2 className="text-xl font-black">Recent offers</h2>
            <p className="mt-1 text-sm text-muted">New and edited offers stay pending until admin approval.</p>
          </div>
          <ButtonLink href="/business-dashboard/offers/new">
            <Plus className="h-4 w-4" />
            New offer
          </ButtonLink>
        </div>
        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[700px] text-left text-sm">
            <thead className="text-xs uppercase text-muted">
              <tr>
                <th className="py-3">Offer</th>
                <th>Business</th>
                <th>Expiry</th>
                <th>Status</th>
                <th>Approval</th>
              </tr>
            </thead>
            <tbody>
              {offers.map((offer) => (
                <tr key={offer.id} className="border-t border-border">
                  <td className="py-4 font-bold">{offer.title}</td>
                  <td>{offer.business.name}</td>
                  <td>{offer.endDate}</td>
                  <td><AdminStatusBadge status={offer.status} /></td>
                  <td><AdminStatusBadge status={offer.approvalStatus} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <section className="mt-6 grid gap-4 md:grid-cols-2">
        <div className="rounded-3xl bg-white p-5 shadow-sm">
          <Store className="h-6 w-6 text-maple" />
          <h2 className="mt-3 text-xl font-black">Profile approval</h2>
          <p className="mt-2 text-sm leading-6 text-muted">Public visibility requires approved business details, approved images, and a valid location.</p>
          <ButtonLink href="/business-dashboard/profile" className="mt-4" variant="outline">
            Edit profile
          </ButtonLink>
        </div>
        <div className="rounded-3xl bg-maple p-5 text-white shadow-sm">
          <h2 className="text-xl font-black">Boost placement</h2>
          <p className="mt-2 text-sm leading-6 text-white/85">Request homepage, category, featured, map pin, or future push notification promotion. Admin approval keeps placements curated.</p>
          <ButtonLink href="/business-dashboard/boosts" className="mt-4 bg-white text-maple hover:bg-cream" variant="ghost">
            Request boost
          </ButtonLink>
        </div>
      </section>
    </DashboardShell>
  );
}
