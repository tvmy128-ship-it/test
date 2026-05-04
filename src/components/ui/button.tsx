import Link from "next/link";
import type { AnchorHTMLAttributes, ButtonHTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/utils";

type ButtonVariant = "primary" | "secondary" | "ghost" | "dark" | "outline";
type ButtonSize = "sm" | "md" | "lg" | "icon";

const variants: Record<ButtonVariant, string> = {
  primary: "bg-maple text-white shadow-sm shadow-maple/20 hover:bg-[#b91d2d]",
  secondary: "bg-orange text-white shadow-sm shadow-orange/20 hover:bg-[#e8782f]",
  ghost: "bg-transparent text-foreground hover:bg-white",
  dark: "bg-foreground text-white hover:bg-neutral-800",
  outline: "border border-border bg-white text-foreground hover:border-maple/40 hover:text-maple",
};

const sizes: Record<ButtonSize, string> = {
  sm: "h-9 gap-2 rounded-xl px-3 text-sm",
  md: "h-11 gap-2 rounded-2xl px-4 text-sm",
  lg: "h-12 gap-2 rounded-2xl px-5 text-base",
  icon: "h-10 w-10 rounded-full p-0",
};

type CommonProps = {
  variant?: ButtonVariant;
  size?: ButtonSize;
  children: ReactNode;
  className?: string;
};

export function Button({
  variant = "primary",
  size = "md",
  className,
  children,
  ...props
}: CommonProps & ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      className={cn(
        "inline-flex shrink-0 items-center justify-center font-semibold transition disabled:cursor-not-allowed disabled:opacity-50",
        variants[variant],
        sizes[size],
        className,
      )}
      {...props}
    >
      {children}
    </button>
  );
}

export function ButtonLink({
  variant = "primary",
  size = "md",
  className,
  children,
  href,
  ...props
}: CommonProps & AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }) {
  return (
    <Link
      href={href}
      className={cn(
        "inline-flex shrink-0 items-center justify-center font-semibold transition",
        variants[variant],
        sizes[size],
        className,
      )}
      {...props}
    >
      {children}
    </Link>
  );
}

