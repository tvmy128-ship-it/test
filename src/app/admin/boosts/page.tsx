import { Megaphone } from "lucide-react";
import { AdminStatusBadge } from "@/components/dashboard/admin-status-badge";
import { ApprovalActions } from "@/components/dashboard/approval-actions";
import { DashboardShell } from "@/components/layout/dashboard-shell";
import { getPublicOffers } from "@/lib/deals";

export default function AdminBoostsPage() {
  const offers = getPublicOffers().slice(0, 6);

  return (
    <DashboardShell area="admin" title="Boost requests" description="Review sponsored placement requests before they appear in premium positions.">
      <div className="grid gap-3">
        {offers.map((offer, index) => (
          <article key={offer.id} className="rounded-3xl bg-white p-4 shadow-sm">
            <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
              <div>
                <div className="flex items-center gap-2">
                  <Megaphone className="h-5 w-5 text-maple" />
                  <AdminStatusBadge status={index < 2 ? "pending" : "approved"} />
                </div>
                <h2 className="mt-2 text-xl font-black">{offer.title}</h2>
                <p className="text-sm text-muted">{index % 2 ? "Sponsored category spot" : "Promoted map pin"}</p>
              </div>
              <ApprovalActions />
            </div>
          </article>
        ))}
      </div>
    </DashboardShell>
  );
}
