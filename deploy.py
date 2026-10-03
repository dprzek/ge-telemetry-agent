#!/usr/bin/env python3
"""
Integrated Installer for the Gemini Enterprise Telemetry, Adoption and Observability Pipeline.
Runs a complete "Zero-Touch" deployment in a single command:
  1. Auto-detection of the project, location and engine (supports friendly app names).
  2. Automatic enablement of the observability configuration on the engine (no manual cURL!).
  3. Configuration of the BigQuery dataset, Cloud Logging sink and IAM permissions.
  4. Backfill of historical logs.
  5. Deployment of deduplicated SQL analytical views in BigQuery.
  6. Deployment of the visual dashboard in Cloud Monitoring.
  7. Deployment and publication of the Telemetry Agent in Gemini Enterprise.
"""

import sys
import os
import json
import time
import argparse
import subprocess
import urllib.request
import google.auth
from google.auth.transport.requests import Request
from google.cloud import bigquery

# Import local modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "scripts")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "cli")))
from backfill_logs_to_bigquery import run_backfill, init_streaming_tables
from telemetry_service import TelemetryService

def get_auth_token():
    creds, _ = google.auth.default()
    creds.refresh(Request())
    return creds.token

def get_default_project():
    proj = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if proj:
        return proj
    try:
        res = subprocess.run(["gcloud", "config", "get-value", "project"], stdout=subprocess.PIPE, text=True, check=True)
        val = res.stdout.strip()
        if val:
            return val
    except Exception:
        pass
    try:
        _, creds_proj = google.auth.default()
        if creds_proj:
            return creds_proj
    except Exception:
        pass
    return None

def resolve_engine(project_id, location, engine_hint, token):
    """
    Matches an Engine ID or an application name to an engine in the project.
    Supports projects with a single engine or multiple engines.
    """
    api_host = f"{location}-discoveryengine.googleapis.com" if location != "global" else "discoveryengine.googleapis.com"
    url = f"https://{api_host}/v1alpha/projects/{project_id}/locations/{location}/collections/default_collection/engines"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "X-Goog-User-Project": project_id})
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            engines = data.get("engines", [])
            
            if engine_hint:
                engine_hint_clean = engine_hint.strip()
                # 1. Exact match on the full engine ID (e.g. my-gemini-app_1234567890)
                for eng in engines:
                    e_id = eng.get("name", "").split("/")[-1]
                    if e_id.lower() == engine_hint_clean.lower():
                        return e_id
                # 2. Exact match on displayName
                for eng in engines:
                    e_id = eng.get("name", "").split("/")[-1]
                    d_name = eng.get("displayName", "")
                    if d_name.lower() == engine_hint_clean.lower():
                        return e_id
                # 3. Engine ID prefix match ({engine_hint}_...)
                for eng in engines:
                    e_id = eng.get("name", "").split("/")[-1]
                    if e_id.lower().startswith(f"{engine_hint_clean.lower()}_"):
                        return e_id
                # 4. A direct engine ID was provided (or no permission to list engines)
                return engine_hint_clean

            # When the user did not provide an engine identifier:
            if not engines:
                return None
            if len(engines) == 1:
                auto_id = engines[0].get("name", "").split("/")[-1]
                print(f"[*] Detected 1 engine in the project: '{auto_id}' (used by default).")
                return auto_id

            # The project contains MULTIPLE engines - do not assume a single engine!
            print("======================================================================")
            print(f"[!] Multiple Gemini Enterprise engines detected in project '{project_id}' ({len(engines)} engines).")
            print("    Specify a concrete Engine ID:")
            for eng in engines:
                e_id = eng.get("name", "").split("/")[-1]
                d_name = eng.get("displayName", "")
                print(f"      • Engine ID: {e_id}  (Name: '{d_name}')")
            print("\n    Usage:")
            print("      ./deploy.sh <ENGINE_ID> [--project PROJECT_ID] [--location LOCATION]")
            print("      ./deploy.sh --engine <ENGINE_ID>")
            print("======================================================================")
            return None
    except Exception as e:
        print(f"    (Warning while looking up engines: {e})")
    return engine_hint

def ensure_required_apis(project_id):
    """Automatically verifies and enables the required Google Cloud APIs."""
    required_apis = [
        "aiplatform.googleapis.com",
        "discoveryengine.googleapis.com",
        "bigquery.googleapis.com",
        "logging.googleapis.com",
        "monitoring.googleapis.com",
        "cloudtrace.googleapis.com",
        "cloudresourcemanager.googleapis.com",
        "cloudbuild.googleapis.com",
        "pubsub.googleapis.com",
        "cloudfunctions.googleapis.com",
        "run.googleapis.com",
        "eventarc.googleapis.com",
        "artifactregistry.googleapis.com"
    ]
    print("--> [1/8] Verifying and enabling the required Google Cloud APIs...")
    try:
        # Quick check of already enabled APIs to avoid waiting idly
        cmd_check = ["gcloud", "services", "list", f"--project={project_id}", "--enabled", "--format=value(config.name)"]
        res = subprocess.run(cmd_check, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        enabled_services = set(res.stdout.split()) if res.returncode == 0 else set()
        
        missing = [api for api in required_apis if api not in enabled_services]
        if not missing:
            print("    ✔ All required APIs are already enabled.")
        else:
            print(f"    Enabling {len(missing)} missing APIs: {', '.join(missing)} (please wait)...")
            cmd = ["gcloud", "services", "enable", *missing, f"--project={project_id}", "--quiet"]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("    ✔ Required Google Cloud APIs have been enabled.")
        
        # Ensure service identities for Vertex AI and Cloud Logging exist in new projects
        for svc in ["aiplatform.googleapis.com", "logging.googleapis.com"]:
            try:
                subprocess.run(
                    ["gcloud", "beta", "services", "identity", "create", f"--service={svc}", f"--project={project_id}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
                )
            except Exception:
                pass
    except Exception as e:
        print(f"    (API verification: {e})")

def enable_engine_observability(project_id, location, engine_id, token, enable_sensitive_logging=True):
    """Automatically enables OpenTelemetry on the engine while adhering to AI Governance standards.
    
    Enabling sensitiveLoggingEnabled=True is required by the Discovery Engine backend
    so that the user identity (useriamprincipal / UPN) is not masked as '<elided>'
    in the activity logs. Protection of prompt content and M365 snippets is provided by
    the Exclusion Filter on the Cloud Logging gateway.
    """
    mode_label = "OpenTelemetry + Identity Attribution" if enable_sensitive_logging else "OpenTelemetry (Anonymized / <elided>)"
    print(f"--> [2/8] Configuring observability for engine '{engine_id}' [{mode_label}]...")
    api_host = f"{location}-discoveryengine.googleapis.com" if location != "global" else "discoveryengine.googleapis.com"
    engine_url = f"https://{api_host}/v1alpha/projects/{project_id}/locations/{location}/collections/default_collection/engines/{engine_id}"
    
    # 1. Check the current configuration
    try:
        req = urllib.request.Request(engine_url, headers={"Authorization": f"Bearer {token}", "X-Goog-User-Project": project_id})
        with urllib.request.urlopen(req) as resp:
            eng_data = json.loads(resp.read().decode())
            obs_cfg = eng_data.get("observabilityConfig", {})
            curr_obs = obs_cfg.get("observabilityEnabled", False)
            curr_sens = obs_cfg.get("sensitiveLoggingEnabled", False)
            if curr_obs and curr_sens == enable_sensitive_logging:
                print(f"    ✔ Engine observability ({mode_label}) is already correctly configured.")
                return
    except Exception as e:
        print(f"    Failed to fetch the engine state: {e}")

    # 2. Enable / update observability via PATCH
    patch_url = f"{engine_url}?updateMask=observabilityConfig"
    patch_payload = {
        "observabilityConfig": {
            "observabilityEnabled": True,
            "sensitiveLoggingEnabled": enable_sensitive_logging
        }
    }
    mode_text = "with user identity attribution (UPN)" if enable_sensitive_logging else "in anonymized mode (<elided>)"
    try:
        patch_req = urllib.request.Request(
            patch_url,
            data=json.dumps(patch_payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Goog-User-Project": project_id
            },
            method="PATCH"
        )
        with urllib.request.urlopen(patch_req) as resp:
            print(f"    ✔ Successfully configured engine observability {mode_text}.")
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8")
        print(f"    ⚠️ Warning while configuring observability: HTTP {e.code} - {err_msg}")

def setup_governance_exclusion_filter(project_id, token, filter_name="drop-gemini-prompts-and-grounding"):
    """Configures the Enterprise Governance Exclusion Filter on the _Default sink in Cloud Logging.
    
    Prevents raw prompt content (gen_ai.user.message) as well as responses and
    M365 document snippets (gen_ai.choice) from being stored in Cloud Logging,
    while preserving user identities and operational metrics.
    """
    print(f"--> [3a/8] Configuring the Enterprise Governance Exclusion Filter in Cloud Logging...")
    url = f"https://logging.googleapis.com/v2/projects/{project_id}/sinks/_Default?updateMask=exclusions"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Goog-User-Project": project_id,
        "Content-Type": "application/json"
    }
    # 1. Fetch existing exclusions
    get_url = f"https://logging.googleapis.com/v2/projects/{project_id}/sinks/_Default"
    exclusions = []
    try:
        req_get = urllib.request.Request(get_url, headers={"Authorization": f"Bearer {token}", "X-Goog-User-Project": project_id})
        with urllib.request.urlopen(req_get) as resp:
            data = json.loads(resp.read().decode())
            exclusions = data.get("exclusions", [])
    except Exception as e:
        print(f"    (Fetching exclusions of the _Default sink: {e})")

    filter_expr = 'logName=~"discoveryengine.googleapis.com%2Fgen_ai.user.message" OR logName=~"discoveryengine.googleapis.com%2Fgen_ai.choice"'
    existing = next((x for x in exclusions if x.get("name") == filter_name), None)
    if existing and existing.get("filter") == filter_expr and not existing.get("disabled", False):
        print(f"    ✔ Exclusion Filter '{filter_name}' in Cloud Logging is already active.")
        return

    new_exclusions = [x for x in exclusions if x.get("name") != filter_name]
    new_exclusions.append({
        "name": filter_name,
        "description": "Enterprise Governance: Drop raw user prompts and M365 grounding responses",
        "filter": filter_expr,
        "disabled": False
    })
    payload = {"exclusions": new_exclusions}
    try:
        patch_req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="PATCH")
        with urllib.request.urlopen(patch_req) as resp:
            print(f"    ✔ Configured Exclusion Filter '{filter_name}' on the _Default sink (0 prompts and 0 M365 in Cloud Logging).")
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8")
        print(f"    ⚠️ Warning while configuring the Exclusion Filter: HTTP {e.code} - {err}")

def ensure_external_member_domains(project_id):
    """Opt-in: relaxes constraints/iam.allowedPolicyMemberDomains at PROJECT level (allowAll: true).

    Required when Gemini Enterprise / BigQuery roles must be granted to identities outside the
    organization's primary domain (e.g. external consultants or auditors using @gmail.com accounts).
    Best effort: requires roles/orgpolicy.policyAdmin; failures are reported, never fatal.
    """
    print("--> [3b/8] Org Policy: allowing external member domains on the project (iam.allowedPolicyMemberDomains)...")
    try:
        res = subprocess.run(
            ["gcloud", "org-policies", "describe", "iam.allowedPolicyMemberDomains", f"--project={project_id}", "--effective", "--format=json"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )
        if res.returncode == 0 and res.stdout.strip():
            rules = json.loads(res.stdout).get("spec", {}).get("rules", [])
            if any(r.get("allowAll") for r in rules):
                print("    ✔ Project already allows all member domains (allowAll: true).")
                return
        policy = {
            "name": f"projects/{project_id}/policies/iam.allowedPolicyMemberDomains",
            "spec": {"inheritFromParent": False, "rules": [{"allowAll": True}]},
        }
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(policy, fh)
            policy_path = fh.name
        res = subprocess.run(["gcloud", "org-policies", "set-policy", policy_path, f"--project={project_id}", "--quiet"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        os.unlink(policy_path)
        if res.returncode == 0:
            print("    ✔ Project-level policy set: iam.allowedPolicyMemberDomains -> allowAll: true.")
        else:
            print(f"    ⚠️ Could not set the org policy (requires roles/orgpolicy.policyAdmin): {res.stderr.strip()[-300:]}")
    except Exception as e:
        print(f"    ⚠️ Org Policy step skipped: {e}")

def setup_bigquery_and_sink(project_id, location, dataset_id, sink_name="gemini-enterprise-telemetry-sink"):
    """Creates the BigQuery dataset, the Cloud Logging sink and grants permissions to the service account."""
    print(f"--> [3/8] Configuring BigQuery dataset '{dataset_id}' and the Cloud Logging sink...")
    bq_client = bigquery.Client(project=project_id)
    
    # 1. BigQuery dataset
    ds_ref = bigquery.DatasetReference(project_id, dataset_id)
    try:
        dataset = bq_client.get_dataset(ds_ref)
        print(f"    Dataset '{dataset_id}' already exists.")
    except Exception:
        dataset = bigquery.Dataset(ds_ref)
        dataset.location = location.upper() if location != "global" else "EU"
        dataset.description = "Gemini Enterprise adoption telemetry, inference operations and audit logs"
        dataset = bq_client.create_dataset(dataset)
        print(f"    ✔ Created dataset '{dataset_id}' in location {dataset.location}.")

    # 2. Cloud Logging sink to BigQuery
    sink_filter = (
        '(logName=~"cloudaudit.googleapis.com" AND protoPayload.serviceName="discoveryengine.googleapis.com") OR '
        '(logName=~"discoveryengine.googleapis.com%2Fgemini_enterprise_user_activity" AND '
        'NOT (jsonPayload.logMetadata.methodName="Search" OR '
        'jsonPayload.logMetadata.methodName="ConverseConversation" OR '
        'jsonPayload.logMetadata.methodName="AnswerQuery")) OR '
        'logName=~"discoveryengine.googleapis.com%2Fgen_ai.client.inference.operation.details"'
    )
    destination = f"bigquery.googleapis.com/projects/{project_id}/datasets/{dataset_id}"
    
    # Check whether the sink exists
    cmd_check = ["gcloud", "logging", "sinks", "describe", sink_name, f"--project={project_id}", "--format=value(writerIdentity)"]
    res = subprocess.run(cmd_check, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    if res.returncode == 0 and res.stdout.strip():
        writer_identity = res.stdout.strip()
        print(f"    Sink '{sink_name}' already exists. Updating the filter...")
        cmd_upd = [
            "gcloud", "logging", "sinks", "update", sink_name,
            f"--log-filter={sink_filter}",
            f"--project={project_id}"
        ]
        subprocess.run(cmd_upd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        cmd_create = [
            "gcloud", "logging", "sinks", "create", sink_name, destination,
            f"--log-filter={sink_filter}",
            f"--project={project_id}",
            "--use-partitioned-tables"
        ]
        subprocess.run(cmd_create, check=True, stdout=subprocess.PIPE)
        res = subprocess.run(cmd_check, stdout=subprocess.PIPE, check=True, text=True)
        writer_identity = res.stdout.strip()
        print(f"    ✔ Created Cloud Logging sink '{sink_name}'.")

    # 2b. Exclude Search queries (where query is a std::string, which collides with the RECORD structure in StreamAssist)
    exclusion_name = "exclude-search-queries"
    exclusion_filter = (
        'logName=~"discoveryengine.googleapis.com%2Fgemini_enterprise_user_activity" AND '
        '(jsonPayload.logMetadata.methodName="Search" OR '
        'jsonPayload.logMetadata.methodName="ConverseConversation" OR '
        'jsonPayload.logMetadata.methodName="AnswerQuery")'
    )
    res_excl = subprocess.run(
        ["gcloud", "logging", "sinks", "describe", sink_name, f"--project={project_id}", "--format=value(exclusions.name)"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
    existing_exclusions = set((res_excl.stdout or "").replace(";", " ").split())
    if exclusion_name in existing_exclusions:
        print(f"    ✔ Search query exclusion rule '{exclusion_name}' already present on the Cloud Logging sink.")
    else:
        cmd_excl = [
            "gcloud", "logging", "sinks", "update", sink_name,
            f"--project={project_id}",
            f"--add-exclusion=name={exclusion_name},description=Exclude Search methods with string query to prevent schema collision with StreamAssist Record,filter={exclusion_filter}"
        ]
        subprocess.run(cmd_excl, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("    ✔ Configured the Search query exclusion rule on the Cloud Logging sink.")

    # 3. Grant BigQuery Data Editor to the sink's service account
    writer_sa = writer_identity.replace("serviceAccount:", "")
    entries = list(dataset.access_entries)
    if not any(getattr(e, "entity_id", None) == writer_sa for e in entries):
        entries.append(bigquery.AccessEntry(role="roles/bigquery.dataEditor", entity_type="userByEmail", entity_id=writer_sa))
        dataset.access_entries = entries
        
        # In newly created GCP projects the sink's service account (gcp-sa-logging)
        # is provisioned on the fly and needs a few seconds for identity propagation in the IAM directory.
        max_attempts = 6
        for attempt in range(1, max_attempts + 1):
            try:
                bq_client.update_dataset(dataset, ["access_entries"])
                break
            except Exception as e:
                err_str = str(e)
                if ("does not exist" in err_str or "400" in err_str) and attempt < max_attempts:
                    sleep_sec = 3 * attempt
                    print(f"    [*] Waiting for IAM identity propagation of the sink service account ({writer_sa}) in BigQuery (attempt {attempt}/{max_attempts}, retrying in {sleep_sec}s)...")
                    time.sleep(sleep_sec)
                    try:
                        dataset = bq_client.get_dataset(ds_ref)
                        entries = list(dataset.access_entries)
                        if not any(getattr(e, "entity_id", None) == writer_sa for e in entries):
                            entries.append(bigquery.AccessEntry(role="roles/bigquery.dataEditor", entity_type="userByEmail", entity_id=writer_sa))
                        dataset.access_entries = entries
                    except Exception:
                        pass
                else:
                    # Fallback: grant the permission at the project level via gcloud
                    print(f"    [*] Granting BigQuery Data Editor to the sink service account at the project level...")
                    res = subprocess.run([
                        "gcloud", "projects", "add-iam-policy-binding", project_id,
                        f"--member={writer_identity}",
                        "--role=roles/bigquery.dataEditor",
                        "--condition=None",
                        "--quiet"
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if res.returncode == 0:
                        break
                    raise
    print("    ✔ Cloud Logging sink and permissions have been configured successfully.")
    return bq_client

def deploy_sql_views(bq_client, project_id, dataset_id):
    """Deploys the SQL analytical views in BigQuery with dynamic parameter substitution."""
    print("--> [5/8] Deploying SQL analytical views in BigQuery...")
    # Guarantee the existence and consistency of tables and columns before creating the views
    init_streaming_tables(bq_client, project_id, dataset_id)

    sql_path = os.path.join(os.path.dirname(__file__), "bigquery", "telemetry_views.sql")
    with open(sql_path, "r", encoding="utf-8") as f:
        raw_sql = f.read()
    
    formatted_sql = raw_sql.format(project_id=project_id, dataset_id=dataset_id)
    job = bq_client.query(formatted_sql)
    job.result()
    print("    ✔ SQL analytical views have been successfully created / updated in BigQuery.")

def deploy_monitoring_dashboard(project_id):
    """Creates the operational dashboard in Cloud Monitoring if it does not exist yet."""
    print("--> [6/8] Checking the Cloud Monitoring dashboard...")
    try:
        res = subprocess.run(["gcloud", "monitoring", "dashboards", "list", f"--project={project_id}", "--format=value(displayName)"], stdout=subprocess.PIPE, text=True)
        if "Gemini Enterprise" in res.stdout:
            print("    Cloud Monitoring dashboard already exists.")
            return
        dash_file = os.path.join(os.path.dirname(__file__), "monitoring", "gemini_enterprise_telemetry_dashboard.json")
        subprocess.run(["gcloud", "monitoring", "dashboards", "create", f"--config-from-file={dash_file}", f"--project={project_id}"], check=True)
        print("    ✔ Successfully created the Cloud Monitoring dashboard.")
    except Exception as e:
        print(f"    (Cloud Monitoring dashboard: {e})")

def ensure_client_dependencies():
    """Verifies and synchronizes runtime library versions with Vertex AI Agent Runtime."""
    try:
        import google.adk
        import google.api_core
        if google.adk.__version__ == "2.9.0" and google.api_core.__version__ == "2.35.0":
            return
    except Exception:
        pass
    print("[*] Synchronizing runtime library versions with Vertex AI Agent Runtime (google-adk==2.9.0)...")
    try:
        subprocess.run([
            sys.executable, "-m", "pip", "install", "--quiet", "--upgrade",
            "google-adk==2.9.0", "google-api-core==2.35.0",
            "google-cloud-aiplatform>=1.70.0", "google-cloud-bigquery>=3.25.0",
            "google-cloud-monitoring>=2.21.0", "cloudpickle>=3.0.0"
        ], check=False)
    except Exception:
        pass

def deploy_telemetry_agent(project_id, location, engine_id, dataset_id="gemini_enterprise_telemetry", reasoning_engine=None, recreate=False, share_with_all_users=False, vertex_location=None):
    """Deploys the dynamic ADK Agent to Vertex AI Agent Runtime and registers it in Gemini Enterprise."""
    print("--> [7/8] Deploying the Telemetry Agent in Gemini Enterprise (Dynamic ADK Agent on Vertex AI Agent Runtime)...")
    agent_script = os.path.join(os.path.dirname(__file__), "agent", "deploy_adk_agent.py")
    cmd = [
        sys.executable, agent_script,
        f"--project={project_id}",
        f"--location={location}",
        f"--engine={engine_id}",
        f"--dataset={dataset_id}"
    ]
    if reasoning_engine:
        cmd.append(f"--reasoning-engine={reasoning_engine}")
    if vertex_location:
        cmd.append(f"--vertex-location={vertex_location}")
    if recreate:
        cmd.append("--recreate")
    if share_with_all_users:
        cmd.append("--share-with-all-users")
    subprocess.run(cmd, check=True)

def main():
    ensure_client_dependencies()
    parser = argparse.ArgumentParser(description="Integrated Installer for the Gemini Enterprise Telemetry Pipeline")
    parser.add_argument("engine", nargs="?", default=None, help="Engine ID (e.g. my-gemini-app_1234567890) or a friendly application name")
    parser.add_argument("--project", "-p", default=None, help="Google Cloud Project ID")
    parser.add_argument("--location", "-l", default=None, help="Discovery Engine location (e.g. eu, global, us)")
    parser.add_argument("--engine", "-e", dest="engine_flag", default=None, help="Gemini Enterprise Engine ID")
    parser.add_argument("--engine-id", dest="engine_id_flag", default=None, help="Explicit Gemini Enterprise Engine ID")
    parser.add_argument("--dataset", "-d", default="gemini_enterprise_telemetry", help="BigQuery dataset ID")
    parser.add_argument("--vertex-location", default=os.environ.get("VERTEX_LOCATION"), help="Vertex AI Agent Engine region (default derived from --location: eu->europe-west1, us/global->us-central1)")
    parser.add_argument("--skip-backfill", action="store_true", help="Skip the log backfill")
    parser.add_argument("--reasoning-engine", default=None, help="Existing Vertex AI Reasoning Engine resource to reuse")
    parser.add_argument("--recreate", action="store_true", help="Force creation of a new Reasoning Engine even if an old one exists")
    parser.add_argument("--disable-sensitive-logging", action="store_true", default=False,
                        help="Disables the sensitiveLoggingEnabled flag on the engine (note: Google will mask user UPNs as <elided>)")
    parser.add_argument("--keep-raw-prompts", action="store_true", default=False,
                        help="Disables the Exclusion Filter in Cloud Logging, keeping raw prompt content and M365 snippets in the _Default bucket")
    parser.add_argument("--enable-sensitive-logging", action="store_true", default=True,
                        help="Enables identity attribution in engine observability (enabled by default in combination with the Exclusion Filter)")
    parser.add_argument("--share-with-all-users", action="store_true", default=False,
                        help="Shares the telemetry agent with all users in the organization (ALL_USERS). By default the agent is private (RESTRICTED - visible only to the deployer)")
    parser.add_argument("--allow-external-members", action="store_true", default=False,
                        help="Set constraints/iam.allowedPolicyMemberDomains to allowAll at project level so external identities (e.g. @gmail.com auditors) can be granted Gemini Enterprise / BigQuery roles")
    parser.add_argument("--skip-auto-observability", action="store_true", default=False,
                        help="Skip deploying the Auto-Observability Enabler automation for newly created agents")
    args = parser.parse_args()

    project_id = args.project or os.environ.get("GOOGLE_CLOUD_PROJECT") or get_default_project()
    if not project_id:
        print("Error: No GCP project ID specified. Use the --project <PROJECT_ID> option or set the GOOGLE_CLOUD_PROJECT variable.")
        sys.exit(1)
    location = args.location or os.environ.get("GOOGLE_CLOUD_LOCATION") or "eu"
    dataset_id = args.dataset
    engine_hint = args.engine_id_flag or args.engine_flag or args.engine or os.environ.get("GEMINI_ENGINE_ID")

    token = get_auth_token()
    engine_id = resolve_engine(project_id, location, engine_hint, token)
    if not engine_id:
        print("Error: No Gemini Enterprise engine specified or found. Provide the engine ID: ./deploy.sh <ENGINE_ID> or the --engine <ENGINE_ID> option.")
        sys.exit(1)

    enable_sensitive = not args.disable_sensitive_logging
    apply_exclusion = not args.keep_raw_prompts
    match_info = f" (matched from: '{engine_hint}')" if engine_hint and engine_hint != engine_id else ""
    if enable_sensitive and apply_exclusion:
        privacy_info = "Enterprise Governance (Identity Attribution + Exclusion Filter: 0 prompts & 0 M365)"
    elif enable_sensitive:
        privacy_info = "Full prompt audit (no Exclusion Filter in Cloud Logging)"
    else:
        privacy_info = "Anonymized / Zero PII (sensitiveLoggingEnabled: false, UPN: <elided>)"

    print("======================================================================")
    print("Starting the automated deployment of the Gemini Enterprise telemetry pipeline")
    print(f"  Project:      {project_id}")
    print(f"  Location:     {location}")
    print(f"  Engine (ID):  {engine_id}{match_info}")
    print(f"  Dataset:      {dataset_id}")
    print(f"  Governance:   {privacy_info}")
    print("======================================================================")

    # 1. Verify and enable APIs
    ensure_required_apis(project_id)

    # 2. Engine observability (Auto-Enable with AI Governance)
    enable_engine_observability(project_id, location, engine_id, token, enable_sensitive_logging=enable_sensitive)

    # 3. BigQuery and Cloud Logging sink
    bq_client = setup_bigquery_and_sink(project_id, location, dataset_id)

    # 3a. Enterprise Governance: Exclusion Filter in Cloud Logging
    if apply_exclusion:
        setup_governance_exclusion_filter(project_id, token)

    # 3b. Optional: allow external member domains (Org Policy) for cross-domain IAM grants
    if args.allow_external_members:
        ensure_external_member_domains(project_id)

    # 4. Log backfill
    if not args.skip_backfill:
        print("--> [4/8] Backfilling logs from the last 30 days...")
        run_backfill(bq_client, project_id, dataset_id, days=30)
    else:
        print("--> [4/8] Skipped the log backfill (--skip-backfill).")

    # 5. SQL views
    deploy_sql_views(bq_client, project_id, dataset_id)

    # 6. Cloud Monitoring dashboard
    deploy_monitoring_dashboard(project_id)

    # 7. Gemini Enterprise agent
    deploy_telemetry_agent(
        project_id, location, engine_id, dataset_id,
        reasoning_engine=args.reasoning_engine,
        recreate=args.recreate,
        share_with_all_users=args.share_with_all_users,
        vertex_location=args.vertex_location
    )

    # 8. Auto-Observability Enabler (event-driven for newly created agents)
    if not args.skip_auto_observability:
        from scripts.setup_auto_observability import setup_auto_observability_enabler
        setup_auto_observability_enabler(project_id, location, engine_id)
    else:
        print("--> [8/8] Skipped the Auto-Observability Enabler configuration (--skip-auto-observability).")

    print("\n======================================================================")
    print("✔ Deployment completed successfully! All components are active.")
    print("======================================================================")
    print("Quick start:")
    print(f"  Utilization CLI: python3 cli/telemetry_cli.py --project {project_id} --location {location} --engine {engine_id} utilization --daily")
    print(f"  Adoption CLI:    python3 cli/telemetry_cli.py --project {project_id} --location {location} --engine {engine_id} adoption")
    print(f"  BigQuery View:   `{project_id}.{dataset_id}.v_user_daily_utilization`")
    print("======================================================================")

if __name__ == "__main__":
    main()
