alter table public.profiles enable row level security;
alter table public.devices enable row level security;
alter table public.workspaces enable row level security;
alter table public.device_workspaces enable row level security;
alter table public.threads enable row level security;
alter table public.sessions enable row level security;
alter table public.attachments enable row level security;
alter table public.attachment_references enable row level security;
alter table public.sync_events enable row level security;
alter table public.snapshots enable row level security;
alter table public.snapshot_items enable row level security;

revoke all on table
  public.profiles,
  public.devices,
  public.workspaces,
  public.device_workspaces,
  public.threads,
  public.sessions,
  public.attachments,
  public.attachment_references,
  public.sync_events,
  public.snapshots,
  public.snapshot_items
from anon, authenticated;

grant select, insert, update, delete on table
  public.profiles,
  public.devices,
  public.workspaces,
  public.device_workspaces,
  public.threads,
  public.sessions,
  public.attachments,
  public.attachment_references,
  public.sync_events,
  public.snapshots,
  public.snapshot_items
to authenticated;

create policy profiles_owner_all
on public.profiles
for all
to authenticated
using ((select auth.uid()) = id)
with check ((select auth.uid()) = id);

create policy devices_owner_all
on public.devices
for all
to authenticated
using ((select auth.uid()) = user_id)
with check ((select auth.uid()) = user_id);

create policy workspaces_owner_all
on public.workspaces
for all
to authenticated
using ((select auth.uid()) = user_id)
with check ((select auth.uid()) = user_id);

create policy device_workspaces_owner_all
on public.device_workspaces
for all
to authenticated
using ((select auth.uid()) = user_id)
with check ((select auth.uid()) = user_id);

create policy threads_owner_all
on public.threads
for all
to authenticated
using ((select auth.uid()) = user_id)
with check ((select auth.uid()) = user_id);

create policy sessions_owner_all
on public.sessions
for all
to authenticated
using ((select auth.uid()) = user_id)
with check ((select auth.uid()) = user_id);

create policy attachments_owner_all
on public.attachments
for all
to authenticated
using ((select auth.uid()) = user_id)
with check ((select auth.uid()) = user_id);

create policy attachment_references_owner_all
on public.attachment_references
for all
to authenticated
using ((select auth.uid()) = user_id)
with check ((select auth.uid()) = user_id);

create policy sync_events_owner_all
on public.sync_events
for all
to authenticated
using ((select auth.uid()) = user_id)
with check ((select auth.uid()) = user_id);

create policy snapshots_owner_all
on public.snapshots
for all
to authenticated
using ((select auth.uid()) = user_id)
with check ((select auth.uid()) = user_id);

create policy snapshot_items_owner_all
on public.snapshot_items
for all
to authenticated
using ((select auth.uid()) = user_id)
with check ((select auth.uid()) = user_id);

insert into storage.buckets (id, name, public)
values ('codex-history-sync', 'codex-history-sync', false)
on conflict (id) do update
set public = excluded.public;

create policy codex_sync_storage_select
on storage.objects
for select
to authenticated
using (
  bucket_id = 'codex-history-sync'
  and (storage.foldername(name))[1] = 'users'
  and (storage.foldername(name))[2] = (select auth.uid())::text
);

create policy codex_sync_storage_insert
on storage.objects
for insert
to authenticated
with check (
  bucket_id = 'codex-history-sync'
  and (storage.foldername(name))[1] = 'users'
  and (storage.foldername(name))[2] = (select auth.uid())::text
);

create policy codex_sync_storage_update
on storage.objects
for update
to authenticated
using (
  bucket_id = 'codex-history-sync'
  and (storage.foldername(name))[1] = 'users'
  and (storage.foldername(name))[2] = (select auth.uid())::text
)
with check (
  bucket_id = 'codex-history-sync'
  and (storage.foldername(name))[1] = 'users'
  and (storage.foldername(name))[2] = (select auth.uid())::text
);

create policy codex_sync_storage_delete
on storage.objects
for delete
to authenticated
using (
  bucket_id = 'codex-history-sync'
  and (storage.foldername(name))[1] = 'users'
  and (storage.foldername(name))[2] = (select auth.uid())::text
);
