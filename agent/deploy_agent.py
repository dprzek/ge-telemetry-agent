#!/usr/bin/env python3
"""
Deploy the Gemini Enterprise Telemetry, Adoption & Observability Agent to Discovery Engine.
Deploys to the specified engine and assistant.
Grounds the agent with real-time BigQuery metrics, day-by-day user activity,
OpenTelemetry traces & spans, and operational observability metrics.
"""

import sys
import os
import json
import urllib.request
import google.auth
from google.auth.transport.requests import Request

# Add cli directory to path to import TelemetryService
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "cli")))
from telemetry_service import TelemetryService

import argparse

parser = argparse.ArgumentParser(description="Deploy the Gemini Enterprise Telemetry Agent")
parser.add_argument("pos_project", nargs="?", default=None, help="GCP Project ID")
parser.add_argument("pos_location", nargs="?", default=None, help="Engine location (e.g. eu)")
parser.add_argument("pos_engine", nargs="?", default=None, help="Engine ID or application name")
parser.add_argument("--project", "-p", dest="flag_project", default=None, help="GCP Project ID")
parser.add_argument("--location", "-l", dest="flag_location", default=None, help="Engine location (e.g. eu)")
parser.add_argument("--engine", "-e", dest="flag_engine", default=None, help="Engine ID or application name")
parser.add_argument("--assistant", "-a", dest="flag_assistant", default=None, help="Assistant ID")
parser.add_argument("--share-with-all-users", action="store_true", default=False,
                    help="Shares the agent with all users in the organization (ALL_USERS). By default the agent is private (RESTRICTED - access only for the deployer)")
args, _ = parser.parse_known_args()

PROJECT_ID = args.flag_project or args.pos_project or os.environ.get("GOOGLE_CLOUD_PROJECT")
if not PROJECT_ID:
    try:
        import subprocess
        PROJECT_ID = subprocess.check_output(["gcloud", "config", "get-value", "project"], text=True).strip()
    except Exception:
        pass
if not PROJECT_ID:
    print("Error: No GCP project ID provided. Use the --project <PROJECT_ID> argument or the GOOGLE_CLOUD_PROJECT variable.")
    sys.exit(1)

LOCATION = args.flag_location or args.pos_location or os.environ.get("GOOGLE_CLOUD_LOCATION", "eu")
ENGINE_ID = args.flag_engine or args.pos_engine or os.environ.get("GEMINI_ENGINE_ID")
ASSISTANT_ID = args.flag_assistant or os.environ.get("GEMINI_ASSISTANT_ID", "default_assistant")

print("======================================================================")
print("Deploying the Gemini Enterprise Telemetry, Adoption & Observability Agent")
print(f"  Project:      {PROJECT_ID}")
print(f"  Location:     {LOCATION}")
print(f"  Engine:       {ENGINE_ID or '(auto-detect)'}")
print(f"  Assistant:    {ASSISTANT_ID}")
print("======================================================================")

# 1. Fetch current telemetry data and observability indicators
print("--> Fetching activity, daily breakdown and OpenTelemetry metrics...")
service = TelemetryService(project_id=PROJECT_ID, location=LOCATION, engine_id=ENGINE_ID)
if service.engine_id:
    if service.engine_id != ENGINE_ID:
        print(f"  ✔ Matched engine identifier: '{ENGINE_ID}' -> '{service.engine_id}'")
    ENGINE_ID = service.engine_id
else:
    print("Error: No Gemini Enterprise engine found in the project. Provide the engine identifier with the --engine <ENGINE_ID> flag.")
    sys.exit(1)
users_summary = service.get_user_summary()
users_daily = service.get_user_daily_breakdown()
adoption = service.get_daily_adoption(days=14)
quotas = service.get_realtime_quotas()
obs_metrics = service.get_observability_metrics(days=7)
recent_traces = service.get_recent_traces(limit=5)

telemetry_data = {
    "observability_settings": obs_metrics.get("observability_settings", {}),
    "operational_metrics": {
        "total_agent_sessions": obs_metrics.get("total_agent_sessions"),
        "total_agent_turns": obs_metrics.get("total_agent_turns"),
        "conversational_depth_turns_per_session": obs_metrics.get("conversational_depth_turns_per_session"),
        "total_sessions_with_tool": obs_metrics.get("total_sessions_with_tool"),
        "tool_adoption_rate_pct": obs_metrics.get("tool_adoption_rate_pct"),
        "total_engine_requests": obs_metrics.get("total_engine_requests"),
        "avg_time_to_first_token_ms": obs_metrics.get("avg_time_to_first_token_ms"),
        "avg_request_total_latency_ms": obs_metrics.get("avg_request_total_latency_ms")
    },
    "recent_opentelemetry_traces": recent_traces,
    "total_tracked_users": len(users_summary),
    "user_summary_all_time": users_summary,
    "user_daily_activity_breakdown": users_daily,
    "organization_daily_adoption": adoption[:10],
    "quotas": quotas
}

instruction_text = f"""
You are the Gemini Enterprise Telemetry, Adoption & Observability Specialist.
Your goal is to monitor, analyze and report on adoption indicators, performance, OpenTelemetry traces & spans, quotas, and per-user utilization with a precise day-by-day breakdown.

### CURRENT TELEMETRY DATA, OBSERVABILITY AND TRACES (FROM BIGQUERY & CLOUD MONITORING):
```json
{json.dumps(telemetry_data, indent=2)}
```

### YOUR TASKS AND RULES OF CONDUCT:

1. **CONVERSATION START - STATEMENT OF METRICS OFFERED**:
   - ALWAYS, when you start a new conversation with a user/administrator (e.g. on a greeting such as 'Hi', 'Good morning', 'Hello', the first query, or a question like 'What metrics do you offer?', 'What can you do?'), begin your response with an official statement presenting the scope of metrics offered:
   
   > "Hello! I am your Gemini Enterprise Telemetry & Observability Agent. 
   > I monitor deployment, adoption, platform performance and quotas across your entire organization.
   > 
   > Here are the 4 main pillars of metrics I provide for you:
   > 1. 📊 **Day-by-Day User Utilization**:
   >    - Precise per-user activity broken down by specific days (events, assistant queries, deep research, agent creation, tokens consumed).
   >    - Rankings of the most active users (Power Users) and retention statistics.
   > 2. 🚀 **Adoption & Engagement Metrics**:
   >    - Active users: DAU (Daily Active Users), WAU (Weekly) and MAU (Monthly).
   >    - Conversational Depth: average number of turns per user session.
   >    - Tool Adoption Rate: percentage of sessions using tools (Search, Connectors, Code Execution).
   > 3. ⏱️ **Observability, OpenTelemetry Traces & Latencies**:
   >    - Access to distributed OpenTelemetry traces (Cloud Trace) and execution spans (`StreamAssist`, `execute_tool`, `invoke_connector`).
   >    - Time-to-First-Token (TTFT) and total response times of the engine and tools.
   >    - Status of the engine's observability configuration (`observabilityConfig`: OpenTelemetry traces and sensitive input/output logging).
   > 4. 🛡️ **Pooled Quotas & Overages**:
   >    - Organization pools: assistant queries, agent creation, Deep Research, image/video generation, WTU credits for developer tools.
   >    - Monitoring of reset schedules (at midnight Pacific Time PT or rolling windows)."

   - If the user has already asked a specific substantive question in their first message (e.g. about the activity of a specific user), present the welcome statement above in a condensed form, and then IMMEDIATELY provide a comprehensive answer to the question asked.

2. **Per-User Utilization Analysis Broken Down by Day**:
   - When the user asks about the adoption or activity of a specific user by day (e.g. "tell me what the adoption of user user@example.com looks like on specific days"):
     * **ALWAYS present a table broken down by individual dates** with data from the `user_daily_activity_breakdown` section.
     * Table columns: `Date (YYYY-MM-DD)`, `Events`, `Assistant Queries`, `Deep Research`, `Agents Created`, `Tokens Consumed`.
     * Below the table add a short trend summary (e.g. on which days the user was most active).
     * **NEVER say that daily data is missing** - you have the full history of every day in `user_daily_activity_breakdown`.

3. **Observability, OpenTelemetry Traces and Performance Metrics**:
   - Use data from `operational_metrics` and `recent_opentelemetry_traces`:
     * Report the Conversational Depth indicator (e.g. 1.82 turns/session).
     * Report the Tool Adoption Rate.
     * Present response times: Time-to-First-Token (TTFT) and total time.
     * Point out Cloud Trace identifiers (`trace_id` and `span_id`) for technical audits.

4. **Quota Monitoring (Quotas & Overages)**:
   - According to the official Google Cloud Gemini Enterprise documentation (https://docs.cloud.google.com/gemini/enterprise/docs/quotas-and-overages):
     * **Assistant Queries**: 160 (Standard) / 200 (Plus) per user per day (organization pool). Reset at midnight PT.
     * **Agent Creation**: 1 (Standard) / 10 (Plus) per user per day (organization pool). Reset at midnight PT.
     * **Deep Research**: 3 (Standard) / 10 (Plus) per user per day (organization pool). Reset at midnight PT.
     * **Image Generation**: 5 (Standard) / 10 (Plus) per user per day. Reset at midnight PT.
     * **Video Generation**: 2 (Standard) / 3 (Plus) per user per day. Reset at midnight PT.
     * **AI Developer Tools (WTU / Antigravity)**: $10 (Standard) / $15 (Plus) per user in a rolling 7-day window.
     * **Data Capacity and Indexing**: 30 GiB (Standard) / 75 GiB (Plus) per user in the regional pool.

5. **Tips for Administrators (BigQuery & Monitoring)**:
   - Dataset: `{PROJECT_ID}.gemini_enterprise_telemetry`:
     * `v_user_daily_utilization` - per-user utilization broken down by day
     * `v_observability_traces` - distributed OpenTelemetry traces (Cloud Trace)
     * `v_user_summary` - aggregated statistics
     * `v_daily_adoption` - organization DAU/WAU trends
     * `v_feature_usage` - breakdown by feature

6. **Communication Style**:
   - Respond professionally and clearly, using well-formatted markdown tables and bullet points.
""".strip()

node = {
    "id": "telemetry_coordinator",
    "displayName": "Telemetry & Observability Coordinator",
    "llmAgentNode": {
        "model": "gemini-2.5-flash",
        "description": "Expert in Gemini Enterprise telemetry, quotas, adoption, OpenTelemetry traces and user utilization analysis (including day-by-day breakdown).",
        "instruction": instruction_text,
        "selectedTools": {
            "tool": [
                {"name": "googleSearch"}
            ]
        }
    }
}

starter_prompts = [
    {"text": "Show the daily user utilization report"},
    {"text": "Check the observability status and engagement metrics"},
    {"text": "Present the status of quotas and usage"}
]

agent_payload = {
    "displayName": "Gemini Enterprise Telemetry & Adoption Monitor",
    "description": "Administrator agent providing telemetry reporting, user adoption metrics, OpenTelemetry observability analysis, quota monitoring, and detailed per-user daily utilization tracking.",
    "sharingConfig": {
        "scope": "ALL_USERS" if args.share_with_all_users else "RESTRICTED"
    },
    "agentInvocationSpec": {
        "invocationMode": "AUTOMATIC"
    },
    "observabilityConfig": {
        "observabilityEnabled": True,
        "sensitiveLoggingEnabled": True
    },
    "starterPrompts": starter_prompts,
    "icon": {"content": ""},
    "lowCodeAgentDefinition": {
        "nodes": [node],
        "rootAgentId": "telemetry_coordinator",
        "draftDisplayName": "Gemini Enterprise Telemetry & Adoption Monitor",
        "draftDescription": "Expert in Gemini Enterprise telemetry, quotas, adoption, OpenTelemetry traces and user utilization analysis (including day-by-day breakdown).",
        "draftStarterPrompts": starter_prompts,
        "draftIcon": {"content": ""}
    }
}

# 3. Obtain an OAuth2 token and call the AgentService REST API
credentials, _ = google.auth.default()
if not credentials.valid:
    credentials.refresh(Request())
token = credentials.token

api_host = f"{LOCATION}-discoveryengine.googleapis.com" if LOCATION != "global" else "discoveryengine.googleapis.com"
base_url = f"https://{api_host}/v1alpha/projects/{PROJECT_ID}/locations/{LOCATION}/collections/default_collection/engines/{ENGINE_ID}/assistants/{ASSISTANT_ID}/agents"

# Check whether an agent with the same name already exists (idempotency)
existing_agent_id = None
try:
    list_req = urllib.request.Request(base_url, headers={
        "Authorization": f"Bearer {token}",
        "X-Goog-User-Project": PROJECT_ID
    })
    with urllib.request.urlopen(list_req) as resp:
        agents_data = json.load(resp)
        for a in agents_data.get("agents", []):
            d_name = a.get("displayName", "")
            if d_name in [agent_payload["displayName"], "Telemetry & Observability Coordinator"] or "telemetry" in d_name.lower():
                existing_agent_id = a.get("name", "").split("/")[-1]
                break
except Exception as e:
    pass

try:
    if existing_agent_id:
        print(f"--> Found existing agent (ID: {existing_agent_id}). Updating...")
        agent_url = f"{base_url}/{existing_agent_id}?updateMask=displayName,description,lowCodeAgentDefinition,sharingConfig,agentInvocationSpec,starterPrompts,icon,observabilityConfig"
        req = urllib.request.Request(
            agent_url,
            data=json.dumps(agent_payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Goog-User-Project": PROJECT_ID
            },
            method="PATCH"
        )
        with urllib.request.urlopen(req) as response:
            result = json.load(response)
            agent_name = result.get("name", f"{base_url}/{existing_agent_id}")
            agent_id = existing_agent_id
            print(f"✔ Agent configuration updated successfully!")
    else:
        req = urllib.request.Request(
            base_url,
            data=json.dumps(agent_payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Goog-User-Project": PROJECT_ID
            },
            method="POST"
        )
        with urllib.request.urlopen(req) as response:
            result = json.load(response)
            agent_name = result.get("name", "")
            agent_id = agent_name.split("/")[-1]
            print(f"✔ New agent created successfully!")

    # Publish the agent revision (:publish)
    publish_url = f"{base_url}/{agent_id}:publish"
    pub_req = urllib.request.Request(
        publish_url,
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Goog-User-Project": PROJECT_ID
        },
        method="POST"
    )
    with urllib.request.urlopen(pub_req) as pub_resp:
        pass
    print(f"✔ Active agent revision published (:publish)!")
    print(f"  Agent Name:   {agent_name}")
    print(f"  Agent ID:     {agent_id}")
    print(f"  Status:       {result.get('state', 'PRIVATE')}")

except urllib.error.HTTPError as e:
    err_body = e.read().decode("utf-8")
    print(f"Agent deployment error: HTTP {e.code} - {err_body}")
    sys.exit(1)
except Exception as e:
    print(f"Unexpected error: {e}")
    sys.exit(1)

print("======================================================================")
print("Agent deployment completed successfully!")
print("======================================================================")
