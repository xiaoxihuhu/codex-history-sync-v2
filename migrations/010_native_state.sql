-- Phase 10 Native State Cloud Layer.
-- This migration is intentionally additive. It never stores Raw SQLite bytes.

create table public.native_state_exports (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  source_device_id uuid,
  format_version integer not null check (format_version > 0),
  codex_schema_fingerprint text not null check (length(codex_schema_fingerprint) = 64),
  codex_schema_json jsonb not null,
  source_codex_version text,
  source_platform text,
  source_database_user_version integer,
  source_database_application_id integer,
  thread_count integer not null default 0 check (thread_count >= 0),
  project_count integer not null default 0 check (project_count >= 0),
  project_root_count integer not null default 0 check (project_root_count >= 0),
  related_state_count integer not null default 0 check (related_state_count >= 0),
  created_at timestamptz not null default timezone('utc', now()),
  completed_at timestamptz,
  is_complete boolean not null default false,
  metadata jsonb not null default '{}'::jsonb,
  unique (user_id, id),
  foreign key (user_id, source_device_id)
    references public.devices (user_id, id)
    on delete set null (source_device_id)
);

create table public.native_projects (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  export_id uuid not null,
  source_project_id text not null,
  name text not null,
  metadata jsonb not null default '{}'::jsonb,
  position bigint,
  created_at_ms bigint,
  updated_at_ms bigint,
  native_metadata jsonb not null default '{}'::jsonb,
  unique (user_id, id),
  unique (export_id, source_project_id),
  unique (user_id, id, export_id),
  foreign key (user_id, export_id)
    references public.native_state_exports (user_id, id) on delete cascade
);

create table public.native_project_roots (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  export_id uuid not null,
  native_project_id uuid not null,
  source_project_id text not null,
  position bigint not null,
  source_path text not null,
  normalized_source_path text,
  metadata jsonb not null default '{}'::jsonb,
  unique (user_id, id),
  unique (native_project_id, position),
  foreign key (user_id, export_id)
    references public.native_state_exports (user_id, id) on delete cascade,
  foreign key (user_id, native_project_id, export_id)
    references public.native_projects (user_id, id, export_id) on delete cascade
);

create table public.native_threads (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  export_id uuid not null,
  source_device_id uuid,
  codex_thread_id text not null,
  source_project_id text,
  native_project_id uuid references public.native_projects(id) on delete set null,
  rollout_relative_path text not null,
  title text,
  name text,
  preview text,
  source text,
  history_mode text,
  model_provider text,
  model text,
  archived boolean not null default false,
  codex_created_at timestamptz,
  codex_updated_at timestamptz,
  codex_recency_at timestamptz,
  native_metadata jsonb not null,
  native_schema_columns jsonb not null default '[]'::jsonb,
  metadata_hash text not null check (length(metadata_hash) = 64),
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  unique (user_id, id),
  unique (export_id, codex_thread_id),
  foreign key (user_id, source_device_id)
    references public.devices (user_id, id)
    on delete set null (source_device_id),
  foreign key (user_id, export_id)
    references public.native_state_exports (user_id, id) on delete cascade
);

create table public.native_related_state (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  export_id uuid not null,
  object_type text not null check (
    object_type in (
      'thread_section',
      'thread_dynamic_tool',
      'thread_spawn_edge'
    )
  ),
  source_table text not null check (
    source_table in (
      'thread_sections',
      'thread_dynamic_tools',
      'thread_spawn_edges'
    )
  ),
  object_key text not null,
  native_metadata jsonb not null,
  metadata_hash text not null check (length(metadata_hash) = 64),
  created_at timestamptz not null default timezone('utc', now()),
  unique (user_id, id),
  unique (export_id, object_type, object_key),
  foreign key (user_id, export_id)
    references public.native_state_exports (user_id, id) on delete cascade
);

create index native_state_exports_user_created_idx
  on public.native_state_exports (user_id, created_at desc, id desc);

create index native_projects_export_position_idx
  on public.native_projects (user_id, export_id, position, id);

create index native_project_roots_export_idx
  on public.native_project_roots (user_id, export_id, native_project_id, position);

create index native_threads_export_recency_idx
  on public.native_threads (user_id, export_id, codex_recency_at desc nulls last);

create index native_threads_codex_thread_idx
  on public.native_threads (user_id, codex_thread_id);

create index native_related_state_export_idx
  on public.native_related_state (user_id, export_id, object_type, object_key);

alter table public.snapshots
  add column native_export_id uuid;

alter table public.snapshots
  add constraint snapshots_native_export_fk
  foreign key (user_id, native_export_id)
  references public.native_state_exports (user_id, id)
  on delete set null (native_export_id);

create index snapshots_native_export_idx
  on public.snapshots (user_id, native_export_id);

alter table public.native_state_exports enable row level security;
alter table public.native_projects enable row level security;
alter table public.native_project_roots enable row level security;
alter table public.native_threads enable row level security;
alter table public.native_related_state enable row level security;

revoke all on table
  public.native_state_exports,
  public.native_projects,
  public.native_project_roots,
  public.native_threads,
  public.native_related_state
from anon, authenticated;

grant select, insert, update, delete on table
  public.native_state_exports,
  public.native_projects,
  public.native_project_roots,
  public.native_threads,
  public.native_related_state
to authenticated;

create policy native_state_exports_owner_all
on public.native_state_exports
for all
to authenticated
using ((select auth.uid()) = user_id)
with check ((select auth.uid()) = user_id);

create policy native_projects_owner_all
on public.native_projects
for all
to authenticated
using (
  (select auth.uid()) = user_id
  and exists (
    select 1
    from public.native_state_exports e
    where e.id = export_id
      and e.user_id = (select auth.uid())
  )
)
with check (
  (select auth.uid()) = user_id
  and exists (
    select 1
    from public.native_state_exports e
    where e.id = export_id
      and e.user_id = (select auth.uid())
  )
);

create policy native_project_roots_owner_all
on public.native_project_roots
for all
to authenticated
using (
  (select auth.uid()) = user_id
  and exists (
    select 1
    from public.native_state_exports e
    where e.id = export_id
      and e.user_id = (select auth.uid())
  )
  and exists (
    select 1
    from public.native_projects p
    where p.id = native_project_id
      and p.export_id = export_id
      and p.user_id = (select auth.uid())
  )
)
with check (
  (select auth.uid()) = user_id
  and exists (
    select 1
    from public.native_state_exports e
    where e.id = export_id
      and e.user_id = (select auth.uid())
  )
  and exists (
    select 1
    from public.native_projects p
    where p.id = native_project_id
      and p.export_id = export_id
      and p.user_id = (select auth.uid())
  )
);

create policy native_threads_owner_all
on public.native_threads
for all
to authenticated
using (
  (select auth.uid()) = user_id
  and exists (
    select 1
    from public.native_state_exports e
    where e.id = export_id
      and e.user_id = (select auth.uid())
  )
  and (
    native_project_id is null
    or exists (
      select 1
      from public.native_projects p
      where p.id = native_project_id
        and p.export_id = export_id
        and p.user_id = (select auth.uid())
    )
  )
)
with check (
  (select auth.uid()) = user_id
  and exists (
    select 1
    from public.native_state_exports e
    where e.id = export_id
      and e.user_id = (select auth.uid())
  )
  and (
    native_project_id is null
    or exists (
      select 1
      from public.native_projects p
      where p.id = native_project_id
        and p.export_id = export_id
        and p.user_id = (select auth.uid())
    )
  )
);

create policy native_related_state_owner_all
on public.native_related_state
for all
to authenticated
using (
  (select auth.uid()) = user_id
  and exists (
    select 1
    from public.native_state_exports e
    where e.id = export_id
      and e.user_id = (select auth.uid())
  )
)
with check (
  (select auth.uid()) = user_id
  and exists (
    select 1
    from public.native_state_exports e
    where e.id = export_id
      and e.user_id = (select auth.uid())
  )
);
