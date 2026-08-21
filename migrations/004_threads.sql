create table public.threads (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  codex_thread_id text not null check (length(codex_thread_id) between 1 and 200),
  workspace_id uuid,
  source_device_id uuid,
  rollout_relative_path text,
  title text not null default '',
  source text not null default '',
  model_provider text not null default '',
  model text,
  original_cwd text,
  archived boolean not null default false,
  codex_created_at timestamptz,
  codex_updated_at timestamptz,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  unique (user_id, id),
  unique (user_id, codex_thread_id),
  foreign key (user_id, workspace_id)
    references public.workspaces (user_id, id) on delete restrict,
  foreign key (user_id, source_device_id)
    references public.devices (user_id, id) on delete restrict,
  check (
    rollout_relative_path is null
    or rollout_relative_path !~ '(^[\\/]|^[A-Za-z]:|(^|[\\/])\.\.([\\/]|$))'
  )
);

create index threads_user_updated_idx
  on public.threads (user_id, codex_updated_at desc nulls last, updated_at desc);

create index threads_user_workspace_idx
  on public.threads (user_id, workspace_id);

create trigger threads_set_updated_at
before update on public.threads
for each row execute function codex_sync_private.set_updated_at();
