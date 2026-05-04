import { Plus } from "lucide-react";
import { CategoryIcon } from "@/components/ui/category-icon";
import { DashboardShell } from "@/components/layout/dashboard-shell";
import { Button } from "@/components/ui/button";
import { categories } from "@/lib/demo-data";

export default function AdminCategoriesPage() {
  return (
    <DashboardShell area="admin" title="Categories" description="Manage active categories and sponsored category spots.">
      <section className="rounded-3xl bg-white p-5 shadow-sm">
        <div className="flex items-center justify-between">
          <h2 className="text-xl font-black">Active categories</h2>
          <Button type="button" variant="outline">
            <Plus className="h-4 w-4" />
            Add
          </Button>
        </div>
        <div className="mt-4 grid gap-3 md:grid-cols-2">
          {categories.map((category) => (
            <div key={category.id} className="flex items-center justify-between rounded-2xl bg-cream p-4">
              <div className="flex items-center gap-3">
                <CategoryIcon name={category.icon} className="h-5 w-5 text-maple" />
                <span className="font-bold">{category.name}</span>
              </div>
              <span className="text-sm font-semibold text-muted">Sort {category.sortOrder}</span>
            </div>
          ))}
        </div>
      </section>
    </DashboardShell>
  );
}

