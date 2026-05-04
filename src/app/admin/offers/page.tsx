import { AdminStatusBadge } from "@/components/dashboard/admin-status-badge";
import { ApprovalActions } from "@/components/dashboard/approval-actions";
import { DashboardShell } from "@/components/layout/dashboard-shell";
import { getPublicOffers } from "@/lib/deals";

export default function AdminOffersPage() {
  const offers = getPublicOffers();

  return (
    <DashboardShell area="admin" title="Offers" description="Approve, reject, edit, remove, feature, boost, or hide bad offers.">
      <div className="grid gap-3">
        {offers.map((offer, index) => (
          <article key={offer.id} className="rounded-3xl bg-white p-4 shadow-sm">
            <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
              <div>
                <div className="flex flex-wrap gap-2">
                  <AdminStatusBadge status={index < 3 ? "pending" : offer.approvalStatus} />
                  <AdminStatusBadge status={offer.status} />
                  {offer.isFeatured ? <span className="rounded-full bg-foreground px-2.5 py-1 text-xs font-black text-white">Featured</span> : null}
                  {offer.isBoosted ? <span className="rounded-full bg-orange/10 px-2.5 py-1 text-xs font-black text-[#8a3a0a]">Boosted</span> : null}
                </div>
                <h2 className="mt-2 text-xl font-black">{offer.title}</h2>
                <p className="text-sm text-muted">{offer.business.name} · expires {offer.endDate}</p>
              </div>
              <ApprovalActions />
            </div>
          </article>
        ))}
      </div>
    </DashboardShell>
  );
}

