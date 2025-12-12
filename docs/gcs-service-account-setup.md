# GCS Service Account Setup Guide

## Step 1: Create Service Account

```bash
# Set your project (if not already set)
gcloud config set project donovans-personal-stuff

# Create the service account
gcloud iam service-accounts create immich-backup \
    --display-name="Immich Backup Service Account" \
    --description="Service account for Immich backup to GCS"
```

## Step 2: Grant Permissions

The service account needs permissions to:
- Read/write objects in the backup bucket
- List objects (for verification)

```bash
# Grant Storage Admin role (full access to storage)
gcloud projects add-iam-policy-binding donovans-personal-stuff \
    --member="serviceAccount:immich-backup@donovans-personal-stuff.iam.gserviceaccount.com" \
    --role="roles/storage.admin"

# OR, for more restrictive permissions (recommended):
# Grant only the specific permissions needed
gcloud projects add-iam-policy-binding donovans-personal-stuff \
    --member="serviceAccount:immich-backup@donovans-personal-stuff.iam.gserviceaccount.com" \
    --role="roles/storage.objectAdmin"
```

**Note:** `storage.objectAdmin` is more restrictive and only allows object operations (read/write/delete) but not bucket management. Since Terraform manages the bucket, this is sufficient.

## Step 3: Create and Download Key

```bash
# Create and download the key
gcloud iam service-accounts keys create immich-backup-key.json \
    --iam-account=immich-backup@donovans-personal-stuff.iam.gserviceaccount.com
```

This creates `immich-backup-key.json` in your current directory.

## Step 4: Encrypt and Add to Secrets

```bash
# Encrypt the JSON key content
cat immich-backup-key.json | sops --encrypt /dev/stdin

# Copy the encrypted output, then edit the secrets file:
sops components/immich/overlays/prod/backup-secrets.enc.yaml

# Paste the encrypted value under GCS_CREDENTIALS
# Also set GCS_BUCKET to your bucket name (e.g., "donovans-personal-stuff-immich-backups")

# Save and encrypt the file
sops -e -i components/immich/overlays/prod/backup-secrets.enc.yaml
```

## Step 5: Clean Up Local Key File

```bash
# Remove the local key file (it's now encrypted in the secrets file)
rm immich-backup-key.json
```

## Alternative: Using GCP Console

1. Go to [GCP Console → IAM & Admin → Service Accounts](https://console.cloud.google.com/iam-admin/serviceaccounts)
2. Click "Create Service Account"
3. Name: `immich-backup`
4. Click "Create and Continue"
5. Grant role: `Storage Object Admin` (or `Storage Admin` for full access)
6. Click "Continue" then "Done"
7. Click on the service account → "Keys" tab → "Add Key" → "Create new key"
8. Choose JSON format → Download
9. Follow Step 4 above to encrypt and add to secrets

## Permissions Explained

- **Storage Admin**: Full control (can create/delete buckets, manage lifecycle, etc.)
- **Storage Object Admin**: Can read/write/delete objects, but not manage buckets (recommended)
- **Storage Object Creator**: Can only create objects (not read/delete) - not sufficient for backups

For backups, you need at least **Storage Object Admin** since the script needs to:
- Upload files (create objects)
- List objects (for verification)
- Read objects (for restore verification)
- Update object metadata (storage class)

