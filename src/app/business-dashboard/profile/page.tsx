import { BusinessForm } from "@/components/forms/business-form";
import { DashboardShell } from "@/components/layout/dashboard-shell";
import { businesses, categories, cities } from "@/lib/demo-data";

export default function BusinessProfileDashboardPage() {
  return (
    <DashboardShell area="business" title="Business profile" description="Update your customer-facing listing, location, contact details, and image assets.">
      <BusinessForm business={businesses[0]} categories={categories} cities={cities} />
    </DashboardShell>
  );
}

