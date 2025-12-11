variable "gcp_project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "gcp_region" {
  description = "GCP Region for resources"
  type        = string
  default     = "us-central1"
}

variable "bucket_name" {
  description = "Name of the GCS bucket for Immich backups"
  type        = string
  default     = "immich-backups"
}

variable "bucket_location" {
  description = "Location for the GCS bucket"
  type        = string
  default     = "US"
}

variable "storage_class" {
  description = "Storage class for the bucket (ARCHIVE is coldest/cheapest)"
  type        = string
  default     = "ARCHIVE"
}

variable "enable_lifecycle_rule" {
  description = "Enable lifecycle rule for automatic cleanup"
  type        = bool
  default     = true
}

variable "lifecycle_rule_age_days" {
  description = "Age in days before deleting old database backups"
  type        = number
  default     = 3650  # 10 years
}

