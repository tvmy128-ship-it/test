import { AdminStatusBadge } from "@/components/dashboard/admin-status-badge";
import { ApprovalActions } from "@/components/dashboard/approval-actions";
import { DashboardShell } from "@/components/layout/dashboard-shell";
import { businesses } from "@/lib/demo-data";

export default function AdminBusinessesPage() {
  return (
    <DashboardShell area="admin" title="Businesses" description="Approve, reject, verify, edit, remove, and block businesses before they appear publicly.">
      <div className="grid gap-3">
        {businesses.map((business, index) => (
          <article key={business.id} className="rounded-3xl bg-white p-4 shadow-sm">
            <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
              <div>
                <div className="flex flex-wrap gap-2">
                  <AdminStatusBadge status={index < 2 ? "pending" : business.approvalStatus} />
                  {business.isVerified ? <span className="rounded-full bg-emerald-50 px-2.5 py-1 text-xs font-black text-emerald-700">Verified</span> : null}
                </div>
                <h2 className="mt-2 text-xl font-black">{business.name}</h2>
                <p className="text-sm text-muted">{business.address}</p>
              </div>
              <ApprovalActions />
            </div>
          </article>
        ))}
      </div>
    </DashboardShell>
  );
}

