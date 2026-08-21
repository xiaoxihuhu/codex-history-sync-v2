create table public.sessions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  thread_id uuid not null,
  source_device_id uuid,
  codex_session_id text,
  relative_path text not null,
  content_hash text not null check (content_hash ~ '^[0-9a-f]{64}$'),
  file_size bigint not null check (file_size >= 0),
  storage_path text not null,
  source_mtime_ns bigint check (source_mtime_ns is null or source_mtime_ns >= 0),
  codex_created_at timestamptz,
  codex_updated_at timestamptz,
  last_uploaded_at timestamptz,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  unique (user_id, id),
  unique (user_id, thread_id, relative_path),
  foreign key (user_id, thread_id)
    references public.threads (user_id, id) on delete cascade,
  foreign key (user_id, source_device_id)
    references public.devices (user_id, id) on delete restrict,
  check (relative_path !~ '(^[\\/]|^[A-Za-z]:|(^|[\\/])\.\.([\\/]|$))'),
  check (storage_path = 'users/' || user_id::text || '/sessions/' || content_hash || '.jsonl')
);

create index sessions_user_thread_idx
  on public.sessions (user_id, thread_id, updated_at desc);

create index sessions_user_hash_idx
  on public.sessions (user_id, content_hash);

create trigger sessions_set_updated_at
before update on public.sessions
for each row execute function codex_sync_private.set_updated_at();
