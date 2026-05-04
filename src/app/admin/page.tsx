import { AnalyticsCard } from "@/components/dashboard/analytics-card";
import { AdminStatusBadge } from "@/components/dashboard/admin-status-badge";
import { ApprovalActions } from "@/components/dashboard/approval-actions";
import { DashboardShell } from "@/components/layout/dashboard-shell";
import { getAdminStats, getBusinessMetrics, getPublicOffers } from "@/lib/deals";

export default function AdminDashboardPage() {
  const stats = getAdminStats();
  const metrics = [
    { label: "Pending businesses", value: stats.pendingBusinesses, change: "Needs review" },
    { label: "Pending offers", value: stats.pendingOffers, change: "Needs review" },
    { label: "Open reports", value: stats.openReports, change: "Moderation" },
    { label: "Active boosts", value: stats.activeBoosts, change: "Live" },
  ];
  const offers = getPublicOffers().slice(0, 5);

  return (
    <DashboardShell area="admin" title="Admin overview" description="Approve businesses and offers, manage reports, placements, categories, cities, users, and analytics.">
      <div className="grid gap-4 md:grid-cols-4">
        {metrics.map((metric) => (
          <AnalyticsCard key={metric.label} metric={metric} />
        ))}
      </div>
      <section className="mt-6 rounded-3xl bg-white p-5 shadow-sm">
        <h2 className="text-xl font-black">Approval queue</h2>
        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[760px] text-left text-sm">
            <thead className="text-xs uppercase text-muted">
              <tr>
                <th className="py-3">Item</th>
                <th>Business</th>
                <th>Status</th>
                <th>Flags</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {offers.map((offer) => (
                <tr key={offer.id} className="border-t border-border">
                  <td className="py-4 font-bold">{offer.title}</td>
                  <td>{offer.business.name}</td>
                  <td><AdminStatusBadge status="pending" /></td>
                  <td>{offer.isBoosted ? "Boost request" : "Standard"}</td>
                  <td><ApprovalActions /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <section className="mt-6 grid gap-4 md:grid-cols-4">
        {getBusinessMetrics().map((metric) => (
          <AnalyticsCard key={metric.label} metric={metric} />
        ))}
      </section>
    </DashboardShell>
  );
}

