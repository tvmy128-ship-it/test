"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Loader2, Save } from "lucide-react";
import { useState } from "react";
import { useForm } from "react-hook-form";
import type { z } from "zod";
import { ImageUploader } from "@/components/forms/image-uploader";
import { Button } from "@/components/ui/button";
import { businessFormSchema } from "@/lib/validation";
import type { Business, Category, City } from "@/types/dealnear";

type BusinessFormInput = z.input<typeof businessFormSchema>;

export function BusinessForm({
  business,
  categories,
  cities,
}: {
  business?: Business;
  categories: Category[];
  cities: City[];
}) {
  const [status, setStatus] = useState<"idle" | "saving" | "saved">("idle");
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<BusinessFormInput, unknown, z.output<typeof businessFormSchema>>({
    resolver: zodResolver(businessFormSchema),
    defaultValues: {
      name: business?.name ?? "",
      categoryId: business?.categoryId ?? categories[0]?.id,
      description: business?.description ?? "",
      address: business?.address ?? "",
      cityId: business?.cityId ?? cities[0]?.id,
      latitude: business?.latitude ?? cities[0]?.latitude,
      longitude: business?.longitude ?? cities[0]?.longitude,
      phone: business?.phone ?? "",
      whatsapp: business?.whatsapp ?? "",
      website: business?.website ?? "",
    },
  });

  async function onSubmit() {
    setStatus("saving");
    await new Promise((resolve) => setTimeout(resolve, 500));
    setStatus("saved");
  }

  return (
    <form onSubmit={handleSubmit(onSubmit)} className="grid gap-5 rounded-3xl bg-white p-5 shadow-sm">
      <div className="grid gap-4 md:grid-cols-2">
        <Field label="Business name" error={errors.name?.message}>
          <input {...register("name")} className="form-input" />
        </Field>
        <Field label="Category" error={errors.categoryId?.message}>
          <select {...register("categoryId")} className="form-input">
            {categories.map((category) => (
              <option key={category.id} value={category.id}>
                {category.name}
              </option>
            ))}
          </select>
        </Field>
      </div>

      <Field label="Description" error={errors.description?.message}>
        <textarea {...register("description")} rows={4} className="form-input min-h-28" />
      </Field>

      <div className="grid gap-4 md:grid-cols-2">
        <ImageUploader label="Logo" limitLabel="1 logo, PNG/JPG/WebP up to 2 MB" />
        <ImageUploader label="Cover image" limitLabel="1 cover, PNG/JPG/WebP up to 5 MB" />
      </div>
      <ImageUploader label="Gallery photos" limitLabel="3-6 photos, PNG/JPG/WebP up to 5 MB each" multiple />

      <div className="grid gap-4 md:grid-cols-2">
        <Field label="Address" error={errors.address?.message}>
          <input {...register("address")} className="form-input" />
        </Field>
        <Field label="City" error={errors.cityId?.message}>
          <select {...register("cityId")} className="form-input">
            {cities.map((city) => (
              <option key={city.id} value={city.id}>
                {city.name}, {city.province}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Latitude" error={errors.latitude?.message}>
          <input {...register("latitude")} type="number" step="0.000001" className="form-input" />
        </Field>
        <Field label="Longitude" error={errors.longitude?.message}>
          <input {...register("longitude")} type="number" step="0.000001" className="form-input" />
        </Field>
        <Field label="Phone" error={errors.phone?.message}>
          <input {...register("phone")} className="form-input" />
        </Field>
        <Field label="WhatsApp" error={errors.whatsapp?.message}>
          <input {...register("whatsapp")} className="form-input" />
        </Field>
        <Field label="Website" error={errors.website?.message}>
          <input {...register("website")} className="form-input" />
        </Field>
      </div>

      <div className="rounded-2xl bg-cream p-4">
        <h3 className="text-sm font-extrabold">Opening hours</h3>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((day) => (
            <div key={day} className="rounded-2xl bg-white p-3">
              <p className="text-xs font-black uppercase text-muted">{day}</p>
              <div className="mt-2 grid grid-cols-2 gap-2">
                <input type="time" defaultValue={day === "Sun" ? "11:00" : "10:00"} className="form-input min-h-10 p-2 text-xs" />
                <input type="time" defaultValue={day === "Sun" ? "19:00" : "21:00"} className="form-input min-h-10 p-2 text-xs" />
              </div>
            </div>
          ))}
        </div>
      </div>

      <Button type="submit" disabled={status === "saving"} className="w-full md:w-fit">
        {status === "saving" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
        {status === "saved" ? "Saved for approval" : "Save profile"}
      </Button>
    </form>
  );
}

function Field({ label, error, children }: { label: string; error?: string; children: React.ReactNode }) {
  return (
    <label className="grid gap-2 text-sm font-bold">
      {label}
      {children}
      {error ? <span className="text-xs font-semibold text-maple">{error}</span> : null}
    </label>
  );
}
