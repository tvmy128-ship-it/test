import { notFound } from "next/navigation";
import { OfferForm } from "@/components/forms/offer-form";
import { DashboardShell } from "@/components/layout/dashboard-shell";
import { categories, offers } from "@/lib/demo-data";

type Params = Promise<{ id: string }>;

export default async function EditOfferPage({ params }: { params: Params }) {
  const { id } = await params;
  const offer = offers.find((item) => item.id === id);
  if (!offer) notFound();

  return (
    <DashboardShell area="business" title="Edit offer" description="Editing a public offer can reset approval to pending so admins can review changes.">
      <OfferForm offer={offer} categories={categories} />
    </DashboardShell>
  );
}

