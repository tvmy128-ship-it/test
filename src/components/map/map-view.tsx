"use client";

import mapboxgl from "mapbox-gl";
import { List, LocateFixed, MapPinned } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { DealCard } from "@/components/deal/deal-card";
import { DealPreviewCard } from "@/components/deal/deal-preview-card";
import { trackOfferEvent } from "@/components/deal/tracked-actions";
import { Button } from "@/components/ui/button";
import type { EnrichedOffer } from "@/types/dealnear";

export function MapView({ offers }: { offers: EnrichedOffer[] }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<mapboxgl.Map | null>(null);
  const [selected, setSelected] = useState<EnrichedOffer | null>(offers[0] ?? null);
  const [mode, setMode] = useState<"map" | "list">("map");
  const token = process.env.NEXT_PUBLIC_MAPBOX_TOKEN;

  useEffect(() => {
    if (!token || !containerRef.current || mapRef.current) return;

    mapboxgl.accessToken = token;
    const map = new mapboxgl.Map({
      container: containerRef.current,
      style: "mapbox://styles/mapbox/light-v11",
      center: [-79.3832, 43.6532],
      zoom: 11.5,
      attributionControl: false,
    });

    map.addControl(new mapboxgl.NavigationControl({ showCompass: false }), "top-right");
    mapRef.current = map;

    const markers = offers.map((offer) => {
      const markerElement = document.createElement("button");
      markerElement.type = "button";
      markerElement.className =
        "flex h-10 min-w-10 items-center justify-center rounded-full border-2 border-white bg-maple px-2 text-xs font-black text-white shadow-lg";
      markerElement.textContent = offer.discountValue;
      markerElement.addEventListener("click", () => {
        setSelected(offer);
        void trackOfferEvent(offer, "map_pin_tap");
      });

      return new mapboxgl.Marker({ element: markerElement, anchor: "bottom" })
        .setLngLat([offer.business.longitude, offer.business.latitude])
        .addTo(map);
    });

    return () => {
      markers.forEach((marker) => marker.remove());
      map.remove();
      mapRef.current = null;
    };
  }, [offers, token]);

  if (!token) {
    return (
      <div className="grid gap-5">
        <div className="relative min-h-[520px] overflow-hidden rounded-[2rem] border border-border bg-white shadow-sm">
          <div className="absolute inset-0 bg-[radial-gradient(circle_at_22%_18%,rgba(255,138,61,0.22),transparent_24%),radial-gradient(circle_at_70%_62%,rgba(215,38,56,0.16),transparent_26%),linear-gradient(135deg,#fff8f3,#ffffff)]" />
          <div className="absolute inset-0 opacity-60 [background-image:linear-gradient(#f0ded3_1px,transparent_1px),linear-gradient(90deg,#f0ded3_1px,transparent_1px)] [background-size:72px_72px]" />
          <div className="absolute left-4 right-4 top-4 rounded-2xl bg-white/95 px-4 py-3 shadow-sm backdrop-blur md:right-auto">
            <p className="text-xs font-bold uppercase text-maple">Toronto deal map</p>
            <h2 className="text-base font-extrabold">{offers.length} approved offers nearby</h2>
          </div>
          {offers.slice(0, 7).map((offer, index) => {
            const positions = [
              ["22%", "30%"],
              ["52%", "24%"],
              ["73%", "42%"],
              ["37%", "56%"],
              ["64%", "68%"],
              ["18%", "72%"],
              ["82%", "76%"],
            ];
            const [left, top] = positions[index] ?? ["50%", "50%"];
            return (
              <button
                key={offer.id}
                type="button"
                className="absolute flex h-11 min-w-11 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border-2 border-white bg-maple px-2 text-xs font-black text-white shadow-xl transition hover:scale-105"
                style={{ left, top }}
                onClick={() => {
                  setSelected(offer);
                  void trackOfferEvent(offer, "map_pin_tap");
                }}
                aria-label={`Preview ${offer.title}`}
              >
                {offer.discountValue}
              </button>
            );
          })}
          <div className="absolute bottom-4 left-4 right-4 md:left-auto md:w-[320px]">
            {selected ? <DealPreviewCard offer={selected} /> : null}
          </div>
        </div>
        <div className="flex items-center gap-2 rounded-3xl bg-white p-4 text-sm font-semibold text-muted shadow-sm">
          <LocateFixed className="h-5 w-5 shrink-0 text-maple" />
          Tap any pin to preview approved offers around Toronto, or switch to the list view for quick scanning.
        </div>
      </div>
    );
  }

  if (mode === "list") {
    return (
      <div className="grid gap-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="text-xl font-extrabold">Nearby deal list</h2>
          </div>
          <Button type="button" variant="outline" onClick={() => setMode("map")}>
            <MapPinned className="h-4 w-4" />
            Map
          </Button>
        </div>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {offers.map((offer) => (
            <DealCard key={offer.id} offer={offer} compact />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="relative min-h-[640px] overflow-hidden rounded-[2rem] border border-border bg-white shadow-sm">
      <div ref={containerRef} className="absolute inset-0" />
      <div className="absolute left-4 right-4 top-4 flex items-center justify-between gap-3">
        <div className="rounded-2xl bg-white/95 px-4 py-3 shadow-sm backdrop-blur">
          <p className="text-xs font-bold uppercase text-maple">Map view</p>
          <h2 className="text-base font-extrabold">{offers.length} approved offers nearby</h2>
        </div>
        <Button type="button" variant="dark" onClick={() => setMode("list")}>
          <List className="h-4 w-4" />
          List
        </Button>
      </div>
      {selected ? (
        <div className="absolute bottom-4 left-4 right-4 md:left-auto md:w-[320px]">
          <DealPreviewCard offer={selected} />
        </div>
      ) : null}
    </div>
  );
}
