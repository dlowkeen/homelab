-- Immich v3 pre-migration compatibility shim
-- Purpose: normalize legacy/drifted v2 schemas before upgrading to Immich v3.
-- Safe to rerun: each fix is guarded/idempotent.
--
-- Run as a superuser/owner with DDL rights, e.g.:
-- psql -v ON_ERROR_STOP=1 -f scripts/immich-v3-pre-migration-compat.sql

BEGIN;

-- 1) asset_exif must be a table (some legacy DBs have a view over exif)
DO $$
DECLARE
  relkind_char "char";
  has_exif_table boolean;
BEGIN
  SELECT c.relkind
  INTO relkind_char
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'public' AND c.relname = 'asset_exif';

  IF relkind_char = 'v' THEN
    SELECT EXISTS (
      SELECT 1
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public' AND c.relname = 'exif' AND c.relkind = 'r'
    ) INTO has_exif_table;

    IF NOT has_exif_table THEN
      RAISE EXCEPTION 'asset_exif is a view, but public.exif table does not exist';
    END IF;

    EXECUTE 'DROP VIEW public.asset_exif';
    EXECUTE 'ALTER TABLE public.exif RENAME TO asset_exif';
  END IF;
END $$;

-- 2) Ensure PK/unique identities needed by v3 migrations.
DO $$
BEGIN
  -- asset(id)
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.asset'::regclass
      AND contype = 'p'
  ) THEN
    IF EXISTS (SELECT 1 FROM public.asset WHERE id IS NULL) THEN
      RAISE EXCEPTION 'Cannot add PK on asset(id): null ids found';
    END IF;
    IF EXISTS (SELECT id FROM public.asset GROUP BY id HAVING count(*) > 1) THEN
      RAISE EXCEPTION 'Cannot add PK on asset(id): duplicate ids found';
    END IF;
    ALTER TABLE public.asset ADD CONSTRAINT asset_id_pk PRIMARY KEY (id);
  END IF;

  -- "user"(id)
  IF EXISTS (
    SELECT 1 FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relname = 'user' AND c.relkind = 'r'
  ) AND NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public."user"'::regclass
      AND contype = 'p'
  ) THEN
    IF EXISTS (SELECT 1 FROM public."user" WHERE id IS NULL) THEN
      RAISE EXCEPTION 'Cannot add PK on "user"(id): null ids found';
    END IF;
    IF EXISTS (SELECT id FROM public."user" GROUP BY id HAVING count(*) > 1) THEN
      RAISE EXCEPTION 'Cannot add PK on "user"(id): duplicate ids found';
    END IF;
    ALTER TABLE public."user" ADD CONSTRAINT user_id_pk PRIMARY KEY (id);
  END IF;

  -- asset_file(id)
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.asset_file'::regclass
      AND contype = 'p'
  ) THEN
    IF EXISTS (SELECT 1 FROM public.asset_file WHERE id IS NULL) THEN
      RAISE EXCEPTION 'Cannot add PK on asset_file(id): null ids found';
    END IF;
    IF EXISTS (SELECT id FROM public.asset_file GROUP BY id HAVING count(*) > 1) THEN
      RAISE EXCEPTION 'Cannot add PK on asset_file(id): duplicate ids found';
    END IF;
    ALTER TABLE public.asset_file ADD CONSTRAINT asset_file_id_pk PRIMARY KEY (id);
  END IF;
END $$;

-- 3) Normalize legacy constraint names expected by migrations.
DO $$
BEGIN
  -- Old DBs may have this name occupied on legacy table asset_files.
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.asset_files'::regclass
      AND conname = 'asset_file_assetId_type_uq'
  ) THEN
    ALTER TABLE public.asset_files DROP CONSTRAINT "asset_file_assetId_type_uq";
  END IF;

  -- Ensure constraint on asset_file has migration-expected name.
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.asset_file'::regclass
      AND conname = 'asset_file_assetid_type_unique'
  ) AND NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.asset_file'::regclass
      AND conname = 'asset_file_assetId_type_uq'
  ) THEN
    ALTER TABLE public.asset_file
      RENAME CONSTRAINT asset_file_assetid_type_unique
      TO "asset_file_assetId_type_uq";
  END IF;
END $$;

-- 4) Remove legacy trigger that blocks dropping album_delete_audit().
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_trigger t
    JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE NOT t.tgisinternal
      AND n.nspname = 'public'
      AND c.relname = 'albums'
      AND t.tgname = 'album_delete_audit'
  ) THEN
    DROP TRIGGER album_delete_audit ON public.albums;
  END IF;
END $$;

-- 5) album_user upsert in migration needs unique (albumId,userId).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname = 'public'
      AND tablename = 'album_user'
      AND indexname = 'album_user_albumId_userId_uq_idx'
  ) THEN
    IF EXISTS (
      SELECT "albumId", "userId"
      FROM public.album_user
      GROUP BY "albumId", "userId"
      HAVING count(*) > 1
    ) THEN
      RAISE EXCEPTION 'Cannot create unique index album_user(albumId,userId): duplicate pairs exist';
    END IF;

    CREATE UNIQUE INDEX album_user_albumId_userId_uq_idx
      ON public.album_user ("albumId", "userId");
  END IF;
END $$;

-- 6) Add missing FK expected to be dropped/reworked by migration.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relname = 'album' AND c.relkind = 'r'
  ) AND NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.album'::regclass
      AND conname = 'album_ownerId_fkey'
  ) THEN
    ALTER TABLE public.album
      ADD CONSTRAINT "album_ownerId_fkey"
      FOREIGN KEY ("ownerId") REFERENCES public.users(id)
      ON UPDATE CASCADE ON DELETE CASCADE;
  END IF;
END $$;

COMMIT;

-- Verification snapshot
SELECT
  conrelid::regclass::text AS table_name,
  conname,
  contype,
  pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conname IN (
  'asset_id_pk',
  'user_id_pk',
  'asset_file_id_pk',
  'asset_file_assetId_type_uq',
  'album_ownerId_fkey'
)
ORDER BY table_name, conname;

SELECT
  c.relname,
  c.relkind
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relname IN ('asset_exif', 'exif', 'asset', 'asset_file', 'user', 'users', 'album', 'albums')
ORDER BY c.relname;
