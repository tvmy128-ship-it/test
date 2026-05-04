import Image from "next/image";
import { BadgeCheck, Clock, MapPin, Phone } from "lucide-react";
import type { Business } from "@/types/dealnear";
import { isBusinessOpenNow } from "@/lib/deals";

export function BusinessHeader({ business }: { business: Business }) {
  const open = isBusinessOpenNow(business);

  return (
    <section className="overflow-hidden rounded-[2rem] bg-white shadow-sm">
      <div className="relative h-56 md:h-72">
        <Image src={business.coverUrl} alt={`${business.name} cover`} fill sizes="100vw" className="object-cover" priority />
        <div className="absolute inset-0 bg-gradient-to-t from-black/55 to-transparent" />
        <div className="absolute bottom-4 left-4 right-4 flex items-end gap-4">
          <div className="relative h-20 w-20 shrink-0 overflow-hidden rounded-3xl border-4 border-white bg-white">
            <Image src={business.logoUrl} alt={`${business.name} logo`} fill sizes="80px" className="object-cover" />
          </div>
          <div className="min-w-0 text-white">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-2xl font-black md:text-4xl">{business.name}</h1>
              {business.isVerified ? (
                <span className="inline-flex items-center gap-1 rounded-full bg-white px-2.5 py-1 text-xs font-black text-maple">
                  <BadgeCheck className="h-3.5 w-3.5" />
                  Verified
                </span>
              ) : null}
            </div>
            <p className="mt-1 line-clamp-2 max-w-2xl text-sm font-medium text-white/90">{business.description}</p>
          </div>
        </div>
      </div>
      <div className="grid gap-3 p-4 text-sm font-semibold text-muted md:grid-cols-3">
        <span className="flex items-center gap-2">
          <MapPin className="h-4 w-4 text-maple" />
          {business.address}
        </span>
        <span className="flex items-center gap-2">
          <Phone className="h-4 w-4 text-maple" />
          {business.phone}
        </span>
        <span className="flex items-center gap-2">
          <Clock className="h-4 w-4 text-maple" />
          {open ? "Open now" : "Closed now"}
        </span>
      </div>
    </section>
  );
}

