import { NextResponse } from "next/server";
import { reportSchema } from "@/lib/validation";
import { isSupabaseConfigured } from "@/lib/supabase/config";
import { createSupabaseServerClient } from "@/lib/supabase/server";

export async function POST(request: Request) {
  const body = await request.json().catch(() => null);
  const parsed = reportSchema.safeParse(body);

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

  const { error } = await supabase.from("reports").insert({
    user_id: user?.id ?? null,
    offer_id: parsed.data.offerId ?? null,
    business_id: parsed.data.businessId ?? null,
    report_type: parsed.data.reportType,
    message: parsed.data.message,
    status: "open",
  });

  if (error) {
    return NextResponse.json({ ok: false }, { status: 500 });
  }

  return NextResponse.json({ ok: true });
}

