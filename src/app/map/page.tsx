import type { Metadata } from "next";
import { MapView } from "@/components/map/map-view";
import { PublicShell } from "@/components/layout/public-shell";
import { getPublicOffers } from "@/lib/deals";

export const metadata: Metadata = {
  title: "Map",
  description: "See nearby DealNear offers on a Mapbox map.",
};

export default function MapPage() {
  const offers = getPublicOffers({ sort: "nearby" });

  return (
    <PublicShell>
      <main className="mx-auto max-w-7xl px-4 py-6">
        <div className="mb-5">
          <p className="text-xs font-black uppercase text-maple">Map</p>
          <h1 className="text-3xl font-black md:text-5xl">Offers around you</h1>
          <p className="mt-2 max-w-2xl text-muted">Pins show approved active deals. Tap a pin to preview the offer.</p>
        </div>
        <MapView offers={offers} />
      </main>
    </PublicShell>
  );
}

