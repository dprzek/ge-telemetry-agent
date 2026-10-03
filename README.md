# <img src="assets/google-cloud.svg" alt="Google Cloud" width="32" height="32" valign="middle"> Gemini Enterprise Telemetry Agent

A complete, production-grade solution for monitoring, analyzing and reporting **Google Cloud Gemini Enterprise** usage telemetry across an organization.

The suite is built on the platform's official mechanisms (OpenTelemetry, Cloud Audit Logs, Cloud Monitoring Quotas), deduplicated BigQuery views, a serverless microservice on **Cloud Run**, and an autonomous ADK agent deployed to **Vertex AI Reasoning Engine** (in secure `RESTRICTED` mode by default).

---

## 🚀 Quick start (1-step deployment)

```bash
# 1. Clone the repository
git clone https://github.com/dprzek/ge-telemetry-agent.git
cd ge-telemetry-agent

# 2. Run the automated deployment:
./deploy.sh <ENGINE_ID> --project <PROJECT_ID> --location eu

# Optional flags:
./deploy.sh <ENGINE_ID> --recreate              # Force a rebuild of the Agent in Vertex AI
./deploy.sh <ENGINE_ID> --share-with-all-users  # Share the agent in the portal with the whole organization (default: RESTRICTED)
./deploy.sh <ENGINE_ID> --keep-raw-prompts      # Keep full prompts in Cloud Logging (default: Exclusion Filter)
./deploy.sh <ENGINE_ID> --skip-auto-observability # Skip the Cloud Run automation for new agents
```

---

## 🏛️ Architecture and Components

```text
[Gemini Enterprise Engine] (OpenTelemetry + Audit Logs)
           │
           ├─► Cloud Logging Sink ────► BigQuery (gemini_enterprise_telemetry: 7 SQL views)
           ├─► Cloud Logging Audit ───► Pub/Sub ──► Cloud Run (Auto-Observability Enabler)
           ├─► Cloud Monitoring    ───► Quotas API, TTFT, Sessions & Operational Dashboard
           │
           └─► Vertex AI Reasoning Engine (ADK Agent: gemini-2.5-flash)
                    │
                    └─► Gemini Enterprise Assistant (Private: RESTRICTED or ALL_USERS)
```

1. **Cloud Run (`ge-auto-observability-enabler`)**:
   - A serverless Cloud Run function (2nd gen) wired through Pub/Sub to `AgentService.CreateAgent` audit events.
   - **Automation**: enables the `observabilityEnabled: true` flag in real time for every newly created custom Low-Code agent, guaranteeing 100% adoption measurement with no administrator intervention.
   - **Backward reconciliation**: at install time it verifies and updates all existing agents in the engine.
   - **Cost**: **$0.00 / month** (scales to zero, fits 100% within the GCP Always Free Tier).

2. **Vertex AI Reasoning Engine**:
   - A secure runtime for the ADK Agent (`adk_telemetry_agent.py`) that combines the `gemini-2.5-flash` model with dynamic BigQuery and Cloud Monitoring analytical tools.

3. **BigQuery & Cloud Logging**:
   - A dedicated dataset in the `EU` region with partitioned tables and 7 optimized SQL views that eliminate duplicates (`QUALIFY ROW_NUMBER()`).
   - No regex-based text parsing — strict joins on unique OpenTelemetry keys (`trace_id`, `session_id`).

---

## 🛡️ AI Governance, Security and Idempotency

- **Enterprise Governance (Zero Prompt & Zero M365 Storage + Full UPN attribution)**:
  The installer activates `sensitiveLoggingEnabled: true` on the engine (which prevents UPN anonymization to `<elided>`) while simultaneously configuring an **Exclusion Filter** on the `_Default` sink in Cloud Logging. Prompt contents (`gen_ai.user.message`) as well as responses and M365/SharePoint/Drive document snippets (`gen_ai.choice`) **are dropped at the Logging gateway and never reach BigQuery**.
- **IAM Isolation (Default RESTRICTED Mode)**:
  The deployed telemetry agent is private (`sharingConfig: RESTRICTED`) — only the deployer has access. It can be shared at any time from the console or with the `--share-with-all-users` flag.
- **Full Idempotency (CI/CD Ready)**:
  The script can safely be run multiple times against the same project:
  - Verifies that GCP resources exist (`describe`) before creating them.
  - Granting IAM permissions (`add-iam-policy-binding`) is conflict-free by nature.
  - New Cloud Run Function deployments create subsequent zero-downtime revisions.
  - Detects a healthy Vertex AI Reasoning Engine container and avoids unnecessary rebuilds.
  - A Self-Healing mechanism automatically prevents BigQuery table schema collisions (`query: STRING`).

---

## 💬 Example questions for the Agent

The telemetry agent queries BigQuery and Cloud Monitoring in real time. Example questions:

- **Opening suggestion (Bottom 10 adoption)**:
  - *"Good morning"* — the Agent greets you with a metrics statement and generates a table of the 10 least active users in the organization for onboarding.
  - *"Show the least active users (Bottom 10) with platform usage statistics."*
- **Users' own agents (two-phase lifecycle)**:
  - *"How many times did jane.doe@your-company.com edit her agent's prompt in the editor (`agent_updates`)?"*
  - *"In how many sessions did the author use their own agents (`author_agent_sessions`), and in how many did other employees (`org_agent_sessions`)?"*
- **User utilization and trends**:
  - *"Show the activity of jane.doe@your-company.com over the last 14 days broken down by day."*
  - *"How many Deep Research tasks and generated images were recorded this month?"*
  - *"How do the DAU metric and token consumption compare to last week?"*

---

## 📊 Metrics tracked in BigQuery (`gemini_enterprise_telemetry`)

| Category (Column) | Measured dimensions and definition | Data source |
| :--- | :--- | :--- |
| **Assistant queries (chat)** | General chat queries (excluding custom agent invocations and Deep Research) | BigQuery (`is_assistant_query`) |
| **Deep Research tasks** | Unique sessions of multi-step market research and knowledge synthesis | BigQuery (`agents/deep_research`) |
| **Images generated** | Number of generated graphics and photos (Imagen models) | BigQuery (`is_image_generation`) |
| **Agents created** | Number of agents successfully created in Agent Designer | Cloud Audit Logs (`CreateAgent`) |
| **Agent edits (agent_updates)** | Number of saved iterations and modifications of an agent's prompt in the editor | BigQuery & Audit (`UpdateAgent`) |
| **Agent views (agent_views)** | Number of views of an agent's profile/card in the portal | BigQuery (`WriteUserEvent`) |
| **Chats with the author's agents (`author_agent_sessions`)** | In how many sessions the author used their own agents (excluding the Telemetry Agent) | BigQuery (`v_author_agent_usage`) |
| **Chats with agents org-wide (`org_agent_sessions`)** | In how many sessions the author's agents were invoked by all employees | BigQuery (`v_author_agent_usage`) |
| **Token consumption** | Volume of prompt, response and total tokens (`total_tokens`) | BigQuery (`gen_ai_client_inference`) |
| **Adoption & Quotas** | DAU/WAU/MAU, retention, quota pools and time to first token (TTFT) | Cloud Monitoring & Trace |

---

## 📁 Repository structure

```text
ge-telemetry-agent/
├── deploy.sh                   # Quick launcher script
├── deploy.py                   # Main integrated Zero-Touch installer (steps 1-8)
├── MANUAL.md                   # Complete administrator manual
├── agent/
│   ├── adk_telemetry_agent.py  # ADK Agent code and tool definitions
│   └── deploy_adk_agent.py     # Deployment to Vertex AI Reasoning Engine
├── functions/
│   └── auto_observability_enabler/ # Cloud Run Function automating agent observability
│       ├── main.py
│       └── requirements.txt
├── scripts/
│   ├── setup_auto_observability.py # Cloud Run and Pub/Sub events installation module
│   ├── backfill_logs_to_bigquery.py # Idempotent backfill of historical logs
│   └── fix_user_activity_schema.py  # Self-healing of the BigQuery table schema
├── bigquery/
│   └── telemetry_views.sql     # 7 analytical SQL views
├── cli/
│   ├── telemetry_service.py    # Analytics layer (BigQuery / Monitoring)
│   └── telemetry_cli.py        # Console CLI tool
├── monitoring/
│   └── gemini_enterprise_telemetry_dashboard.json # Cloud Monitoring dashboard
└── tests/
    └── test_suite.py           # Unit and integration test suite
```

---

## License

Apache License 2.0. See the [LICENSE](LICENSE) file for details.
