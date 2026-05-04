"use client";

import { Heart, Loader2, Send, Siren } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import type { AnalyticsEventType, EnrichedOffer, ReportType } from "@/types/dealnear";
import { cn } from "@/lib/utils";
import { useEffect } from "react";

function getSessionId() {
  const key = "dealnear_session_id";
  const existing = window.localStorage.getItem(key);
  if (existing) return existing;
  const created = crypto.randomUUID();
  window.localStorage.setItem(key, created);
  return created;
}

export async function trackOfferEvent(offer: EnrichedOffer, eventType: AnalyticsEventType) {
  try {
    await fetch("/api/analytics", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      keepalive: true,
      body: JSON.stringify({
        offerId: offer.id,
        businessId: offer.businessId,
        eventType,
        cityId: offer.cityId,
        sessionId: getSessionId(),
      }),
    });
  } catch {
    // Analytics must never block customer actions.
  }
}

export function OfferViewTracker({
  offer,
  eventType = "view",
}: {
  offer: EnrichedOffer;
  eventType?: AnalyticsEventType;
}) {
  useEffect(() => {
    void trackOfferEvent(offer, eventType);
  }, [eventType, offer]);

  return null;
}

export function TrackedLink({
  offer,
  eventType,
  href,
  children,
  className,
}: {
  offer: EnrichedOffer;
  eventType: AnalyticsEventType;
  href: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <a
      href={href}
      className={cn(
        "inline-flex h-11 items-center justify-center gap-2 rounded-2xl border border-border bg-white px-4 text-sm font-bold transition hover:border-maple/40 hover:text-maple",
        className,
      )}
      onClick={() => void trackOfferEvent(offer, eventType)}
      target={href.startsWith("http") ? "_blank" : undefined}
      rel={href.startsWith("http") ? "noreferrer" : undefined}
    >
      {children}
    </a>
  );
}

export function SaveOfferButton({ offer }: { offer: EnrichedOffer }) {
  const [saved, setSaved] = useState(false);

  return (
    <Button
      type="button"
      variant={saved ? "dark" : "outline"}
      onClick={() => {
        setSaved((value) => !value);
        void trackOfferEvent(offer, "save");
      }}
    >
      <Heart className={cn("h-4 w-4", saved && "fill-white")} aria-hidden="true" />
      {saved ? "Saved" : "Save"}
    </Button>
  );
}

export function ReportIssueForm({ offer }: { offer: EnrichedOffer }) {
  const [status, setStatus] = useState<"idle" | "saving" | "sent">("idle");

  async function submit(formData: FormData) {
    setStatus("saving");
    const reportType = formData.get("reportType") as ReportType;
    const message = String(formData.get("message") ?? "");

    await fetch("/api/reports", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        offerId: offer.id,
        businessId: offer.businessId,
        reportType,
        message,
      }),
    }).catch(() => null);

    setStatus("sent");
  }

  return (
    <form action={submit} className="rounded-3xl border border-border bg-white p-5 shadow-sm">
      <div className="flex items-center gap-2">
        <Siren className="h-5 w-5 text-maple" aria-hidden="true" />
        <h2 className="text-lg font-extrabold">Report issue</h2>
      </div>
      <div className="mt-4 grid gap-3">
        <select name="reportType" className="h-12 rounded-2xl border border-border bg-cream px-3 text-sm font-semibold">
          <option value="offer_expired">Offer expired</option>
          <option value="business_refused_offer">Business refused offer</option>
          <option value="wrong_info">Wrong info</option>
          <option value="fake_offer">Fake offer</option>
          <option value="other">Other issue</option>
        </select>
        <textarea
          name="message"
          required
          minLength={5}
          rows={4}
          placeholder="What happened?"
          className="rounded-2xl border border-border bg-cream p-3 text-sm font-medium"
        />
      </div>
      <Button type="submit" className="mt-4 w-full" disabled={status === "saving" || status === "sent"} variant="outline">
        {status === "saving" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
        {status === "sent" ? "Report sent" : "Submit report"}
      </Button>
    </form>
  );
}
