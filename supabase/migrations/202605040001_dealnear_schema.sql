create extension if not exists "pgcrypto";
create extension if not exists "postgis";

create type public.user_role as enum ('customer', 'business_owner', 'admin');
create type public.approval_status as enum ('pending', 'approved', 'rejected');
create type public.offer_status as enum ('active', 'paused', 'expired');
create type public.discount_type as enum ('percentage', 'fixed_amount', 'bogo', 'meal_deal', 'special_price', 'other');
create type public.analytics_event_type as enum ('view', 'save', 'call_tap', 'whatsapp_tap', 'direction_tap', 'show_offer_tap', 'map_pin_tap');
create type public.report_type as enum ('offer_expired', 'business_refused_offer', 'wrong_info', 'fake_offer', 'other');
create type public.report_status as enum ('open', 'reviewed', 'resolved', 'rejected');
create type public.placement_type as enum ('homepage', 'category', 'map_pin', 'featured', 'push_notification');
create type public.boost_request_status as enum ('pending', 'approved', 'rejected', 'completed');

create table public.users (
  id uuid primary key references auth.users(id) on delete cascade,
  email text not null unique,
  phone text,
  role public.user_role not null default 'customer',
  full_name text,
  avatar_url text,
  created_at timestamptz not null default now()
);

create table public.categories (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  slug text not null unique,
  icon text not null,
  parent_id uuid references public.categories(id) on delete set null,
  sort_order integer not null default 0,
  is_active boolean not null default true
);

create table public.cities (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  province_state text,
  country text not null default 'Canada',
  latitude double precision not null,
  longitude double precision not null,
  is_active boolean not null default true
);

create table public.businesses (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references public.users(id) on delete cascade,
  name text not null,
  slug text not null unique,
  description text not null,
  category_id uuid not null references public.categories(id),
  city_id uuid not null references public.cities(id),
  address text not null,
  latitude double precision not null,
  longitude double precision not null,
  phone text not null,
  whatsapp text not null,
  website text,
  logo_url text,
  cover_url text,
  opening_hours jsonb not null default '{}'::jsonb,
  is_verified boolean not null default false,
  approval_status public.approval_status not null default 'pending',
  rejection_reason text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index businesses_location_gix on public.businesses using gist (st_setsrid(st_makepoint(longitude, latitude), 4326));
create index businesses_owner_idx on public.businesses(owner_id);
create index businesses_public_idx on public.businesses(approval_status, city_id, category_id);

create table public.business_photos (
  id uuid primary key default gen_random_uuid(),
  business_id uuid not null references public.businesses(id) on delete cascade,
  image_url text not null,
  sort_order integer not null default 0,
  created_at timestamptz not null default now()
);

create table public.offers (
  id uuid primary key default gen_random_uuid(),
  business_id uuid not null references public.businesses(id) on delete cascade,
  category_id uuid not null references public.categories(id),
  city_id uuid not null references public.cities(id),
  title text not null,
  slug text not null unique,
  description text not null,
  image_url text not null,
  discount_type public.discount_type not null,
  discount_value text not null,
  original_price numeric(10, 2),
  deal_price numeric(10, 2),
  start_date date not null,
  end_date date not null,
  terms text not null,
  promo_code text,
  status public.offer_status not null default 'active',
  approval_status public.approval_status not null default 'pending',
  rejection_reason text,
  is_featured boolean not null default false,
  is_boosted boolean not null default false,
  boost_starts_at timestamptz,
  boost_ends_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint offers_valid_dates check (end_date >= start_date)
);

create index offers_public_idx on public.offers(approval_status, status, end_date, is_featured, is_boosted);
create index offers_business_idx on public.offers(business_id);
create index offers_category_idx on public.offers(category_id);
create index offers_city_idx on public.offers(city_id);

create table public.saved_offers (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id) on delete cascade,
  offer_id uuid not null references public.offers(id) on delete cascade,
  created_at timestamptz not null default now(),
  unique(user_id, offer_id)
);

create table public.offer_analytics (
  id uuid primary key default gen_random_uuid(),
  offer_id uuid not null references public.offers(id) on delete cascade,
  business_id uuid not null references public.businesses(id) on delete cascade,
  event_type public.analytics_event_type not null,
  user_id uuid references public.users(id) on delete set null,
  session_id text not null,
  city_id uuid references public.cities(id) on delete set null,
  created_at timestamptz not null default now()
);

create index offer_analytics_offer_idx on public.offer_analytics(offer_id, event_type, created_at);
create index offer_analytics_business_idx on public.offer_analytics(business_id, event_type, created_at);

create table public.reports (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references public.users(id) on delete set null,
  offer_id uuid references public.offers(id) on delete set null,
  business_id uuid references public.businesses(id) on delete set null,
  report_type public.report_type not null,
  message text not null,
  status public.report_status not null default 'open',
  created_at timestamptz not null default now()
);

create table public.boost_requests (
  id uuid primary key default gen_random_uuid(),
  business_id uuid not null references public.businesses(id) on delete cascade,
  offer_id uuid not null references public.offers(id) on delete cascade,
  placement_type public.placement_type not null,
  requested_start_date date not null,
  requested_end_date date not null,
  status public.boost_request_status not null default 'pending',
  admin_notes text,
  created_at timestamptz not null default now(),
  constraint boost_request_valid_dates check (requested_end_date >= requested_start_date)
);

create table public.featured_placements (
  id uuid primary key default gen_random_uuid(),
  offer_id uuid not null references public.offers(id) on delete cascade,
  placement_type public.placement_type not null,
  starts_at timestamptz not null,
  ends_at timestamptz not null,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  constraint featured_placement_valid_dates check (ends_at >= starts_at)
);

create table public.admin_actions (
  id uuid primary key default gen_random_uuid(),
  admin_id uuid not null references public.users(id) on delete cascade,
  action_type text not null,
  target_type text not null,
  target_id uuid not null,
  notes text,
  created_at timestamptz not null default now()
);

create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create trigger businesses_touch_updated_at
before update on public.businesses
for each row execute function public.touch_updated_at();

create trigger offers_touch_updated_at
before update on public.offers
for each row execute function public.touch_updated_at();

create or replace function public.current_user_role()
returns public.user_role
language sql
stable
security definer
set search_path = public
as $$
  select role from public.users where id = auth.uid()
$$;

create or replace function public.is_admin()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select coalesce(public.current_user_role() = 'admin', false)
$$;

create or replace function public.owns_business(target_business_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1
    from public.businesses b
    where b.id = target_business_id
      and b.owner_id = auth.uid()
  )
$$;

create or replace function public.nearby_offers(
  lat double precision,
  lng double precision,
  radius_km double precision default 25,
  search_text text default null,
  category_slug text default null
)
returns table (
  offer_id uuid,
  distance_km double precision
)
language sql
stable
security definer
set search_path = public
as $$
  select
    o.id as offer_id,
    st_distance(
      st_setsrid(st_makepoint(b.longitude, b.latitude), 4326)::geography,
      st_setsrid(st_makepoint(lng, lat), 4326)::geography
    ) / 1000 as distance_km
  from public.offers o
  join public.businesses b on b.id = o.business_id
  join public.categories c on c.id = o.category_id
  where b.approval_status = 'approved'
    and o.approval_status = 'approved'
    and o.status = 'active'
    and o.end_date >= current_date
    and st_dwithin(
      st_setsrid(st_makepoint(b.longitude, b.latitude), 4326)::geography,
      st_setsrid(st_makepoint(lng, lat), 4326)::geography,
      radius_km * 1000
    )
    and (
      search_text is null
      or o.title ilike '%' || search_text || '%'
      or b.name ilike '%' || search_text || '%'
      or c.name ilike '%' || search_text || '%'
    )
    and (category_slug is null or c.slug = category_slug)
  order by
    o.is_featured desc,
    o.is_boosted desc,
    distance_km asc,
    o.created_at desc
$$;

alter table public.users enable row level security;
alter table public.categories enable row level security;
alter table public.cities enable row level security;
alter table public.businesses enable row level security;
alter table public.business_photos enable row level security;
alter table public.offers enable row level security;
alter table public.saved_offers enable row level security;
alter table public.offer_analytics enable row level security;
alter table public.reports enable row level security;
alter table public.boost_requests enable row level security;
alter table public.featured_placements enable row level security;
alter table public.admin_actions enable row level security;

create policy "users can read own profile or admins can read all"
on public.users for select
using (id = auth.uid() or public.is_admin());

create policy "users can update own profile"
on public.users for update
using (id = auth.uid())
with check (id = auth.uid());

create policy "admins can manage users"
on public.users for all
using (public.is_admin())
with check (public.is_admin());

create policy "active categories are public"
on public.categories for select
using (is_active = true or public.is_admin());

create policy "admins can manage categories"
on public.categories for all
using (public.is_admin())
with check (public.is_admin());

create policy "active cities are public"
on public.cities for select
using (is_active = true or public.is_admin());

create policy "admins can manage cities"
on public.cities for all
using (public.is_admin())
with check (public.is_admin());

create policy "approved businesses are public"
on public.businesses for select
using (approval_status = 'approved' or owner_id = auth.uid() or public.is_admin());

create policy "business owners can create businesses"
on public.businesses for insert
with check (owner_id = auth.uid() and public.current_user_role() in ('business_owner', 'admin'));

create policy "business owners can update own businesses"
on public.businesses for update
using (owner_id = auth.uid())
with check (owner_id = auth.uid() and approval_status in ('pending', 'approved', 'rejected'));

create policy "admins can manage businesses"
on public.businesses for all
using (public.is_admin())
with check (public.is_admin());

create policy "approved business photos are public"
on public.business_photos for select
using (
  exists (
    select 1 from public.businesses b
    where b.id = business_id
      and (b.approval_status = 'approved' or b.owner_id = auth.uid() or public.is_admin())
  )
);

create policy "business owners can manage own photos"
on public.business_photos for all
using (public.owns_business(business_id))
with check (public.owns_business(business_id));

create policy "admins can manage business photos"
on public.business_photos for all
using (public.is_admin())
with check (public.is_admin());

create policy "approved active offers are public"
on public.offers for select
using (
  (
    approval_status = 'approved'
    and status = 'active'
    and end_date >= current_date
    and exists (
      select 1 from public.businesses b
      where b.id = business_id
        and b.approval_status = 'approved'
    )
  )
  or public.owns_business(business_id)
  or public.is_admin()
);

create policy "business owners can create own offers"
on public.offers for insert
with check (public.owns_business(business_id));

create policy "business owners can update own offers"
on public.offers for update
using (public.owns_business(business_id))
with check (public.owns_business(business_id));

create policy "business owners can delete own offers"
on public.offers for delete
using (public.owns_business(business_id));

create policy "admins can manage offers"
on public.offers for all
using (public.is_admin())
with check (public.is_admin());

create policy "users can read own saved offers"
on public.saved_offers for select
using (user_id = auth.uid() or public.is_admin());

create policy "users can save offers"
on public.saved_offers for insert
with check (user_id = auth.uid());

create policy "users can unsave offers"
on public.saved_offers for delete
using (user_id = auth.uid());

create policy "public analytics inserts are allowed"
on public.offer_analytics for insert
with check (event_type in ('view', 'save', 'call_tap', 'whatsapp_tap', 'direction_tap', 'show_offer_tap', 'map_pin_tap'));

create policy "business owners can read own analytics"
on public.offer_analytics for select
using (public.owns_business(business_id) or public.is_admin());

create policy "public can create reports"
on public.reports for insert
with check (status = 'open');

create policy "users can read own reports"
on public.reports for select
using (user_id = auth.uid() or public.is_admin());

create policy "admins can manage reports"
on public.reports for all
using (public.is_admin())
with check (public.is_admin());

create policy "business owners can manage own boost requests"
on public.boost_requests for all
using (public.owns_business(business_id))
with check (public.owns_business(business_id));

create policy "admins can manage boost requests"
on public.boost_requests for all
using (public.is_admin())
with check (public.is_admin());

create policy "active featured placements are public"
on public.featured_placements for select
using (is_active = true or public.is_admin());

create policy "admins can manage featured placements"
on public.featured_placements for all
using (public.is_admin())
with check (public.is_admin());

create policy "admins can read admin actions"
on public.admin_actions for select
using (public.is_admin());

create policy "admins can create admin actions"
on public.admin_actions for insert
with check (public.is_admin());

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values
  ('avatars', 'avatars', true, 2097152, array['image/png', 'image/jpeg', 'image/webp']),
  ('business-logos', 'business-logos', true, 2097152, array['image/png', 'image/jpeg', 'image/webp']),
  ('business-covers', 'business-covers', true, 5242880, array['image/png', 'image/jpeg', 'image/webp']),
  ('business-gallery', 'business-gallery', true, 5242880, array['image/png', 'image/jpeg', 'image/webp']),
  ('offer-images', 'offer-images', true, 5242880, array['image/png', 'image/jpeg', 'image/webp'])
on conflict (id) do update set
  public = excluded.public,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

create policy "public can read dealnear images"
on storage.objects for select
using (bucket_id in ('avatars', 'business-logos', 'business-covers', 'business-gallery', 'offer-images'));

create policy "authenticated users can upload scoped images"
on storage.objects for insert
with check (
  auth.role() = 'authenticated'
  and bucket_id in ('avatars', 'business-logos', 'business-covers', 'business-gallery', 'offer-images')
);

create policy "authenticated users can update own scoped images"
on storage.objects for update
using (auth.role() = 'authenticated' and owner = auth.uid())
with check (auth.role() = 'authenticated' and owner = auth.uid());

create policy "authenticated users can delete own scoped images"
on storage.objects for delete
using (auth.role() = 'authenticated' and owner = auth.uid());

