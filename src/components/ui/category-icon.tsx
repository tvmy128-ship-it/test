import {
  Coffee,
  Croissant,
  IceCreamBowl,
  ShoppingBasket,
  Store,
  Utensils,
  type LucideIcon,
} from "lucide-react";

const icons: Record<string, LucideIcon> = {
  Coffee,
  Croissant,
  IceCreamBowl,
  ShoppingBasket,
  Store,
  Utensils,
};

export function CategoryIcon({ name, className }: { name: string; className?: string }) {
  const Icon = icons[name] ?? Store;
  return <Icon className={className} aria-hidden="true" />;
}

