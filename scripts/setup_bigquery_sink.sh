#!/usr/bin/env bash
# ==============================================================================
# Gemini Enterprise Telemetry Pipeline - BigQuery and Cloud Logging Sink Setup
# ==============================================================================
set -euo pipefail

PROJECT_ID="${1:-${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || echo '')}}"
if [ -z "${PROJECT_ID}" ]; then
  echo "Error: Missing GCP project ID. Provide it as the 1st argument or set the GOOGLE_CLOUD_PROJECT variable."
  exit 1
fi
LOCATION="${2:-EU}"
DATASET_ID="${3:-gemini_enterprise_telemetry}"
SINK_NAME="${4:-gemini-enterprise-telemetry-sink}"

echo "======================================================================"
echo "Gemini Enterprise telemetry pipeline setup"
echo "  Project:      ${PROJECT_ID}"
echo "  Location:     ${LOCATION}"
echo "  Dataset:      ${DATASET_ID}"
echo "  Sink name:    ${SINK_NAME}"
echo "======================================================================"

# 1. Create the BigQuery dataset if it does not exist yet
echo "--> Checking / creating BigQuery dataset '${DATASET_ID}' in location '${LOCATION}'..."
if ! bq show --project_id="${PROJECT_ID}" "${DATASET_ID}" >/dev/null 2>&1; then
  bq --location="${LOCATION}" --project_id="${PROJECT_ID}" mk \
    --dataset \
    --description="Gemini Enterprise adoption telemetry, inference operations and audit logs" \
    "${DATASET_ID}"
  echo "    Successfully created dataset '${DATASET_ID}'."
else
  echo "    Dataset '${DATASET_ID}' already exists."
fi

# 2. Cloud Logging filter definition
# Filters:
# - Gemini Enterprise user activity logs (queries, deep research, sessions)
# - Inference operation details (token consumption: prompt, response, cache, latencies)
# - Cloud Audit activity and data access (agent creation and modification, permissions)
LOG_FILTER='(resource.type="discoveryengine.googleapis.com/Agent" OR resource.type="consumed_api" OR resource.type="audited_resource" OR protoPayload.serviceName="discoveryengine.googleapis.com") AND (logName=~"discoveryengine.googleapis.com" OR logName=~"cloudaudit.googleapis.com")'

# 3. Create or update the Cloud Logging sink
DESTINATION="bigquery.googleapis.com/projects/${PROJECT_ID}/datasets/${DATASET_ID}"
echo "--> Checking / creating Cloud Logging sink '${SINK_NAME}'..."

if gcloud logging sinks describe "${SINK_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  echo "    Updating existing sink '${SINK_NAME}'..."
  gcloud logging sinks update "${SINK_NAME}" "${DESTINATION}" \
    --project="${PROJECT_ID}" \
    --log-filter="${LOG_FILTER}" \
    --use-partitioned-tables
else
  echo "    Creating new sink '${SINK_NAME}'..."
  gcloud logging sinks create "${SINK_NAME}" "${DESTINATION}" \
    --project="${PROJECT_ID}" \
    --log-filter="${LOG_FILTER}" \
    --use-partitioned-tables
fi

# 4. Grant the BigQuery Data Editor role to the Cloud Logging sink identity
echo "--> Fetching the sink's service identity (Writer Identity)..."
WRITER_IDENTITY=$(gcloud logging sinks describe "${SINK_NAME}" --project="${PROJECT_ID}" --format='value(writerIdentity)')
echo "    Service identity: ${WRITER_IDENTITY}"

echo "--> Granting the BigQuery Data Editor role to the sink service account..."
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="${WRITER_IDENTITY}" \
  --role="roles/bigquery.dataEditor" \
  --condition=None \
  --quiet >/dev/null

# Additional explicit grant of permissions on the BigQuery dataset itself
WRITER_SA="${WRITER_IDENTITY#serviceAccount:}"
python3 -c "
from google.cloud import bigquery
try:
    c = bigquery.Client(project='${PROJECT_ID}')
    ds = c.get_dataset('${DATASET_ID}')
    entries = list(ds.access_entries)
    if not any(e.entity_id == '${WRITER_SA}' for e in entries):
        entries.append(bigquery.AccessEntry(role='roles/bigquery.dataEditor', entity_type='userByEmail', entity_id='${WRITER_SA}'))
        ds.access_entries = entries
        c.update_dataset(ds, ['access_entries'])
except Exception:
    pass
"

echo "======================================================================"
echo "✔ BigQuery Cloud Logging sink has been configured successfully!"
echo "  Destination:      ${DESTINATION}"
echo "  Service account:  ${WRITER_IDENTITY}"
echo "======================================================================"
