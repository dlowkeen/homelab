# Terraform - Immich GCS Bucket

This directory contains Terraform configuration for creating the GCS bucket used by the Immich backup system.

## Prerequisites

1. **Terraform installed** (>= 1.0)
   ```bash
   # macOS
   brew install terraform
   
   # Or download from https://www.terraform.io/downloads
   ```

2. **GCP credentials configured**
   ```bash
   # Option 1: Application Default Credentials (recommended)
   gcloud auth application-default login
   
   # Option 2: Service account key file
   export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account-key.json"
   ```

3. **GCP project with billing enabled**

## Setup

### 1. Configure Variables

Copy the example variables file:
```bash
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` with your values:
```hcl
gcp_project_id = "your-gcp-project-id"
gcp_region     = "us-central1"
bucket_name    = "immich-backups"
bucket_location = "US"
storage_class  = "ARCHIVE"
```

### 2. Initialize Terraform

```bash
cd terraform
terraform init
```

### 3. Review Plan

```bash
terraform plan
```

This will show you what resources will be created:
- GCS bucket: `immich-backups`
- Lifecycle rules for automatic cleanup

### 4. Apply Configuration

```bash
terraform apply
```

Type `yes` when prompted to create the resources.

### 5. Verify

Check that the bucket was created:
```bash
gsutil ls gs://immich-backups
```

Or view in the GCP Console: https://console.cloud.google.com/storage/browser

## Configuration

### Bucket Settings

- **Storage Class**: ARCHIVE (coldest/cheapest)
- **Location**: US (multi-region)
- **Versioning**: Disabled (not needed for append-only backups)
- **Lifecycle Rule**: Optional auto-deletion of database backups after 10 years

### Customization

Edit `terraform.tfvars` to customize:
- Bucket name
- Storage class (STANDARD, NEARLINE, COLDLINE, ARCHIVE)
- Location/region
- Lifecycle rules

## Remote State (Optional)

For production use, consider storing Terraform state in GCS:

1. Create a state bucket:
   ```bash
   gsutil mb -p YOUR_PROJECT_ID gs://your-terraform-state-bucket
   ```

2. Uncomment and configure the backend in `main.tf`:
   ```hcl
   backend "gcs" {
     bucket = "your-terraform-state-bucket"
     prefix = "immich-backups"
   }
   ```

3. Re-initialize:
   ```bash
   terraform init
   ```

## Updating

To modify the bucket configuration:

1. Edit `terraform.tfvars` or `*.tf` files
2. Review changes: `terraform plan`
3. Apply: `terraform apply`

## Destroying

⚠️ **Warning**: This will delete the bucket and all backups!

```bash
terraform destroy
```

## Outputs

After applying, Terraform will output:
- `bucket_name`: The created bucket name
- `bucket_url`: The bucket URL

Use these values when configuring the backup secrets.

## Integration with Backup System

After creating the bucket:

1. Update `components/immich/overlays/prod/backup-secrets.enc.yaml`:
   - Set `GCS_BUCKET` to the bucket name
   - Set `GCS_CREDENTIALS` to your service account JSON

2. Encrypt the secrets:
   ```bash
   sops -e -i components/immich/overlays/prod/backup-secrets.enc.yaml
   ```

## Troubleshooting

### Authentication Errors

If you get authentication errors:
```bash
gcloud auth application-default login
```

### Bucket Already Exists

If the bucket name is taken, either:
1. Choose a different name in `terraform.tfvars`
2. Or delete the existing bucket (if it's safe to do so)

### Permission Errors

Ensure your GCP credentials have:
- Storage Admin role (for bucket creation)
- Or at minimum: Storage Object Admin + Storage Bucket Admin

