import { AnalyticsCard } from "@/components/dashboard/analytics-card";
import { DashboardShell } from "@/components/layout/dashboard-shell";
import { getBusinessMetrics, getPublicOffers } from "@/lib/deals";

export default function BusinessAnalyticsPage() {
  const metrics = getBusinessMetrics();
  const offers = getPublicOffers().slice(0, 5);

  return (
    <DashboardShell area="business" title="Analytics" description="Track offer views, saves, calls, WhatsApp taps, directions, show-offer taps, and map pin taps.">
      <div className="grid gap-4 md:grid-cols-4">
        {metrics.map((metric) => (
          <AnalyticsCard key={metric.label} metric={metric} />
        ))}
      </div>
      <section className="mt-6 rounded-3xl bg-white p-5 shadow-sm">
        <h2 className="text-xl font-black">Offer performance</h2>
        <div className="mt-4 grid gap-3">
          {offers.map((offer, index) => (
            <div key={offer.id} className="grid gap-2 rounded-2xl bg-cream p-4 md:grid-cols-[1fr_repeat(4,110px)] md:items-center">
              <span className="font-bold">{offer.title}</span>
              <span className="text-sm font-semibold text-muted">Views {(320 - index * 24).toLocaleString("en-CA")}</span>
              <span className="text-sm font-semibold text-muted">Saves {48 - index * 3}</span>
              <span className="text-sm font-semibold text-muted">Calls {22 - index}</span>
              <span className="text-sm font-semibold text-muted">Show {36 - index * 2}</span>
            </div>
          ))}
        </div>
      </section>
    </DashboardShell>
  );
}
