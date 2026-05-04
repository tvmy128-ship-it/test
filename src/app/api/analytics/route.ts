import { NextResponse } from "next/server";
import { analyticsSchema } from "@/lib/validation";
import { isSupabaseConfigured } from "@/lib/supabase/config";
import { createSupabaseServerClient } from "@/lib/supabase/server";

export async function POST(request: Request) {
  const body = await request.json().catch(() => null);
  const parsed = analyticsSchema.safeParse(body);

  if (!parsed.success) {
    return NextResponse.json({ ok: false }, { status: 400 });
  }

  if (!isSupabaseConfigured()) {
    return NextResponse.json({ ok: true, mode: "demo" });
  }

  const supabase = await createSupabaseServerClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  const { error } = await supabase.from("offer_analytics").insert({
    offer_id: parsed.data.offerId,
    business_id: parsed.data.businessId,
    event_type: parsed.data.eventType,
    session_id: parsed.data.sessionId,
    city_id: parsed.data.cityId ?? null,
    user_id: user?.id ?? null,
  });

  if (error) {
    return NextResponse.json({ ok: false }, { status: 500 });
  }

  return NextResponse.json({ ok: true });
}

