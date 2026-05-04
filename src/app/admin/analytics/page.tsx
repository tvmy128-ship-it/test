import { AnalyticsCard } from "@/components/dashboard/analytics-card";
import { DashboardShell } from "@/components/layout/dashboard-shell";
import { getAdminStats, getBusinessMetrics } from "@/lib/deals";

export default function AdminAnalyticsPage() {
  const stats = getAdminStats();
  const platform = [
    { label: "Total users", value: stats.totalUsers, change: "+22%" },
    { label: "Active offers", value: stats.activeOffers, change: "+11%" },
    { label: "Open reports", value: stats.openReports, change: "Review" },
    { label: "Active boosts", value: stats.activeBoosts, change: "Live" },
  ];

  return (
    <DashboardShell area="admin" title="Platform analytics" description="High-level moderation and performance metrics for DealNear.">
      <div className="grid gap-4 md:grid-cols-4">
        {platform.map((metric) => (
          <AnalyticsCard key={metric.label} metric={metric} />
        ))}
      </div>
      <div className="mt-4 grid gap-4 md:grid-cols-4">
        {getBusinessMetrics().map((metric) => (
          <AnalyticsCard key={metric.label} metric={metric} />
        ))}
      </div>
    </DashboardShell>
  );
}

