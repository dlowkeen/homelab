resource "google_storage_bucket" "immich_backups" {
  name          = var.bucket_name
  location      = var.bucket_location
  storage_class = var.storage_class
  project       = var.gcp_project_id

  # Prevent accidental deletion
  force_destroy = false

  # Uniform bucket-level access (recommended)
  uniform_bucket_level_access = true

  # Versioning disabled (not needed for append-only backups)
  versioning {
    enabled = false
  }

  # Lifecycle rule for automatic cleanup of old database backups
  dynamic "lifecycle_rule" {
    for_each = var.enable_lifecycle_rule ? [1] : []
    content {
      condition {
        age        = var.lifecycle_rule_age_days
        with_state = "ANY"
        matches_prefix = ["database/"]
      }
      action {
        type = "Delete"
      }
    }
  }

  # Labels for organization
  labels = {
    purpose     = "immich-backups"
    managed-by  = "terraform"
    environment = "production"
  }
}

# Output the bucket name for reference
output "bucket_name" {
  description = "Name of the created GCS bucket"
  value       = google_storage_bucket.immich_backups.name
}

output "bucket_url" {
  description = "URL of the created GCS bucket"
  value       = google_storage_bucket.immich_backups.url
}

