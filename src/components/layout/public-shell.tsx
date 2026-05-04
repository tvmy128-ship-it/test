import Link from "next/link";
import { Heart, Home, LogIn, MapPinned, Search, Store } from "lucide-react";
import { ButtonLink } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const navItems = [
  { href: "/", label: "Home", icon: Home },
  { href: "/deals", label: "Search", icon: Search },
  { href: "/map", label: "Map", icon: MapPinned },
  { href: "/saved", label: "Saved", icon: Heart },
];

export function PublicShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-cream pb-20 md:pb-0">
      <header className="sticky top-0 z-30 border-b border-border/80 bg-cream/90 backdrop-blur-xl">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-3">
          <Link href="/" className="flex items-center gap-2">
            <span className="flex h-10 w-10 items-center justify-center rounded-2xl bg-maple text-lg font-black text-white">D</span>
            <span>
              <span className="block text-lg font-black leading-none">DealNear</span>
              <span className="block text-xs font-bold text-muted">Toronto deals</span>
            </span>
          </Link>
          <nav className="hidden items-center gap-1 md:flex">
            {navItems.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className="rounded-full px-4 py-2 text-sm font-bold text-muted transition hover:bg-white hover:text-maple"
              >
                {item.label}
              </Link>
            ))}
          </nav>
          <div className="hidden items-center gap-2 md:flex">
            <ButtonLink href="/business-dashboard" variant="outline">
              <Store className="h-4 w-4" />
              For businesses
            </ButtonLink>
            <ButtonLink href="/login" variant="dark">
              <LogIn className="h-4 w-4" />
              Login
            </ButtonLink>
          </div>
          <ButtonLink href="/login" variant="outline" className="md:hidden">
            Login
          </ButtonLink>
        </div>
      </header>
      {children}
      <nav className="fixed inset-x-3 bottom-3 z-40 grid grid-cols-4 rounded-[1.5rem] border border-border bg-white/95 p-2 shadow-2xl shadow-neutral-900/15 backdrop-blur md:hidden">
        {navItems.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            className={cn("flex flex-col items-center gap-1 rounded-2xl px-2 py-2 text-xs font-bold text-muted hover:bg-cream hover:text-maple")}
          >
            <item.icon className="h-5 w-5" aria-hidden="true" />
            {item.label}
          </Link>
        ))}
      </nav>
    </div>
  );
}
