# Immich GCS Backup System

This directory contains the backup system for Immich production data.

## Overview

The backup system provides:
- **Incremental backups**: Only uploads new/changed files
- **Idempotent**: Safe to run multiple times
- **Append-only**: Never deletes from GCS (archived files marked in manifest)
- **Verification**: Checksums and metadata validation
- **Database backups**: PostgreSQL dumps with version tracking

## Components

### Scripts

- `backup.py`: Main backup script that handles library files and database
- `verify-restore.py`: Restore testing script for periodic verification
- `Dockerfile`: Container image definition

### Kubernetes Resources

Located in `components/immich/overlays/prod/`:
- `backup-cronjob.yaml`: Kubernetes CronJob definition
- `backup-config.yaml`: Configuration ConfigMap
- `backup-secrets.enc.yaml`: Encrypted secrets (GCS credentials, bucket name)

## Setup Instructions

### 1. Create GCS Bucket

Use Terraform to create the bucket (see `terraform/` directory):
```bash
cd terraform
terraform init
terraform plan
terraform apply
```

### 2. Create GCS Service Account

1. Create a service account in GCP with Storage Admin permissions
2. Download the JSON key file
3. Encrypt it with SOPS and add to `backup-secrets.enc.yaml`:

```bash
# Get the JSON key content
cat gcs-service-account-key.json | sops --encrypt /dev/stdin

# Add the encrypted value to backup-secrets.enc.yaml under GCS_CREDENTIALS
```

### 3. Update Secrets

Edit `components/immich/overlays/prod/backup-secrets.enc.yaml`:
- Set `GCS_BUCKET` to your bucket name
- Set `GCS_CREDENTIALS` to the encrypted JSON key content

Encrypt with SOPS:
```bash
sops -e -i components/immich/overlays/prod/backup-secrets.enc.yaml
```

### 4. Build and Push Docker Image

```bash
# Build the image
docker build -t your-registry/immich-backup:latest apps/immich-backup/

# Push to registry
docker push your-registry/immich-backup:latest

# Update the image reference in backup-cronjob.yaml
```

### 5. Update Configuration

Edit `components/immich/overlays/prod/backup-config.yaml`:
- Update `immich_version` to match your deployed version
- Adjust `schedule` if needed (cron format)
- Modify `db_backup_retention` if desired

### 6. Deploy

The resources will be deployed via Flux when you commit and push:
```bash
git add components/immich/overlays/prod/backup-*.yaml
git commit -m "feat: add Immich backup system"
git push
```

Or manually apply:
```bash
kubectl apply -k components/immich/overlays/prod/
```

## Usage

### Manual Backup

Run a backup job manually:
```bash
kubectl create job --from=cronjob/immich-backup immich-backup-manual -n immich
```

### Check Backup Status

```bash
# View recent jobs
kubectl get jobs -n immich -l app=immich-backup

# View logs
kubectl logs -n immich job/immich-backup-<timestamp>
```

### Restore Verification

Run the restore verification script (weekly/monthly):
```bash
kubectl run -it --rm verify-restore \
  --image=your-registry/immich-backup:latest \
  --restart=Never \
  --command -- /app/verify-restore.py \
  --env="GCS_BUCKET=immich-backups" \
  --env="GOOGLE_APPLICATION_CREDENTIALS=/etc/gcs/credentials.json"
```

## GCS Bucket Structure

```
immich-backups/
├── manifest.json                    # Current backup manifest
├── manifest-{timestamp}.json        # Historical manifests
├── library/
│   └── {preserved-immich-structure}/
│       └── {files}
└── database/
    ├── 2024-01-15T03-00-00-immich-v2.3.1.sql.gz
    ├── 2024-01-16T03-00-00-immich-v2.3.1.sql.gz
    └── ...
```

## Manifest Format

The manifest is stored as a SQLite database (`manifest.db`) in GCS for efficient querying without loading everything into memory.

**Schema:**
- `manifest_metadata` table: Stores backup-level metadata (version, last_backup timestamp, immich_version)
- `files` table: Stores per-file data:
  - `file_path`: Local filesystem path (primary key)
  - `checksum`: SHA256 hash for deduplication
  - `size`: File size in bytes
  - `archived`: Boolean flag (0 or 1)

**Optimizations:**
- `gcs_path` is derived from `file_path` (adds "library/" prefix) - not stored
- `backed_up_at` is stored at backup level in metadata - not per file
- Results in ~50% smaller database size (~252 bytes per file vs ~520 bytes)

**Example:**
- File: `/upload/2024/01/photo.jpg`
- GCS Path: `library/upload/2024/01/photo.jpg` (derived)
- Stored: `file_path`, `checksum`, `size`, `archived`

## Troubleshooting

### Backup Fails

1. Check job logs: `kubectl logs -n immich job/immich-backup-<timestamp>`
2. Verify GCS credentials are correct
3. Check database connectivity
4. Ensure PVC is mounted correctly

### Verification Failures

If verification fails:
1. Check GCS bucket permissions
2. Verify files exist in GCS
3. Check network connectivity from cluster to GCS

### Database Backup Issues

1. Verify PostgreSQL service is accessible
2. Check database credentials in `immich-database-secret`
3. Ensure `pg_dump` is available in the container

## Cost Optimization

- Uses GCS Archive storage class (coldest/cheapest)
- Incremental backups minimize upload costs
- Database backup retention limits storage costs
- Consider lifecycle policies for older backups

## Security

- Secrets are encrypted with SOPS
- Library PVC mounted read-only
- Service account has minimal required permissions
- GCS credentials stored as Kubernetes secrets

