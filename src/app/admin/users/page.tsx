import { ShieldCheck, Store, UserRound } from "lucide-react";
import { AdminStatusBadge } from "@/components/dashboard/admin-status-badge";
import { DashboardShell } from "@/components/layout/dashboard-shell";

const users = [
  { email: "admin@dealnear.local", role: "admin", icon: ShieldCheck },
  { email: "owner@dealnear.local", role: "business_owner", icon: Store },
  { email: "customer@dealnear.local", role: "customer", icon: UserRound },
];

export default function AdminUsersPage() {
  return (
    <DashboardShell area="admin" title="Users" description="Manage customer, business owner, and admin roles.">
      <div className="grid gap-3">
        {users.map((user) => (
          <article key={user.email} className="flex flex-col gap-3 rounded-3xl bg-white p-4 shadow-sm md:flex-row md:items-center md:justify-between">
            <div className="flex items-center gap-3">
              <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-cream text-maple">
                <user.icon className="h-5 w-5" />
              </div>
              <div>
                <h2 className="font-black">{user.email}</h2>
                <p className="text-sm text-muted">Active platform account</p>
              </div>
            </div>
            <AdminStatusBadge status={user.role} />
          </article>
        ))}
      </div>
    </DashboardShell>
  );
}
