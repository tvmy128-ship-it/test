import type { MetadataRoute } from "next";
import { getPublicBusinesses, getPublicOffers } from "@/lib/deals";

export default function sitemap(): MetadataRoute.Sitemap {
  const baseUrl = process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3000";
  const staticRoutes = ["", "/deals", "/map", "/categories", "/saved", "/login"];
  const offerRoutes = getPublicOffers().flatMap((offer) => [`/deals/${offer.slug}`, `/deals/${offer.slug}/show`]);
  const businessRoutes = getPublicBusinesses().map((business) => `/business/${business.slug}`);

  return [...staticRoutes, ...offerRoutes, ...businessRoutes].map((route) => ({
    url: `${baseUrl}${route}`,
    lastModified: new Date(),
    changeFrequency: route.includes("/show") ? "weekly" : "daily",
    priority: route === "" ? 1 : 0.7,
  }));
}

