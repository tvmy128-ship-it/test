import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "DealNear",
    short_name: "DealNear",
    description: "Nearby local deals and offers from trusted businesses.",
    start_url: "/",
    display: "standalone",
    background_color: "#FFF8F3",
    theme_color: "#D72638",
    orientation: "portrait",
    icons: [
      {
        src: "/icons/icon.svg",
        sizes: "any",
        type: "image/svg+xml",
        purpose: "any",
      },
      {
        src: "/icons/maskable.svg",
        sizes: "any",
        type: "image/svg+xml",
        purpose: "maskable",
      },
    ],
    categories: ["food", "shopping", "lifestyle"],
  };
}

