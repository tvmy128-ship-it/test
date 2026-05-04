import { Search, SlidersHorizontal } from "lucide-react";
import { Button } from "@/components/ui/button";

export function SearchBar({
  defaultValue = "",
  placeholder = "Search deals or shops",
}: {
  defaultValue?: string;
  placeholder?: string;
}) {
  return (
    <div className="flex w-full items-center gap-2 rounded-3xl border border-border bg-white p-2 shadow-sm">
      <Search className="ml-2 h-5 w-5 shrink-0 text-muted" aria-hidden="true" />
      <input
        name="q"
        defaultValue={defaultValue}
        placeholder={placeholder}
        className="min-w-0 flex-1 bg-transparent px-1 text-base font-medium placeholder:text-muted"
      />
      <Button type="submit" size="icon" aria-label="Search deals">
        <SlidersHorizontal className="h-5 w-5" aria-hidden="true" />
      </Button>
    </div>
  );
}
