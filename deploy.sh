#!/usr/bin/env bash
# ==============================================================================
# Gemini Enterprise Telemetry Suite - Zero-Touch One-Command Deployment Wrapper
# Usage:
#   ./deploy.sh [ENGINE_ID_OR_NAME] [--project <PROJECT_ID>] [--location <LOC>] [--enable-sensitive-logging] [--recreate]
# Examples:
#   # Default deployment in Privacy-by-Design mode (recommended for AI Governance):
#   ./deploy.sh my-gemini-app_1234567890 --project my-project-id --location eu
#
#   # Optional deployment with full prompt content auditing:
#   ./deploy.sh my-gemini-app_1234567890 --enable-sensitive-logging
# ==============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Make sure the client dependencies are 100% consistent with the Vertex AI Agent Runtime environment version
if ! python3 -c "import google.adk, google.api_core; assert google.adk.__version__ == '2.9.0' and google.api_core.__version__ == '2.35.0'" 2>/dev/null; then
    echo "[*] Synchronizing runtime library versions with Vertex AI Agent Runtime (google-adk==2.9.0)..."
    python3 -m pip install --quiet --upgrade \
        "google-adk==2.9.0" \
        "google-api-core==2.35.0" \
        "google-cloud-aiplatform>=1.70.0" \
        "google-cloud-bigquery>=3.25.0" \
        "google-cloud-monitoring>=2.21.0" \
        "cloudpickle>=3.0.0"
fi

export PYTHONUNBUFFERED=1
exec python3 "${SCRIPT_DIR}/deploy.py" "$@"
