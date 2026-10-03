#!/usr/bin/env python3
"""
Gemini Enterprise Telemetry & Adoption Service.
Integrates BigQuery (historical & real-time streamed per-user daily utilization & adoption,
OpenTelemetry traces & spans) and Cloud Monitoring (real-time pooled quotas, limit headroom,
operational metrics, latencies, and tool adoption ratios).
"""

import os
import json
import urllib.request
from datetime import datetime, timezone, timedelta
import google.auth
from google.auth.transport.requests import Request
from google.cloud import bigquery

class TelemetryService:
    def __init__(self, project_id=None, dataset_id="gemini_enterprise_telemetry", location="eu", engine_id=None):
        if not project_id:
            project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not project_id:
            try:
                import subprocess
                project_id = subprocess.check_output(["gcloud", "config", "get-value", "project"], text=True).strip()
            except Exception:
                pass
        self.project_id = project_id or "default-project"
        self.dataset_id = dataset_id
        self.location = location
        self.engine_id = engine_id or os.environ.get("GEMINI_ENGINE_ID")
        self.credentials, default_proj = google.auth.default()
        if self.project_id == "default-project" and default_proj:
            self.project_id = default_proj
        self.bq_client = bigquery.Client(project=self.project_id, credentials=self.credentials)
        self.engine_id = self._resolve_engine_id()

    def _resolve_engine_id(self):
        """
        Automatically resolves the engine_id by checking for a direct match,
        the displayName value, or a resource prefix (e.g. 'my-app' -> 'my-app_1234567890').
        If no engine_id is provided, picks the first available engine in the project.
        """
        try:
            api_host = f"{self.location}-discoveryengine.googleapis.com" if self.location != "global" else "discoveryengine.googleapis.com"
            token = self._get_access_token()
            
            # 1. If an engine_id was provided, check whether it works directly
            if self.engine_id:
                direct_url = f"https://{api_host}/v1alpha/projects/{self.project_id}/locations/{self.location}/collections/default_collection/engines/{self.engine_id}"
                req = urllib.request.Request(direct_url, headers={"Authorization": f"Bearer {token}", "X-Goog-User-Project": self.project_id})
                try:
                    with urllib.request.urlopen(req) as resp:
                        if resp.status == 200:
                            return self.engine_id
                except urllib.error.HTTPError as e:
                    if e.code != 404:
                        return self.engine_id

            # 2. List the engines and find a match, or pick the first available one
            list_url = f"https://{api_host}/v1alpha/projects/{self.project_id}/locations/{self.location}/collections/default_collection/engines"
            req = urllib.request.Request(list_url, headers={"Authorization": f"Bearer {token}", "X-Goog-User-Project": self.project_id})
            with urllib.request.urlopen(req) as resp:
                data = json.load(resp)
                engines = data.get("engines", [])
                if not self.engine_id and engines:
                    return engines[0].get("name", "").split("/")[-1]
                for eng in engines:
                    eid = eng.get("name", "").split("/")[-1]
                    dname = eng.get("displayName", "")
                    if self.engine_id and (dname == self.engine_id or eid == self.engine_id or eid.startswith(f"{self.engine_id}_")):
                        return eid
        except Exception:
            pass
        return self.engine_id

    def _get_access_token(self):
        if not self.credentials.valid:
            self.credentials.refresh(Request())
        return self.credentials.token

    def get_user_summary(self, start_date=None, end_date=None, user_id=None, order_by="desc", limit=None):
        """
        Query aggregate utilization per user across customizable time spans.
        Supports sorting by activity level:
        - order_by='desc': most active users (Top N)
        - order_by='asc' (or 'bottom', 'least_active'): least active / performing users (Bottom N)
        """
        where_clauses = []
        if start_date:
            where_clauses.append(f"activity_date >= '{start_date}'")
        if end_date:
            where_clauses.append(f"activity_date <= '{end_date}'")
        if user_id:
            where_clauses.append(f"LOWER(user_id) LIKE LOWER('%{user_id}%')")

        where_stmt = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        direction = "ASC" if str(order_by).lower() in ("asc", "bottom", "least_active", "low") else "DESC"
        limit_stmt = f"LIMIT {int(limit)}" if limit is not None else ""

        query = f"""
        SELECT
            user_id,
            COUNT(DISTINCT activity_date) AS active_days,
            SUM(total_events) AS total_events,
            SUM(assistant_queries) AS assistant_queries,
            SUM(deep_research_count) AS deep_research_count,
            SUM(images_generated) AS images_generated,
            SUM(agents_created) AS agents_created,
            SUM(agent_updates) AS agent_updates,
            SUM(agent_views) AS agent_views,
            SUM(ui_page_views) AS ui_page_views,
            SUM(failed_requests) AS failed_requests,
            SUM(author_agent_invocations) AS author_agent_invocations,
            SUM(author_agent_sessions) AS author_agent_sessions,
            SUM(org_agent_invocations) AS org_agent_invocations,
            SUM(org_agent_sessions) AS org_agent_sessions,
            MAX(org_agent_unique_callers) AS org_agent_unique_callers,
            SUM(input_tokens) AS input_tokens,
            SUM(output_tokens) AS output_tokens,
            SUM(total_tokens) AS total_tokens,
            MIN(first_seen) AS first_active,
            MAX(last_seen) AS last_active
        FROM `{self.project_id}.{self.dataset_id}.v_user_daily_utilization`
        {where_stmt}
        GROUP BY user_id
        ORDER BY total_events {direction}, assistant_queries {direction}, total_tokens {direction}
        {limit_stmt}
        """
        job = self.bq_client.query(query)
        results = []
        for row in job.result():
            results.append({
                "user_id": row.user_id,
                "active_days": row.active_days,
                "total_events": row.total_events,
                "assistant_queries": row.assistant_queries,
                "deep_research_count": row.deep_research_count,
                "images_generated": row.images_generated,
                "agents_created": row.agents_created,
                "agent_updates": row.agent_updates,
                "agent_views": row.agent_views,
                "ui_page_views": row.ui_page_views,
                "failed_requests": row.failed_requests,
                "author_agent_invocations": row.author_agent_invocations,
                "author_agent_sessions": row.author_agent_sessions,
                "org_agent_invocations": row.org_agent_invocations,
                "org_agent_sessions": row.org_agent_sessions,
                "org_agent_unique_callers": row.org_agent_unique_callers,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "total_tokens": row.total_tokens,
                "first_active": str(row.first_active) if row.first_active else None,
                "last_active": str(row.last_active) if row.last_active else None,
            })
        return results

    def get_user_daily_breakdown(self, start_date=None, end_date=None, user_id=None):
        """
        Query exact day-by-day utilization per user.
        Answers: "show me user X's adoption broken down by individual days"
        """
        where_clauses = []
        if start_date:
            where_clauses.append(f"activity_date >= '{start_date}'")
        if end_date:
            where_clauses.append(f"activity_date <= '{end_date}'")
        if user_id:
            where_clauses.append(f"LOWER(user_id) LIKE LOWER('%{user_id}%')")

        where_stmt = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        query = f"""
        SELECT
            activity_date,
            user_id,
            total_events,
            assistant_queries,
            deep_research_count,
            images_generated,
            agents_created,
            agent_updates,
            agent_views,
            ui_page_views,
            failed_requests,
            author_agent_invocations,
            author_agent_sessions,
            org_agent_invocations,
            org_agent_sessions,
            org_agent_unique_callers,
            input_tokens,
            output_tokens,
            total_tokens,
            first_seen,
            last_seen
        FROM `{self.project_id}.{self.dataset_id}.v_user_daily_utilization`
        {where_stmt}
        ORDER BY activity_date DESC, total_events DESC
        """
        job = self.bq_client.query(query)
        results = []
        for row in job.result():
            results.append({
                "activity_date": str(row.activity_date),
                "user_id": row.user_id,
                "total_events": row.total_events,
                "assistant_queries": row.assistant_queries,
                "deep_research_count": row.deep_research_count,
                "images_generated": row.images_generated,
                "agents_created": row.agents_created,
                "agent_updates": row.agent_updates,
                "agent_views": row.agent_views,
                "ui_page_views": row.ui_page_views,
                "failed_requests": row.failed_requests,
                "author_agent_invocations": row.author_agent_invocations,
                "author_agent_sessions": row.author_agent_sessions,
                "org_agent_invocations": row.org_agent_invocations,
                "org_agent_sessions": row.org_agent_sessions,
                "org_agent_unique_callers": row.org_agent_unique_callers,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "total_tokens": row.total_tokens,
                "first_seen": str(row.first_seen) if row.first_seen else None,
                "last_seen": str(row.last_seen) if row.last_seen else None,
            })
        return results

    def get_daily_adoption(self, days=30):
        """
        Query organization-level daily adoption metrics.
        Tracks DAU, query volume, tokens burned, and agents created.
        """
        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        query = f"""
        SELECT
            activity_date,
            daily_active_users,
            total_interactions,
            total_assistant_queries,
            total_deep_research_queries,
            total_images_generated,
            total_agents_created,
            total_agent_updates,
            total_agent_views,
            total_custom_agent_invocations,
            total_tokens_burned
        FROM `{self.project_id}.{self.dataset_id}.v_daily_adoption`
        WHERE activity_date >= '{cutoff_date}'
        ORDER BY activity_date DESC
        """
        job = self.bq_client.query(query)
        results = []
        for row in job.result():
            results.append({
                "activity_date": str(row.activity_date),
                "daily_active_users": row.daily_active_users,
                "total_interactions": row.total_interactions,
                "total_assistant_queries": row.total_assistant_queries,
                "total_deep_research_queries": row.total_deep_research_queries,
                "total_images_generated": row.total_images_generated,
                "total_agents_created": row.total_agents_created,
                "total_agent_updates": row.total_agent_updates,
                "total_agent_views": row.total_agent_views,
                "total_custom_agent_invocations": row.total_custom_agent_invocations,
                "total_tokens_burned": row.total_tokens_burned,
            })
        return results

    def get_feature_breakdown(self):
        """
        Query feature breakdown across Gemini Enterprise features.
        """
        query = f"""
        SELECT
            feature_name,
            total_calls,
            distinct_users,
            earliest_invocation,
            latest_invocation
        FROM `{self.project_id}.{self.dataset_id}.v_feature_usage`
        ORDER BY total_calls DESC
        """
        job = self.bq_client.query(query)
        results = []
        for row in job.result():
            results.append({
                "feature_name": row.feature_name,
                "total_calls": row.total_calls,
                "distinct_users": row.distinct_users,
                "earliest_invocation": str(row.earliest_invocation) if row.earliest_invocation else None,
                "latest_invocation": str(row.latest_invocation) if row.latest_invocation else None,
            })
        return results

    def get_observability_settings(self):
        """
        Fetch Discovery Engine Engine-level Observability settings:
        - OpenTelemetry trace instrumentation
        - Sensitive prompt/response logging
        """
        token = self._get_access_token()
        api_host = f"{self.location}-discoveryengine.googleapis.com" if self.location != "global" else "discoveryengine.googleapis.com"
        url = f"https://{api_host}/v1alpha/projects/{self.project_id}/locations/{self.location}/collections/default_collection/engines/{self.engine_id}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "X-Goog-User-Project": self.project_id})
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.load(resp)
                cfg = data.get("observabilityConfig", {})
                return {
                    "engine_id": self.engine_id,
                    "location": self.location,
                    "observability_enabled": cfg.get("observabilityEnabled", False),
                    "sensitive_logging_enabled": cfg.get("sensitiveLoggingEnabled", False),
                    "app_type": data.get("appType", "APP_TYPE_CHAT")
                }
        except Exception as e:
            return {
                "engine_id": self.engine_id,
                "location": self.location,
                "observability_enabled": True,
                "sensitive_logging_enabled": False,
                "error": str(e)
            }

    def get_observability_metrics(self, days=7):
        """
        Query Cloud Monitoring for Operational Metrics:
        - Sessions & Conversational Depth (Turns per Session)
        - Tool Adoption Rate (Sessions with Tool / Total Sessions)
        - Latencies: Time to First Token (TTFT) and Total Latency
        """
        token = self._get_access_token()
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days)
        start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        metric_keys = {
            "sessions": "discoveryengine.googleapis.com/agent_session_count",
            "sessions_with_tool": "discoveryengine.googleapis.com/agent_session_with_tool_count",
            "turns": "discoveryengine.googleapis.com/agent_turn_count",
            "engine_requests": "discoveryengine.googleapis.com/engine/request_count",
            "ttft_latency": "discoveryengine.googleapis.com/engine/time_to_first_token_latency",
            "total_latency": "discoveryengine.googleapis.com/engine/request_total_latency",
        }

        raw_values = {}
        for key, metric_type in metric_keys.items():
            url = (
                f"https://monitoring.googleapis.com/v3/projects/{self.project_id}/timeSeries"
                f"?filter=metric.type%3D%22{metric_type}%22"
                f"&interval.startTime={start_str}&interval.endTime={end_str}"
            )
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            try:
                with urllib.request.urlopen(req) as resp:
                    data = json.load(resp)
                    ts = data.get("timeSeries", [])
                    raw_values[key] = ts
            except Exception:
                raw_values[key] = []

        # Parse counts
        def sum_int64(ts_list):
            total = 0
            for s in ts_list:
                for pt in s.get("points", []):
                    v = pt.get("value", {}).get("int64Value", 0)
                    total += int(v)
            return total

        total_sessions = sum_int64(raw_values["sessions"])
        total_sessions_with_tool = sum_int64(raw_values["sessions_with_tool"])
        total_turns = sum_int64(raw_values["turns"])
        total_engine_requests = sum_int64(raw_values["engine_requests"])

        # Calculate ratios
        conversational_depth = round(total_turns / max(1, total_sessions), 2) if total_sessions > 0 else 1.0
        tool_adoption_pct = round((total_sessions_with_tool / max(1, total_sessions)) * 100, 1) if total_sessions > 0 else 0.0

        # Parse distribution mean latencies (ms)
        def get_mean_latency(ts_list):
            means = []
            for s in ts_list:
                for pt in s.get("points", []):
                    dist = pt.get("value", {}).get("distributionValue", {})
                    if "mean" in dist:
                        means.append(float(dist["mean"]))
            return round(sum(means) / len(means), 2) if means else None

        avg_ttft_ms = get_mean_latency(raw_values["ttft_latency"])
        avg_total_latency_ms = get_mean_latency(raw_values["total_latency"])

        obs_settings = self.get_observability_settings()

        return {
            "period_days": days,
            "observability_settings": obs_settings,
            "total_agent_sessions": total_sessions,
            "total_sessions_with_tool": total_sessions_with_tool,
            "tool_adoption_rate_pct": tool_adoption_pct,
            "total_agent_turns": total_turns,
            "conversational_depth_turns_per_session": conversational_depth,
            "total_engine_requests": total_engine_requests,
            "avg_time_to_first_token_ms": avg_ttft_ms,
            "avg_request_total_latency_ms": avg_total_latency_ms
        }

    def get_recent_traces(self, limit=10):
        """
        Query BigQuery for recent OpenTelemetry trace and span executions.
        """
        query = f"""
        SELECT
            timestamp,
            trace_id,
            span_id,
            user_id,
            method_name,
            answer_state,
            agent_display_name
        FROM `{self.project_id}.{self.dataset_id}.v_observability_traces`
        ORDER BY timestamp DESC
        LIMIT {limit}
        """
        try:
            job = self.bq_client.query(query)
            results = []
            for row in job.result():
                results.append({
                    "timestamp": str(row.timestamp),
                    "trace_id": row.trace_id,
                    "span_id": row.span_id,
                    "user_id": row.user_id,
                    "method_name": row.method_name,
                    "answer_state": row.answer_state or "SUCCESS",
                    "agent_display_name": row.agent_display_name or "N/A"
                })
            return results
        except Exception:
            return []

    def get_realtime_quotas(self):
        """
        Query Cloud Monitoring API for real-time pooled quota limits and usage.
        """
        token = self._get_access_token()
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=24)
        start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        metrics_to_check = {
            "Assistant Queries": "discoveryengine.googleapis.com/quota/text_answer_gen_tier_enterprise_regional/limit",
            "Agents Created": "discoveryengine.googleapis.com/quota/agents_tier_enterprise_regional/limit",
            "Deep Research": "discoveryengine.googleapis.com/quota/deep_research_query_total_tier_enterprise_regional/limit",
            "Image Generation": "discoveryengine.googleapis.com/quota/image_gen_tier_enterprise_regional/limit",
            "Video Generation": "discoveryengine.googleapis.com/quota/video_gen_numbers_tier_enterprise_regional/limit",
            "Storage / Total Size": "discoveryengine.googleapis.com/total_document_size_regional",
        }

        quotas = {}
        for label, metric_type in metrics_to_check.items():
            url = (
                f"https://monitoring.googleapis.com/v3/projects/{self.project_id}/timeSeries"
                f"?filter=metric.type%3D%22{metric_type}%22"
                f"&interval.startTime={start_str}&interval.endTime={end_str}"
            )
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            try:
                with urllib.request.urlopen(req) as resp:
                    data = json.load(resp)
                    ts = data.get("timeSeries", [])
                    if ts and "points" in ts[0] and ts[0]["points"]:
                        val = ts[0]["points"][0]["value"]
                        metric_val = val.get("int64Value", val.get("doubleValue", 0))
                        quotas[label] = {
                            "metric": metric_type,
                            "value": metric_val,
                            "status": "ACTIVE"
                        }
                    else:
                        quotas[label] = {
                            "metric": metric_type,
                            "value": "Unlimited / Dynamic Pooling",
                            "status": "DEFAULT"
                        }
            except Exception:
                quotas[label] = {
                    "metric": metric_type,
                    "value": "Managed Tier",
                    "status": "OK"
                }

        quotas["Quota Reset Cycle"] = {
            "Assistant, Agents, Images, Video, Deep Research": "Daily at midnight PT (Pooled)",
            "AI Developer Tools (WTU)": "Rolling 7-day pooled cycle",
            "Data Storage & Indexing": "Continuous regional pool"
        }
        return quotas

    def generate_digest_markdown(self, days=14):
        """
        Generate a comprehensive executive markdown digest suitable for
        admins or grounding an LLM agent, including exact day-by-day tables,
        traces, and observability metrics.
        """
        adoption = self.get_daily_adoption(days=days)
        users = self.get_user_summary()
        daily_breakdown = self.get_user_daily_breakdown()
        quotas = self.get_realtime_quotas()
        obs_metrics = self.get_observability_metrics(days=days)
        traces = self.get_recent_traces(limit=5)

        total_users = len(users)
        total_queries = sum(u["assistant_queries"] for u in users)
        total_deep_research = sum(u["deep_research_count"] for u in users)
        total_agents = sum(u["agents_created"] for u in users)
        total_tokens = sum(u["total_tokens"] for u in users)

        md = []
        md.append(f"# Gemini Enterprise Telemetry, Adoption and Observability Report")
        md.append(f"*Project*: `{self.project_id}` | *Generated*: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")

        md.append("## 1. Executive Summary")
        md.append(f"- **Monitored Users**: {total_users}")
        md.append(f"- **Total Assistant Queries**: {total_queries}")
        md.append(f"- **Deep Research Queries**: {total_deep_research}")
        md.append(f"- **Agents Created / Modified**: {total_agents}")
        md.append(f"- **Total Tokens Consumed**: {total_tokens:,}")
        md.append(f"- **Conversational Depth (Turns/Session)**: {obs_metrics['conversational_depth_turns_per_session']}")
        md.append(f"- **Tool Adoption Rate**: {obs_metrics['tool_adoption_rate_pct']}%")
        md.append(f"- **Time to First Token (TTFT)**: {obs_metrics['avg_time_to_first_token_ms'] or 'No data'} ms")
        md.append("")

        md.append("## 2. Per-User Summary (Aggregated)")
        md.append("| User ID | Active Days | Events | Assistant Queries | Deep Research | Agents Created | Author Invocations (queries/sessions) | Org Invocations (queries/sessions/users) | Tokens Consumed | First Activity | Last Activity |")
        md.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for u in users:
            first_seen = u["first_active"][:10] if u["first_active"] else "No data"
            last_seen = u["last_active"][:10] if u["last_active"] else "No data"
            author_call = f"{u.get('author_agent_invocations', 0)} ({u.get('author_agent_sessions', 0)})"
            org_call = f"{u.get('org_agent_invocations', 0)} ({u.get('org_agent_sessions', 0)} / {u.get('org_agent_unique_callers', 0)})"
            md.append(f"| `{u['user_id']}` | {u['active_days']} | {u['total_events']} | {u['assistant_queries']} | {u['deep_research_count']} | {u['agents_created']} | {author_call} | {org_call} | {u['total_tokens']:,} | {first_seen} | {last_seen} |")
        md.append("")

        md.append("## 3. Detailed Day-by-Day User Utilization Breakdown")
        md.append("| Date | User ID | Events | Assistant Queries | Deep Research | Agents Created | Author Invocations | Org Invocations | Tokens Consumed |")
        md.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for d in daily_breakdown:
            author_call = f"{d.get('author_agent_invocations', 0)} ({d.get('author_agent_sessions', 0)})"
            org_call = f"{d.get('org_agent_invocations', 0)} ({d.get('org_agent_sessions', 0)} / {d.get('org_agent_unique_callers', 0)})"
            md.append(f"| {d['activity_date']} | `{d['user_id']}` | {d['total_events']} | {d['assistant_queries']} | {d['deep_research_count']} | {d['agents_created']} | {author_call} | {org_call} | {d['total_tokens']:,} |")
        md.append("")

        md.append("## 4. Observability and Platform Operations Metrics")
        md.append(f"- **Observability Enabled**: {obs_metrics['observability_settings']['observability_enabled']}")
        md.append(f"- **Sensitive Logging Enabled**: {obs_metrics['observability_settings']['sensitive_logging_enabled']}")
        md.append(f"- **Total Agent Sessions**: {obs_metrics['total_agent_sessions']}")
        md.append(f"- **Total Conversational Turns**: {obs_metrics['total_agent_turns']}")
        md.append(f"- **Conversational Depth**: {obs_metrics['conversational_depth_turns_per_session']} turns / session")
        md.append(f"- **Average Time to First Token (TTFT)**: {obs_metrics['avg_time_to_first_token_ms']} ms")
        md.append(f"- **Average Total Response Latency**: {obs_metrics['avg_request_total_latency_ms']} ms")
        md.append("")

        if traces:
            md.append("### Recent Distributed OpenTelemetry Traces (Cloud Trace)")
            md.append("| Time (UTC) | Trace ID | Span ID | User | Method | Status |")
            md.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
            for t in traces:
                md.append(f"| {t['timestamp'][:19]} | `{t['trace_id']}` | `{t['span_id']}` | `{t['user_id']}` | `{t['method_name']}` | `{t['answer_state']}` |")
            md.append("")

        md.append("## 5. Organization Daily Adoption Trends (Recent Days)")
        md.append("| Date | Active Users (DAU) | Total Interactions | Assistant Queries | Deep Research | Agents Created |")
        md.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
        for d in adoption[:7]:
            md.append(f"| {d['activity_date']} | {d['daily_active_users']} | {d['total_interactions']} | {d['total_assistant_queries']} | {d['total_deep_research_queries']} | {d['total_agents_created']} |")
        md.append("")

        md.append("## 6. Quota Enforcement and Reset Schedules")
        md.append("All quotas are pooled at the organization level according to the [Gemini Enterprise Quotas Documentation](https://docs.cloud.google.com/gemini/enterprise/docs/quotas-and-overages):")
        md.append("- **Assistant Queries**: 160 (Standard) / 200 (Plus) per user/day. Resets at midnight PT.")
        md.append("- **Agent Creation**: 1 (Standard) / 10 (Plus) per user/day. Resets at midnight PT.")
        md.append("- **Deep Research**: 3 (Standard) / 10 (Plus) per user/day. Resets at midnight PT.")
        md.append("- **Image Generation**: 5 (Standard) / 10 (Plus) per user/day. Resets at midnight PT.")
        md.append("- **Video Generation**: 2 (Standard) / 3 (Plus) per user/day. Resets at midnight PT.")
        md.append("- **AI Developer Tools (WTU)**: $10 (Standard) / $15 (Plus) per user in a rolling 7-day window.")
        md.append("- **Storage and Indexing**: 30 GiB (Standard) / 75 GiB (Plus) per user in the regional pool.")
        md.append("")

        return "\n".join(md)
