-- Preserve user ownership while clearing nullable Native references.
-- This migration is safe after the original 010 and after the production
-- hotfix because it replaces only the three affected foreign keys.

alter table public.native_state_exports
  drop constraint if exists native_state_exports_source_device_id_fkey;

alter table public.native_state_exports
  drop constraint if exists native_state_exports_user_id_source_device_id_fkey;

alter table public.native_state_exports
  add constraint native_state_exports_user_id_source_device_id_fkey
  foreign key (user_id, source_device_id)
  references public.devices (user_id, id)
  on delete set null (source_device_id);

alter table public.native_threads
  drop constraint if exists native_threads_source_device_id_fkey;

alter table public.native_threads
  drop constraint if exists native_threads_user_id_source_device_id_fkey;

alter table public.native_threads
  add constraint native_threads_user_id_source_device_id_fkey
  foreign key (user_id, source_device_id)
  references public.devices (user_id, id)
  on delete set null (source_device_id);

alter table public.snapshots
  drop constraint if exists snapshots_native_export_fk;

alter table public.snapshots
  add constraint snapshots_native_export_fk
  foreign key (user_id, native_export_id)
  references public.native_state_exports (user_id, id)
  on delete set null (native_export_id);
