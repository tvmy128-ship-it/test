import { Siren } from "lucide-react";
import { AdminStatusBadge } from "@/components/dashboard/admin-status-badge";
import { DashboardShell } from "@/components/layout/dashboard-shell";
import { Button } from "@/components/ui/button";
import { getPublicOffers } from "@/lib/deals";

export default function AdminReportsPage() {
  const offers = getPublicOffers().slice(0, 5);

  return (
    <DashboardShell area="admin" title="Reports" description="Review customer reports for expired offers, refused offers, wrong information, fake offers, and other issues.">
      <div className="grid gap-3">
        {offers.map((offer, index) => (
          <article key={offer.id} className="rounded-3xl bg-white p-4 shadow-sm">
            <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
              <div>
                <div className="flex items-center gap-2">
                  <Siren className="h-5 w-5 text-maple" />
                  <AdminStatusBadge status={index % 2 ? "reviewed" : "open"} />
                </div>
                <h2 className="mt-2 text-xl font-black">{index % 2 ? "Business refused offer" : "Offer expired"}</h2>
                <p className="text-sm text-muted">{offer.title} · {offer.business.name}</p>
              </div>
              <div className="flex gap-2">
                <Button type="button" variant="outline" size="sm">Resolve</Button>
                <Button type="button" variant="ghost" size="sm">Reject</Button>
              </div>
            </div>
          </article>
        ))}
      </div>
    </DashboardShell>
  );
}

