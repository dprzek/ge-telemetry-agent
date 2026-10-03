#!/usr/bin/env python3
"""
Backfill of historical Gemini Enterprise Cloud Logging logs into BigQuery.
Creates and populates partitioned telemetry tables for audit events,
user activity and GenAI model inference operations from the last N days.
Guarantees that all tables and schemas are initialized so that the SQL analytical views
can be created immediately even when no historical events exist.
"""

import sys
import os
import json
import subprocess
from google.cloud import bigquery

def create_partitioned_table(client, table_id, schema):
    table = bigquery.Table(table_id, schema=schema)
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY, field="timestamp"
    )
    tbl = client.create_table(table, exists_ok=True)
    existing_field_names = {f.name for f in tbl.schema}
    missing_fields = [f for f in schema if f.name not in existing_field_names]
    if missing_fields:
        tbl.schema = list(tbl.schema) + missing_fields
        tbl = client.update_table(tbl, ["schema"])
    return tbl

def fetch_logs(project_id, filter_str, days=30, limit=1000):
    cmd = [
        "gcloud", "logging", "read", filter_str,
        f"--project={project_id}",
        f"--freshness={days}d",
        f"--limit={limit}",
        "--format=json"
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        return []
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return []


def parse_activity_entry(e):
    agents_obj = e.get("jsonPayload", {}).get("request", {}).get("agentsSpec") or e.get("jsonPayload", {}).get("request", {}).get("agentsspec") or {}
    specs_list = agents_obj.get("agentSpecs") or agents_obj.get("agentspecs") or [{}]
    first_spec = specs_list[0] if specs_list else {}
    agent_id = first_spec.get("agentId") or first_spec.get("agentid") or ""

    return {
        "insert_id": e.get("insertId", ""),
        "insertId": e.get("insertId", ""),
        "timestamp": e.get("timestamp"),
        "user_iam_principal": e.get("jsonPayload", {}).get("userIamPrincipal", e.get("jsonPayload", {}).get("useriamprincipal", "")),
        "user_pseudo_id": e.get("jsonPayload", {}).get("request", {}).get("userEvent", {}).get("userPseudoId", e.get("jsonPayload", {}).get("request", {}).get("userevent", {}).get("userpseudoid", "")),
        "method_name": e.get("jsonPayload", {}).get("logMetadata", {}).get("methodName", e.get("jsonPayload", {}).get("logmetadata", {}).get("methodname", "")),
        "engine": e.get("jsonPayload", {}).get("request", {}).get("userEvent", {}).get("engine", e.get("jsonPayload", {}).get("request", {}).get("userevent", {}).get("engine", e.get("jsonPayload", {}).get("logMetadata", {}).get("name", ""))),
        "page_type": e.get("jsonPayload", {}).get("request", {}).get("userEvent", {}).get("agentspaceInfo", {}).get("agentspacePageType", e.get("jsonPayload", {}).get("request", {}).get("userevent", {}).get("agentspaceinfo", {}).get("agentspacepagetype", "")),
        "event_type": e.get("jsonPayload", {}).get("request", {}).get("userEvent", {}).get("eventType", e.get("jsonPayload", {}).get("request", {}).get("userevent", {}).get("eventtype", "")),
        "agent_id": agent_id,
        "raw_payload": json.dumps(e.get("jsonPayload", {}))
    }

def parse_inference_entry(e):
    jp = e.get("jsonPayload", {})
    return {
        "insert_id": e.get("insertId", ""),
        "insertId": e.get("insertId", ""),
        "timestamp": e.get("timestamp"),
        "user_id": jp.get("user.id", jp.get("user_id", "")),
        "conversation_id": jp.get("gen_ai.conversation.id", jp.get("conversation_id", "")),
        "agent_name": jp.get("gen_ai.agent.name", e.get("resource", {}).get("labels", {}).get("agent_id", "")),
        "engine_id": e.get("resource", {}).get("labels", {}).get("engine_id", ""),
        "assistant_id": e.get("resource", {}).get("labels", {}).get("assistant_id", ""),
        "input_tokens": int(jp.get("gen_ai.usage.input_tokens", jp.get("gen_ai_usage_input_tokens", 0)) or 0),
        "output_tokens": int(jp.get("gen_ai.usage.output_tokens", jp.get("gen_ai_usage_output_tokens", 0)) or 0),
        "cached_tokens": int(jp.get("gen_ai.usage.cache_read.input_tokens", jp.get("gen_ai_usage_cached_tokens", 0)) or 0),
        "finish_reason": (jp.get("gen_ai.response.finish_reasons", [""])[0] if jp.get("gen_ai.response.finish_reasons") else ""),
        "raw_payload": json.dumps(jp)
    }

def parse_audit_entry(e):
    proto = e.get("protoPayload", {})
    return {
        "insert_id": e.get("insertId", ""),
        "insertId": e.get("insertId", ""),
        "timestamp": e.get("timestamp"),
        "principal_email": proto.get("authenticationInfo", {}).get("principalEmail", "unknown"),
        "method_name": proto.get("methodName", ""),
        "resource_name": proto.get("resourceName", ""),
        "raw_payload": json.dumps(proto),
        "protopayload_auditlog": {
            "methodName": proto.get("methodName", ""),
            "resourceName": proto.get("resourceName", ""),
            "status": {
                "code": proto.get("status", {}).get("code", 0) if isinstance(proto.get("status"), dict) else 0,
                "message": proto.get("status", {}).get("message", "") if isinstance(proto.get("status"), dict) else "",
            },
            "authenticationInfo": {
                "principalEmail": proto.get("authenticationInfo", {}).get("principalEmail", "unknown")
            }
        }
    }

def repair_user_activity_schema_if_needed(client, project_id, dataset_id, user_act_schema, force=False):
    """Checks whether the discoveryengine_googleapis_com_gemini_enterprise_user_activity table
    has a jsonPayload.request.query column of type STRING (which causes StreamAssist rejections:
    'This field: query is not a record').
    If so (or when force=True), backs up the old table and recreates the correct RECORD schema
    with the parts REPEATED RECORD<text STRING> structure.
    """
    table_id = f"{project_id}.{dataset_id}.discoveryengine_googleapis_com_gemini_enterprise_user_activity"
    try:
        tbl = client.get_table(table_id)
    except Exception:
        # Table does not exist - it will be created with a fresh schema
        return False

    is_query_string = False
    for f in tbl.schema:
        if f.name == "jsonPayload" and f.fields:
            for sub in f.fields:
                if sub.name == "request" and sub.fields:
                    for rsub in sub.fields:
                        if rsub.name == "query" and rsub.field_type == "STRING":
                            is_query_string = True
                            break

    if not is_query_string and not force:
        return False

    print(f"[!] Detected a BigQuery schema mismatch for table: {table_id}")
    print("    Field 'jsonPayload.request.query' has type STRING instead of RECORD (or repair was forced).")
    print("    This blocks StreamAssist ingestion with the error: 'This field: query is not a record.'")
    print("    Starting the Self-Healing procedure: migrating the old table and recreating the correct schema...")

    bad_table_name = "discoveryengine_googleapis_com_gemini_enterprise_user_activity_bad_schema"
    try:
        existing_tables = [t.table_id for t in client.list_tables(f"{project_id}.{dataset_id}")]
        if bad_table_name in existing_tables:
            import time
            bad_table_name = f"discoveryengine_googleapis_com_gemini_enterprise_user_activity_bad_schema_{int(time.time())}"

        # 1. Attempt a fast RENAME
        rename_sql = f"ALTER TABLE `{table_id}` RENAME TO `{bad_table_name}`"
        try:
            client.query(rename_sql).result()
            print(f"    ✔ Backed up the old table as '{bad_table_name}'.")
        except Exception as rename_err:
            print(f"    [*] ALTER TABLE RENAME blocked by the streaming buffer ({rename_err}).")
            print("    [*] Preserving data via a CTAS snapshot and recreating the table...")
            copy_sql = f"CREATE OR REPLACE TABLE `{project_id}.{dataset_id}.{bad_table_name}` AS SELECT * FROM `{table_id}`"
            client.query(copy_sql).result()
            client.delete_table(table_id, not_found_ok=True)
            print(f"    ✔ Saved a snapshot of the old table as '{bad_table_name}' and deleted the broken table.")
    except Exception as e:
        print(f"    [!] Error while migrating the broken table: {e}")
        return False

    create_partitioned_table(client, table_id, user_act_schema)
    print("    ✔ Created a new 'discoveryengine_googleapis_com_gemini_enterprise_user_activity' table with query of type RECORD.")
    return True

def init_streaming_tables(client, project_id, dataset_id, force_repair=False):
    """Initializes the empty streaming and backfill tables for the Cloud Logging sink if they do not exist yet.
    Automatically verifies and repairs schema mismatches (e.g. query field of type STRING -> RECORD).
    """
    # 1. User activity table (Logging stream)
    user_act_schema = [
        bigquery.SchemaField("logName", "STRING"),
        bigquery.SchemaField("timestamp", "TIMESTAMP"),
        bigquery.SchemaField("receiveTimestamp", "TIMESTAMP"),
        bigquery.SchemaField("severity", "STRING"),
        bigquery.SchemaField("insertId", "STRING"),
        bigquery.SchemaField("trace", "STRING"),
        bigquery.SchemaField("spanId", "STRING"),
        bigquery.SchemaField("useriamprincipal", "STRING"),
        bigquery.SchemaField("jsonPayload", "RECORD", fields=[
            bigquery.SchemaField("useriamprincipal", "STRING"),
            bigquery.SchemaField("servicetextreply", "STRING"),
            bigquery.SchemaField("logmetadata", "RECORD", fields=[
                bigquery.SchemaField("timestamp", "STRING"),
                bigquery.SchemaField("methodname", "STRING"),
                bigquery.SchemaField("servicename", "STRING"),
                bigquery.SchemaField("name", "STRING"),
                bigquery.SchemaField("servicelabel", "STRING"),
            ]),
            bigquery.SchemaField("response", "RECORD", fields=[
                bigquery.SchemaField("name", "STRING"),
                bigquery.SchemaField("displayname", "STRING"),
                bigquery.SchemaField("description", "STRING"),
                bigquery.SchemaField("answer", "RECORD", fields=[
                    bigquery.SchemaField("name", "STRING"),
                    bigquery.SchemaField("state", "STRING"),
                ]),
                bigquery.SchemaField("agentinfo", "RECORD", fields=[
                    bigquery.SchemaField("spiffeid", "STRING"),
                    bigquery.SchemaField("displayname", "STRING"),
                    bigquery.SchemaField("agentkind", "STRING"),
                    bigquery.SchemaField("agent", "STRING"),
                ]),
            ]),
            bigquery.SchemaField("request", "RECORD", fields=[
                bigquery.SchemaField("parent", "STRING"),
                bigquery.SchemaField("name", "STRING"),
                bigquery.SchemaField("userevent", "RECORD", fields=[
                    bigquery.SchemaField("engine", "STRING"),
                    bigquery.SchemaField("eventtime", "STRING"),
                    bigquery.SchemaField("userpseudoid", "STRING"),
                    bigquery.SchemaField("eventtype", "STRING"),
                    bigquery.SchemaField("agentspaceinfo", "RECORD", fields=[
                        bigquery.SchemaField("agentspacepagetype", "STRING"),
                    ]),
                ]),
                bigquery.SchemaField("agentsspec", "RECORD", fields=[
                    bigquery.SchemaField("agentspecs", "RECORD", mode="REPEATED", fields=[
                        bigquery.SchemaField("agentid", "STRING"),
                    ]),
                ]),
                bigquery.SchemaField("query", "RECORD", mode="NULLABLE", fields=[
                    bigquery.SchemaField("parts", "RECORD", mode="REPEATED", fields=[
                        bigquery.SchemaField("text", "STRING", mode="NULLABLE"),
                    ]),
                ]),
                bigquery.SchemaField("agent", "RECORD", fields=[
                    bigquery.SchemaField("name", "STRING"),
                ]),
                bigquery.SchemaField("policy", "RECORD", fields=[
                    bigquery.SchemaField("bindings", "RECORD", mode="REPEATED", fields=[
                        bigquery.SchemaField("role", "STRING"),
                        bigquery.SchemaField("members", "STRING", mode="REPEATED"),
                    ]),
                ]),
            ]),
            bigquery.SchemaField("status", "RECORD", fields=[
                bigquery.SchemaField("code", "INTEGER"),
                bigquery.SchemaField("message", "STRING"),
            ]),
        ]),
    ]
    repair_user_activity_schema_if_needed(client, project_id, dataset_id, user_act_schema, force=force_repair)
    create_partitioned_table(client, f"{project_id}.{dataset_id}.discoveryengine_googleapis_com_gemini_enterprise_user_activity", user_act_schema)

    # 2. GenAI inference operations table (Logging stream)
    inference_schema = [
        bigquery.SchemaField("logName", "STRING"),
        bigquery.SchemaField("timestamp", "TIMESTAMP"),
        bigquery.SchemaField("receiveTimestamp", "TIMESTAMP"),
        bigquery.SchemaField("severity", "STRING"),
        bigquery.SchemaField("insertId", "STRING"),
        bigquery.SchemaField("trace", "STRING"),
        bigquery.SchemaField("spanId", "STRING"),
        bigquery.SchemaField("jsonPayload", "RECORD", fields=[
            bigquery.SchemaField("gen_ai_usage_input_tokens", "FLOAT"),
            bigquery.SchemaField("gen_ai_usage_output_tokens", "FLOAT"),
            bigquery.SchemaField("gen_ai_usage_reasoning_output_tokens", "FLOAT"),
            bigquery.SchemaField("gen_ai_agent_name", "STRING"),
            bigquery.SchemaField("gen_ai_conversation_id", "STRING"),
            bigquery.SchemaField("gcp_vertex_agent_invocation_id", "STRING"),
            bigquery.SchemaField("gcp_vertex_agent_event_id", "STRING"),
            bigquery.SchemaField("gen_ai_response_finish_reasons", "STRING", mode="REPEATED"),
        ]),
    ]
    create_partitioned_table(client, f"{project_id}.{dataset_id}.discoveryengine_googleapis_com_gen_ai_client_inference_operation_details", inference_schema)

    # 3. Cloud Audit Activity events table
    audit_schema = [
        bigquery.SchemaField("insert_id", "STRING"),
        bigquery.SchemaField("insertId", "STRING"),
        bigquery.SchemaField("timestamp", "TIMESTAMP"),
        bigquery.SchemaField("principal_email", "STRING"),
        bigquery.SchemaField("method_name", "STRING"),
        bigquery.SchemaField("resource_name", "STRING"),
        bigquery.SchemaField("raw_payload", "STRING"),
        bigquery.SchemaField("protopayload_auditlog", "RECORD", fields=[
            bigquery.SchemaField("methodName", "STRING"),
            bigquery.SchemaField("resourceName", "STRING"),
            bigquery.SchemaField("status", "RECORD", fields=[
                bigquery.SchemaField("code", "INTEGER"),
                bigquery.SchemaField("message", "STRING"),
            ]),
            bigquery.SchemaField("authenticationInfo", "RECORD", fields=[
                bigquery.SchemaField("principalEmail", "STRING"),
            ]),
        ]),
    ]
    create_partitioned_table(client, f"{project_id}.{dataset_id}.cloudaudit_googleapis_com_activity", audit_schema)

    # 4. Backfill user activity table
    backfill_user_schema = [
        bigquery.SchemaField("insert_id", "STRING"),
        bigquery.SchemaField("insertId", "STRING"),
        bigquery.SchemaField("timestamp", "TIMESTAMP"),
        bigquery.SchemaField("user_iam_principal", "STRING"),
        bigquery.SchemaField("user_pseudo_id", "STRING"),
        bigquery.SchemaField("method_name", "STRING"),
        bigquery.SchemaField("engine", "STRING"),
        bigquery.SchemaField("page_type", "STRING"),
        bigquery.SchemaField("event_type", "STRING"),
        bigquery.SchemaField("agent_id", "STRING"),
        bigquery.SchemaField("raw_payload", "STRING"),
    ]
    create_partitioned_table(client, f"{project_id}.{dataset_id}.gemini_enterprise_user_activity", backfill_user_schema)

    # 5. Backfill GenAI model inference table
    backfill_inf_schema = [
        bigquery.SchemaField("insert_id", "STRING"),
        bigquery.SchemaField("insertId", "STRING"),
        bigquery.SchemaField("timestamp", "TIMESTAMP"),
        bigquery.SchemaField("user_id", "STRING"),
        bigquery.SchemaField("conversation_id", "STRING"),
        bigquery.SchemaField("agent_name", "STRING"),
        bigquery.SchemaField("engine_id", "STRING"),
        bigquery.SchemaField("assistant_id", "STRING"),
        bigquery.SchemaField("input_tokens", "INT64"),
        bigquery.SchemaField("output_tokens", "INT64"),
        bigquery.SchemaField("cached_tokens", "INT64"),
        bigquery.SchemaField("finish_reason", "STRING"),
        bigquery.SchemaField("raw_payload", "STRING"),
    ]
    create_partitioned_table(client, f"{project_id}.{dataset_id}.gen_ai_client_inference_operation_details", backfill_inf_schema)

def run_backfill(client, project_id, dataset_id, days=30):
    """Main log backfill procedure."""
    print(f"=== Gemini Enterprise log backfill for {project_id} (Last {days} days) ===")
    init_streaming_tables(client, project_id, dataset_id)

    # Ingestion task configuration
    tasks = [
        {
            "name": "Cloud Audit Activity",
            "filter": f'logName=~"cloudaudit.googleapis.com" AND protoPayload.serviceName="discoveryengine.googleapis.com"',
            "table": f"{project_id}.{dataset_id}.cloudaudit_googleapis_com_activity",
            "schema": [
                bigquery.SchemaField("insert_id", "STRING"),
                bigquery.SchemaField("insertId", "STRING"),
                bigquery.SchemaField("timestamp", "TIMESTAMP"),
                bigquery.SchemaField("principal_email", "STRING"),
                bigquery.SchemaField("method_name", "STRING"),
                bigquery.SchemaField("resource_name", "STRING"),
                bigquery.SchemaField("raw_payload", "STRING"),
                bigquery.SchemaField("protopayload_auditlog", "RECORD", fields=[
                    bigquery.SchemaField("methodName", "STRING"),
                    bigquery.SchemaField("resourceName", "STRING"),
                    bigquery.SchemaField("status", "RECORD", fields=[
                        bigquery.SchemaField("code", "INTEGER"),
                        bigquery.SchemaField("message", "STRING"),
                    ]),
                    bigquery.SchemaField("authenticationInfo", "RECORD", fields=[
                        bigquery.SchemaField("principalEmail", "STRING"),
                    ]),
                ]),
            ],
            "transform": parse_audit_entry
        },
        {
            "name": "User Activity",
            "filter": f'logName="projects/{project_id}/logs/discoveryengine.googleapis.com%2Fgemini_enterprise_user_activity"',
            "table": f"{project_id}.{dataset_id}.gemini_enterprise_user_activity",
            "schema": [
                bigquery.SchemaField("insert_id", "STRING"),
                bigquery.SchemaField("insertId", "STRING"),
                bigquery.SchemaField("timestamp", "TIMESTAMP"),
                bigquery.SchemaField("user_iam_principal", "STRING"),
                bigquery.SchemaField("user_pseudo_id", "STRING"),
                bigquery.SchemaField("method_name", "STRING"),
                bigquery.SchemaField("engine", "STRING"),
                bigquery.SchemaField("page_type", "STRING"),
                bigquery.SchemaField("event_type", "STRING"),
                bigquery.SchemaField("agent_id", "STRING"),
                bigquery.SchemaField("raw_payload", "STRING"),
            ],
            "transform": parse_activity_entry
        },
        {
            "name": "GenAI Model Inference (Tokens)",
            "filter": f'logName="projects/{project_id}/logs/discoveryengine.googleapis.com%2Fgen_ai.client.inference.operation.details"',
            "table": f"{project_id}.{dataset_id}.gen_ai_client_inference_operation_details",
            "schema": [
                bigquery.SchemaField("insert_id", "STRING"),
                bigquery.SchemaField("insertId", "STRING"),
                bigquery.SchemaField("timestamp", "TIMESTAMP"),
                bigquery.SchemaField("user_id", "STRING"),
                bigquery.SchemaField("conversation_id", "STRING"),
                bigquery.SchemaField("agent_name", "STRING"),
                bigquery.SchemaField("engine_id", "STRING"),
                bigquery.SchemaField("assistant_id", "STRING"),
                bigquery.SchemaField("input_tokens", "INT64"),
                bigquery.SchemaField("output_tokens", "INT64"),
                bigquery.SchemaField("cached_tokens", "INT64"),
                bigquery.SchemaField("finish_reason", "STRING"),
                bigquery.SchemaField("raw_payload", "STRING"),
            ],
            "transform": parse_inference_entry
        }
    ]

    for t in tasks:
        print(f"--> Processing: {t['name']}...")
        create_partitioned_table(client, t["table"], t["schema"])
        entries = fetch_logs(project_id, t["filter"], days=days)
        print(f"    Fetched {len(entries)} entries.")
        if entries:
            existing_ids = set()
            try:
                col = "insert_id" if any(f.name == "insert_id" for f in t["schema"]) else "insertId"
                chk_query = f"SELECT DISTINCT {col} FROM `{t['table']}` WHERE {col} IS NOT NULL"
                job = client.query(chk_query)
                for row in job.result():
                    if row[0]:
                        existing_ids.add(row[0])
            except Exception:
                pass

            new_entries = [e for e in entries if e.get("insertId") not in existing_ids]
            if len(new_entries) < len(entries):
                print(f"    Skipped {len(entries) - len(new_entries)} already ingested records (idempotency).")
            
            if new_entries:
                rows = [t["transform"](e) for e in new_entries]
                errors = client.insert_rows_json(t["table"], rows)
                if errors:
                    print(f"    ⚠️ Errors writing to {t['table']}: {errors}")
                else:
                    print(f"    ✔ Wrote {len(rows)} new records to {t['table']}.")
            else:
                print(f"    ✔ All records in {t['table']} are already up to date (no new entries).")

    print("✔ Log backfill completed successfully!")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Backfill Gemini Enterprise logs into BigQuery")
    parser.add_argument("pos_project", nargs="?", default=None, help="Google Cloud Project ID (positional)")
    parser.add_argument("pos_dataset", nargs="?", default=None, help="Dataset ID (positional)")
    parser.add_argument("pos_days", nargs="?", type=int, default=None, help="Number of days (positional)")
    parser.add_argument("--project", default=None, help="Google Cloud Project ID")
    parser.add_argument("--dataset", default=None, help="Dataset ID")
    parser.add_argument("--days", type=int, default=None, help="Number of days")
    args = parser.parse_args()

    p_id = args.project or args.pos_project or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not p_id:
        try:
            import subprocess
            p_id = subprocess.check_output(["gcloud", "config", "get-value", "project"], text=True).strip()
        except Exception:
            pass
    if not p_id:
        print("Error: No GCP project ID provided. Use: python3 backfill_logs_to_bigquery.py --project <PROJECT_ID>")
        sys.exit(1)

    d_id = args.dataset or args.pos_dataset or os.environ.get("DATASET_ID", "gemini_enterprise_telemetry")
    d_days = args.days if args.days is not None else (args.pos_days if args.pos_days is not None else 30)

    bq_client = bigquery.Client(project=p_id)
    run_backfill(bq_client, p_id, d_id, d_days)
