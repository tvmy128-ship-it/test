import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { Mail } from "lucide-react";
import { PublicShell } from "@/components/layout/public-shell";
import { Button } from "@/components/ui/button";
import { isSupabaseConfigured } from "@/lib/supabase/config";
import { createSupabaseServerClient } from "@/lib/supabase/server";

export const metadata: Metadata = {
  title: "Login",
};

async function signIn(formData: FormData) {
  "use server";

  if (!isSupabaseConfigured()) {
    redirect("/profile?demo=true");
  }

  const email = String(formData.get("email") ?? "");
  const supabase = await createSupabaseServerClient();
  await supabase.auth.signInWithOtp({
    email,
    options: {
      emailRedirectTo: `${process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3000"}/profile`,
    },
  });

  redirect("/login?sent=true");
}

type SearchParams = Promise<Record<string, string | string[] | undefined>>;

export default async function LoginPage({ searchParams }: { searchParams: SearchParams }) {
  const params = await searchParams;
  const sent = params.sent === "true";

  return (
    <PublicShell>
      <main className="mx-auto flex min-h-[calc(100vh-80px)] max-w-lg items-center px-4 py-8">
        <section className="w-full rounded-[2rem] bg-white p-6 shadow-xl shadow-neutral-900/10">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-maple text-white">
            <Mail className="h-6 w-6" />
          </div>
          <h1 className="mt-4 text-3xl font-black">Login to DealNear</h1>
          <p className="mt-2 text-sm leading-6 text-muted">Customers only need an account to save offers. Business owners can use the same login to manage offers.</p>
          <form action={signIn} className="mt-5 grid gap-3">
            <label className="grid gap-2 text-sm font-bold">
              Email
              <input name="email" type="email" required placeholder="you@example.com" className="form-input" />
            </label>
            <Button type="submit" className="w-full">
              {isSupabaseConfigured() ? "Send magic link" : "Continue"}
            </Button>
          </form>
          {sent ? <p className="mt-4 rounded-2xl bg-emerald-50 p-3 text-sm font-bold text-emerald-700">Check your email for the login link.</p> : null}
        </section>
      </main>
    </PublicShell>
  );
}
