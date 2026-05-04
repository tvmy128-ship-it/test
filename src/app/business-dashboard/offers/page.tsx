import { Edit3, Pause, Plus, Trash2 } from "lucide-react";
import { AdminStatusBadge } from "@/components/dashboard/admin-status-badge";
import { DashboardShell } from "@/components/layout/dashboard-shell";
import { ButtonLink } from "@/components/ui/button";
import { getPublicOffers } from "@/lib/deals";

export default function BusinessOffersPage() {
  const offers = getPublicOffers();

  return (
    <DashboardShell area="business" title="Offers" description="Create, edit, pause, delete, and review approval status for your active offers.">
      <div className="mb-4 flex justify-end">
        <ButtonLink href="/business-dashboard/offers/new">
          <Plus className="h-4 w-4" />
          New offer
        </ButtonLink>
      </div>
      <div className="grid gap-3">
        {offers.map((offer) => (
          <article key={offer.id} className="rounded-3xl bg-white p-4 shadow-sm">
            <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
              <div>
                <div className="flex flex-wrap gap-2">
                  <AdminStatusBadge status={offer.status} />
                  <AdminStatusBadge status={offer.approvalStatus} />
                  {offer.isFeatured ? <span className="rounded-full bg-foreground px-2.5 py-1 text-xs font-black text-white">Featured</span> : null}
                </div>
                <h2 className="mt-2 text-xl font-black">{offer.title}</h2>
                <p className="mt-1 text-sm text-muted">{offer.discountValue} · expires {offer.endDate}</p>
              </div>
              <div className="flex flex-wrap gap-2">
                <ButtonLink href={`/business-dashboard/offers/${offer.id}/edit`} variant="outline" size="sm">
                  <Edit3 className="h-4 w-4" />
                  Edit
                </ButtonLink>
                <button className="inline-flex h-9 items-center gap-2 rounded-xl bg-cream px-3 text-sm font-bold text-muted">
                  <Pause className="h-4 w-4" />
                  Pause
                </button>
                <button className="inline-flex h-9 items-center gap-2 rounded-xl bg-maple/10 px-3 text-sm font-bold text-maple">
                  <Trash2 className="h-4 w-4" />
                  Delete
                </button>
              </div>
            </div>
          </article>
        ))}
      </div>
    </DashboardShell>
  );
}

