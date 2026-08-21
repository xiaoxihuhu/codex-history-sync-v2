create table public.sync_events (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  device_id uuid,
  run_id uuid not null,
  direction text not null check (direction in ('upload', 'download', 'local')),
  event_type text not null check (
    event_type in (
      'scan',
      'manifest',
      'upload',
      'download',
      'attachment',
      'backup',
      'restore',
      'repair',
      'verify',
      'error'
    )
  ),
  entity_type text,
  entity_id uuid,
  status text not null check (status in ('pending', 'running', 'succeeded', 'failed', 'retrying')),
  attempt_count integer not null default 0 check (attempt_count >= 0),
  error_code text,
  error_message text,
  details jsonb not null default '{}'::jsonb,
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz not null default timezone('utc', now()),
  unique (user_id, id),
  foreign key (user_id, device_id)
    references public.devices (user_id, id) on delete restrict
);

create index sync_events_user_run_idx
  on public.sync_events (user_id, run_id, created_at);

create index sync_events_user_status_idx
  on public.sync_events (user_id, status, created_at);
