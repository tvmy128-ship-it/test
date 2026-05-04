import { OfferForm } from "@/components/forms/offer-form";
import { DashboardShell } from "@/components/layout/dashboard-shell";
import { categories } from "@/lib/demo-data";

export default function NewOfferPage() {
  return (
    <DashboardShell area="business" title="New offer" description="Create a deal with one main image, dates, terms, pricing, and category.">
      <OfferForm categories={categories} />
    </DashboardShell>
  );
}

