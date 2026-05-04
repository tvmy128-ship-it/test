"use client";

import { ImagePlus, X } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/utils";

export function ImageUploader({
  label,
  limitLabel,
  multiple,
  className,
}: {
  label: string;
  limitLabel: string;
  multiple?: boolean;
  className?: string;
}) {
  const [fileNames, setFileNames] = useState<string[]>([]);

  return (
    <label className={cn("block rounded-3xl border border-dashed border-border bg-cream p-4", className)}>
      <span className="flex items-center gap-3">
        <span className="flex h-11 w-11 items-center justify-center rounded-2xl bg-white text-maple">
          <ImagePlus className="h-5 w-5" aria-hidden="true" />
        </span>
        <span>
          <span className="block text-sm font-extrabold">{label}</span>
          <span className="block text-xs font-semibold text-muted">{limitLabel}</span>
        </span>
      </span>
      <input
        type="file"
        accept="image/png,image/jpeg,image/webp"
        multiple={multiple}
        className="sr-only"
        onChange={(event) => {
          const names = Array.from(event.currentTarget.files ?? []).map((file) => file.name);
          setFileNames(names);
        }}
      />
      {fileNames.length > 0 ? (
        <span className="mt-3 flex flex-wrap gap-2">
          {fileNames.map((name) => (
            <span key={name} className="inline-flex items-center gap-1 rounded-full bg-white px-2.5 py-1 text-xs font-bold text-muted">
              {name}
              <X className="h-3 w-3" />
            </span>
          ))}
        </span>
      ) : null}
    </label>
  );
}

