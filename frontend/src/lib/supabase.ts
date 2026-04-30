/**
 * Supabase client used for read-only listings (history, leads, outreach,
 * analytics). Talks to PostgREST directly — bypasses the Render backend
 * so cold-start latency never affects landing-page reads.
 *
 * Mutations (search submissions, enrichment, review actions) still go
 * through axios → FastAPI; the anon key has SELECT-only privileges via
 * the RLS policies installed in alembic migration b3f5a9c1d702.
 */

import { createClient, type SupabaseClient } from "@supabase/supabase-js";

const url = import.meta.env.VITE_SUPABASE_URL;
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY;

if (!url || !anonKey) {
  // Fail loud at module load so a misconfigured deploy is obvious in the
  // console rather than producing mysterious 401s on every read.
  console.error(
    "[supabase] VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY missing — " +
      "read-only listings will fail. Check your .env / Vercel env vars."
  );
}

export const supabase: SupabaseClient = createClient(url ?? "", anonKey ?? "", {
  auth: {
    // We don't use Supabase Auth — we only read public data via the anon
    // role. Disabling persistence keeps the client from writing to
    // localStorage on every page load.
    persistSession: false,
    autoRefreshToken: false,
  },
});
