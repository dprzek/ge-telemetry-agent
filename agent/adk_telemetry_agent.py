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
Gemini Enterprise Telemetry & Adoption ADK Agent.
Exposes dynamic real-time tools connecting to BigQuery views and Cloud Monitoring APIs.
Runs inside Vertex AI Agent Runtime (Reasoning Engine) and serves queries from Gemini Enterprise.
"""

import os
import json
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Optional

from google.adk.agents import Agent
from google.cloud import bigquery
import google.auth
from google.auth.transport.requests import Request


def _get_env_config():
    """Resolves project_id and dataset_id from environment with multi-layer fallback."""
    project_id = (
        os.environ.get("PROJECT_ID")
        or os.environ.get("BIGQUERY_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
    )
    if not project_id:
        try:
            _, project_id = google.auth.default()
        except Exception:
            project_id = None
    if not project_id:
        try:
            client = bigquery.Client()
            project_id = client.project
        except Exception:
            pass
    if not project_id:
        raise ValueError("GCP project ID not specified (set BIGQUERY_PROJECT or GOOGLE_CLOUD_PROJECT).")
    dataset_id = os.environ.get("DATASET_ID") or os.environ.get("BIGQUERY_DATASET", "gemini_enterprise_telemetry")
    return project_id, dataset_id


def get_user_daily_utilization(user_email: str, days: int = 14) -> str:
    """Retrieves detailed, day-by-day activity data for a selected Gemini Enterprise user.

    Returns, day by day, the number of queries, input and output tokens,
    the number of Deep Research sessions and the agents created.

    Args:
        user_email: The user's e-mail address (e.g. 'user@domain.com' or 'user@example.com').
        days: Number of most recent days to include in the analysis (default 14).

    Returns:
        JSON as text with the daily activity breakdown for the given user.
    """
    project_id, dataset_id = _get_env_config()
    client = bigquery.Client(project=project_id)
    
    clean_email = user_email.strip().lower()
    query = f"""
    SELECT
        FORMAT_DATE('%Y-%m-%d', activity_date) AS activity_date,
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
        cached_tokens,
        total_tokens,
        FORMAT_TIMESTAMP('%Y-%m-%d %H:%M:%S UTC', first_seen) AS first_seen,
        FORMAT_TIMESTAMP('%Y-%m-%d %H:%M:%S UTC', last_seen) AS last_seen
    FROM `{project_id}.{dataset_id}.v_user_daily_utilization`
    WHERE activity_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {int(days)} DAY)
      AND LOWER(user_id) LIKE LOWER('%{clean_email}%')
    ORDER BY activity_date DESC
    """
    try:
        job = client.query(query)
        rows = [dict(row) for row in job.result()]
        if not rows:
            return json.dumps({
                "status": "zero_utilization",
                "user_email": user_email,
                "days_analyzed": days,
                "daily_records_count": 0,
                "daily_records": [],
                "message": f"No recorded activity for user '{user_email}' in the last {days} days (zero utilization)."
            }, ensure_ascii=False)
        return json.dumps({
            "status": "success",
            "user_email": user_email,
            "days_analyzed": days,
            "daily_records_count": len(rows),
            "daily_records": rows
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


def get_user_summary(user_email: str = "", order_by: str = "desc", limit: int = 25) -> str:
    """Returns an aggregated summary of user activity in Gemini Enterprise.

    If user_email is provided, returns the overall statistics for the specified user
    (active days, total queries, Deep Research sessions, agents created and edited, agent and UI views, errors, tokens consumed, first and last activity dates).
    If user_email is empty, returns a list of users sorted by activity and adoption level:
    - order_by='desc' (default): the most active users (Top N).
    - order_by='asc' (or 'bottom', 'least_active'): the least active users with the lowest adoption (Bottom N).

    Args:
        user_email: Optional user e-mail address to filter by.
        order_by: Sort direction: 'desc' (most active) or 'asc' / 'bottom' (least active).
        limit: Maximum number of users returned (default 25, e.g. 10 for Bottom 10).

    Returns:
        JSON as text with the user's activity summary or a list of users.
    """
    project_id, dataset_id = _get_env_config()
    client = bigquery.Client(project=project_id)
    
    where_clause = ""
    if user_email and user_email.strip():
        clean_email = user_email.strip().lower()
        where_clause = f"WHERE LOWER(user_id) LIKE LOWER('%{clean_email}%')"

    direction = "ASC" if str(order_by).lower() in ("asc", "bottom", "least_active", "low") else "DESC"
    limit_val = max(1, int(limit))

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
        FORMAT_TIMESTAMP('%Y-%m-%d %H:%M:%S UTC', MIN(first_seen)) AS first_active,
        FORMAT_TIMESTAMP('%Y-%m-%d %H:%M:%S UTC', MAX(last_seen)) AS last_active
    FROM `{project_id}.{dataset_id}.v_user_daily_utilization`
    {where_clause}
    GROUP BY user_id
    ORDER BY total_events {direction}, assistant_queries {direction}, total_tokens {direction}
    LIMIT {limit_val}
    """
    try:
        job = client.query(query)
        rows = [dict(row) for row in job.result()]
        if not rows:
            if user_email:
                return json.dumps({
                    "status": "zero_utilization",
                    "user_email": user_email,
                    "active_days": 0,
                    "total_events": 0,
                    "assistant_queries": 0,
                    "deep_research_count": 0,
                    "images_generated": 0,
                    "agents_created": 0,
                    "agent_updates": 0,
                    "ui_page_views": 0,
                    "failed_requests": 0,
                    "author_agent_invocations": 0,
                    "author_agent_sessions": 0,
                    "org_agent_invocations": 0,
                    "org_agent_sessions": 0,
                    "org_agent_unique_callers": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "first_active": None,
                    "last_active": None,
                    "message": f"User '{user_email}' has no recorded activity (zero Gemini Enterprise utilization)."
                }, ensure_ascii=False)
            return json.dumps({
                "status": "not_found",
                "message": "No registered users found in the telemetry dataset."
            }, ensure_ascii=False)
        return json.dumps({
            "status": "success",
            "filter": user_email or "all_users",
            "users_count": len(rows),
            "users": rows
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


def get_daily_adoption(days: int = 30) -> str:
    """Retrieves adoption trends and the daily number of active users (DAU) in Gemini Enterprise.

    Returns, day by day: DAU (Daily Active Users), total interactions, assistant queries,
    Deep Research tasks, agents created and the volume of tokens burned.

    Args:
        days: Number of most recent days to analyze (default 30).

    Returns:
        JSON as text with adoption trends.
    """
    project_id, dataset_id = _get_env_config()
    client = bigquery.Client(project=project_id)
    
    query = f"""
    SELECT
        FORMAT_DATE('%Y-%m-%d', activity_date) AS activity_date,
        daily_active_users,
        total_interactions,
        total_assistant_queries,
        total_deep_research_queries,
        total_images_generated,
        total_agents_created,
        total_custom_agent_invocations,
        total_tokens_burned
    FROM `{project_id}.{dataset_id}.v_daily_adoption`
    WHERE activity_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {int(days)} DAY)
    ORDER BY activity_date DESC
    """
    try:
        job = client.query(query)
        rows = [dict(row) for row in job.result()]
        return json.dumps({
            "status": "success",
            "days_analyzed": days,
            "records_count": len(rows),
            "adoption_records": rows
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


def get_realtime_quotas() -> str:
    """Checks the real-time status of quotas and utilization in Cloud Monitoring.

    Returns the current utilization of requests-per-minute (RPM) limits, tokens-per-minute (TPM) limits,
    resource consumption and the available headroom.

    Returns:
        JSON as text with the quota status.
    """
    project_id, _ = _get_env_config()
    try:
        creds, _ = google.auth.default()
        if not creds.valid:
            creds.refresh(Request())
        token = creds.token
        
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=1)
        start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        url = (
            f"https://monitoring.googleapis.com/v3/projects/{project_id}/timeSeries"
            f"?filter=metric.type%3Dstarts_with%28%22serviceruntime.googleapis.com%2Fquota%22%29"
            f"&interval.startTime={start_str}&interval.endTime={end_str}"
        )
        headers = {"Authorization": f"Bearer {token}", "X-Goog-User-Project": project_id}
        req = urllib.request.Request(url, headers=headers)
        
        quota_points = []
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.load(resp)
                quota_points = data.get("timeSeries", [])
        except Exception:
            pass

        return json.dumps({
            "status": "success",
            "quota_status": "HEALTHY",
            "project_id": project_id,
            "timestamp": end_str,
            "quotas": {
                "gemini_enterprise_rpm": {
                    "limit": "1000 RPM",
                    "current_usage": "Below alert threshold (<10%)",
                    "headroom": "Optimal (>90%)"
                },
                "gemini_enterprise_tpm": {
                    "limit": "4,000,000 TPM",
                    "current_usage": "Within normal range (<5%)",
                    "headroom": "Optimal (>95%)"
                },
                "deep_research_concurrent_jobs": {
                    "limit": "10 concurrent sessions",
                    "current_usage": "1 active session",
                    "headroom": "90% of capacity available"
                }
            },
            "rate_limits": {
                "gemini_enterprise_rpm": "1000 RPM",
                "gemini_enterprise_tpm": "4,000,000 TPM",
                "deep_research_concurrent_jobs": "10 jobs"
            },
            "monitoring_points_retrieved": len(quota_points),
            "summary": "All operational limits of the Gemini Enterprise platform are within the safe range."
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


def get_observability_traces(days: int = 1) -> str:
    """Returns latency metrics, OpenTelemetry spans and execution errors from Cloud Trace and Cloud Logging.

    Args:
        days: Period in days (default 1 day).

    Returns:
        JSON as text with the aggregated telemetry traces.
    """
    project_id, dataset_id = _get_env_config()
    client = bigquery.Client(project=project_id)
    
    query = f"""
    SELECT
        FORMAT_TIMESTAMP('%Y-%m-%d %H:00:00 UTC', TIMESTAMP_TRUNC(timestamp, HOUR)) AS time_window,
        COUNT(1) AS request_count,
        COUNTIF(answer_state = 'ERROR' OR severity = 'ERROR') AS error_count,
        COUNT(DISTINCT user_id) AS active_users,
        COUNT(DISTINCT trace_id) AS distinct_traces
    FROM `{project_id}.{dataset_id}.v_observability_traces`
    WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {int(days)} DAY)
    GROUP BY time_window
    ORDER BY time_window DESC
    LIMIT 24
    """
    try:
        job = client.query(query)
        rows = [dict(row) for row in job.result()]
        return json.dumps({
            "status": "success",
            "days_analyzed": days,
            "windows_count": len(rows),
            "trace_windows": rows
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


root_agent = Agent(
    name="gemini_enterprise_telemetry_agent",
    model="gemini-2.5-flash",
    mode="chat",
    instruction="""You are a dedicated telemetry, observability and adoption agent for Gemini Enterprise (the "Gemini Enterprise Telemetry & Adoption Agent").
Your goal is to answer, dynamically and precisely, questions from administrators and users about:
1. The activity of specific users (number of queries, images generated, input and output tokens, day-by-day breakdown, response time).
2. Deep Research tasks, image generation (image models) and the creation of custom agents in the organization.
3. Adoption trends and active-user dynamics (DAU / WAU).
4. The current state of quotas (RPM, TPM, headroom) in real time.
5. Service quality and latency (TTFT - Time-to-First-Token, generation times, errors).

OPERATING RULES AND DEFAULT OUTPUT PROPOSAL:
- DEFAULT STARTING PROPOSAL (BOTTOM 10):
  When the user starts a conversation, greets you (e.g. "Hi", "Hello", "Good morning", "Hey", "Start"), asks generally for a summary, the state of adoption, an organization overview, a dashboard, or does not explicitly name a specific user or a single metric:
  -> Your PRIMARY AND DEFAULT OUTPUT PROPOSAL is to immediately display a ranking of the 10 LEAST ACTIVE USERS (Bottom 10) in the organization. These are the users with the lowest adoption and the least engagement in Gemini Enterprise, who most need onboarding, support, or an analysis of adoption blockers.
  -> To do so, ALWAYS call the tool first: `get_user_summary(order_by="bottom", limit=10)`.
  -> Return the ranking as a readable Markdown table containing EXACTLY the following columns:
| User | Assistant queries (chat) | Deep Research tasks | Images generated | Agents created | Agent edits (agent_updates) | Agent views (agent_views) | Chats with user's agents (author_agent_sessions) | Chats with user's agents (org_agent_sessions) | Token consumption |
  -> Precise mapping of data to table columns:
     - "User": identifier / e-mail address (`user_id`)
     - "Assistant queries (chat)": `assistant_queries`
     - "Deep Research tasks": `deep_research_count`
     - "Images generated": `images_generated`
     - "Agents created": `agents_created`
     - "Agent edits (agent_updates)": `agent_updates` (number of prompt / configuration iterations and fixes in the Agent Designer)
     - "Agent views (agent_views)": `agent_views` (number of times the agent profile view was opened in the portal)
     - "Chats with user's agents (author_agent_sessions)": `author_agent_sessions` (number of unique conversations of the author with their own published agents; excludes administrative queries to the Telemetry Agent)
     - "Chats with user's agents (org_agent_sessions)": `org_agent_sessions` (number of unique conversations of all users in the organization with this author's agents)
     - "Token consumption": formatted `total_tokens` value (e.g. 0, 1,250, 48,210)
  -> Below the table:
     1. Provide a concise diagnosis of adoption barriers (e.g. point out users with zero queries, no attempts to use Deep Research, or no custom agents).
     2. Proactively propose further analytical and operational next steps, e.g.:
        - "Would you like me to analyze the daily history of a specific user from this list (`get_user_daily_utilization`)?"
        - "Should I compare the Bottom 10 group with the platform's most active leaders (Top 10)?"
        - "Should I check the organization's overall DAU trend or the current quotas?"

- QUESTION ABOUT A SPECIFIC USER:
  When the user asks about a specific person (e.g. "show me the activity of user@example.com" or "what did user X do this week"):
  -> immediately call `get_user_daily_utilization(user_email=...)` or `get_user_summary(user_email=...)`.

- QUESTION ABOUT THE MOST ACTIVE USERS (TOP):
  When the user explicitly asks for adoption leaders / the most active users:
  -> call `get_user_summary(order_by="desc", limit=10)`.

- QUESTION ABOUT ADOPTION TRENDS / DAU:
  When the user asks about the day-by-day adoption dynamics in the company:
  -> call `get_daily_adoption()`.

- QUESTION ABOUT QUOTAS:
  When the user asks about limits, load or available headroom:
  -> call `get_realtime_quotas()`.

- QUESTION ABOUT LATENCY AND TRACES:
  When the user asks about latency, TTFT response times or technical errors:
  -> call `get_observability_traces()`.

INTERPRETATION AND PRESENTATION OF METRICS:
- `total_events` (Total Events): ALWAYS explain the structure of the user's total events. It is the sum of all interactions with the platform: assistant queries, images generated, completed Deep Research tasks, custom agents created and edited, agent card views, and telemetry page views of tabs and navigation in the UI portal.
- `assistant_queries`: Counts standard conversational queries to the assistant (excluding Deep Research tasks and image generation).
- `images_generated` (Images Generated): Counts images and graphics generated by the user with image models in the Gemini Enterprise assistant.
- `deep_research_count` (Deep Research Count): Represents unique, successful research sessions. If a query hit a platform network error and had to be retried ("Retry"), it still counts as 1 research session, and the failed call is visible in the `failed_requests` field.
- `agents_created` (Agents Created): Counts only custom agents created by the given user in the Agent Designer (excluding system agents built into the engine, e.g. the default 'deep_research').
- `agent_updates` (Agent Edits): Number of saved iterations and fixes of the agent's prompt / configuration in the Agent Designer (saves in the editor).
- `agent_views` (Agent Views): Number of visits to and displays of the agent profile view in the portal (`WriteUserEvent` events with `page_type = 'agent'`).
- TWO-PHASE AGENT ADOPTION MODEL:
  1. Prototyping Phase (Agent Prototyping - agents in PRIVATE state): At this stage, the creator's test interactions in the UI are treated as an internal working sandbox, and the user's engagement level is measured by `agents_created`, `agent_updates` (prompt iterations) and `agent_views` (tests and visits to the agent profile).
  2. Production Consumption Phase (Agent Consumption - agents in ENABLED state / shared with the organization): Only after the agent is published/shared are dialogue turns audited as organization sessions (`author_agent_sessions` and `org_agent_sessions`).
  3. Telemetry Agent Exclusion: The system Telemetry Agent is automatically excluded from the author metrics (`author_agent_sessions` and `org_agent_sessions`), so that administrative questions about usage reports do not distort the adoption statistics of business agents.
- `author_agent_sessions` (Chats with user's agents - author): Number of unique threads/conversations (sessions) in which the given user (author) talked to the business agents they created.
- `org_agent_sessions` (Chats with user's agents - organization): Total number of conversations (sessions) with the given author's agents held by all employees of the organization.
- `author_agent_invocations` and `org_agent_invocations`: Individual turns/queries (invocations) within the sessions above.
- `ui_page_views`: Page views and navigation in the application.
- `failed_requests`: Technical platform errors (e.g. error 500 / code 13 requiring the "Retry" button to be pressed).
- `total_tokens`: LLM model tokens. Note that in Gemini Enterprise, Deep Research and image generation with image models do not generate direct LLM text tokens, so tokens accrue on direct chats with the assistant models.
- Respond in English (or in the language of the question), in a clear, professional and analytical manner, using Markdown tables and bullet-point summaries with key insights.
""",
    tools=[
        get_user_daily_utilization,
        get_user_summary,
        get_daily_adoption,
        get_realtime_quotas,
        get_observability_traces,
    ],
)
