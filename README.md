# DealNear

Mobile-first Next.js PWA for nearby local deals and offers. The MVP includes a public customer app, business dashboard, admin dashboard, Supabase schema/RLS/storage, Mapbox map support, analytics events, reports, and Toronto demo data.

## Stack

- Next.js App Router, TypeScript, Tailwind CSS
- Supabase Auth, Database, Storage, RLS
- Mapbox GL JS
- PWA manifest, service worker, offline fallback

## Local setup

```powershell
npm.cmd install
Copy-Item .env.example .env.local
npm.cmd run dev
```

Use `npm.cmd`/`npx.cmd` on this Windows machine because PowerShell blocks the `.ps1` npm shims.

## Supabase

The app renders from seeded demo data when Supabase env vars are not configured. To run a local Supabase backend:

```powershell
npx.cmd supabase start
npx.cmd supabase db reset
```

Then copy the local API URL and anon key into `.env.local`.

## Mapbox

Set `NEXT_PUBLIC_MAPBOX_TOKEN` in `.env.local` to enable the interactive map. Without it, `/map` falls back to an approved offers list.

## Verification

```powershell
npm.cmd run lint
npm.cmd run typecheck
npm.cmd run build
```

