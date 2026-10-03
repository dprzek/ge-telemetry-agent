---
name: gemini-enterprise-telemetry
description: >-
  Deploy, configure and analyze telemetry, user adoption and OpenTelemetry observability in Gemini Enterprise.
  Tracks per-day user utilization, distributed traces and spans in Cloud Trace,
  operational metrics (sessions, conversation depth, tool usage rate, time to first token TTFT),
  quotas (assistant queries, agents, deep research, multimedia, WTU credits),
  BigQuery views, Cloud Monitoring dashboards and the Gemini Enterprise agent.
---

# Skill: Gemini Enterprise Telemetry and Observability Monitoring

This skill contains the procedures, automation scripts, SQL analytical views and agent configuration for monitoring and reporting on usage, adoption and performance telemetry of the **Gemini Enterprise** platform in an organization.

## Architecture and Data Flow

1. **Cloud Logging and Audit Log Sink**: Automatically captures user activity, prompt interactions, model tokens, OpenTelemetry spans and administrative actions (`CreateAgent`, `UpdateAgent`).
2. **BigQuery Telemetry Dataset (`gemini_enterprise_telemetry`)**: Stores partitioned logs with 6 analytical views:
   - `v_user_daily_utilization`: Precise per-user activity broken down by day (events, queries, deep research, agents created, tokens consumed).
   - `v_observability_traces`: Correlations of distributed OpenTelemetry traces (trace IDs, span IDs, methods, execution statuses).
   - `v_user_summary`: Aggregated per-user statistics since first activity.
   - `v_daily_adoption`: Organization adoption indicators (DAU/WAU/MAU trends, queries, tokens burned).
   - `v_feature_usage`: Usage of individual Gemini Enterprise modules and features.
   - `v_token_telemetry`: Input, output and cached token metrics.
3. **Cloud Monitoring and OpenTelemetry Integration (Official Google Cloud APIs, Zero Regex)**:
   - Observability settings: [`observabilityConfig`](https://cloud.google.com/gemini/enterprise/docs/manage-observability-settings) (enabling OpenTelemetry traces and sensitive input/output logging).
   - Distributed traces and spans: [Cloud Trace OpenTelemetry Spans](https://cloud.google.com/gemini/enterprise/docs/access-traces-and-spans) joined deterministically in BigQuery on `trace_id` and `session_id`.
   - Operational metrics: [Cloud Monitoring](https://cloud.google.com/gemini/enterprise/docs/access-metrics) (`agent_session_count`, `agent_turn_count`, `agent_session_with_tool_count`, `engine/time_to_first_token_latency` TTFT).
   - Continuous monitoring of quotas and their consumption rate (`discoveryengine.googleapis.com/quota/*`).
4. **Dynamic ADK Agent (Google Agent Development Kit & Vertex AI Agent Runtime)**:
   - Deployed in the managed **Vertex AI Agent Runtime (Reasoning Engine)** environment and integrated with the Gemini Enterprise application as `Gemini Enterprise Telemetry & Adoption Agent`.
   - Instead of static snapshots, the agent dynamically queries BigQuery in real time (`v_user_daily_utilization`, `v_user_summary`, `v_daily_adoption`, `v_observability_traces`) and the Cloud Monitoring API using 5 dedicated Python tools.
5. **Administrator CLI**: Instant investigation of per-day utilization (`--daily`), adoption trends and observability metrics (`python3 cli/telemetry_cli.py observability --traces`).

---

## Prerequisites and IAM Roles

To deploy the telemetry pipeline in a customer's Google Cloud project, make sure the following roles are granted:

| IAM Role | Purpose |
| :--- | :--- |
| `roles/discoveryengine.agentspaceAdmin` | Manage and deploy agents in Discovery Engine / Gemini Enterprise |
| `roles/logging.configWriter` | Create the Cloud Logging sink that routes logs to BigQuery |
| `roles/bigquery.admin` | Create the BigQuery dataset, tables and SQL analytical views |
| `roles/monitoring.viewer` / `roles/monitoring.editor` | Read quota metrics and deploy the Cloud Monitoring dashboard |
| `roles/cloudtrace.user` | Analyze distributed traces in Google Cloud Trace |
| `roles/resourcemanager.projectIamAdmin` | Grant BigQuery Data Editor to the Cloud Logging sink identity |

---

## Automated One-Command Deployment

Run the deployment script, specifying the customer's project and engine ID:

```bash
./scripts/deploy_pipeline.sh <PROJECT_ID> <LOCATION> <ENGINE_ID> [DATASET_ID]
```

Example for the `my-gemini-app` engine in the EU region:
```bash
./scripts/deploy_pipeline.sh my-project-id eu my-gemini-app_1234567890 gemini_enterprise_telemetry
```

---

## Main Administrator CLI Commands

### 1. Observability Metrics and Distributed Traces:
```bash
python3 cli/telemetry_cli.py observability --traces
```

### 2. Per-Day User Activity:
```bash
python3 cli/telemetry_cli.py utilization --daily --user admin@yourcompany.com
```

### 3. Organization Adoption Trends (DAU):
```bash
python3 cli/telemetry_cli.py adoption --days 30
```

### 4. Quota and Pool Status:
```bash
python3 cli/telemetry_cli.py quotas
```
