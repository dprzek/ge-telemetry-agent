variable "project_id" {
  description = "Google Cloud project ID"
  type        = string
}

variable "region" {
  description = "Google Cloud location for Gemini Enterprise and BigQuery"
  type        = string
  default     = "EU"
}

variable "dataset_id" {
  description = "BigQuery dataset ID for Gemini Enterprise telemetry"
  type        = string
  default     = "gemini_enterprise_telemetry"
}

variable "sink_name" {
  description = "Cloud Logging sink name"
  type        = string
  default     = "gemini-enterprise-telemetry-sink"
}
