#!/usr/bin/env python3
"""
Command-line interface (CLI) for Gemini Enterprise administrators.
Enables exploring adoption metrics, per-user daily utilization,
OpenTelemetry observability metrics, distributed traces, and quotas.
"""

import os
import sys
import argparse
import json
from telemetry_service import TelemetryService

def main():
    parser = argparse.ArgumentParser(description="Gemini Enterprise Telemetry, Adoption and Observability CLI")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"), help="GCP project ID (defaults to $GOOGLE_CLOUD_PROJECT)")
    parser.add_argument("--dataset", default=os.environ.get("DATASET_ID", "gemini_enterprise_telemetry"), help="BigQuery dataset ID")
    parser.add_argument("--engine", default=os.environ.get("GEMINI_ENGINE_ID"), help="Discovery Engine engine ID (e.g. my-app or the full ID)")
    parser.add_argument("--engine-id", dest="engine_id_flag", default=None, help="Explicit Discovery Engine engine identifier (Engine ID)")
    parser.add_argument("--location", default=os.environ.get("GOOGLE_CLOUD_LOCATION", "eu"), help="Google Cloud location (e.g. eu, us)")
    
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. User Utilization command
    p_user = subparsers.add_parser("utilization", help="Inspect user utilization within a selected time range")
    p_user.add_argument("--user", help="Filter by e-mail address / user ID")
    p_user.add_argument("--from-date", dest="from_date", help="Start date (YYYY-MM-DD)")
    p_user.add_argument("--to-date", dest="to_date", help="End date (YYYY-MM-DD)")
    p_user.add_argument("--daily", action="store_true", help="Show a detailed day-by-day activity breakdown")
    p_user.add_argument("--bottom", type=int, help="Show the N least active users (Bottom N)")
    p_user.add_argument("--order", choices=["desc", "asc"], default="desc", help="Sort direction (default desc)")
    p_user.add_argument("--limit", type=int, help="Maximum number of users")
    p_user.add_argument("--format", choices=["table", "json"], default="table", help="Output format (table or json)")

    # 2. Organization Adoption command
    p_adopt = subparsers.add_parser("adoption", help="Show organization adoption trends (DAU/WAU/MAU)")
    p_adopt.add_argument("--days", type=int, default=14, help="Number of recent days to analyze")
    p_adopt.add_argument("--format", choices=["table", "json"], default="table", help="Output format (table or json)")

    # 3. Feature Breakdown command
    p_feat = subparsers.add_parser("features", help="Show usage statistics for individual Gemini Enterprise modules")
    p_feat.add_argument("--format", choices=["table", "json"], default="table", help="Output format (table or json)")

    # 4. Observability and Traces command
    p_obs = subparsers.add_parser("observability", help="Check OpenTelemetry observability settings, engagement metrics and traces")
    p_obs.add_argument("--days", type=int, default=7, help="Metrics analysis window in days")
    p_obs.add_argument("--traces", action="store_true", help="Include recent distributed traces (Cloud Trace)")
    p_obs.add_argument("--format", choices=["table", "json"], default="table", help="Output format (table or json)")

    # 5. Quotas command
    p_quota = subparsers.add_parser("quotas", help="Show quotas, consumption status and reset schedule")
    p_quota.add_argument("--format", choices=["table", "json"], default="table", help="Output format (table or json)")

    # 6. Markdown Report command
    p_rep = subparsers.add_parser("report", help="Generate a full markdown telemetry and observability report")
    p_rep.add_argument("--output", help="Optional output file path")
    p_rep.add_argument("--days", type=int, default=14, help="Number of days to include in the report")

    args = parser.parse_args()
    engine_val = args.engine_id_flag or args.engine
    service = TelemetryService(
        project_id=args.project,
        dataset_id=args.dataset,
        location=args.location,
        engine_id=engine_val
    )

    if args.command == "utilization":
        if args.daily:
            results = service.get_user_daily_breakdown(start_date=args.from_date, end_date=args.to_date, user_id=args.user)
            if args.format == "json":
                print(json.dumps(results, indent=2))
            else:
                if not results:
                    if args.user:
                        print(f"\nUser '{args.user}' has no activity entries in the selected period (zero utilization).")
                    else:
                        print("No daily activity entries for the given criteria.")
                    return
                print(f"\n=== Daily User Utilization Report ({len(results)} daily entries) ===")
                print(f"{'Date':<12} | {'User ID':<28} | {'Events':<9} | {'Queries':<9} | {'Images':<7} | {'Deep Rsrch':<10} | {'Agents':<7} | {'Edits':<7} | {'Views':<7} | {'Author Calls':<13} | {'Org Calls':<13} | {'Tokens':<9}")
                print("-" * 184)
                for r in results:
                    author_call = f"{r.get('author_agent_invocations', 0)} ({r.get('author_agent_sessions', 0)})"
                    org_call = f"{r.get('org_agent_invocations', 0)} ({r.get('org_agent_sessions', 0)}/{r.get('org_agent_unique_callers', 0)})"
                    print(f"{r['activity_date']:<12} | {r['user_id']:<28} | {r['total_events']:<9} | {r['assistant_queries']:<9} | {r.get('images_generated', 0):<7} | {r['deep_research_count']:<10} | {r['agents_created']:<7} | {r.get('agent_updates', 0):<7} | {r.get('agent_views', 0):<7} | {author_call:<13} | {org_call:<13} | {r['total_tokens']:<9,}")
        else:
            order = "asc" if args.bottom else args.order
            limit = args.bottom or args.limit
            results = service.get_user_summary(start_date=args.from_date, end_date=args.to_date, user_id=args.user, order_by=order, limit=limit)
            if args.format == "json":
                print(json.dumps(results, indent=2))
            else:
                if not results:
                    if args.user:
                        print(f"\nUser '{args.user}' has zero utilization (0 queries, 0 Deep Research sessions, 0 agents created, 0 tokens).")
                    else:
                        print("No utilization data for the given criteria.")
                    return
                header_title = f"Least Active Users (Bottom {len(results)})" if (args.bottom or order == "asc") else f"Aggregated User Utilization Summary ({len(results)} users)"
                print(f"\n=== {header_title} ===")
                print(f"{'User ID':<28} | {'Active Days':<11} | {'Events':<9} | {'Queries':<9} | {'Images':<7} | {'Deep Rsrch':<10} | {'Agents':<7} | {'Edits':<7} | {'Views':<7} | {'Author Calls':<13} | {'Org Calls':<13} | {'Tokens':<9}")
                print("-" * 184)
                for r in results:
                    author_call = f"{r.get('author_agent_invocations', 0)} ({r.get('author_agent_sessions', 0)})"
                    org_call = f"{r.get('org_agent_invocations', 0)} ({r.get('org_agent_sessions', 0)}/{r.get('org_agent_unique_callers', 0)})"
                    print(f"{r['user_id']:<28} | {r['active_days']:<11} | {r['total_events']:<9} | {r['assistant_queries']:<9} | {r.get('images_generated', 0):<7} | {r['deep_research_count']:<10} | {r['agents_created']:<7} | {r.get('agent_updates', 0):<7} | {r.get('agent_views', 0):<7} | {author_call:<13} | {org_call:<13} | {r['total_tokens']:<9,}")
                print("\nTip: Add the '--daily' flag to see each user's activity day by day.")

    elif args.command == "adoption":
        results = service.get_daily_adoption(days=args.days)
        if args.format == "json":
            print(json.dumps(results, indent=2))
        else:
            print(f"\n=== Organization Daily Adoption Trends (Last {args.days} Days) ===")
            print(f"{'Date':<12} | {'DAU':<5} | {'Events':<9} | {'Queries':<9} | {'Images':<7} | {'Deep Rsrch':<10} | {'Agents':<7} | {'Tokens':<10}")
            print("-" * 87)
            for r in results:
                print(f"{r['activity_date']:<12} | {r['daily_active_users']:<5} | {r['total_interactions']:<9} | {r['total_assistant_queries']:<9} | {r.get('total_images_generated', 0):<7} | {r['total_deep_research_queries']:<10} | {r['total_agents_created']:<7} | {r['total_tokens_burned']:<10,}")

    elif args.command == "features":
        results = service.get_feature_breakdown()
        if args.format == "json":
            print(json.dumps(results, indent=2))
        else:
            print(f"\n=== Module and Feature Usage Statistics ===")
            print(f"{'Feature Name':<35} | {'Calls':<12} | {'Users':<15}")
            print("-" * 68)
            for r in results:
                print(f"{r['feature_name']:<35} | {r['total_calls']:<12} | {r['distinct_users']:<15}")

    elif args.command == "observability":
        metrics = service.get_observability_metrics(days=args.days)
        traces = service.get_recent_traces(limit=10) if args.traces else []
        if args.format == "json":
            out = {"metrics": metrics, "recent_traces": traces}
            print(json.dumps(out, indent=2))
        else:
            cfg = metrics["observability_settings"]
            print(f"\n=== Gemini Enterprise: Observability and OpenTelemetry Metrics ===")
            print(f"• Engine ID:                   {cfg.get('engine_id')}")
            print(f"• Location:                    {cfg.get('location')}")
            print(f"• Observability Enabled:       {cfg.get('observability_enabled')}")
            print(f"• Sensitive Logging Enabled:   {cfg.get('sensitive_logging_enabled')}")
            print(f"• App Type:                    {cfg.get('app_type')}")
            print("-" * 68)
            print(f"• Agent Sessions:              {metrics['total_agent_sessions']}")
            print(f"• Conversational Turns:        {metrics['total_agent_turns']}")
            print(f"• Conversational Depth:        {metrics['conversational_depth_turns_per_session']} turns/session")
            print(f"• Sessions Using Tools:        {metrics['total_sessions_with_tool']}")
            print(f"• Tool Adoption Rate:          {metrics['tool_adoption_rate_pct']}%")
            print(f"• Total Requests:              {metrics['total_engine_requests']}")
            print(f"• Avg Time to First Token:     {metrics['avg_time_to_first_token_ms'] or 'No data'} ms")
            print(f"• Avg Total Latency:           {metrics['avg_request_total_latency_ms'] or 'No data'} ms")

            if args.traces and traces:
                print(f"\n=== Recent Distributed OpenTelemetry Traces ({len(traces)} entries) ===")
                print(f"{'Time (UTC)':<20} | {'Trace ID':<34} | {'Method':<14} | {'User':<26} | {'Status':<8}")
                print("-" * 110)
                for t in traces:
                    print(f"{t['timestamp'][:19]:<20} | {t['trace_id']:<34} | {t['method_name']:<14} | {t['user_id']:<26} | {t['answer_state']:<8}")

    elif args.command == "quotas":
        quotas = service.get_realtime_quotas()
        if args.format == "json":
            print(json.dumps(quotas, indent=2))
        else:
            print(f"\n=== Gemini Enterprise Quota Status (Quotas & Overages) ===")
            for k, v in quotas.items():
                if isinstance(v, dict) and "metric" in v:
                    print(f"• {k:<30}: {v['value']} ({v['status']})")
                elif isinstance(v, dict):
                    print(f"\n{k}:")
                    for sub_k, sub_v in v.items():
                        print(f"  - {sub_k:<45}: {sub_v}")

    elif args.command == "report":
        digest = service.generate_digest_markdown(days=args.days)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(digest)
            print(f"Report saved to file: {args.output}")
        else:
            print(digest)

if __name__ == "__main__":
    main()
