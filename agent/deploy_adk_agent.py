#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Deploy Dynamic ADK Agent to Vertex AI Agent Runtime (Reasoning Engine)
and register it in Gemini Enterprise.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
import google.auth
from google.auth.transport.requests import Request
from google.cloud import storage

import vertexai
from vertexai import agent_engines
from vertexai.agent_engines import _utils

# Add current directory to path so we can import the agent
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent.adk_telemetry_agent as telemetry_agent_module
from agent.adk_telemetry_agent import root_agent

# Ensure cloudpickle embeds the module bytecode directly into the pickle blob
# so the remote Reasoning Engine container doesn't fail with No module named 'agent'
cloudpickle = _utils._import_cloudpickle_or_raise()
cloudpickle.register_pickle_by_value(telemetry_agent_module)


def map_location_to_vertex_region(location):
    """Maps a Gemini Enterprise multi-region (eu / us / global) to a Vertex AI Agent Engine region."""
    loc = (location or "eu").lower()
    if loc == "eu":
        return "europe-west1"
    if loc in ("us", "global"):
        return "us-central1"
    if "-" in loc:
        return loc  # already a regional location
    return "europe-west1"


def get_access_token(credentials):
    if not credentials.valid:
        credentials.refresh(Request())
    return credentials.token


def get_project_number(project_id, credentials):
    try:
        res = subprocess.run(
            ["gcloud", "projects", "describe", project_id, "--format=value(projectNumber)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True
        )
        pnum = res.stdout.strip()
        if pnum:
            return pnum
    except Exception:
        pass
    try:
        url = f"https://cloudresourcemanager.googleapis.com/v1/projects/{project_id}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {get_access_token(credentials)}",
                "X-Goog-User-Project": project_id
            }
        )
        with urllib.request.urlopen(req) as resp:
            data = json.load(resp)
            return data.get("projectNumber")
    except Exception as e:
        print(f"[WARN] Could not automatically retrieve projectNumber: {e}")
        return None


def resolve_engine_id(project_id, location, engine_hint, credentials):
    api_host = f"{location}-discoveryengine.googleapis.com" if location != "global" else "discoveryengine.googleapis.com"
    token = get_access_token(credentials)
    
    # 1. Check directly
    if engine_hint:
        direct_url = f"https://{api_host}/v1alpha/projects/{project_id}/locations/{location}/collections/default_collection/engines/{engine_hint}"
        req = urllib.request.Request(direct_url, headers={"Authorization": f"Bearer {token}", "X-Goog-User-Project": project_id})
        try:
            with urllib.request.urlopen(req) as resp:
                if resp.status == 200:
                    return engine_hint
        except urllib.error.HTTPError as e:
            if e.code != 404:
                return engine_hint

    # 2. List engines
    list_url = f"https://{api_host}/v1alpha/projects/{project_id}/locations/{location}/collections/default_collection/engines"
    req = urllib.request.Request(list_url, headers={"Authorization": f"Bearer {token}", "X-Goog-User-Project": project_id})
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.load(resp)
            engines = data.get("engines", [])
            if not engine_hint and engines:
                if len(engines) == 1:
                    return engines[0].get("name", "").split("/")[-1]
                print(f"[!] Multiple engines ({len(engines)}) detected in project '{project_id}'. Specify the target engine with the --engine <ENGINE_ID> option.")
                for eng in engines:
                    print(f"    - Engine ID: {eng.get('name', '').split('/')[-1]} (Name: {eng.get('displayName', '')})")
                sys.exit(1)
            for eng in engines:
                eid = eng.get("name", "").split("/")[-1]
                dname = eng.get("displayName", "")
                if engine_hint and (eid.lower() == engine_hint.lower() or dname.lower() == engine_hint.lower() or eid.lower().startswith(f"{engine_hint.lower()}_")):
                    return eid
    except Exception as e:
        print(f"[WARN] Error listing engines: {e}")
        
    return engine_hint


def ensure_staging_bucket(project_id, location, credentials):
    bucket_name = f"{project_id}-vertex-staging-{location}"
    client = storage.Client(project=project_id, credentials=credentials)
    try:
        bucket = client.get_bucket(bucket_name)
        print(f"[OK] Using existing staging bucket: gs://{bucket_name}")
        return f"gs://{bucket_name}"
    except Exception:
        print(f"[*] Creating staging bucket gs://{bucket_name} in region {location}...")
        try:
            client.create_bucket(bucket_name, location=location)
            print(f"[OK] Bucket created: gs://{bucket_name}")
            return f"gs://{bucket_name}"
        except Exception as e:
            print(f"[WARN] Could not create bucket: {e}")
            return f"gs://{bucket_name}"


def delete_existing_agents(project_id, project_number, location, engine_id, credentials, target_display_name):
    """Deletes old versions of the agent with the given name or the old static agent."""
    api_host = f"{location}-discoveryengine.googleapis.com" if location != "global" else "discoveryengine.googleapis.com"
    token = get_access_token(credentials)
    list_url = f"https://{api_host}/v1alpha/projects/{project_number or project_id}/locations/{location}/collections/default_collection/engines/{engine_id}/assistants/default_assistant/agents"
    req = urllib.request.Request(list_url, headers={"Authorization": f"Bearer {token}", "X-Goog-User-Project": project_id})
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.load(resp)
            agents = data.get("agents", [])
            for ag in agents:
                dname = ag.get("displayName", "")
                ag_name = ag.get("name", "")
                if dname == target_display_name or "Telemetry" in dname:
                    print(f"[*] Deleting previous agent version: {dname} ({ag_name})...")
                    del_req = urllib.request.Request(
                        f"https://{api_host}/v1alpha/{ag_name}",
                        headers={"Authorization": f"Bearer {token}", "X-Goog-User-Project": project_id},
                        method="DELETE"
                    )
                    try:
                        urllib.request.urlopen(del_req)
                        print(f"[OK] Old agent deleted: {ag_name}")
                    except Exception as de:
                        print(f"[WARN] Error deleting old agent: {de}")
    except Exception as e:
        print(f"[WARN] Could not retrieve the list of existing agents: {e}")


def register_adk_agent_in_gemini(project_id, project_number, location, engine_id, reasoning_engine_resource_name, credentials, share_with_all_users=False):
    api_host = f"{location}-discoveryengine.googleapis.com" if location != "global" else "discoveryengine.googleapis.com"
    token = get_access_token(credentials)
    url = f"https://{api_host}/v1alpha/projects/{project_number or project_id}/locations/{location}/collections/default_collection/engines/{engine_id}/assistants/default_assistant/agents"
    
    scope = "ALL_USERS" if share_with_all_users else "RESTRICTED"
    payload = {
        "displayName": "Gemini Enterprise Telemetry & Adoption Agent",
        "description": "Dynamic ADK agent for real-time Gemini Enterprise telemetry, utilization and adoption.",
        "state": "ENABLED",
        "sharingConfig": {
            "scope": scope
        },
        "observabilityConfig": {
            "observabilityEnabled": True,
            "sensitiveLoggingEnabled": True
        },
        "adk_agent_definition": {
            "tool_settings": {
                "tool_description": "Tool for retrieving real-time telemetry, user adoption, token consumption, response times and quotas in Gemini Enterprise."
            },
            "provisioned_reasoning_engine": {
                "reasoning_engine": reasoning_engine_resource_name
            }
        }
    }
    
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Goog-User-Project": project_id
        },
        method="POST"
    )
    
    try:
        with urllib.request.urlopen(req) as resp:
            res = json.load(resp)
            print(f"[OK] Agent successfully registered in Gemini Enterprise!")
            print(f"     Name: {res.get('displayName')}")
            print(f"     Resource: {res.get('name')}")
            print(f"     State: {res.get('state')}")
            sharing_scope = res.get('sharingConfig', {}).get('scope', scope)
            sharing_desc = "All users in the organization (ALL_USERS)" if sharing_scope == "ALL_USERS" else "Deployer only (RESTRICTED)"
            print(f"     Sharing: {sharing_scope} ({sharing_desc})")
            return res
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        print(f"[ERROR] Error registering agent in Gemini Enterprise: HTTP {e.code}")
        print(f"        Details: {err_body}")
        raise


def ensure_reasoning_engine_permissions(project_id, project_number, dataset_id, credentials):
    """Automatically verifies and grants IAM permissions for the Vertex Reasoning Engine service accounts."""
    # 0. Ensure the Vertex AI Service Identity is created for new projects
    try:
        subprocess.run(
            ["gcloud", "beta", "services", "identity", "create", "--service=aiplatform.googleapis.com", f"--project={project_id}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
        )
    except Exception:
        pass

    sa_emails = [
        f"service-{project_number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com",
        f"service-{project_number}@gcp-sa-aiplatform.iam.gserviceaccount.com",
    ]
    roles = [
        "roles/bigquery.jobUser",
        "roles/monitoring.viewer",
        "roles/cloudtrace.user"
    ]

    for sa_email in sa_emails:
        print(f"[*] Verifying and configuring IAM permissions for the Reasoning Engine service account: {sa_email}")
        # 1. Project-level roles (BigQuery Job User, Monitoring Viewer, Cloud Trace User)
        for role in roles:
            try:
                cmd = [
                    "gcloud", "projects", "add-iam-policy-binding", project_id,
                    f"--member=serviceAccount:{sa_email}",
                    f"--role={role}",
                    "--condition=None"
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                print(f"    ✔ Granted role {role} to {sa_email}")
            except Exception as e:
                print(f"    [INFO] Status of role {role} for {sa_email}: verification completed ({e})")

        # 2. READER access on the BigQuery dataset
        try:
            from google.cloud import bigquery
            from google.cloud.bigquery import AccessEntry
            bq_client = bigquery.Client(project=project_id, credentials=credentials)
            dataset = bq_client.get_dataset(dataset_id)
            current_entries = list(dataset.access_entries)
            already_has_access = any(
                entry.entity_id == sa_email and entry.role in ("READER", "WRITER", "OWNER")
                for entry in current_entries
            )
            if not already_has_access:
                current_entries.append(AccessEntry(role="READER", entity_type="userByEmail", entity_id=sa_email))
                dataset.access_entries = current_entries
                bq_client.update_dataset(dataset, ["access_entries"])
                print(f"    ✔ Granted READER permission on BigQuery dataset '{dataset_id}' to {sa_email}")
            else:
                print(f"    ✔ Account {sa_email} already has READER permission on BigQuery dataset '{dataset_id}'")
        except Exception as bqe:
            print(f"    [INFO] BigQuery dataset permission verification for {sa_email}: {bqe}")


def main():
    parser = argparse.ArgumentParser(description="Deploy Dynamic ADK Telemetry Agent to Vertex AI Agent Runtime & Gemini Enterprise")
    default_proj = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("PROJECT_ID")
    if not default_proj:
        try:
            _, default_proj = google.auth.default()
        except Exception:
            default_proj = ""
    default_engine = os.environ.get("GEMINI_ENGINE_ID", "")
    parser.add_argument("--project", "-p", default=default_proj, help="Google Cloud Project ID")
    parser.add_argument("--engine", default=default_engine, help="Discovery Engine / Gemini Enterprise Engine ID or application name")
    parser.add_argument("--engine-id", dest="engine_id_flag", default=None, help="Explicit Discovery Engine identifier (Engine ID)")
    parser.add_argument("--location", default="eu", help="Gemini Enterprise Location (eu, global, us)")
    parser.add_argument("--vertex-location", default=None, help="Vertex AI Reasoning Engine region (default: derived from --location: eu->europe-west1, us->us-central1, global->us-central1)")
    parser.add_argument("--dataset", default="gemini_enterprise_telemetry", help="BigQuery Dataset ID")
    parser.add_argument("--reasoning-engine", default=None, help="Existing Vertex AI Reasoning Engine resource name to reuse")
    parser.add_argument("--recreate", action="store_true", help="Forces creation of a new Reasoning Engine even if an old one exists")
    parser.add_argument("--share-with-all-users", action="store_true", default=False,
                        help="Shares the agent with all users in the organization (ALL_USERS). By default the agent is visible only to the deployer (RESTRICTED).")
    args = parser.parse_args()

    engine_input = args.engine_id_flag or args.engine
    if not args.vertex_location:
        args.vertex_location = map_location_to_vertex_region(args.location)
    if not engine_input and not default_engine:
        pass  # resolve_engine_id will list engines or error clearly

    credentials, _ = google.auth.default()
    project_number = get_project_number(args.project, credentials)
    engine_id = resolve_engine_id(args.project, args.location, engine_input, credentials)

    print(f"============================================================")
    print(f"  DEPLOY DYNAMIC ADK AGENT -> VERTEX AI AGENT RUNTIME       ")
    print(f"============================================================")
    print(f"  Project:         {args.project} ({project_number})")
    print(f"  Gemini Engine:   {engine_id} ({args.location})")
    print(f"  Vertex Region:   {args.vertex_location}")
    print(f"  BigQuery Dataset:{args.dataset}")
    print(f"============================================================")

    # 1. Staging bucket
    staging_bucket = ensure_staging_bucket(args.project, args.vertex_location, credentials)

    # 2. Vertex AI initialization
    print(f"[*] Initializing Vertex AI ({args.project}, {args.vertex_location})...")
    vertexai.init(
        project=args.project,
        location=args.vertex_location,
        staging_bucket=staging_bucket
    )

    # 3. Ensure IAM permissions for the Reasoning Engine account
    ensure_reasoning_engine_permissions(args.project, project_number, args.dataset, credentials)

    # 4. Deploy the Agent to Vertex AI Reasoning Engine
    engine_resource_name = args.reasoning_engine
    if not engine_resource_name and not args.recreate:
        from vertexai.preview import reasoning_engines as re_preview
        try:
            candidate_engines = list(re_preview.ReasoningEngine.list())
            matching = [
                ce for ce in candidate_engines 
                if ce.display_name == "Gemini Enterprise Telemetry & Adoption Engine"
            ]
            if matching:
                candidate_name = matching[0].resource_name
                print(f"[*] Detected existing Vertex AI Reasoning Engine: {candidate_name}")
                print(f"    Verifying operational health (health-check)...")
                try:
                    test_re = agent_engines.get(candidate_name)
                    import asyncio
                    async def _verify():
                        async for event in test_re.streaming_agent_run_with_events(request_json='{"message":{"parts":[{"text":"ping"}]}}'):
                            if isinstance(event, dict) and (event.get('code') or event.get('errorCode')):
                                raise RuntimeError(event.get('message') or event.get('errorMessage'))
                            break
                    asyncio.run(_verify())
                    engine_resource_name = candidate_name
                    print(f"    ✔ Existing Reasoning Engine is working correctly and responding to queries.")
                except Exception as test_err:
                    print(f"    [!] Existing Reasoning Engine reports an error ({test_err}).")
                    print(f"        Automatically deploying a new, fixed version...")
                    engine_resource_name = None
        except Exception:
            pass

    if not engine_resource_name:
        print(f"[*] Deploying ADK Agent to Vertex AI Reasoning Engine...")
        if hasattr(root_agent, 'mode') and root_agent.mode is None:
            root_agent.mode = 'chat'
        print(f"    Agent: {root_agent.name} (Tools: {len(root_agent.tools)}, Mode: {getattr(root_agent, 'mode', 'default')})")
        
        os.environ["BIGQUERY_PROJECT"] = args.project
        os.environ["BIGQUERY_DATASET"] = args.dataset

        try:
            engine = agent_engines.create(
                agent_engine=root_agent,
                display_name="Gemini Enterprise Telemetry & Adoption Engine",
                description="Managed ADK Agent Runtime for Gemini Enterprise Telemetry & Adoption",
                env_vars={
                    "BIGQUERY_PROJECT": args.project,
                    "BIGQUERY_DATASET": args.dataset,
                },
                extra_packages=["agent"],
                requirements=[
                    "google-adk==2.9.0",
                    "google-api-core==2.35.0",
                    "google-cloud-aiplatform",
                    "google-cloud-bigquery>=3.25.0",
                    "google-cloud-monitoring>=2.21.0",
                ]
            )
            engine_resource_name = engine.resource_name
            print(f"[OK] Vertex AI Reasoning Engine deployed!")
            print(f"     Resource: {engine_resource_name}")
        except Exception as e:
            print(f"[WARN] An error or timeout occurred while waiting for create(): {e}")
            print(f"[*] Verifying whether the Reasoning Engine was successfully created on Vertex AI...")
            import time
            from vertexai.preview import reasoning_engines as re_preview
            for _ in range(6):
                time.sleep(10)
                try:
                    candidate_engines = list(re_preview.ReasoningEngine.list())
                    matching = [
                        ce for ce in candidate_engines 
                        if ce.display_name == "Gemini Enterprise Telemetry & Adoption Engine"
                    ]
                    if matching:
                        engine_resource_name = matching[0].resource_name
                        print(f"    ✔ Detected active Reasoning Engine on the Vertex AI platform: {engine_resource_name}")
                        break
                except Exception:
                    pass
            if not engine_resource_name:
                raise
    else:
        print(f"[*] Using Reasoning Engine: {engine_resource_name}")

    # 4. Remove old agent instances in Gemini Enterprise
    print(f"[*] Cleaning up previous agent instances in Gemini Enterprise...")
    delete_existing_agents(args.project, project_number, args.location, engine_id, credentials, "Gemini Enterprise Telemetry & Adoption Agent")

    # 5. Register the new ADK Agent in Gemini Enterprise
    print(f"[*] Registering the Agent in the Gemini Enterprise application...")
    res = register_adk_agent_in_gemini(
        args.project,
        project_number,
        args.location,
        engine_id,
        engine_resource_name,
        credentials,
        share_with_all_users=args.share_with_all_users
    )

    print(f"\n============================================================")
    print(f"  DEPLOYMENT COMPLETED SUCCESSFULLY!                         ")
    print(f"============================================================")
    print(f"  Agent:           {res.get('displayName')}")
    print(f"  Gemini Agent ID: {res.get('name')}")
    print(f"  Reasoning Engine:{engine_resource_name}")
    print(f"  Status:          {res.get('state')}")
    print(f"============================================================")


if __name__ == "__main__":
    main()
