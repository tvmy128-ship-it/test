export type UserRole = "customer" | "business_owner" | "admin";

export type ApprovalStatus = "pending" | "approved" | "rejected";

export type OfferStatus = "active" | "paused" | "expired";

export type DiscountType =
  | "percentage"
  | "fixed_amount"
  | "bogo"
  | "meal_deal"
  | "special_price"
  | "other";

export type AnalyticsEventType =
  | "view"
  | "save"
  | "call_tap"
  | "whatsapp_tap"
  | "direction_tap"
  | "show_offer_tap"
  | "map_pin_tap";

export type ReportType =
  | "offer_expired"
  | "business_refused_offer"
  | "wrong_info"
  | "fake_offer"
  | "other";

export type BoostPlacementType =
  | "homepage"
  | "category"
  | "map_pin"
  | "featured"
  | "push_notification";

export type Category = {
  id: string;
  name: string;
  slug: string;
  icon: string;
  parentId?: string;
  sortOrder: number;
  isActive: boolean;
};

export type City = {
  id: string;
  name: string;
  province: string;
  country: string;
  latitude: number;
  longitude: number;
  isActive: boolean;
};

export type OpeningHours = Record<
  string,
  {
    open: string;
    close: string;
    closed?: boolean;
  }
>;

export type Business = {
  id: string;
  ownerId: string;
  name: string;
  slug: string;
  description: string;
  categoryId: string;
  cityId: string;
  address: string;
  latitude: number;
  longitude: number;
  phone: string;
  whatsapp: string;
  website?: string;
  logoUrl: string;
  coverUrl: string;
  galleryUrls: string[];
  openingHours: OpeningHours;
  isVerified: boolean;
  approvalStatus: ApprovalStatus;
  rejectionReason?: string;
  createdAt: string;
  updatedAt: string;
};

export type Offer = {
  id: string;
  businessId: string;
  categoryId: string;
  cityId: string;
  title: string;
  slug: string;
  description: string;
  imageUrl: string;
  extraImageUrls?: string[];
  discountType: DiscountType;
  discountValue: string;
  originalPrice?: number;
  dealPrice?: number;
  startDate: string;
  endDate: string;
  terms: string;
  promoCode?: string;
  status: OfferStatus;
  approvalStatus: ApprovalStatus;
  rejectionReason?: string;
  isFeatured: boolean;
  isBoosted: boolean;
  boostStartsAt?: string;
  boostEndsAt?: string;
  createdAt: string;
  updatedAt: string;
};

export type EnrichedOffer = Offer & {
  business: Business;
  category: Category;
  city: City;
  distanceKm?: number;
};

export type AnalyticsMetric = {
  label: string;
  value: number;
  change: string;
};

