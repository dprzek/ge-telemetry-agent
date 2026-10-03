#!/usr/bin/env bash
# ==============================================================================
# Gemini Enterprise Telemetry Pipeline - Backward compatibility with deploy_pipeline.sh
# ==============================================================================
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ENGINE_ID="${1:-${GEMINI_ENGINE_ID:-}}"
PROJECT_ID="${2:-${GOOGLE_CLOUD_PROJECT:-}}"
LOCATION="${3:-${GOOGLE_CLOUD_LOCATION:-eu}}"
DATASET_ID="${4:-gemini_enterprise_telemetry}"

ARGS=()
if [ -n "${ENGINE_ID}" ]; then ARGS+=("${ENGINE_ID}"); fi
if [ -n "${PROJECT_ID}" ]; then ARGS+=("--project" "${PROJECT_ID}"); fi
if [ -n "${LOCATION}" ]; then ARGS+=("--location" "${LOCATION}"); fi
if [ -n "${DATASET_ID}" ]; then ARGS+=("--dataset" "${DATASET_ID}"); fi

exec python3 "${ROOT_DIR}/deploy.py" "${ARGS[@]}"
