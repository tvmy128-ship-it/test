insert into auth.users (
  id,
  instance_id,
  aud,
  role,
  email,
  encrypted_password,
  email_confirmed_at,
  raw_user_meta_data,
  created_at,
  updated_at
)
values
  ('00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 'admin@dealnear.local', crypt('dealnear-demo', gen_salt('bf')), now(), '{"full_name":"DealNear Admin"}', now(), now()),
  ('00000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 'owner@dealnear.local', crypt('dealnear-demo', gen_salt('bf')), now(), '{"full_name":"Demo Business Owner"}', now(), now()),
  ('00000000-0000-0000-0000-000000000003', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 'customer@dealnear.local', crypt('dealnear-demo', gen_salt('bf')), now(), '{"full_name":"Demo Customer"}', now(), now())
on conflict (id) do nothing;

insert into public.users (id, email, phone, role, full_name, avatar_url)
values
  ('00000000-0000-0000-0000-000000000001', 'admin@dealnear.local', null, 'admin', 'DealNear Admin', null),
  ('00000000-0000-0000-0000-000000000002', 'owner@dealnear.local', '+14165550100', 'business_owner', 'Demo Business Owner', null),
  ('00000000-0000-0000-0000-000000000003', 'customer@dealnear.local', null, 'customer', 'Demo Customer', null)
on conflict (id) do update set role = excluded.role, full_name = excluded.full_name;

insert into public.categories (id, name, slug, icon, sort_order, is_active)
values
  ('10000000-0000-0000-0000-000000000001', 'Food', 'food', 'Utensils', 1, true),
  ('10000000-0000-0000-0000-000000000002', 'Cafes', 'cafes', 'Coffee', 2, true),
  ('10000000-0000-0000-0000-000000000003', 'Grocery', 'grocery', 'ShoppingBasket', 3, true),
  ('10000000-0000-0000-0000-000000000004', 'Bakeries', 'bakeries', 'Croissant', 4, true),
  ('10000000-0000-0000-0000-000000000005', 'Desserts', 'desserts', 'IceCreamBowl', 5, true),
  ('10000000-0000-0000-0000-000000000006', 'Local shops', 'local-shops', 'Store', 6, true)
on conflict (slug) do update set name = excluded.name, icon = excluded.icon, sort_order = excluded.sort_order, is_active = excluded.is_active;

insert into public.cities (id, name, province_state, country, latitude, longitude, is_active)
values
  ('20000000-0000-0000-0000-000000000001', 'Toronto', 'Ontario', 'Canada', 43.6532, -79.3832, true)
on conflict (id) do update set name = excluded.name, province_state = excluded.province_state, latitude = excluded.latitude, longitude = excluded.longitude, is_active = excluded.is_active;

insert into public.businesses (
  id,
  owner_id,
  name,
  slug,
  description,
  category_id,
  city_id,
  address,
  latitude,
  longitude,
  phone,
  whatsapp,
  logo_url,
  cover_url,
  opening_hours,
  is_verified,
  approval_status
)
values
  ('30000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000002', 'Maple Shawarma House', 'maple-shawarma-house', 'Family-run shawarma counter serving halal wraps, plates, and late lunch combos.', '10000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001', '218 Queen St W, Toronto, ON', 43.6509, -79.3904, '+14165550101', '+14165550101', 'https://images.unsplash.com/photo-1625398407796-82650a8c135f?auto=format&fit=crop&w=240&q=80', 'https://images.unsplash.com/photo-1633321702518-7feccafb94d5?auto=format&fit=crop&w=1400&q=80', '{"monday":{"open":"10:00","close":"21:00"},"tuesday":{"open":"10:00","close":"21:00"},"wednesday":{"open":"10:00","close":"21:00"},"thursday":{"open":"10:00","close":"21:00"},"friday":{"open":"10:00","close":"22:00"},"saturday":{"open":"10:00","close":"22:00"},"sunday":{"open":"11:00","close":"19:00"}}', true, 'approved'),
  ('30000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000002', 'North Bean Cafe', 'north-bean-cafe', 'Neighbourhood cafe with espresso, fresh pastries, and quiet morning deals.', '10000000-0000-0000-0000-000000000002', '20000000-0000-0000-0000-000000000001', '401 College St, Toronto, ON', 43.6566, -79.4073, '+14165550103', '+14165550103', 'https://images.unsplash.com/photo-1514432324607-a09d9b4aefdd?auto=format&fit=crop&w=240&q=80', 'https://images.unsplash.com/photo-1495474472287-4d71bcdd2085?auto=format&fit=crop&w=1400&q=80', '{"monday":{"open":"07:00","close":"18:00"},"tuesday":{"open":"07:00","close":"18:00"},"wednesday":{"open":"07:00","close":"18:00"},"thursday":{"open":"07:00","close":"18:00"},"friday":{"open":"07:00","close":"19:00"},"saturday":{"open":"08:00","close":"19:00"},"sunday":{"open":"08:00","close":"17:00"}}', true, 'approved'),
  ('30000000-0000-0000-0000-000000000003', '00000000-0000-0000-0000-000000000002', 'Market North Grocer', 'market-north-grocer', 'Independent supermarket with pantry staples, produce, halal meats, and weekly baskets.', '10000000-0000-0000-0000-000000000003', '20000000-0000-0000-0000-000000000001', '710 Bloor St W, Toronto, ON', 43.6639, -79.4176, '+14165550105', '+14165550105', 'https://images.unsplash.com/photo-1542838132-92c53300491e?auto=format&fit=crop&w=240&q=80', 'https://images.unsplash.com/photo-1542838132-92c53300491e?auto=format&fit=crop&w=1400&q=80', '{"monday":{"open":"08:00","close":"22:00"},"tuesday":{"open":"08:00","close":"22:00"},"wednesday":{"open":"08:00","close":"22:00"},"thursday":{"open":"08:00","close":"22:00"},"friday":{"open":"08:00","close":"22:00"},"saturday":{"open":"08:00","close":"22:00"},"sunday":{"open":"09:00","close":"20:00"}}', true, 'approved')
on conflict (slug) do update set name = excluded.name, description = excluded.description, approval_status = excluded.approval_status;

insert into public.offers (
  id,
  business_id,
  category_id,
  city_id,
  title,
  slug,
  description,
  image_url,
  discount_type,
  discount_value,
  original_price,
  deal_price,
  start_date,
  end_date,
  terms,
  promo_code,
  status,
  approval_status,
  is_featured,
  is_boosted
)
values
  ('40000000-0000-0000-0000-000000000001', '30000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001', '25% off chicken shawarma combo', '25-off-chicken-shawarma-combo', 'Chicken shawarma wrap, fries, and a canned drink for less during lunch and dinner.', 'https://images.unsplash.com/photo-1633321702518-7feccafb94d5?auto=format&fit=crop&w=1200&q=80', 'percentage', '25%', 15.99, 11.99, '2026-05-01', '2026-05-31', 'Valid for dine-in and pickup. One offer per customer per visit. Cannot be combined with other offers.', 'MAPLE25', 'active', 'approved', true, true),
  ('40000000-0000-0000-0000-000000000002', '30000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000002', '20000000-0000-0000-0000-000000000001', 'Coffee + croissant for $5.99', 'coffee-croissant-for-599', 'Choose any small brewed coffee with a butter croissant before 11 AM.', 'https://images.unsplash.com/photo-1495474472287-4d71bcdd2085?auto=format&fit=crop&w=1200&q=80', 'meal_deal', '$5.99', 8.50, 5.99, '2026-05-01', '2026-05-18', 'Morning deal only. Available while pastries last. Taxes extra.', null, 'active', 'approved', true, false),
  ('40000000-0000-0000-0000-000000000003', '30000000-0000-0000-0000-000000000003', '10000000-0000-0000-0000-000000000003', '20000000-0000-0000-0000-000000000001', '$10 off grocery basket over $50', '10-off-grocery-basket-over-50', 'Save on pantry, produce, and household essentials when your basket reaches $50.', 'https://images.unsplash.com/photo-1542838132-92c53300491e?auto=format&fit=crop&w=1200&q=80', 'fixed_amount', '$10', null, null, '2026-05-01', '2026-05-25', 'Minimum $50 before tax. Excludes gift cards and tobacco. One redemption per day.', 'BASKET10', 'active', 'approved', true, false)
on conflict (slug) do update set title = excluded.title, description = excluded.description, approval_status = excluded.approval_status;

