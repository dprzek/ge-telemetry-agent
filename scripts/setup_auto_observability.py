#!/usr/bin/env python3
"""
Module for automatically enabling observability on newly created agents in Gemini Enterprise.
Configures:
  1. Cloud Pub/Sub topic: gemini-enterprise-agent-events
  2. Cloud Logging sink: gemini-enterprise-agent-events-sink (filter on CreateAgent in Cloud Audit Logs)
  3. IAM permissions (Publisher for the sink, Admin for the function's service account)
  4. Cloud Run Function (2nd gen) deployment: ge-auto-observability-enabler
  5. Initial reconciliation of existing agents in the engine (Zero-Agent-Left-Behind)
"""

import sys
import os
import json
import re
import subprocess
import urllib.request
import google.auth
from google.auth.transport.requests import Request

def get_auth_token():
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(Request())
    return creds.token

def map_location_to_functions_region(location):
    loc_lower = location.lower()
    if loc_lower == "eu":
        return "europe-west1"
    elif loc_lower == "us":
        return "us-central1"
    elif loc_lower == "global":
        return "europe-west1"
    elif "-" in loc_lower:
        return loc_lower
    return "europe-west1"

def reconcile_existing_agents(project_id, location, engine_id, token):
    """
    Scans all existing agents in the engine and immediately enables observabilityEnabled: true on them.
    """
    api_host = f"{location}-discoveryengine.googleapis.com" if location != "global" else "discoveryengine.googleapis.com"
    url = f"https://{api_host}/v1alpha/projects/{project_id}/locations/{location}/collections/default_collection/engines/{engine_id}/assistants/default_assistant/agents"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "X-Goog-User-Project": project_id})
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            agents = data.get("agents", [])
            patched_count = 0
            for a in agents:
                a_name = a.get("name")
                obs = a.get("observabilityConfig") or {}
                if not (obs.get("observabilityEnabled") and obs.get("sensitiveLoggingEnabled")):
                    patch_url = f"https://{api_host}/v1alpha/{a_name}?updateMask=observabilityConfig"
                    patch_body = json.dumps({"observabilityConfig": {"observabilityEnabled": True, "sensitiveLoggingEnabled": True}}).encode("utf-8")
                    patch_req = urllib.request.Request(
                        patch_url, data=patch_body, method="PATCH",
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "X-Goog-User-Project": project_id}
                    )
                    with urllib.request.urlopen(patch_req) as p_resp:
                        if p_resp.status in (200, 204):
                            patched_count += 1
            return len(agents), patched_count
    except Exception as e:
        print(f"    (Warning during initial agent verification: {e})")
        return 0, 0

def setup_auto_observability_enabler(project_id, location, engine_id):
    print("--> [8/8] Configuring automatic observability enablement for new agents (Event-Driven)...")
    token = get_auth_token()
    topic_name = "gemini-enterprise-agent-events"
    sink_name = "gemini-enterprise-agent-events-sink"
    sa_name = "sa-ge-auto-obs"
    sa_email = f"{sa_name}@{project_id}.iam.gserviceaccount.com"
    fn_name = "ge-auto-observability-enabler"
    fn_region = map_location_to_functions_region(location)

    # 1. Pub/Sub Topic
    print(f"    Checking / creating Pub/Sub topic '{topic_name}'...")
    res = subprocess.run(["gcloud", "pubsub", "topics", "describe", topic_name, f"--project={project_id}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode != 0:
        subprocess.run(["gcloud", "pubsub", "topics", "create", topic_name, f"--project={project_id}", "--quiet"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"    ✔ Created Pub/Sub topic '{topic_name}'.")
    else:
        print(f"    ✔ Pub/Sub topic '{topic_name}' already exists.")

    # 2. Cloud Logging Sink
    print(f"    Configuring Cloud Logging sink '{sink_name}' (filter on CreateAgent and UpdateAgent)...")
    log_filter = (
        'logName=~"cloudaudit.googleapis.com%2Factivity" AND '
        'protoPayload.methodName=~"google.cloud.discoveryengine.v1.*\\.AgentService\\.(CreateAgent|UpdateAgent)" AND '
        'NOT protoPayload.authenticationInfo.principalEmail: "sa-ge-auto-obs"'
    )
    destination = f"pubsub.googleapis.com/projects/{project_id}/topics/{topic_name}"

    res = subprocess.run(["gcloud", "logging", "sinks", "describe", sink_name, f"--project={project_id}", "--format=value(writerIdentity)"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    if res.returncode != 0 or not res.stdout.strip():
        sink_res = subprocess.run([
            "gcloud", "logging", "sinks", "create", sink_name, destination,
            f"--log-filter={log_filter}", f"--project={project_id}", "--quiet"
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        res = subprocess.run(["gcloud", "logging", "sinks", "describe", sink_name, f"--project={project_id}", "--format=value(writerIdentity)"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    else:
        # Update the filter on the existing sink
        subprocess.run([
            "gcloud", "logging", "sinks", "update", sink_name, destination,
            f"--log-filter={log_filter}", f"--project={project_id}", "--quiet"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    
    writer_identity = res.stdout.strip()
    if writer_identity:
        subprocess.run([
            "gcloud", "pubsub", "topics", "add-iam-policy-binding", topic_name,
            f"--member={writer_identity}", "--role=roles/pubsub.publisher",
            f"--project={project_id}", "--quiet"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    print(f"    ✔ Cloud Logging sink '{sink_name}' is active (filter: CreateAgent + UpdateAgent).")

    # 3. Service account for the function
    print(f"    Verifying service account '{sa_email}'...")
    sa_check = subprocess.run(["gcloud", "iam", "service-accounts", "describe", sa_email, f"--project={project_id}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sa_check.returncode != 0:
        subprocess.run([
            "gcloud", "iam", "service-accounts", "create", sa_name,
            "--display-name=Gemini Enterprise Auto Observability Enabler",
            f"--project={project_id}", "--quiet"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    # Roles for the service account
    for role in ["roles/discoveryengine.admin", "roles/logging.logWriter", "roles/run.invoker", "roles/eventarc.eventReceiver"]:
        subprocess.run([
            "gcloud", "projects", "add-iam-policy-binding", project_id,
            f"--member=serviceAccount:{sa_email}", f"--role={role}",
            "--condition=None", "--quiet"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    # Permissions for Pub/Sub and Cloud Build
    try:
        proj_num_res = subprocess.run([
            "gcloud", "projects", "describe", project_id, "--format=value(projectNumber)"
        ], stdout=subprocess.PIPE, text=True, check=True)
        proj_num = proj_num_res.stdout.strip()
        pubsub_sa = f"serviceAccount:service-{proj_num}@gcp-sa-pubsub.iam.gserviceaccount.com"
        compute_sa = f"serviceAccount:{proj_num}-compute@developer.gserviceaccount.com"

        # Pub/Sub token creator on the function's SA (for Eventarc OIDC)
        subprocess.run([
            "gcloud", "iam", "service-accounts", "add-iam-policy-binding", sa_email,
            f"--member={pubsub_sa}", "--role=roles/iam.serviceAccountTokenCreator",
            f"--project={project_id}", "--quiet"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

        # Build permissions on the compute SA
        for b_role in ["roles/logging.logWriter", "roles/storage.objectViewer", "roles/artifactregistry.writer"]:
            subprocess.run([
                "gcloud", "projects", "add-iam-policy-binding", project_id,
                f"--member={compute_sa}", f"--role={b_role}",
                "--condition=None", "--quiet"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except Exception:
        pass

    # 4. Cloud Run Function deployment
    source_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "functions", "auto_observability_enabler"))
    print(f"    Deploying Cloud Run function (2nd gen) '{fn_name}' in region '{fn_region}'...")
    deploy_cmd = [
        "gcloud", "functions", "deploy", fn_name,
        "--gen2",
        "--runtime=python311",
        f"--region={fn_region}",
        f"--source={source_dir}",
        "--entry-point=auto_enable_observability",
        f"--trigger-topic={topic_name}",
        f"--trigger-service-account={sa_email}",
        f"--service-account={sa_email}",
        # Cost profile: 256 MiB / 1 vCPU, scale-to-zero (no idle instances), bounded fan-out
        "--memory=256Mi",
        "--cpu=1",
        "--min-instances=0",
        "--max-instances=3",
        "--timeout=60s",
        f"--set-env-vars=GCP_PROJECT={project_id},LOCATION={location},ENGINE_ID={engine_id}",
        f"--project={project_id}",
        "--quiet"
    ]
    deploy_res = subprocess.run(deploy_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if deploy_res.returncode != 0:
        raise RuntimeError(f"Cloud Run function deployment failed: {deploy_res.stderr.strip()[-2000:]}")

    # Invocation permissions: the Eventarc trigger authenticates with the function's own service account
    # (roles/run.invoker granted above). No public (allUsers) binding is required or created.
    subprocess.run([
        "gcloud", "run", "services", "add-iam-policy-binding", fn_name,
        f"--region={fn_region}", f"--member=serviceAccount:{sa_email}", "--role=roles/run.invoker",
        f"--project={project_id}", "--quiet"
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    print(f"    ✔ Function '{fn_name}' was successfully deployed and wired to CreateAgent events.")

    # 5. Initial agent reconciliation
    total_agents, patched = reconcile_existing_agents(project_id, location, engine_id, token)
    if patched > 0:
        print(f"    ✔ Initial sync: Enabled observability on {patched} of {total_agents} existing agents.")
    else:
        print(f"    ✔ Initial sync: All existing agents ({total_agents}) already have observability enabled.")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Deploys the Gemini Enterprise agent observability automation")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    parser.add_argument("--location", default="eu")
    parser.add_argument("--engine", required=True)
    args = parser.parse_args()
    setup_auto_observability_enabler(args.project, args.location, args.engine)
