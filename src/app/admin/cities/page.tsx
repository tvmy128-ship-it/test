import { MapPin, Plus } from "lucide-react";
import { DashboardShell } from "@/components/layout/dashboard-shell";
import { Button } from "@/components/ui/button";
import { cities } from "@/lib/demo-data";

export default function AdminCitiesPage() {
  return (
    <DashboardShell area="admin" title="Cities and areas" description="Manage city fallback locations when users deny geolocation.">
      <section className="rounded-3xl bg-white p-5 shadow-sm">
        <div className="flex items-center justify-between">
          <h2 className="text-xl font-black">Cities</h2>
          <Button type="button" variant="outline">
            <Plus className="h-4 w-4" />
            Add
          </Button>
        </div>
        <div className="mt-4 grid gap-3">
          {cities.map((city) => (
            <div key={city.id} className="flex items-center justify-between rounded-2xl bg-cream p-4">
              <div className="flex items-center gap-3">
                <MapPin className="h-5 w-5 text-maple" />
                <span className="font-bold">{city.name}, {city.province}</span>
              </div>
              <span className="text-sm font-semibold text-muted">{city.latitude}, {city.longitude}</span>
            </div>
          ))}
        </div>
      </section>
    </DashboardShell>
  );
}

