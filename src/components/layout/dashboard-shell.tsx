import Link from "next/link";
import { BarChart3, BriefcaseBusiness, LayoutDashboard, MapPin, Megaphone, Plus, ShieldCheck, Siren, Sparkles, Tags, Users } from "lucide-react";
import { ButtonLink } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const businessNav = [
  { href: "/business-dashboard", label: "Overview", icon: LayoutDashboard },
  { href: "/business-dashboard/profile", label: "Profile", icon: BriefcaseBusiness },
  { href: "/business-dashboard/offers", label: "Offers", icon: Tags },
  { href: "/business-dashboard/analytics", label: "Analytics", icon: BarChart3 },
  { href: "/business-dashboard/boosts", label: "Boosts", icon: Megaphone },
];

const adminNav = [
  { href: "/admin", label: "Overview", icon: ShieldCheck },
  { href: "/admin/businesses", label: "Businesses", icon: BriefcaseBusiness },
  { href: "/admin/offers", label: "Offers", icon: Tags },
  { href: "/admin/categories", label: "Categories", icon: Tags },
  { href: "/admin/cities", label: "Cities", icon: MapPin },
  { href: "/admin/reports", label: "Reports", icon: Siren },
  { href: "/admin/featured", label: "Featured", icon: Sparkles },
  { href: "/admin/boosts", label: "Boosts", icon: Megaphone },
  { href: "/admin/users", label: "Users", icon: Users },
  { href: "/admin/analytics", label: "Analytics", icon: BarChart3 },
];

export function DashboardShell({
  area,
  title,
  description,
  children,
}: {
  area: "business" | "admin";
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  const nav = area === "business" ? businessNav : adminNav;

  return (
    <div className="min-h-screen bg-cream">
      <aside className="fixed inset-y-0 left-0 hidden w-72 border-r border-border bg-white p-5 lg:block">
        <Link href="/" className="flex items-center gap-2">
          <span className="flex h-10 w-10 items-center justify-center rounded-2xl bg-maple text-lg font-black text-white">D</span>
          <span className="text-lg font-black">DealNear</span>
        </Link>
        <nav className="mt-8 grid gap-2">
          {nav.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={cn("flex items-center gap-3 rounded-2xl px-4 py-3 text-sm font-bold text-muted hover:bg-cream hover:text-maple")}
            >
              <item.icon className="h-5 w-5" aria-hidden="true" />
              {item.label}
            </Link>
          ))}
        </nav>
        {area === "business" ? (
          <ButtonLink href="/business-dashboard/offers/new" className="mt-6 w-full" variant="primary">
            <Plus className="h-4 w-4" />
            New offer
          </ButtonLink>
        ) : null}
      </aside>
      <main className="lg:pl-72">
        <header className="sticky top-0 z-20 border-b border-border bg-cream/90 backdrop-blur">
          <div className="mx-auto max-w-6xl px-4 py-4">
            <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
              <div>
                <p className="text-xs font-black uppercase text-maple">{area === "business" ? "Business dashboard" : "Admin dashboard"}</p>
                <h1 className="text-2xl font-black md:text-3xl">{title}</h1>
                <p className="mt-1 max-w-2xl text-sm leading-6 text-muted">{description}</p>
              </div>
              <ButtonLink href="/" variant="outline">
                Public app
              </ButtonLink>
            </div>
            <nav className="mt-4 flex gap-2 overflow-x-auto lg:hidden">
              {nav.map((item) => (
                <Link key={item.href} href={item.href} className="inline-flex shrink-0 items-center gap-2 rounded-full bg-white px-3 py-2 text-sm font-bold text-muted">
                  <item.icon className="h-4 w-4" aria-hidden="true" />
                  {item.label}
                </Link>
              ))}
            </nav>
          </div>
        </header>
        <div className="mx-auto max-w-6xl px-4 py-6">{children}</div>
      </main>
    </div>
  );
}
