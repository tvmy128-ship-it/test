import { z } from "zod";

export const businessFormSchema = z.object({
  name: z.string().min(2, "Business name is required"),
  categoryId: z.string().min(1, "Choose a category"),
  description: z.string().min(20, "Add a short description"),
  address: z.string().min(5, "Address is required"),
  cityId: z.string().min(1, "Choose a city"),
  latitude: z.coerce.number().min(-90).max(90),
  longitude: z.coerce.number().min(-180).max(180),
  phone: z.string().min(7, "Phone is required"),
  whatsapp: z.string().min(7, "WhatsApp is required"),
  website: z.string().url().optional().or(z.literal("")),
});

export const offerFormSchema = z.object({
  title: z.string().min(3, "Title is required"),
  description: z.string().min(20, "Description is required"),
  discountType: z.enum(["percentage", "fixed_amount", "bogo", "meal_deal", "special_price", "other"]),
  discountValue: z.string().min(1, "Discount value is required"),
  originalPrice: z.coerce.number().optional(),
  dealPrice: z.coerce.number().optional(),
  startDate: z.string().min(1, "Start date is required"),
  endDate: z.string().min(1, "Expiry date is required"),
  terms: z.string().min(10, "Terms are required"),
  categoryId: z.string().min(1, "Choose a category"),
  promoCode: z.string().optional(),
  status: z.enum(["active", "paused"]).default("active"),
});

export const reportSchema = z.object({
  offerId: z.string().optional(),
  businessId: z.string().optional(),
  reportType: z.enum(["offer_expired", "business_refused_offer", "wrong_info", "fake_offer", "other"]),
  message: z.string().min(5).max(1000),
});

export const analyticsSchema = z.object({
  offerId: z.string(),
  businessId: z.string(),
  eventType: z.enum(["view", "save", "call_tap", "whatsapp_tap", "direction_tap", "show_offer_tap", "map_pin_tap"]),
  sessionId: z.string().min(4).max(128),
  cityId: z.string().optional(),
});

