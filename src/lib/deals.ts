import { differenceInCalendarDays, isAfter, parseISO } from "date-fns";
import { analyticsMetrics, businesses, categories, cities, offers } from "@/lib/demo-data";
import type { AnalyticsMetric, Business, Category, City, EnrichedOffer, Offer } from "@/types/dealnear";

const toronto = cities[0];

type SearchOptions = {
  query?: string;
  category?: string;
  distanceKm?: number;
  sort?: "nearby" | "newest" | "expiring" | "discount";
  openNow?: boolean;
  featured?: boolean;
  lat?: number;
  lng?: number;
};

export function getCategories() {
  return categories.filter((category) => category.isActive).sort((a, b) => a.sortOrder - b.sortOrder);
}

export function getCities() {
  return cities.filter((city) => city.isActive);
}

export function getCategoryBySlug(slug: string) {
  return categories.find((category) => category.slug === slug);
}

export function getCityById(id: string) {
  return cities.find((city) => city.id === id) ?? toronto;
}

export function getBusinessBySlug(slug: string) {
  return businesses.find((business) => business.slug === slug && business.approvalStatus === "approved");
}

export function getBusinessById(id: string) {
  return businesses.find((business) => business.id === id);
}

export function getOfferBySlug(slug: string) {
  return enrichOffers(offers).find((offer) => offer.slug === slug && isPublicOffer(offer));
}

export function getOffersForBusiness(businessId: string) {
  return getPublicOffers().filter((offer) => offer.businessId === businessId);
}

export function getPublicBusinesses() {
  return businesses.filter((business) => business.approvalStatus === "approved");
}

export function getPublicOffers(options: SearchOptions = {}) {
  return searchOffers(options);
}

export function searchOffers(options: SearchOptions = {}) {
  const origin = {
    latitude: options.lat ?? toronto.latitude,
    longitude: options.lng ?? toronto.longitude,
  };

  let result = enrichOffers(offers)
    .filter(isPublicOffer)
    .map((offer) => ({
      ...offer,
      distanceKm: distanceKm(origin.latitude, origin.longitude, offer.business.latitude, offer.business.longitude),
    }));

  if (options.query) {
    const term = options.query.toLowerCase();
    result = result.filter(
      (offer) =>
        offer.title.toLowerCase().includes(term) ||
        offer.business.name.toLowerCase().includes(term) ||
        offer.category.name.toLowerCase().includes(term),
    );
  }

  if (options.category && options.category !== "all") {
    result = result.filter((offer) => offer.category.slug === options.category || offer.categoryId === options.category);
  }

  if (options.distanceKm) {
    result = result.filter((offer) => (offer.distanceKm ?? 0) <= Number(options.distanceKm));
  }

  if (options.openNow) {
    result = result.filter((offer) => isBusinessOpenNow(offer.business));
  }

  if (options.featured) {
    result = result.filter((offer) => offer.isFeatured || offer.isBoosted);
  }

  const sort = options.sort ?? "nearby";
  result.sort((a, b) => {
    if (a.isFeatured !== b.isFeatured) return Number(b.isFeatured) - Number(a.isFeatured);
    if (a.isBoosted !== b.isBoosted) return Number(b.isBoosted) - Number(a.isBoosted);

    if (sort === "newest") return Date.parse(b.createdAt) - Date.parse(a.createdAt);
    if (sort === "expiring") return Date.parse(a.endDate) - Date.parse(b.endDate);
    if (sort === "discount") return discountScore(b) - discountScore(a);
    return (a.distanceKm ?? 0) - (b.distanceKm ?? 0);
  });

  return result;
}

export function getHomeSections() {
  const publicOffers = getPublicOffers();

  return [
    { title: "Nearby Deals", href: "/deals?sort=nearby", offers: publicOffers.slice(0, 6) },
    { title: "Featured Deals", href: "/deals?featured=true", offers: publicOffers.filter((offer) => offer.isFeatured).slice(0, 6) },
    { title: "Expiring Soon", href: "/deals?sort=expiring", offers: [...publicOffers].sort((a, b) => Date.parse(a.endDate) - Date.parse(b.endDate)).slice(0, 6) },
    { title: "Food Deals", href: "/deals?category=food", offers: publicOffers.filter((offer) => offer.category.slug === "food").slice(0, 6) },
    { title: "Grocery Deals", href: "/deals?category=grocery", offers: publicOffers.filter((offer) => offer.category.slug === "grocery").slice(0, 6) },
    { title: "Cafe Deals", href: "/deals?category=cafes", offers: publicOffers.filter((offer) => offer.category.slug === "cafes").slice(0, 6) },
    { title: "New Deals", href: "/deals?sort=newest", offers: [...publicOffers].sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt)).slice(0, 6) },
  ];
}

export function getBusinessMetrics(): AnalyticsMetric[] {
  return analyticsMetrics;
}

export function getAdminStats() {
  return {
    pendingBusinesses: 3,
    pendingOffers: 5,
    openReports: 4,
    activeBoosts: offers.filter((offer) => offer.isBoosted).length,
    totalUsers: 1240,
    activeOffers: getPublicOffers().length,
  };
}

export function enrichOffers(seedOffers: Offer[]): EnrichedOffer[] {
  return seedOffers
    .map((offer) => {
      const business = businesses.find((item) => item.id === offer.businessId);
      const category = categories.find((item) => item.id === offer.categoryId);
      const city = cities.find((item) => item.id === offer.cityId);

      if (!business || !category || !city) return undefined;

      return {
        ...offer,
        business,
        category,
        city,
      };
    })
    .filter((offer): offer is EnrichedOffer => Boolean(offer));
}

export function isPublicOffer(offer: EnrichedOffer) {
  return (
    offer.status === "active" &&
    offer.approvalStatus === "approved" &&
    offer.business.approvalStatus === "approved" &&
    !isAfter(new Date(), parseISO(offer.endDate))
  );
}

export function daysUntilExpiry(endDate: string) {
  return differenceInCalendarDays(parseISO(endDate), new Date());
}

export function isBusinessOpenNow(business: Business, timeZone = "America/Toronto") {
  const parts = new Intl.DateTimeFormat("en-US", {
    weekday: "long",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone,
  }).formatToParts(new Date());
  const day = parts.find((part) => part.type === "weekday")?.value.toLowerCase() ?? "";
  const hour = parts.find((part) => part.type === "hour")?.value.padStart(2, "0") ?? "00";
  const minute = parts.find((part) => part.type === "minute")?.value.padStart(2, "0") ?? "00";
  const hours = business.openingHours[day];
  if (!hours || hours.closed) return false;

  const current = `${hour === "24" ? "00" : hour}:${minute}`;
  return current >= hours.open && current <= hours.close;
}

export function distanceKm(lat1: number, lon1: number, lat2: number, lon2: number) {
  const earthRadiusKm = 6371;
  const dLat = toRadians(lat2 - lat1);
  const dLon = toRadians(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos(toRadians(lat1)) * Math.cos(toRadians(lat2)) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
  return earthRadiusKm * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

export function formatDistance(distance?: number) {
  if (distance == null) return "Nearby";
  if (distance < 1) return `${Math.round(distance * 1000)} m`;
  return `${distance.toFixed(distance < 10 ? 1 : 0)} km`;
}

function discountScore(offer: Offer) {
  const number = Number.parseFloat(offer.discountValue.replace(/[^0-9.]/g, ""));
  if (Number.isNaN(number)) return 0;
  return number;
}

function toRadians(value: number) {
  return (value * Math.PI) / 180;
}

export type { Business, Category, City, EnrichedOffer, Offer };
