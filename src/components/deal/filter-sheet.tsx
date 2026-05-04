import { getCategories } from "@/lib/deals";
import { Button } from "@/components/ui/button";

export function FilterSheet({
  category,
  distance,
  sort,
  openNow,
}: {
  category?: string;
  distance?: string;
  sort?: string;
  openNow?: string;
}) {
  const categories = getCategories();

  return (
    <details className="rounded-3xl border border-border bg-white p-4 shadow-sm open:shadow-md">
      <summary className="cursor-pointer list-none text-sm font-extrabold">Filters</summary>
      <div className="mt-4 grid gap-4 md:grid-cols-4">
        <label className="grid gap-2 text-sm font-bold">
          Category
          <select name="category" defaultValue={category ?? "all"} className="h-11 rounded-2xl border border-border bg-cream px-3">
            <option value="all">All categories</option>
            {categories.map((item) => (
              <option key={item.id} value={item.slug}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
        <label className="grid gap-2 text-sm font-bold">
          Distance
          <select name="distance" defaultValue={distance ?? "25"} className="h-11 rounded-2xl border border-border bg-cream px-3">
            <option value="5">Within 5 km</option>
            <option value="10">Within 10 km</option>
            <option value="25">Within 25 km</option>
            <option value="50">Within 50 km</option>
          </select>
        </label>
        <label className="grid gap-2 text-sm font-bold">
          Sort
          <select name="sort" defaultValue={sort ?? "nearby"} className="h-11 rounded-2xl border border-border bg-cream px-3">
            <option value="nearby">Nearby first</option>
            <option value="newest">Newest</option>
            <option value="expiring">Expiring soon</option>
            <option value="discount">Highest discount</option>
          </select>
        </label>
        <label className="flex items-end gap-3 rounded-2xl border border-border bg-cream px-3 py-3 text-sm font-bold">
          <input name="openNow" value="true" type="checkbox" defaultChecked={openNow === "true"} className="h-5 w-5 accent-maple" />
          Open now
        </label>
      </div>
      <Button type="submit" className="mt-4 w-full md:w-auto">
        Apply filters
      </Button>
    </details>
  );
}

