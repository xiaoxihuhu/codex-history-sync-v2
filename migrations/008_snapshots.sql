create table public.snapshots (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  source_device_id uuid,
  workspace_id uuid,
  snapshot_type text not null check (snapshot_type in ('automatic', 'manual', 'pre_restore')),
  label text,
  status text not null default 'pending' check (status in ('pending', 'complete', 'failed')),
  manifest_hash text check (manifest_hash is null or manifest_hash ~ '^[0-9a-f]{64}$'),
  manifest_storage_path text,
  thread_count integer not null default 0 check (thread_count >= 0),
  session_count integer not null default 0 check (session_count >= 0),
  attachment_count integer not null default 0 check (attachment_count >= 0),
  completed_at timestamptz,
  created_at timestamptz not null default timezone('utc', now()),
  unique (user_id, id),
  foreign key (user_id, source_device_id)
    references public.devices (user_id, id) on delete restrict,
  foreign key (user_id, workspace_id)
    references public.workspaces (user_id, id) on delete restrict,
  check (
    manifest_storage_path is null
    or manifest_storage_path like 'users/' || user_id::text || '/snapshots/%'
  )
);

create table public.snapshot_items (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  snapshot_id uuid not null,
  object_type text not null check (object_type in ('thread', 'session', 'attachment')),
  object_id uuid not null,
  content_hash text check (content_hash is null or content_hash ~ '^[0-9a-f]{64}$'),
  storage_path text,
  created_at timestamptz not null default timezone('utc', now()),
  unique (user_id, id),
  unique (user_id, snapshot_id, object_type, object_id),
  foreign key (user_id, snapshot_id)
    references public.snapshots (user_id, id) on delete cascade
);

create index snapshots_user_created_idx
  on public.snapshots (user_id, created_at desc);

create index snapshot_items_snapshot_idx
  on public.snapshot_items (user_id, snapshot_id);
