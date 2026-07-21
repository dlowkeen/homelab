# Immich v3 Prod Migration Retrospective (2026-07-21)

## Scope

This document captures what happened during the Immich prod migration to `v3.0.3`, the issues encountered, and the final successful run pattern to reuse for future migrations.

## Final successful target state

- `immich-postgresql`: `ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0`
- `immich-server`: `ghcr.io/immich-app/immich-server:v3.0.3`
- `immich-machine-learning`: `ghcr.io/immich-app/immich-machine-learning:v3.0.3`
- `immich-valkey`: unchanged (`redis:7.2-alpine`)

## Pre-migration controls that mattered

- Suspend Flux reconciliation for:
  - `kustomization/immich-prod`
  - `helmrelease/immich`
- Take a fresh pre-cutover DB backup.
- Run `scripts/immich-v3-pre-migration-compat.sql` before image upgrades.

## What went wrong in early attempts

### 1) Postgres image drift / mixed rollout state

- Server started while Postgres runtime was still effectively on old `pgvecto-rs` image.
- Error observed:
  - `No vector extension found. Available extensions: vchord, vector`

### 2) Postgres read-block failures during server startup paths

- During one attempt, server bootstrap failed with `CONNECTION_CLOSED`.
- Postgres logs showed:
  - `could not read block 0 in file "base/16384/<relfilenode>": read only 0 of 8192 bytes`
- Mapped relations were mostly index files (e.g. `UQ_assets_owner_checksum`, `asset_file_assetId_type_uq`, `smart_search_pkey`, `face_search_pkey`, `idx_ocr_search_text`).
- Targeted `REINDEX` helped partially but new failing indexes appeared.
- We rolled back by restoring from fresh logical backup.

## Why dev did not reproduce this automatically

- `pg_dump`/restore is logical backup/restore.
- It recreates tables/indexes from SQL, but does not copy raw PostgreSQL relation files (`base/<db_oid>/<relfilenode>`).
- File-level/index-page corruption can therefore exist in prod runtime and not appear in dev logical restore.

## Recovery and retry pattern that succeeded

1. Restore prod DB to known good backup after failed attempt.
2. Keep `immich-server` and `immich-machine-learning` at `0`.
3. Upgrade Postgres to v3 image.
4. Verify active runtime pod image/digest (not just deployment spec).
5. Run pre-server read-only DB gates:
   - force sequential scans (`enable_indexscan=off`, `enable_bitmapscan=off`, `enable_indexonlyscan=off`)
   - count key tables (`asset`, `asset_file`, `person`, `smart_search`, `face_search`, `ocr_search`)
6. Check Postgres logs for hard stop signatures:
   - `could not read block`
   - `read only 0 of 8192`
   - `FATAL` / `PANIC`
7. Only if gates pass: start `immich-server` v3 and wait for migration completion.
8. Start `immich-machine-learning` v3 and verify healthy startup.

## Hard stop / rollback triggers

- Any Postgres read-block error:
  - `could not read block ...`
  - `read only 0 of 8192`
- Server bootstrap/migration errors:
  - repeated `CONNECTION_CLOSED`
  - crashloop during migrations
- On trigger:
  - scale server/ml down
  - restore latest pre-cutover backup
  - return to v2 state if needed

## Storage findings

- Immich media (`/mnt/bigboi`) and Postgres storage are separate.
- Postgres PVC uses `local-path` host path:
  - `/opt/local-path-provisioner/pvc-..._immich_immich-postgresql-data`
- NVMe SMART long test completed without error after incident.
- No conclusive active disk failure signal found, but prior unsafe shutdown count is non-zero.

## Recommended runbook for next migrations

- Use this exact gated rollout sequence.
- Always verify runtime image digest for Postgres before server start.
- Add explicit log gate before server start.
- Keep fresh rollback backup from immediately before cutover.
- Keep Flux suspended through manual staging; only resume after IaC matches cluster state.
