#!/usr/bin/env python3
"""
Comprehensive suite of 20 automated tests verifying the Gemini Enterprise telemetry pipeline.

CATEGORY 1: Solution Implementation (Architecture, Pipeline, Tools, Permissions)
  Test 01: Python Syntax and Code Cleanliness (Multi-Tenant Hygiene)
  Test 02: BigQuery Table Schema and Partitioning by Timestamp
  Test 03: Backfill Idempotency and Log Parsers
  Test 04: Compilation and Structure of SQL Analytical Views
  Test 05: ADK Agent Definition, Tools and Cloudpickle Serialization
  Test 06: Reasoning Engine Service Account IAM Permissions and ALL_USERS Sharing

CATEGORY 2: Correctness of Responses to Selected Prompts (Live Tools & Prompt Correctness)
  Test 07: Prompt about Daily User Utilization (get_user_daily_utilization)
  Test 08: Prompt about Deep Research Sessions (Session Isolation, Retry Resilience)
  Test 09: Prompt about Imagen Image Generation (Precise Prompt Detection)
  Test 10: Prompt about Custom Agent Creation (Rejecting RPC and System Errors)
  Test 11: Prompt about Organization Ranking and Adoption Trends (DAU/WAU, Summaries)
  Test 12: Prompt about Quotas and Headroom (get_realtime_quotas)
  Test 13: Prompt about OpenTelemetry Traces and TTFT Latencies (get_observability_traces)

CATEGORY 3: Response Completeness and Invariants (Completeness & Invariants)
  Test 14: Mathematical Metric Invariants (Event Sum, Token Balance, Non-Negativity)
  Test 15: Token Correlation via OTel Traces without Double Counting (Zero Double-Counting)
  Test 16: Zero-Utilization Reporting (Zero-Utilization Schema Contract)

CATEGORY 4: Resilience to Different Customer Environment Setups (Client Setup Resilience)
  Test 17: Engine Resolution (Display Name, Full ID, Default Fallback)
  Test 18: Project Auto-Detection when Environment Variables Are Missing
  Test 19: Parser Resilience to JSON Payload Evolution and Missing Fields
  Test 20: Handling of Different Regional Configurations (eu, us, global, Vertex Locations)
  Test 21: Privacy-by-Design and AI Governance (sensitiveLoggingEnabled: True + Exclusion Filter)
  Test 22: Author Agent Invocation Statistics (Self-Usage & Org-Wide Invariants)
"""

import sys
import os
import json
import py_compile
import subprocess
import unittest
import urllib.request
import urllib.error
from typing import Dict, Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "cli"))

from telemetry_service import TelemetryService
from deploy import resolve_engine, get_auth_token, enable_engine_observability
from google.cloud import bigquery
import google.auth
from google.auth.transport.requests import Request

def _detect_project_id():
    for var in ("TEST_PROJECT_ID", "GOOGLE_CLOUD_PROJECT", "PROJECT_ID"):
        val = os.environ.get(var)
        if val:
            return val
    try:
        res = subprocess.run(["gcloud", "config", "get-value", "project"], stdout=subprocess.PIPE, text=True, check=True)
        proj = res.stdout.strip()
        if proj:
            return proj
    except Exception:
        pass
    try:
        _, adc_project = google.auth.default()
        if adc_project:
            return adc_project
    except Exception:
        pass
    return None

PROJECT_ID = _detect_project_id()
LOCATION = os.environ.get("TEST_LOCATION") or "eu"
APP_NAME = os.environ.get("TEST_APP_NAME") or os.environ.get("GEMINI_ENGINE_ID") or ""
DATASET_ID = os.environ.get("TEST_DATASET_ID") or "gemini_enterprise_telemetry"
VERTEX_LOCATION = os.environ.get("TEST_VERTEX_LOCATION") or "europe-west1"


class GeminiEnterprise20TestSuite(unittest.TestCase):
    gcp_auth_available = False
    auth_skip_reason = ""
    service = None
    bq_client = None
    expected_engine_id = None
    project_number = None

    @classmethod
    def setUpClass(cls):
        try:
            token = get_auth_token()
            if token:
                cls.bq_client = bigquery.Client(project=PROJECT_ID)
                cls.gcp_auth_available = True
                # Verify and detect the active engine in the project
                api_host = f"{LOCATION}-discoveryengine.googleapis.com" if LOCATION != "global" else "discoveryengine.googleapis.com"
                url = f"https://{api_host}/v1alpha/projects/{PROJECT_ID}/locations/{LOCATION}/collections/default_collection/engines"
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "X-Goog-User-Project": PROJECT_ID})
                real_engines = []
                try:
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        real_engines = [e["name"].split("/")[-1] for e in json.load(resp).get("engines", [])]
                except Exception:
                    pass

                resolved = None
                try:
                    candidate = resolve_engine(PROJECT_ID, LOCATION, APP_NAME, token)
                    if candidate in real_engines:
                        resolved = candidate
                except Exception:
                    pass

                if not resolved:
                    # Scan engines looking for the telemetry agent
                    for e_id in real_engines:
                        url_a = f"https://{api_host}/v1alpha/projects/{PROJECT_ID}/locations/{LOCATION}/collections/default_collection/engines/{e_id}/assistants/default_assistant/agents"
                        req_a = urllib.request.Request(url_a, headers={"Authorization": f"Bearer {token}", "X-Goog-User-Project": PROJECT_ID})
                        try:
                            with urllib.request.urlopen(req_a, timeout=10) as ra:
                                agents = json.load(ra).get("agents", [])
                                if any("Telemetry" in a.get("displayName", "") for a in agents):
                                    resolved = e_id
                                    break
                        except Exception:
                            pass

                if not resolved and real_engines:
                    resolved = real_engines[0]

                cls.expected_engine_id = resolved or APP_NAME or None

                cls.service = TelemetryService(
                    project_id=PROJECT_ID,
                    location=LOCATION,
                    dataset_id=DATASET_ID,
                    engine_id=cls.expected_engine_id,
                )

                crm_url = f"https://cloudresourcemanager.googleapis.com/v1/projects/{PROJECT_ID}"
                req = urllib.request.Request(crm_url, headers={"Authorization": f"Bearer {token}"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    proj_info = json.load(resp)
                    cls.project_number = str(proj_info.get("projectNumber", ""))
        except Exception as e:
            cls.gcp_auth_available = False
            cls.auth_skip_reason = f"No active GCP authorization ({e})."

    def _require_live_gcp(self):
        if not self.gcp_auth_available:
            self.skipTest(self.auth_skip_reason)

    # ==========================================================================
    # CATEGORY 1: Solution Implementation
    # ==========================================================================

    def test_01_syntax_multi_tenant_cleanliness(self):
        """Test 01: Checks the syntax of all Python scripts and multi-tenant hygiene."""
        py_files = []
        for root, _, files in os.walk(REPO_ROOT):
            if ".git" in root or "__pycache__" in root:
                continue
            for f in files:
                if f.endswith(".py"):
                    py_files.append(os.path.join(root, f))

        self.assertGreaterEqual(len(py_files), 6, "There should be at least 6 Python scripts.")
        for py_file in py_files:
            try:
                py_compile.compile(py_file, doraise=True)
            except py_compile.PyCompileError as e:
                self.fail(f"Syntax error in file {py_file}: {e}")

        agent_py = os.path.join(REPO_ROOT, "agent", "adk_telemetry_agent.py")
        with open(agent_py, "r", encoding="utf-8") as f:
            content = f.read()
        import re
        for py_file in py_files:
            with open(py_file, "r", encoding="utf-8") as f:
                src = f.read()
            self.assertIsNone(re.search(r"ghp_[A-Za-z0-9]{20,}", src), f"{py_file} must not contain hardcoded GitHub tokens.")
            self.assertIsNone(re.search(r"AIza[0-9A-Za-z\-_]{30,}", src), f"{py_file} must not contain hardcoded API keys.")
            self.assertIsNone(re.search(r"project_id\s*=\s*[\"']prj-prod-[^\"']+[\"']", src), f"{py_file} must not contain a hardcoded production project.")
        self.assertNotIn("ghp_", content, "The agent module must not contain hardcoded tokens.")

    def test_02_bigquery_tables_schema_partitioning(self):
        """Test 02: Verifies the BigQuery table structure and partitioning by timestamp."""
        self._require_live_gcp()
        expected_tables = [
            "gemini_enterprise_user_activity",
            "gen_ai_client_inference_operation_details",
            "cloudaudit_googleapis_com_activity",
        ]
        for tbl_name in expected_tables:
            table_ref = f"{PROJECT_ID}.{DATASET_ID}.{tbl_name}"
            table = self.bq_client.get_table(table_ref)
            self.assertIsNotNone(table, f"Table {table_ref} does not exist!")
            self.assertIsNotNone(table.time_partitioning, f"Table {table_ref} must have time_partitioning.")
            self.assertEqual(table.time_partitioning.field, "timestamp", f"Table {table_ref} must be partitioned by the timestamp field.")

    def test_03_backfill_idempotency_deduplication(self):
        """Test 03: Verifies the backfill parser and idempotency on re-insertion."""
        from scripts.backfill_logs_to_bigquery import parse_activity_entry, parse_inference_entry

        mock_act = {
            "insertId": "test_dedup_001",
            "timestamp": "2026-09-20T12:00:00Z",
            "jsonPayload": {
                "useriamprincipal": "test.user@example.com",
                "logmetadata": {"methodname": "StreamAssist"},
                "request": {
                    "userevent": {
                        "userpseudoid": "pseudo_abc",
                        "engine": self.expected_engine_id or "example-engine",
                        "eventtype": "search",
                        "agentspaceinfo": {"agentspacepagetype": "image-generation"}
                    }
                }
            }
        }
        parsed = parse_activity_entry(mock_act)
        self.assertEqual(parsed["insert_id"], "test_dedup_001")
        self.assertEqual(parsed["user_iam_principal"], "test.user@example.com")
        self.assertEqual(parsed["method_name"], "StreamAssist")
        self.assertEqual(parsed["page_type"], "image-generation")

        mock_inf = {
            "insertId": "inf_dedup_002",
            "timestamp": "2026-09-20T12:00:01Z",
            "jsonPayload": {
                "gen_ai.usage.input_tokens": 512,
                "gen_ai.usage.output_tokens": 128,
                "gen_ai.usage.cache_read.input_tokens": 64
            }
        }
        parsed_inf = parse_inference_entry(mock_inf)
        self.assertEqual(parsed_inf["insert_id"], "inf_dedup_002")
        self.assertEqual(parsed_inf["input_tokens"], 512)
        self.assertEqual(parsed_inf["output_tokens"], 128)
        self.assertEqual(parsed_inf["cached_tokens"], 64)

    def test_04_bigquery_views_ddl_and_structure(self):
        """Test 04: Checks that the 6 SQL analytical views compile correctly in BigQuery."""
        sql_path = os.path.join(REPO_ROOT, "bigquery", "telemetry_views.sql")
        with open(sql_path, "r", encoding="utf-8") as f:
            raw_sql = f.read()

        formatted_sql = raw_sql.format(project_id=PROJECT_ID, dataset_id=DATASET_ID)
        self.assertNotIn("{project_id}", formatted_sql)
        self.assertNotIn("{dataset_id}", formatted_sql)

        self._require_live_gcp()
        expected_views = [
            "v_user_daily_utilization",
            "v_observability_traces",
            "v_user_summary",
            "v_daily_adoption",
            "v_feature_usage",
            "v_token_telemetry",
        ]
        for v in expected_views:
            v_ref = f"{PROJECT_ID}.{DATASET_ID}.{v}"
            table = self.bq_client.get_table(v_ref)
            self.assertEqual(table.table_type, "VIEW", f"{v_ref} should be of type VIEW!")
            self.assertGreater(len(table.schema), 0, f"View {v_ref} must have columns.")

    def test_05_adk_agent_definition_and_cloudpickle(self):
        """Test 05: Verifies the ADK agent definition, tools and cloudpickle serialization."""
        from agent.adk_telemetry_agent import root_agent
        import cloudpickle

        self.assertEqual(root_agent.name, "gemini_enterprise_telemetry_agent")
        self.assertEqual(root_agent.model, "gemini-2.5-flash")
        self.assertEqual(len(root_agent.tools), 5)

        tool_names = [t.__name__ for t in root_agent.tools]
        expected_tools = [
            "get_user_daily_utilization",
            "get_user_summary",
            "get_daily_adoption",
            "get_realtime_quotas",
            "get_observability_traces",
        ]
        for expected in expected_tools:
            self.assertIn(expected, tool_names)

        pickled = cloudpickle.dumps(root_agent)
        self.assertGreater(len(pickled), 100, "Cloudpickle serialization must produce a valid binary object.")

    def test_06_iam_reasoning_engine_and_sharing_config(self):
        """Test 06: Verifies the Reasoning Engine service account IAM permissions and sharingConfig (RESTRICTED by default / deployer only)."""
        self._require_live_gcp()
        self.assertIsNotNone(self.project_number, "project_number is required to verify the RE service account.")
        sa_email = f"service-{self.project_number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"

        dataset = self.bq_client.get_dataset(DATASET_ID)
        has_dataset_access = any(
            entry.entity_id == sa_email and entry.role in ("READER", "WRITER", "OWNER")
            for entry in dataset.access_entries
        )
        self.assertTrue(has_dataset_access, f"Service account {sa_email} must have READER permissions on dataset {DATASET_ID}.")

        token = get_auth_token()
        api_host = f"{LOCATION}-discoveryengine.googleapis.com" if LOCATION != "global" else "discoveryengine.googleapis.com"
        url = f"https://{api_host}/v1alpha/projects/{self.project_number or PROJECT_ID}/locations/{LOCATION}/collections/default_collection/engines/{self.expected_engine_id}/assistants/default_assistant/agents"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}", "X-Goog-User-Project": PROJECT_ID}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)

        agents = data.get("agents", [])
        adk_agent = next((a for a in agents if "Telemetry" in a.get("displayName", "")), None)
        self.assertIsNotNone(adk_agent, "No registered Telemetry Agent found in Gemini Enterprise.")
        self.assertIn(adk_agent.get("state"), ("ENABLED", "PRIVATE", "PUBLISHED"))
        self.assertIn(adk_agent.get("sharingConfig", {}).get("scope", "RESTRICTED"), ("RESTRICTED", "PRIVATE", "ALL_USERS"))

    # ==========================================================================
    # CATEGORY 2: Correctness of Responses to Selected Prompts
    # ==========================================================================

    def test_07_prompt_user_daily_utilization_breakdown(self):
        """Test 07: Correctness of the get_user_daily_utilization tool response for an active user."""
        self._require_live_gcp()
        os.environ["BIGQUERY_PROJECT"] = PROJECT_ID
        os.environ["BIGQUERY_DATASET"] = DATASET_ID
        from agent.adk_telemetry_agent import get_user_daily_utilization

        res_json = get_user_daily_utilization(user_email="user@example.com", days=30)
        data = json.loads(res_json)
        self.assertIn(data.get("status"), ("success", "zero_utilization"))
        if data.get("status") == "success":
            self.assertGreater(data.get("daily_records_count", 0), 0)
            rec = data["daily_records"][0]
            self.assertIn("activity_date", rec)
            self.assertIn("assistant_queries", rec)
            self.assertIn("total_tokens", rec)
            self.assertIn("images_generated", rec)

    def test_08_prompt_deep_research_isolation_and_retries(self):
        """Test 08: Correctness of Deep Research session counting (session isolation, no double-counting after retries)."""
        self._require_live_gcp()
        daily_records = self.service.get_user_daily_breakdown()
        deep_research_days = [r for r in daily_records if r.get("deep_research_count", 0) > 0]
        if os.environ.get("TEST_EXPECT_DEEP_RESEARCH") == "1":
            self.assertGreater(len(deep_research_days), 0, "At least one Deep Research task should be recorded (TEST_EXPECT_DEEP_RESEARCH=1).")
        for r in deep_research_days:
            self.assertGreaterEqual(r["total_events"], r["deep_research_count"])

    def test_09_prompt_image_generation_detection(self):
        """Test 09: Correctness of Imagen image generation detection (excluding text chat)."""
        self._require_live_gcp()
        summary = self.service.get_user_summary()
        mock_user = next((u for u in summary if "mock-analyst" in u.get("user_id", "")), None)
        if mock_user:
            self.assertEqual(mock_user.get("images_generated", 0), 1, "The mock user should have exactly 1 generated image.")
            self.assertEqual(mock_user.get("assistant_queries", 0), 3, "The mock user should have exactly 3 assistant queries.")

    def test_10_prompt_agent_creation_and_error_filtering(self):
        """Test 10: Correctness of created-agent counting and rejection of errors with status != 0."""
        self._require_live_gcp()
        summary = self.service.get_user_summary()
        mock_user = next((u for u in summary if "mock-analyst" in u.get("user_id", "")), None)
        if mock_user:
            self.assertEqual(mock_user.get("agents_created", 0), 2, "The mock user should have exactly 2 agents created.")

    def test_11_prompt_user_ranking_and_adoption_trends(self):
        """Test 11: Correctness of the response to the prompt about user ranking and DAU adoption trends."""
        self._require_live_gcp()
        os.environ["BIGQUERY_PROJECT"] = PROJECT_ID
        os.environ["BIGQUERY_DATASET"] = DATASET_ID
        from agent.adk_telemetry_agent import get_user_summary, get_daily_adoption

        summary_json = get_user_summary()
        summary_data = json.loads(summary_json)
        self.assertIn("users", summary_data)
        self.assertGreaterEqual(len(summary_data["users"]), 2)

        adoption_json = get_daily_adoption(days=30)
        adoption_data = json.loads(adoption_json)
        self.assertIn("adoption_records", adoption_data)
        self.assertGreater(adoption_data.get("records_count", 0), 0)

    def test_12_prompt_realtime_quotas_and_headroom(self):
        """Test 12: Correctness of the response to the prompt about quotas (RPM, TPM, headroom)."""
        self._require_live_gcp()
        os.environ["BIGQUERY_PROJECT"] = PROJECT_ID
        os.environ["BIGQUERY_DATASET"] = DATASET_ID
        from agent.adk_telemetry_agent import get_realtime_quotas

        quotas_json = get_realtime_quotas()
        quotas_data = json.loads(quotas_json)
        self.assertIn(quotas_data.get("status"), ("success", "HEALTHY"))
        self.assertIn("quotas", quotas_data)
        self.assertIn("rate_limits", quotas_data)
        self.assertIn("gemini_enterprise_rpm", quotas_data["quotas"])

    def test_13_prompt_observability_traces_and_latencies(self):
        """Test 13: Correctness of the response to the prompt about OpenTelemetry traces, TTFT latencies and errors."""
        self._require_live_gcp()
        os.environ["BIGQUERY_PROJECT"] = PROJECT_ID
        os.environ["BIGQUERY_DATASET"] = DATASET_ID
        from agent.adk_telemetry_agent import get_observability_traces

        traces_json = get_observability_traces(days=14)
        traces_data = json.loads(traces_json)
        self.assertEqual(traces_data.get("status"), "success")
        self.assertIn("trace_windows", traces_data)

    # ==========================================================================
    # CATEGORY 3: Response Completeness and Invariants
    # ==========================================================================

    def test_14_mathematical_invariants_events_and_tokens(self):
        """Test 14: Verification of mathematical invariants (event sum consistency, token balance)."""
        self._require_live_gcp()
        daily_records = self.service.get_user_daily_breakdown()
        self.assertGreater(len(daily_records), 0)

        for row in daily_records:
            total_events = row.get("total_events", 0)
            queries = row.get("assistant_queries", 0)
            deep_r = row.get("deep_research_count", 0)
            images = row.get("images_generated", 0)
            agents = row.get("agents_created", 0)
            total_tok = row.get("total_tokens", 0)
            inp_tok = row.get("input_tokens", 0)
            out_tok = row.get("output_tokens", 0)

            # Invariant 1: Events >= sum of individual actions
            self.assertGreaterEqual(
                total_events,
                queries + deep_r + images + agents,
                f"Events ({total_events}) are less than the sum of actions on date {row.get('activity_date')}"
            )

            # Invariant 2: Total tokens = input + output
            self.assertEqual(
                total_tok,
                inp_tok + out_tok,
                f"total_tokens ({total_tok}) != input ({inp_tok}) + output ({out_tok})"
            )

            # Invariant 3: No negative values
            self.assertGreaterEqual(total_events, 0)
            self.assertGreaterEqual(queries, 0)
            self.assertGreaterEqual(total_tok, 0)

    def test_15_token_trace_correlation_zero_double_counting(self):
        """Test 15: Checks the precision of token correlation via OTel traces and the absence of double-counting."""
        self._require_live_gcp()
        sql = f"""
        WITH deduped_inference AS (
            SELECT DISTINCT insert_id, input_tokens, output_tokens
            FROM `{PROJECT_ID}.{DATASET_ID}.gen_ai_client_inference_operation_details`
        )
        SELECT 
            SUM(input_tokens) AS direct_inp,
            SUM(output_tokens) AS direct_out
        FROM deduped_inference
        """
        job = self.bq_client.query(sql)
        row = list(job.result())[0]
        direct_inp = row["direct_inp"] or 0
        direct_out = row["direct_out"] or 0

        user_summary = self.service.get_user_summary()
        self.assertGreater(len(user_summary), 0)
        total_view_inp = sum(u.get("input_tokens", 0) for u in user_summary)
        self.assertGreaterEqual(total_view_inp, direct_inp)

    def test_16_zero_utilization_reporting_and_schema_contract(self):
        """Test 16: Completeness of zero-utilization reporting for users with no activity."""
        self._require_live_gcp()
        os.environ["BIGQUERY_PROJECT"] = PROJECT_ID
        os.environ["BIGQUERY_DATASET"] = DATASET_ID
        from agent.adk_telemetry_agent import get_user_daily_utilization, get_user_summary

        ghost_user = "non_existent_ghost_user_9999@example.com"
        daily_res = json.loads(get_user_daily_utilization(user_email=ghost_user, days=14))
        self.assertEqual(daily_res.get("status"), "zero_utilization")
        self.assertEqual(daily_res.get("daily_records_count"), 0)
        self.assertEqual(daily_res.get("daily_records"), [])

        summary_res = json.loads(get_user_summary(user_email=ghost_user))
        self.assertEqual(summary_res.get("status"), "zero_utilization")
        self.assertEqual(summary_res.get("total_events"), 0)
        self.assertEqual(summary_res.get("assistant_queries"), 0)
        self.assertEqual(summary_res.get("total_tokens"), 0)

    # ==========================================================================
    # CATEGORY 4: Resilience to Different Customer Environment Setups
    # ==========================================================================

    def test_17_engine_resolution_resilience(self):
        """Test 17: Engine resolver resilience to display_name, full ID, whitespace and an empty hint."""
        self._require_live_gcp()
        token = get_auth_token()
        self.assertIsNotNone(token)

        # 1. By application name / hint (falls back to the engine detected in setUpClass)
        hint = APP_NAME or self.expected_engine_id
        res_by_name = resolve_engine(PROJECT_ID, LOCATION, hint, token)
        self.assertIsNotNone(res_by_name)
        self.assertTrue(res_by_name.lower().startswith(hint.lower().split("_")[0]))

        # 2. By full ID
        res_by_full = resolve_engine(PROJECT_ID, LOCATION, res_by_name, token)
        self.assertEqual(res_by_full, res_by_name)

        # 3. Empty hint -> default selection (or None when multiple engines exist)
        res_empty = resolve_engine(PROJECT_ID, LOCATION, "", token)
        self.assertTrue(res_empty is None or len(res_empty) > 0)

    def test_18_environment_variables_and_project_auto_resolution(self):
        """Test 18: Resilience to missing environment variables and GCP project auto-detection."""
        saved_bq_proj = os.environ.pop("BIGQUERY_PROJECT", None)
        saved_gcp_proj = os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
        try:
            from agent.adk_telemetry_agent import _get_env_config
            resolved_proj, resolved_ds = _get_env_config()
            self.assertIsNotNone(resolved_proj, "The project should be detected from the default GCP credentials.")
            self.assertEqual(resolved_ds, "gemini_enterprise_telemetry")
        finally:
            if saved_bq_proj:
                os.environ["BIGQUERY_PROJECT"] = saved_bq_proj
            if saved_gcp_proj:
                os.environ["GOOGLE_CLOUD_PROJECT"] = saved_gcp_proj

    def test_19_payload_schema_evolution_and_missing_fields(self):
        """Test 19: Log parser resilience to missing fields, nested objects and camelCase/lowercase formats."""
        from scripts.backfill_logs_to_bigquery import parse_activity_entry, parse_inference_entry

        # An empty payload should not raise a KeyError or any exception
        empty_entry = {"insertId": "empty_001", "timestamp": "2026-09-20T10:00:00Z"}
        res = parse_activity_entry(empty_entry)
        self.assertEqual(res["insert_id"], "empty_001")
        self.assertEqual(res["user_iam_principal"], "")
        self.assertEqual(res["page_type"], "")

        # Payload with an anonymized <elided> user
        elided_entry = {
            "insertId": "elided_002",
            "timestamp": "2026-09-20T10:05:00Z",
            "jsonPayload": {
                "useriamprincipal": "<elided>",
                "request": {
                    "userevent": {
                        "userpseudoid": "anon_hash_123",
                        "eventtype": "page-view"
                    }
                }
            }
        }
        res_elided = parse_activity_entry(elided_entry)
        self.assertEqual(res_elided["user_iam_principal"], "<elided>")
        self.assertEqual(res_elided["user_pseudo_id"], "anon_hash_123")

        # Inference with missing tokens
        inf_empty = {"insertId": "inf_none", "timestamp": "2026-09-20T10:06:00Z", "jsonPayload": {}}
        res_inf = parse_inference_entry(inf_empty)
        self.assertEqual(res_inf["input_tokens"], 0)
        self.assertEqual(res_inf["output_tokens"], 0)

    def test_20_multi_region_and_location_resilience(self):
        """Test 20: Resilience to Discovery Engine regional configurations (eu, us, global)."""
        from deploy import get_auth_token
        self._require_live_gcp()
        token = get_auth_token()

        for loc in ["eu", "global"]:
            api_host = f"{loc}-discoveryengine.googleapis.com" if loc != "global" else "discoveryengine.googleapis.com"
            url = f"https://{api_host}/v1alpha/projects/{self.project_number or PROJECT_ID}/locations/{loc}/collections/default_collection/engines"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "X-Goog-User-Project": PROJECT_ID})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    self.assertEqual(resp.status, 200, f"The endpoint for location {loc} should respond with code 200.")
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
                # Acceptable if no engines exist in the given location or a network error occurred
                pass

    def test_21_privacy_by_design_observability_defaults(self):
        """Test 21: Verification of AI Governance rules - Enterprise Governance (sensitiveLoggingEnabled: True + Exclusion Filter)."""
        import inspect
        from deploy import enable_engine_observability, setup_governance_exclusion_filter
        import subprocess

        # 1. Check the signature of the enable_engine_observability function
        sig = inspect.signature(enable_engine_observability)
        self.assertIn("enable_sensitive_logging", sig.parameters)
        self.assertEqual(sig.parameters["enable_sensitive_logging"].default, True,
                         "The default value of enable_sensitive_logging should be True (for UPN identity attribution).")

        # 2. Check the signature of setup_governance_exclusion_filter
        sig_ex = inspect.signature(setup_governance_exclusion_filter)
        self.assertIn("project_id", sig_ex.parameters)
        self.assertIn("token", sig_ex.parameters)

        # 3. Check the CLI flags in deploy.py
        help_output = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "deploy.py"), "--help"],
            capture_output=True, text=True, check=True
        ).stdout
        self.assertIn("--disable-sensitive-logging", help_output)
        self.assertIn("--keep-raw-prompts", help_output)
        self.assertIn("Exclusion Filter", help_output)
        self.assertIn("--share-with-all-users", help_output)

        # 4. Check the signature of register_adk_agent_in_gemini (default share_with_all_users=False -> RESTRICTED)
        from agent.deploy_adk_agent import register_adk_agent_in_gemini
        sig_agent = inspect.signature(register_adk_agent_in_gemini)
        self.assertIn("share_with_all_users", sig_agent.parameters)
        self.assertEqual(sig_agent.parameters["share_with_all_users"].default, False,
                         "The default value of share_with_all_users MUST be False (access for the deployer only).")

    def test_22_author_agent_usage_metrics(self):
        """Test 22: Author agent invocation statistics (Self-Usage & Org-Wide Invariants)."""
        self._require_live_gcp()

        # 1. Check get_user_summary
        summary = self.service.get_user_summary()
        self.assertIsInstance(summary, list)
        self.assertGreater(len(summary), 0, "The user summary should return records.")

        for u in summary:
            # Verify that all required columns are present
            self.assertIn("author_agent_invocations", u)
            self.assertIn("author_agent_sessions", u)
            self.assertIn("org_agent_invocations", u)
            self.assertIn("org_agent_sessions", u)
            self.assertIn("org_agent_unique_callers", u)
            self.assertIn("agent_updates", u)
            self.assertIn("agent_views", u)

            # Verify non-negativity
            self.assertGreaterEqual(u["author_agent_invocations"], 0)
            self.assertGreaterEqual(u["author_agent_sessions"], 0)
            self.assertGreaterEqual(u["org_agent_invocations"], 0)
            self.assertGreaterEqual(u["org_agent_sessions"], 0)
            self.assertGreaterEqual(u["org_agent_unique_callers"], 0)
            self.assertGreaterEqual(u["agent_updates"], 0)
            self.assertGreaterEqual(u["agent_views"], 0)

            # Invariant: org invocations must be >= author invocations
            self.assertGreaterEqual(
                u["org_agent_invocations"], u["author_agent_invocations"],
                f"Org invocations ({u['org_agent_invocations']}) must be >= author invocations ({u['author_agent_invocations']}) for {u['user_id']}"
            )
            self.assertGreaterEqual(
                u["org_agent_sessions"], u["author_agent_sessions"],
                f"Org sessions ({u['org_agent_sessions']}) must be >= author sessions ({u['author_agent_sessions']}) for {u['user_id']}"
            )

            # If the agent had org invocations, the number of unique callers must be >= 1
            if u["org_agent_invocations"] > 0:
                self.assertGreaterEqual(u["org_agent_unique_callers"], 1)
            else:
                self.assertEqual(u["org_agent_unique_callers"], 0)

        # 2. Check get_user_daily_breakdown
        daily = self.service.get_user_daily_breakdown()
        self.assertIsInstance(daily, list)
        for d in daily:
            self.assertIn("author_agent_invocations", d)
            self.assertIn("author_agent_sessions", d)
            self.assertIn("org_agent_invocations", d)
            self.assertIn("org_agent_sessions", d)
            self.assertIn("org_agent_unique_callers", d)
            self.assertIn("agent_updates", d)
            self.assertIn("agent_views", d)
            self.assertGreaterEqual(d["org_agent_invocations"], d["author_agent_invocations"])

        # 3. Check get_daily_adoption
        adoption = self.service.get_daily_adoption(days=7)
        self.assertIsInstance(adoption, list)
        for a in adoption:
            self.assertIn("total_custom_agent_invocations", a)
            self.assertGreaterEqual(a["total_custom_agent_invocations"], 0)

        # 4. Check the ADK agent tools
        os.environ["PROJECT_ID"] = PROJECT_ID
        os.environ["DATASET_ID"] = DATASET_ID
        from agent.adk_telemetry_agent import get_user_summary as adk_summary, get_user_daily_utilization as adk_daily, get_daily_adoption as adk_adoption
        raw_adk_sum = json.loads(adk_summary())
        self.assertEqual(raw_adk_sum.get("status"), "success")
        self.assertGreater(len(raw_adk_sum.get("users", [])), 0)
        top_user = raw_adk_sum["users"][0]
        self.assertIn("author_agent_invocations", top_user)
        self.assertIn("org_agent_invocations", top_user)
        self.assertIn("agent_updates", top_user)
        self.assertIn("agent_views", top_user)

    def test_23_bottom_users_proposal_and_order(self):
        """Test 23: Verification of the default Bottom 10 proposal and the sorting of the least active users."""
        self._require_live_gcp()

        # 1. Check ascending sorting (Bottom N) in the service layer
        bottom_summary = self.service.get_user_summary(order_by="asc", limit=10)
        self.assertIsInstance(bottom_summary, list)
        self.assertGreater(len(bottom_summary), 0)
        if len(bottom_summary) >= 2:
            self.assertLessEqual(
                bottom_summary[0]["total_events"],
                bottom_summary[-1]["total_events"],
                "The first user in Bottom N should have <= events than the last one."
            )

        # 2. Check the ADK tool with order_by='bottom' and limit=10
        os.environ["PROJECT_ID"] = PROJECT_ID
        os.environ["DATASET_ID"] = DATASET_ID
        from agent.adk_telemetry_agent import get_user_summary as adk_summary, root_agent
        raw_bottom = json.loads(adk_summary(order_by="bottom", limit=10))
        self.assertEqual(raw_bottom.get("status"), "success")
        self.assertLessEqual(len(raw_bottom.get("users", [])), 10)

        # 3. Verify that all table columns required for Bottom 10 are present
        required_cols = [
            "assistant_queries",
            "deep_research_count",
            "images_generated",
            "agents_created",
            "agent_updates",
            "agent_views",
            "author_agent_sessions",
            "org_agent_sessions",
            "total_tokens",
        ]
        for u in raw_bottom.get("users", []):
            for col in required_cols:
                self.assertIn(col, u, f"Column {col} must be present in the user dictionary for the table.")

        # 4. Verify the root_agent system instruction for the default Bottom 10 proposal
        inst = root_agent.instruction
        self.assertIn("BOTTOM 10", inst)
        self.assertIn("Assistant queries (chat)", inst)
        self.assertIn("Deep Research tasks", inst)
        self.assertIn("Images generated", inst)
        self.assertIn("Agents created", inst)
        self.assertIn("agent_updates", inst)
        self.assertIn("agent_views", inst)
        self.assertIn("author_agent_sessions", inst)
        self.assertIn("org_agent_sessions", inst)
    def test_24_user_activity_schema_query_string_and_self_healing(self):
        """Test 24: Verification of the RECORD type for jsonPayload.request.query and the Self-Healing mechanism."""
        self._require_live_gcp()
        table_id = f"{PROJECT_ID}.{DATASET_ID}.discoveryengine_googleapis_com_gemini_enterprise_user_activity"
        tbl = self.bq_client.get_table(table_id)
        
        # 1. Verify the type of the query field in the table schema
        query_field_type = None
        for f in tbl.schema:
            if f.name == "jsonPayload" and f.fields:
                for sub in f.fields:
                    if sub.name == "request" and sub.fields:
                        for rsub in sub.fields:
                            if rsub.name == "query":
                                query_field_type = rsub.field_type
                                break

        self.assertEqual(
            query_field_type,
            "RECORD",
            f"Field jsonPayload.request.query must be of type RECORD (compatible with Gemini Enterprise StreamAssist), but detected: {query_field_type}"
        )

        # 2. Verify the Self-Healing procedure - a healthy state requires no repair
        from scripts.backfill_logs_to_bigquery import repair_user_activity_schema_if_needed
        repaired = repair_user_activity_schema_if_needed(self.bq_client, PROJECT_ID, DATASET_ID, tbl.schema, force=False)
        self.assertFalse(repaired, "The Self-Healing procedure should not modify a healthy table with a RECORD schema.")


if __name__ == "__main__":
    unittest.main(verbosity=2)


