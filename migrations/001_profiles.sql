create extension if not exists pgcrypto with schema extensions;

create schema if not exists codex_sync_private;
revoke all on schema codex_sync_private from public;

create or replace function codex_sync_private.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at = timezone('utc', now());
  return new;
end;
$$;

revoke all on function codex_sync_private.set_updated_at() from public, anon, authenticated;

create table public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now())
);

create trigger profiles_set_updated_at
before update on public.profiles
for each row execute function codex_sync_private.set_updated_at();

comment on table public.profiles is
  'Codex History Sync account profile. Authorization is always based on auth.uid(), never email.';

create or replace function codex_sync_private.handle_new_auth_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profiles (id, email)
  values (new.id, new.email)
  on conflict (id) do update
  set email = excluded.email;
  return new;
end;
$$;

revoke all on function codex_sync_private.handle_new_auth_user() from public, anon, authenticated;

create trigger codex_sync_on_auth_user_created
after insert on auth.users
for each row execute function codex_sync_private.handle_new_auth_user();
