"use client";

import { createBrowserClient } from "@supabase/ssr";
import { requireSupabaseConfig } from "@/lib/supabase/config";

export function createSupabaseBrowserClient() {
  const { supabaseUrl, supabaseAnonKey } = requireSupabaseConfig();
  return createBrowserClient(supabaseUrl, supabaseAnonKey);
}

