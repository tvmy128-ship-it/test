"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Loader2, Save } from "lucide-react";
import { useState } from "react";
import { useForm } from "react-hook-form";
import type { z } from "zod";
import { ImageUploader } from "@/components/forms/image-uploader";
import { Button } from "@/components/ui/button";
import { offerFormSchema } from "@/lib/validation";
import type { Category, Offer } from "@/types/dealnear";

type OfferFormInput = z.input<typeof offerFormSchema>;

export function OfferForm({ offer, categories }: { offer?: Offer; categories: Category[] }) {
  const [status, setStatus] = useState<"idle" | "saving" | "saved">("idle");
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<OfferFormInput, unknown, z.output<typeof offerFormSchema>>({
    resolver: zodResolver(offerFormSchema),
    defaultValues: {
      title: offer?.title ?? "",
      description: offer?.description ?? "",
      discountType: offer?.discountType ?? "percentage",
      discountValue: offer?.discountValue ?? "",
      originalPrice: offer?.originalPrice,
      dealPrice: offer?.dealPrice,
      startDate: offer?.startDate ?? "2026-05-04",
      endDate: offer?.endDate ?? "2026-05-31",
      terms: offer?.terms ?? "",
      categoryId: offer?.categoryId ?? categories[0]?.id,
      promoCode: offer?.promoCode ?? "",
      status: offer?.status === "paused" ? "paused" : "active",
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
        <Field label="Offer title" error={errors.title?.message}>
          <input {...register("title")} className="form-input" />
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

      <Field label="Short description" error={errors.description?.message}>
        <textarea {...register("description")} rows={4} className="form-input min-h-28" />
      </Field>

      <div className="grid gap-4 md:grid-cols-2">
        <ImageUploader label="Main offer image" limitLabel="1 main image, PNG/JPG/WebP up to 5 MB" />
        <ImageUploader label="Extra offer images" limitLabel="Optional 1-2 images" multiple />
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        <Field label="Discount type" error={errors.discountType?.message}>
          <select {...register("discountType")} className="form-input">
            <option value="percentage">Percentage</option>
            <option value="fixed_amount">Fixed amount</option>
            <option value="bogo">BOGO</option>
            <option value="meal_deal">Meal deal</option>
            <option value="special_price">Special price</option>
            <option value="other">Other</option>
          </select>
        </Field>
        <Field label="Discount value" error={errors.discountValue?.message}>
          <input {...register("discountValue")} placeholder="25% or $5.99" className="form-input" />
        </Field>
        <Field label="Promo code" error={errors.promoCode?.message}>
          <input {...register("promoCode")} className="form-input" />
        </Field>
        <Field label="Original price" error={errors.originalPrice?.message}>
          <input {...register("originalPrice")} type="number" step="0.01" className="form-input" />
        </Field>
        <Field label="Deal price" error={errors.dealPrice?.message}>
          <input {...register("dealPrice")} type="number" step="0.01" className="form-input" />
        </Field>
        <Field label="Status" error={errors.status?.message}>
          <select {...register("status")} className="form-input">
            <option value="active">Active</option>
            <option value="paused">Paused</option>
          </select>
        </Field>
        <Field label="Start date" error={errors.startDate?.message}>
          <input {...register("startDate")} type="date" className="form-input" />
        </Field>
        <Field label="Expiry date" error={errors.endDate?.message}>
          <input {...register("endDate")} type="date" className="form-input" />
        </Field>
      </div>

      <Field label="Terms and conditions" error={errors.terms?.message}>
        <textarea {...register("terms")} rows={4} className="form-input min-h-28" />
      </Field>

      <div className="rounded-2xl bg-orange/10 p-4 text-sm font-semibold text-[#8a3a0a]">
        New and edited offers stay pending until admin approval. Rejected offers show the admin reason here.
      </div>

      <Button type="submit" disabled={status === "saving"} className="w-full md:w-fit">
        {status === "saving" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
        {status === "saved" ? "Submitted for approval" : "Save offer"}
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
