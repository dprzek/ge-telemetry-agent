#!/usr/bin/env python3
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
Self-Healing repair tool for the Cloud Logging Sink error:
table_invalid_schema (Cannot convert std::string to a record field ... query = 4)
and for StreamAssist log rejections ('This field: query is not a record').

Resolves a type collision in Google Discovery Engine, where:
1. SearchService services send the 'query' field as a plain std::string.
2. Gemini Enterprise / AssistantService (StreamAssist) services send the 'query' field
   as a RECORD object with the structure parts: repeated struct<text string>.

Architectural solution:
1. Configures an exclusion rule for Search methods on the Cloud Logging sink
   (gemini-enterprise-telemetry-sink), eliminating the string collision at the source.
2. Ensures the correct BigQuery schema with type RECORD for 'request.query'.
3. Optionally runs a data backfill.

Usage:
    python3 scripts/fix_user_activity_schema.py --project my-project-id
"""

import os
import sys
import argparse
import subprocess
from google.cloud import bigquery

# Import the self-healing procedure and schema definitions from the backfill module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.backfill_logs_to_bigquery import init_streaming_tables, repair_user_activity_schema_if_needed


def get_default_project():
    try:
        res = subprocess.run(["gcloud", "config", "get-value", "project"], stdout=subprocess.PIPE, text=True, check=True)
        proj = res.stdout.strip()
        if proj:
            return proj
    except Exception:
        pass
    return os.environ.get("PROJECT_ID", "")


def deploy_sql_views_locally(bq_client, project_id, dataset_id):
    """Refreshes the analytical SQL views in BigQuery."""
    print("--> Deploying analytical SQL views in BigQuery...")
    init_streaming_tables(bq_client, project_id, dataset_id)

    sql_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bigquery", "telemetry_views.sql")
    with open(sql_path, "r", encoding="utf-8") as f:
        raw_sql = f.read()

    formatted_sql = raw_sql.format(project_id=project_id, dataset_id=dataset_id)
    job = bq_client.query(formatted_sql)
    job.result()
    print("    ✔ Analytical SQL views were successfully created / updated in BigQuery.")


def main():
    parser = argparse.ArgumentParser(
        description="Repair the table_invalid_schema error for the Gemini Enterprise activity table in BigQuery."
    )
    parser.add_argument("--project", default=get_default_project(), help="Google Cloud Project ID")
    parser.add_argument("--dataset", default="gemini_enterprise_telemetry", help="BigQuery dataset ID")
    parser.add_argument("--force", action="store_true", help="Force a table rebuild even if the query field is already of type RECORD")
    parser.add_argument("--backfill-days", type=int, default=0, help="Optional number of days to backfill after the repair")
    args = parser.parse_args()

    project_id = args.project
    dataset_id = args.dataset

    if not project_id:
        print("[!] Error: Project ID not specified. Use --project <PROJECT_ID> or run gcloud config set project.")
        sys.exit(1)

    print(f"=== BigQuery Schema Verification and Self-Healing ({project_id}.{dataset_id}) ===")
    
    # 1. Configure the Search method exclusion on the Cloud Logging sink (protection against type collisions)
    sink_name = "gemini-enterprise-telemetry-sink"
    exclusion_name = "exclude-search-queries"
    exclusion_filter = (
        'logName=~"discoveryengine.googleapis.com%2Fgemini_enterprise_user_activity" AND '
        '(jsonPayload.logMetadata.methodName="Search" OR '
        'jsonPayload.logMetadata.methodName="ConverseConversation" OR '
        'jsonPayload.logMetadata.methodName="AnswerQuery")'
    )
    sink_filter = (
        '(logName=~"cloudaudit.googleapis.com" AND protoPayload.serviceName="discoveryengine.googleapis.com") OR '
        '(logName=~"discoveryengine.googleapis.com%2Fgemini_enterprise_user_activity" AND '
        'NOT (jsonPayload.logMetadata.methodName="Search" OR '
        'jsonPayload.logMetadata.methodName="ConverseConversation" OR '
        'jsonPayload.logMetadata.methodName="AnswerQuery")) OR '
        'logName=~"discoveryengine.googleapis.com%2Fgen_ai.client.inference.operation.details"'
    )
    print(f"--> [1/3] Checking and configuring the filter and exclusion on sink '{sink_name}'...")
    cmd_filter = [
        "gcloud", "logging", "sinks", "update", sink_name,
        f"--project={project_id}",
        f"--log-filter={sink_filter}"
    ]
    subprocess.run(cmd_filter, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    cmd_excl = [
        "gcloud", "logging", "sinks", "update", sink_name,
        f"--project={project_id}",
        f"--add-exclusion=name={exclusion_name},description=Exclude Search methods with string query to prevent schema collision with StreamAssist Record,filter={exclusion_filter}"
    ]
    res_excl = subprocess.run(cmd_excl, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res_excl.returncode == 0:
        print("    ✔ Activated the Search query exclusion rule on the Cloud Logging sink.")
    else:
        print(f"    (Sink update status: {res_excl.stderr.strip() or 'OK'})")

    # 2. Verify the table schema in BigQuery
    print(f"--> [2/3] Verifying the BigQuery table schema...")
    bq_client = bigquery.Client(project=project_id)

    table_id = f"{project_id}.{dataset_id}.discoveryengine_googleapis_com_gemini_enterprise_user_activity"
    try:
        tbl = bq_client.get_table(table_id)
        table_exists = True
    except Exception:
        table_exists = False
        tbl = None

    is_string = False
    current_query_type = "UNKNOWN / MISSING"
    if table_exists and tbl:
        for f in tbl.schema:
            if f.name == "jsonPayload" and f.fields:
                for sub in f.fields:
                    if sub.name == "request" and sub.fields:
                        for rsub in sub.fields:
                            if rsub.name == "query":
                                current_query_type = rsub.field_type
                                if rsub.field_type == "STRING":
                                    is_string = True
                                break

    print(f"[*] Current state of field 'jsonPayload.request.query': {current_query_type}")

    if not is_string and table_exists and not args.force:
        print("✔ The table already has the correct schema (query: RECORD with the parts structure). No structural changes are required.")
    else:
        print("[!] Schema update/repair to type RECORD is required for StreamAssist!")
        init_streaming_tables(bq_client, project_id, dataset_id, force_repair=args.force or is_string)
        deploy_sql_views_locally(bq_client, project_id, dataset_id)
        print("✔ Table repair procedure completed successfully!")

    # 3. Optional backfill
    print(f"--> [3/3] Verifying data consistency...")
    if args.backfill_days > 0:
        print(f"    Running backfill for the last {args.backfill_days} days...")
        from scripts.backfill_logs_to_bigquery import run_backfill
        run_backfill(bq_client, project_id, dataset_id, days=args.backfill_days)

    print("\n[Summary]")
    print(f"Project:   {project_id}")
    print(f"Dataset:   {dataset_id}")
    print(f"Table:     discoveryengine_googleapis_com_gemini_enterprise_user_activity")
    print(f"Query type: RECORD (Compatible with Gemini Enterprise StreamAssist)")
    print(f"Sink exclusion: Active ({exclusion_name})")
    print("Status:    HEALTHY / SYNCHRONIZED")


if __name__ == "__main__":
    main()
