create table public.workspaces (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null check (length(name) between 1 and 300),
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  unique (user_id, id)
);

create table public.device_workspaces (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  device_id uuid not null,
  workspace_id uuid not null,
  local_path text not null check (length(local_path) between 1 and 4096),
  is_default boolean not null default false,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  unique (user_id, id),
  unique (device_id, workspace_id),
  foreign key (user_id, device_id)
    references public.devices (user_id, id) on delete cascade,
  foreign key (user_id, workspace_id)
    references public.workspaces (user_id, id) on delete cascade
);

create index workspaces_user_updated_idx
  on public.workspaces (user_id, updated_at desc);

create index device_workspaces_workspace_idx
  on public.device_workspaces (user_id, workspace_id);

create trigger workspaces_set_updated_at
before update on public.workspaces
for each row execute function codex_sync_private.set_updated_at();

create trigger device_workspaces_set_updated_at
before update on public.device_workspaces
for each row execute function codex_sync_private.set_updated_at();
