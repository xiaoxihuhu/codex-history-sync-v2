create table public.attachments (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  thread_id uuid,
  session_id uuid,
  source_device_id uuid,
  message_id text,
  sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  file_name text not null check (length(file_name) between 1 and 1024),
  file_extension text not null default '',
  mime_type text not null default 'application/octet-stream',
  file_size bigint not null check (file_size >= 0),
  storage_path text not null,
  original_local_path text,
  original_reference_location text,
  reference_kind text not null default 'unknown',
  last_uploaded_at timestamptz,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  unique (user_id, id),
  unique (user_id, sha256),
  foreign key (user_id, thread_id)
    references public.threads (user_id, id) on delete restrict,
  foreign key (user_id, session_id)
    references public.sessions (user_id, id) on delete restrict,
  foreign key (user_id, source_device_id)
    references public.devices (user_id, id) on delete restrict,
  check (
    storage_path like 'users/' || user_id::text || '/attachments/%'
    and storage_path like '%/' || sha256 || '%'
  )
);

create table public.attachment_references (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  attachment_id uuid not null,
  thread_id uuid,
  session_id uuid,
  message_id text,
  reference_kind text not null,
  reference_location text not null,
  original_local_path text,
  created_at timestamptz not null default timezone('utc', now()),
  unique (user_id, id),
  unique (user_id, attachment_id, reference_location),
  foreign key (user_id, attachment_id)
    references public.attachments (user_id, id) on delete cascade,
  foreign key (user_id, thread_id)
    references public.threads (user_id, id) on delete restrict,
  foreign key (user_id, session_id)
    references public.sessions (user_id, id) on delete restrict
);

create index attachments_user_thread_idx
  on public.attachments (user_id, thread_id);

create index attachment_references_message_idx
  on public.attachment_references (user_id, message_id);

create trigger attachments_set_updated_at
before update on public.attachments
for each row execute function codex_sync_private.set_updated_at();
