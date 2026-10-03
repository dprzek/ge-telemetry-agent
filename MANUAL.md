# Gemini Enterprise Telemetry, Adoption and Observability Manual

This manual contains complete instructions for deploying, configuring and operating the **Gemini Enterprise Telemetry, Adoption, Quota & OpenTelemetry Observability Suite** in any Google Cloud customer environment.

---

## 1. Architecture and Telemetry Pipeline

```
                                      Gemini Enterprise
                             (Enterprise Engine / Agent Builder)
                                              │
                      ┌───────────────────────┴───────────────────────┐
                      ▼                                               ▼
         Cloud Logging and Audit Trails                   Cloud Monitoring (Real-Time)
         - discoveryengine_googleapis_com_*              - discoveryengine.googleapis.com/quota/*
         - cloudaudit_googleapis_com_activity            - agent_session_count & agent_turn_count
         - gen_ai.client.inference.operation.details     - agent_session_with_tool_count
                      │                                  - engine/time_to_first_token_latency (TTFT)
                      ▼ (Cloud Logging Sink)             - agent_total_latencies & tool_latencies
           BigQuery Dataset                                           │
           (gemini_enterprise_telemetry)                              │
           - Partitioned and clustered tables                         │
           - 7 Analytical Views:                                      │
             * v_user_daily_utilization (daily breakdown per user)    │
             * v_author_agent_usage (invocations of authored agents)  │
             * v_observability_traces (OpenTelemetry traces & spans)  │
             * v_user_summary (all-time statistics per user)          │
             * v_daily_adoption (DAU/WAU/MAU trends)                  │
             * v_feature_usage (module and feature usage)             │
             * v_token_telemetry (input, output, cached tokens)       │
                      │                                               │
                      └───────────────────────┬───────────────────────┘
                                              ▼
                         Telemetry Service and Administrator CLI
                                              │
                      ┌───────────────────────┴───────────────────────┐
                      ▼                                               ▼
         Gemini Enterprise Agent                           CLI Tools and Monitoring
         (Deployed in Agent Designer)                      - Per-user queries by day (--daily)
         - Opens with a metrics statement                  - Observability metrics and traces
         - Full utilization history by day                 - Real-time quota alerts
         - Observability and TTFT reporting                - Visual Cloud Monitoring dashboard
```

---

## 2. Gemini Enterprise Observability Architecture

The solution natively integrates with the three key observability areas of Google Cloud Gemini Enterprise. Everything is built on **official platform interfaces, with no log scraping or regex-based parsing of raw text (Zero Regex)**:

### 2.1 Managing Observability Settings ([Official Documentation](https://cloud.google.com/gemini/enterprise/docs/manage-observability-settings) / [Devsite](https://clouddocs.devsite.corp.google.com/gemini/enterprise/docs/manage-observability-settings))
Gemini Enterprise allows precise control over telemetry instrumentation at the **Engine (Assistant application)** and **Individual Agent** level:

1. **OpenTelemetry Traces and Logs Instrumentation (`observabilityEnabled`)**:
   - Records distributed execution spans, call paths, error codes, latencies (TTFT) and token volumes (input, output, reasoning).
   - **Does not record** the contents of user prompts or responses. Fully sufficient to feed the telemetry pipeline and adoption analytics!
2. **Sensitive Logging of Prompts and Responses (`sensitiveLoggingEnabled`)**:
   - Stores the full text of prompts entered by users (`gen_ai.user.message`) and the text of generated responses (`gen_ai.choice`), including quoted document snippets from external connectors (e.g. Microsoft 365, SharePoint, Google Drive).
   - *Compliance/AI Governance note*: According to the Google Cloud documentation, sensitive data is not filtered out (*"Sensitive data isn't filtered out of the audit logs"*). In corporate environments it is recommended to keep this flag set to `false` (Privacy-by-Design), unless the organization runs a formal content audit.

#### Enabling via the Google Cloud Console:
- Navigate to: **Gemini Enterprise > Configurations > Observability** (or **Agents > [Agent Name] > Configuration**).
- Turn on the toggle: **Enable instrumentation of OpenTelemetry traces and logs**.
- (Optionally, only for content auditing purposes) Turn on the toggle: **Enable logging of prompt inputs and response outputs**.

#### Enabling via the REST API (Default Privacy-by-Design mode):
```bash
curl -X PATCH \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: <PROJECT_ID>" \
  "https://<LOCATION>-discoveryengine.googleapis.com/v1alpha/projects/<PROJECT_ID>/locations/<LOCATION>/collections/default_collection/engines/<ENGINE_ID>?updateMask=observabilityConfig" \
  -d '{
    "observabilityConfig": {
      "observabilityEnabled": true,
      "sensitiveLoggingEnabled": false
    }
  }'
```

#### 2.1.1 Auto-Observability Enabler Automation for User Agents (Event-Driven)
By default, Low-Code agents (`Agent`) newly created in the Gemini Enterprise UI have an undefined `observabilityConfig` flag (`null`), so queries sent directly to them do not emit `StreamAssist` events or tokens to Cloud Logging.

So that the administrator does not have to manually enable observability for every new agent, the installer deploys a serverless automation based on an Event-Driven architecture:
1. **Cloud Audit Logs**: When a user creates a new agent through the UI or API, Google Cloud generates a `cloudaudit.googleapis.com/activity` audit entry with the method `google.cloud.discoveryengine.v1main.AgentService.CreateAgent`.
2. **Cloud Logging Sink**: The `gemini-enterprise-agent-events-sink` sink immediately forwards this entry to the Cloud Pub/Sub topic `gemini-enterprise-agent-events`.
3. **Cloud Run Function (2nd gen)**: The `ge-auto-observability-enabler` function receives the notification, extracts the identifier of the newly created agent and asynchronously issues a `PATCH .../agents/{agent_id}?updateMask=observabilityConfig` call with `observabilityConfig.observabilityEnabled: true`.
4. **Backward synchronization (Reconciliation)**: During installation the module immediately checks all previously existing agents in the engine and enables the observability flag on each of them.
5. **Operating cost**: **\$0.00 USD / month**. All components (Admin Activity Audit Logs, Pub/Sub up to 10 GB, Cloud Run up to 2M invocations per month) fit entirely within the free *Google Cloud Always Free Tier*.

---

### 2.2 Accessing OpenTelemetry Traces and Spans ([Official Documentation](https://cloud.google.com/gemini/enterprise/docs/access-traces-and-spans) / [Devsite](https://clouddocs.devsite.corp.google.com/gemini/enterprise/docs/access-traces-and-spans))
When instrumentation is active, Gemini Enterprise emits complete distributed traces to the **Google Cloud Trace** service (30-day retention):

> [!NOTE]
> **No regular expressions (Zero Regex)**:
> Event correlation in the SQL views (`v_observability_traces`, `v_user_daily_utilization`) and joining assistant logs with the model inference table (`gen_ai_client_inference_operation_details`) are performed **strictly on the native OpenTelemetry keys `trace_id` and `session_id`**. This rules out text parsing errors, Cartesian products and double counting of tokens.

- **Trace Context**: Propagated via the `x-cloud-trace-context` header and recorded in the table and the `v_observability_traces` view.
- **Trace Hierarchy and Spans**:
  * **Root Turn Span**: `AssistantService.StreamAssist` (or `AssistantService.Assist`) representing the user's entire conversation turn.
  * **Tool Execution Spans**: `execute_tool <tool_name>` (e.g. the `googleSearch` search tool, the Python code interpreter, dedicated enterprise tools).
  * **Data Connector Spans**: `invoke_connector <connector_name>` (e.g. Salesforce, Jira, Google Drive connectors).
  * **Sub-Agent Spans**: `invoke_agent <agent_name>` (task delegation in multi-agent structures).
  * **Model Inference Spans**: `model_generate_content` (LLM processing time, tokens, generation finish reason).

---

### 2.3 Accessing Cloud Monitoring Operational Metrics ([Official Documentation](https://cloud.google.com/gemini/enterprise/docs/access-metrics) / [Devsite](https://clouddocs.devsite.corp.google.com/gemini/enterprise/docs/access-metrics))
Metrics are automatically published under the `discoveryengine.googleapis.com/` namespace in Google Cloud Monitoring (6-week retention). The CLI tools and the telemetry service query them directly through the Cloud Monitoring API:

#### A. Conversational Engagement and Adoption Indicators:
- **`agent_session_count`**: Total number of conversational sessions established with agents.
- **`agent_session_with_tool_count`**: Number of sessions in which the agent actively invoked at least one tool.
  * **Tool Adoption Rate (%)** $= \frac{\text{agent\_session\_with\_tool\_count}}{\text{agent\_session\_count}} \times 100$
- **`agent_turn_count`**: Total number of turns (questions and answers) in conversations.
  * **Conversational Depth** $= \frac{\text{agent\_turn\_count}}{\text{agent\_session\_count}}$ (measures engagement and dialogue length vs. quick session abandonment).

#### B. Perceived Responsiveness and UX Quality:
- **`engine/time_to_first_token_latency` (TTFT)**: Statistical distribution of the time from the user submitting a question to the first response token being generated.
- **`agent_total_latencies`**: Distribution of the total duration of a conversational turn.
- **`tool_total_latencies`**: Execution overhead of individual external tools.

---

## 3. Prerequisites and IAM Roles

The account deploying the solution must hold the following IAM roles in the Google Cloud project:

| IAM Role | Purpose |
| :--- | :--- |
| `roles/discoveryengine.agentspaceAdmin` | Managing and deploying agents in Gemini Enterprise / Agent Designer |
| `roles/logging.configWriter` | Creating Cloud Logging sinks that route logs to BigQuery |
| `roles/bigquery.admin` | Managing the dataset, partitioned tables and SQL views |
| `roles/monitoring.editor` | Deploying dashboards and reading metrics in Cloud Monitoring |
| `roles/cloudtrace.user` | Accessing and analyzing distributed traces in Google Cloud Trace |
| `roles/resourcemanager.projectIamAdmin` | Granting BigQuery Data Editor permissions to the Cloud Logging sink identity |

---

## 4. Quick One-Command Deployment (Zero-Touch One-Command)

The entire pipeline (enabling observability, BigQuery configuration, Cloud Logging sink, IAM permissions, backfill, analytical SQL views, Cloud Monitoring dashboard and Agent deployment) is launched with a single hands-off command:

```bash
# 1. Clone the repository
git clone https://github.com/dprzek/ge-telemetry-agent.git
cd ge-telemetry-agent

# 2. Run the deployment, pointing at the engine ID (or the friendly application name)
./deploy.sh <ENGINE_ID_OR_NAME>

# Example with the exact engine ID:
./deploy.sh my-gemini-app_1234567890 --project my-project-id --location eu

# Force a redeployment of the Agent:
./deploy.sh my-gemini-app_1234567890 --recreate
```

> [!TIP]
> **What does `deploy.sh` (or `python3 deploy.py`) automate?**
> 1. **Observability auto-configuration (Privacy-by-Design)**: Automatically enables `observabilityEnabled: true` and `sensitiveLoggingEnabled: false` on the engine — protecting employee data and Microsoft 365 document snippets while still collecting 100% of operational and adoption metrics. Full prompt auditing can optionally be enabled with the `--enable-sensitive-logging` flag.
> 2. **Precise and flexible engine matching**: Accepts the exact engine identifier (`ENGINE_ID`, e.g. `my-gemini-app_1234567890`) or a friendly name. In projects with multiple engines it guards against mistakes and presents a list of available engines to choose from.
> 3. **Cloud Logging sink and permissions**: Automatically creates the BigQuery dataset in the `EU` location and the Cloud Logging sink, and grants the `roles/bigquery.dataEditor` role.
> 4. **Log backfill**: Fills in the history from the last 30 days.
> 5. **SQL analytics and Dashboard**: Deploys deduplicated SQL views and creates a dashboard in Cloud Monitoring.
> 6. **Telemetry Agent**: Creates and registers the Agent in private mode (`RESTRICTED` - accessible only to the deployer; optionally with the `--share-with-all-users` flag).

---

## 5. Manual Step-by-Step Deployment (Change Management Procedure)

In corporate environments that require approval of each individual stage, perform the following steps:

### Step 5.1: Enable Observability on the Engine (Privacy-by-Design)
```bash
curl -X PATCH \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: <PROJECT_ID>" \
  "https://<LOCATION>-discoveryengine.googleapis.com/v1alpha/projects/<PROJECT_ID>/locations/<LOCATION>/collections/default_collection/engines/<ENGINE_ID>?updateMask=observabilityConfig" \
  -d '{"observabilityConfig": {"observabilityEnabled": true, "sensitiveLoggingEnabled": false}}'
```

### Step 5.2: Create the BigQuery Dataset and the Cloud Logging Sink
```bash
./scripts/setup_bigquery_sink.sh <PROJECT_ID> <REGION> <DATASET_ID> <SINK_NAME>
```

### Step 5.3: Backfill Historical Logs
```bash
python3 scripts/backfill_logs_to_bigquery.py <PROJECT_ID> <DATASET_ID> 30
```

### Step 5.4: Deploy the Analytical SQL Views in BigQuery
```bash
sed "s/{project_id}/<PROJECT_ID>/g; s/{dataset_id}/<DATASET_ID>/g" \
  bigquery/telemetry_views.sql | bq query --project_id=<PROJECT_ID> --use_legacy_sql=false
```

### Step 5.5: Create the Dashboard in Cloud Monitoring
```bash
gcloud monitoring dashboards create \
  --config-from-file=monitoring/gemini_enterprise_telemetry_dashboard.json \
  --project=<PROJECT_ID>
```

### Step 5.6: Deploy the Dynamic ADK Agent (Vertex AI Agent Runtime) and Sharing
Deploys the ADK Agent to Vertex AI Reasoning Engine and registers it in Gemini Enterprise. By default the agent is registered in private mode (`RESTRICTED` - accessible only to the person deploying it). To share it with the whole organization, add the `--share-with-all-users` flag:
```bash
python3 agent/deploy_adk_agent.py \
  --project="<PROJECT_ID>" \
  --location="<LOCATION>" \
  --vertex-location="europe-west1" \
  --engine="<ENGINE_ID>" \
  --dataset="gemini_enterprise_telemetry" \
  [--share-with-all-users]
```

#### Granting Permissions to Users in the GCP Project:
So that employees can sign in to the Gemini Enterprise application and see the Telemetry Agent, the administrator assigns them IAM roles:
```bash
gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member="user:user@your-company.com" \
  --role="roles/discoveryengine.user"

gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member="user:user@your-company.com" \
  --role="roles/discoveryengine.agentspaceUser"
```

---

## 6. Administrator Guide: Operating Telemetry and Observability

### A. Dynamic ADK Agent in Gemini Enterprise (Reasoning Engine)

The `Gemini Enterprise Telemetry & Adoption Agent` runs in the managed **Vertex AI Agent Runtime (Reasoning Engine)** environment. Unlike static solutions, it does not inject data snapshots into the prompt. Instead, on every user question it dynamically invokes tools in real time:

- **Real-Time Tools**:
  1. `get_user_daily_utilization(user_email, days)` — fetches the user's exact daily activity from BigQuery (queries, tokens, Deep Research, agents).
  2. `get_user_summary(user_email)` — aggregate statistics or a ranking of the most active users.
  3. `get_daily_adoption(days)` — daily DAU trends and query/token volume.
  4. `get_realtime_quotas()` — checks quotas (RPM, TPM, headroom) via the Cloud Monitoring API.
  5. `get_observability_traces(days)` — response times, error states and OpenTelemetry traces.

When a user or administrator talks to the agent, the agent autonomously queries the relevant services and presents the results in structured Markdown tables.

---

### B. Using the CLI for Administrators

#### 1. Inspecting Observability, Latencies and OpenTelemetry Traces:
```bash
python3 cli/telemetry_cli.py observability --traces
```
*Example response:*
```text
=== Gemini Enterprise: Observability and OpenTelemetry Metrics ===
• Engine ID:                   <ENGINE_ID> (e.g. my-gemini-app_1234567890)
• Location:                    eu
• Observability Enabled:       True
• Sensitive Logging Enabled:   True
• Application Type:            APP_TYPE_INTRANET
--------------------------------------------------------------------
• Agent Session Count:         4600
• Conversational Turn Count:   8360
• Conversational Depth:        1.82 turns/session
• Sessions Using Tools:        0
• Tool Adoption Rate:          0.0%
• Total Query Count:           9067
• Avg Time to First Token:     47747.19 ms
• Avg Total Time:              208540.94 ms

=== Recent Distributed OpenTelemetry Traces (2 entries) ===
Time (UTC)           | Trace ID                           | Method         | User                       | Status  
--------------------------------------------------------------------------------------------------------------
2026-09-18 11:43:09  | 5379e14ddba2e5c1860cefde7554f4c3   | StreamAssist   | user@example.com           | SUCCESS 
2026-09-18 11:31:08  | bd5b4d06073dd350c3cbe37912532b4c   | StreamAssist   | user@example.com           | SUCCEEDED
```

#### 2. User Utilization and Authored Agent Invocations Report:
```bash
# Aggregate summary of all users together with authored agent statistics:
python3 cli/telemetry_cli.py utilization

# Summary of the 10 least active users (Bottom 10):
python3 cli/telemetry_cli.py utilization --bottom 10

# Detailed daily utilization of a specific employee:
python3 cli/telemetry_cli.py utilization --daily --user admin@example.com
```
*Example output (aggregate summary):*
```text
=== Gemini Enterprise User Activity Summary ===
User ID                       | Events | Queries| Deep Rsrch | Images | Agents | Author Invoc. | Org Invoc.   | Tokens 
-------------------------------------------------------------------------------------------------------------------------
admin@example.com             | 24     | 16     | 0          | 0      | 2      | 6 (2)         | 6 (2 / 1)    | 12,450 
user@example.com              | 3      | 0      | 0          | 0      | 1      | 0 (0)         | 0 (0 / 0)    | 0      
```
*Description of the new authored-agent columns:*
- **Author Invoc. (`author_agent_*`)**: Invocations of the agents created by the author, made by the author themselves, in the format: `queries (sessions)`.
- **Org Invoc. (`org_agent_*`)**: Total invocations of those agents across the whole organization in the format: `queries (sessions / unique users)`.

#### 3. Organization Adoption Trends (DAU):
```bash
python3 cli/telemetry_cli.py adoption --days 30
```

#### 4. Checking Quota Status (Quotas & Overages):
```bash
python3 cli/telemetry_cli.py quotas
```

---

### C. Example SQL Queries in BigQuery

#### 1. Query for Recent Distributed OpenTelemetry Traces:
```sql
SELECT
  timestamp,
  trace_id,
  span_id,
  user_id,
  method_name,
  answer_state,
  agent_display_name
FROM `<PROJECT_ID>.gemini_enterprise_telemetry.v_observability_traces`
ORDER BY timestamp DESC
LIMIT 20;
```

#### 2. Query for User Activity Broken Down Day by Day:
```sql
SELECT
  activity_date,
  user_id,
  total_events,
  assistant_queries,
  deep_research_count,
  agents_created,
  total_tokens
FROM `<PROJECT_ID>.gemini_enterprise_telemetry.v_user_daily_utilization`
WHERE activity_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 14 DAY)
```

#### 3. Query for Authored Agent Invocations (Self vs. Org):
```sql
SELECT
  creator_user_id,
  author_agent_invocations,
  author_agent_sessions,
  org_agent_invocations,
  org_agent_sessions,
  org_agent_unique_callers
FROM `<PROJECT_ID>.gemini_enterprise_telemetry.v_author_agent_usage`
ORDER BY org_agent_invocations DESC;
```

---

## 7. Official Gemini Enterprise Quota Summary

The table below reflects the official Google Cloud quotas for the Gemini Enterprise editions:

| Service / Feature | Standard Edition | Plus Edition | Reset Cycle | Enforcement Scope |
| :--- | :--- | :--- | :--- | :--- |
| **Assistant Queries** | 160 / user / day | 200 / user / day | Midnight PT | Organization Pool |
| **Agent Creation** | 1 / user / day | 10 / user / day | Midnight PT | Organization Pool |
| **Deep Research** | 3 / user / day | 10 / user / day | Midnight PT | Organization Pool |
| **Image Generation** | 5 / user / day | 10 / user / day | Midnight PT | Organization Pool |
| **Video Generation** | 2 / user / day | 3 / user / day | Midnight PT | Organization Pool |
| **AI Dev Tools (WTU)**| $10 / user / cycle | $15 / user / cycle | Rolling 7-day window | Organization Pool |
| **Capacity and Indexing**| 30 GiB / user | 75 GiB / user | Continuous regional pool | Project / Region |

---

## 8. Validation Procedure and the 22-Scenario Test Suite

After deploying the pipeline and the agent in the customer environment, the administrator can verify the entire environment by running the automated suite of 24 tests:

```bash
python3 tests/test_suite.py
```

### Summary of the 22 Test Scenarios:

1. **Test 01: Python Syntax and Code Hygiene (Multi-Tenant Hygiene)**
   - Verifies that the Python files compile and contain no hard-coded project / customer IDs.
2. **Test 02: BigQuery Table Schema and Partitioning**
   - Confirms daily partitioning on the `timestamp` field and clustering on `user_id`.
3. **Test 03: Idempotent Backfill (Backfill & Deduplication)**
   - Verifies the audit and inference log parser and duplicate elimination.
4. **Test 04: Compilation and Schema of the 7 BigQuery Views (SQL DDL)**
   - Checks that all 7 analytical SQL views compile correctly in BigQuery.
5. **Test 05: ADK Agent Validation (`google.adk.agents.Agent`) and Cloudpickle**
   - Confirms the ADK agent structure, the presence of dynamic tools and binary serialization.
6. **Test 06: Reasoning Engine Account IAM Permissions and `sharingConfig`**
   - Inspects the IAM permissions of the Vertex AI account and the default private mode (`RESTRICTED` - deployer only).
7. **Test 07: `get_user_daily_utilization` Tool Response**
   - Checks that the daily utilization history is generated correctly for an active user.
8. **Test 08: Correct Counting of Deep Research Sessions**
   - Verifies session isolation and the absence of double counting when research queries are retried.
9. **Test 09: Detection of Imagen Image Generation**
   - Confirms correct categorization of image-generating queries.
10. **Test 10: Counting Created Agents and Rejecting Errors**
    - Checks the counting of `CreateAgent` events and the rejection of operations that ended in error.
11. **Test 11: User Ranking and DAU Adoption Trends**
    - Confirms data aggregation in the `get_user_summary` and `get_daily_adoption` tools.
12. **Test 12: Real-Time Quotas and Headroom**
    - Verifies reading of Cloud Monitoring Quotas metrics (RPM, TPM, headroom).
13. **Test 13: OpenTelemetry Traces, TTFT Times and Errors**
    - Inspects time-to-first-token response times and OTel span correlations.
14. **Test 14: Mathematical Invariants of Events and Tokens**
    - Verifies consistency: $\text{Events} \ge \text{Queries} + \text{Deep Research} + \text{Agents}$ and $\text{Tokens} = \text{Input} + \text{Output}$.
15. **Test 15: Precision of Token Attribution by OTel Traces**
    - Checks for the absence of duplicates when correlating the model inference table with assistant sessions.
16. **Test 16: Zero-Utilization Reporting (Zero-Utilization Contract)**
    - Confirms the correctness of the response contract for users with no recorded activity.
17. **Test 17: Engine Resolver Insensitivity to Naming**
    - Inspects flexible engine matching by `display_name`, full ID, with spaces and with an empty hint.
18. **Test 18: Auto-detection of the GCP Project from the Environment**
    - Confirms resilience to missing environment variables and auto-detection via `google.auth.default()`.
19. **Test 19: Resilience to Payload Schema Evolution**
    - Verifies the log parser when fields are missing, with nested structures and camelCase/lowercase variants.
20. **Test 20: Insensitivity to Regional Configurations (eu, global, us)**
    - Checks the resilience of communication with regional Discovery Engine endpoints.
21. **Test 21: AI Governance and Privacy-by-Design Rules**
    - Verifies the default `sensitiveLoggingEnabled: True` + Exclusion Filter in Cloud Logging and `sharingConfig: RESTRICTED`.
22. **Test 22: Authored Agent Invocation Statistics (Self-Usage & Org-Wide Invariants)**
    - Verifies the author's agent invocation metrics (`author_agent_*`) and the organization's (`org_agent_*`), along with their mathematical invariants.
23. **Test 23: Opening Bottom 10 Suggestion and User Activity Sorting**
    - Confirms ascending sorting (`order_by="bottom"` / `"asc"`), the integrity of the output table columns and the ADK agent's system instruction.

---

## 9. Troubleshooting & Self-Healing

### Error 400 in Gemini Enterprise: `Reasoning Engine stream closed cleanly without producing any events`

#### Symptom:
In the Gemini Enterprise web interface, when trying to send a message to the Telemetry Agent, the following message appears:
```text
Agent returned an error (400): Agent failed with error: Reasoning Engine stream closed cleanly without producing any events (reasoning_engine=projects/.../locations/europe-west1/reasoningEngines/..., attempt=3/3)
```

#### Diagnosis and Root Cause:
When the `streaming_agent_run_with_events()` method (used by Gemini Enterprise AgentSpace) is invoked, the ADK runner method (`google/adk/runners.py`) is executed in the Vertex AI Reasoning Engine container. It checks the agent's mode attribute: `if self.agent.mode is None: self.agent.mode = 'chat'`.
If the agent object was serialized in the client environment (e.g. an old Cloud Shell image or with a different `google-adk` version), the Pydantic model lacked the `mode` field, which raised `AttributeError: 'LlmAgent' object has no attribute 'mode'` and returned an event with error code 498. Because the stream contained no text content, Gemini Enterprise aborted the call with error 400.

#### Automatic Resolution (Self-Healing in the Installer):
1. **Explicit mode declaration**: In `agent/adk_telemetry_agent.py` the `root_agent` object has the `mode="chat"` parameter explicitly defined, which guarantees its presence in the Pydantic attribute dictionary and deserialization regardless of the environment.
2. **Client dependency synchronization**: The `deploy.sh` and `deploy.py` scripts automatically install and verify the runtime libraries (`google-adk==2.9.0`, `google-api-core==2.35.0`) before serialization.
3. **Automatic Health-Check**: During deployment the script checks the existing Reasoning Engine by performing a test `streaming_agent_run_with_events()` call. If the engine reports an error (e.g. code 498 or a missing attribute), the installer automatically discards the broken instance and deploys a new, fully working version.
4. **Forced recreation (`--recreate`)**: If an immediate container rebuild needs to be forced, use the flag:
   ```bash
   ./deploy.sh <ENGINE_ID> --recreate
   ```

### Cloud Logging Sink Error: `table_invalid_schema: Cannot convert std::string to a record field ... query = 4`

#### Symptom:
The GCP project administrator receives an e-mail notification:
```text
The following log sink in a project you own had errors while routing logs. Due to this error, logs are not being routed to the sink destination.
Project ID: my-project-id
Log Sink Name: gemini-enterprise-telemetry-sink
Sink Destination: bigquery.googleapis.com/projects/.../datasets/gemini_enterprise_telemetry
Error Code: table_invalid_schema
Error Detail: Cannot convert std::string to a record field:optional .Msg_0_CLOUD_QUERY_TABLE.Msg_1_CLOUD_QUERY_TABLE_jsonpayload.Msg_15_CLOUD_QUERY_TABLE_jsonpayload_request.Msg_22_CLOUD_QUERY_TABLE_jsonpayload_request_query query = 4;
```
Routing of user activity event logs to BigQuery is suspended.

#### Diagnosis and Root Cause:
In Google Cloud Discovery Engine, two different service types operate under the same log bucket (`discoveryengine.googleapis.com/gemini_enterprise_user_activity`):
1. **SearchService** (search methods: `Search`, `ConverseConversation`, `AnswerQuery`) – the `query` field in protobuf is a plain `std::string`.
2. **AssistantService** (Gemini Enterprise / Chat / Agents methods: `StreamAssist`, `Assist`) – the `query` field in protobuf is a `RECORD` object with the structure `parts: repeated struct<text string>`.

BigQuery does not support type unions (polymorphic columns) on the same field. When a search query (`Search`) lands in a table with a `RECORD` column, the Cloud Logging Sink reports the error `Cannot convert std::string to a record field`. Conversely, if the table had the `STRING` type, `StreamAssist` chat events would be rejected to `export_errors` with the error `This field: query is not a record`.

#### Architectural Resolution:
A collision-elimination-at-source model was applied (Log Router Sink Filter):
1. **Sink Exclusion**: The `exclude-search-queries` rule on the `gemini-enterprise-telemetry-sink` sink excludes `Search`/`ConverseConversation` text queries, which are not part of the Gemini Enterprise agent telemetry.
2. **BigQuery Schema**: The `discoveryengine_googleapis_com_gemini_enterprise_user_activity` table has the native `RECORD` type with the `parts` structure, which ensures 100% error-free recording of assistant and agent interactions and the associated LLM tokens.

#### Automated Resolution (1 command – Self-Healing):
A dedicated self-repair script is available in the repository:
```bash
python3 scripts/fix_user_activity_schema.py --project <PROJECT_ID>
```
The script automatically:
1. Configures the `exclude-search-queries` exclusion rule on the Cloud Logging sink.
2. Verifies and adjusts the BigQuery table to the `RECORD` type (handling any streaming buffer limitations).
3. Recompiles the analytical views in BigQuery.
4. (Optionally) Allows an immediate backfill of data from the last N days (`--backfill-days 30`).

Alternatively, simply run a redeployment (`./deploy.sh <ENGINE_ID>`), which will automatically update the sink and view configuration.



