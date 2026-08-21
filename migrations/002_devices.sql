create table public.devices (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  device_name text not null check (length(device_name) between 1 and 200),
  os_name text not null check (length(os_name) between 1 and 100),
  os_version text not null default '',
  client_version text not null check (length(client_version) between 1 and 100),
  first_registered_at timestamptz not null default timezone('utc', now()),
  last_seen_at timestamptz,
  last_backup_at timestamptz,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  unique (user_id, id)
);

create index devices_user_last_seen_idx
  on public.devices (user_id, last_seen_at desc);

create trigger devices_set_updated_at
before update on public.devices
for each row execute function codex_sync_private.set_updated_at();
