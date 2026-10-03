-- NOTE: The image-generation keyword regexes in this file intentionally match both English and Polish
-- prompts (e.g. "generate image" / "wygeneruj obraz"). They are functional and must be kept as-is.
-- 1. Unified daily user activity view (day-by-day breakdown)
CREATE OR REPLACE VIEW `{project_id}.{dataset_id}.v_user_daily_utilization` AS
WITH agent_creators AS (
  SELECT
    agent_id,
    author_user_id,
    COALESCE(MAX(agent_display_name), "") AS agent_display_name,
    MIN(created_at) AS created_at
  FROM (
    SELECT
      REGEXP_EXTRACT(COALESCE(
        JSON_VALUE(TO_JSON_STRING(protopayload_auditlog), "$.response.name"),
        JSON_VALUE(TO_JSON_STRING(protopayload_auditlog), "$.responseJson.name"),
        TO_JSON_STRING(protopayload_auditlog)
      ), r"/agents/([0-9a-zA-Z_\-]+)") AS agent_id,
      COALESCE(
        JSON_VALUE(TO_JSON_STRING(protopayload_auditlog), "$.request.agent.displayName"),
        JSON_VALUE(TO_JSON_STRING(protopayload_auditlog), "$.response.displayName"),
        ""
      ) AS agent_display_name,
      COALESCE(
        NULLIF(principal_email, ""),
        NULLIF(JSON_VALUE(TO_JSON_STRING(protopayload_auditlog), "$.authenticationInfo.principalEmail"), ""),
        "unknown"
      ) AS author_user_id,
      timestamp AS created_at
    FROM `{project_id}.{dataset_id}.cloudaudit_googleapis_com_activity`
    WHERE (
      method_name LIKE "%CreateAgent%"
      OR JSON_VALUE(TO_JSON_STRING(protopayload_auditlog), "$.methodName") LIKE "%CreateAgent%"
    )
    AND COALESCE(SAFE_CAST(JSON_VALUE(TO_JSON_STRING(protopayload_auditlog), "$.status.code") AS INT64), 0) = 0
    
    UNION ALL
    
    SELECT
      COALESCE(
        NULLIF(agent_id, ""),
        REGEXP_EXTRACT(raw_payload, r"/agents/([0-9a-zA-Z_\-]+)")
      ) AS agent_id,
      COALESCE(
        REGEXP_EXTRACT(raw_payload, r'"displayName":\s*"([^"]+)"'),
        ""
      ) AS agent_display_name,
      COALESCE(NULLIF(NULLIF(TRIM(user_iam_principal), "<elided>"), ""), "unknown") AS author_user_id,
      timestamp AS created_at
    FROM `{project_id}.{dataset_id}.gemini_enterprise_user_activity`
    WHERE method_name = "CreateAgent"

    UNION ALL

    SELECT
      COALESCE(
        REGEXP_EXTRACT(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.name"), ""), r"/agents/([0-9a-zA-Z_\-]+)"),
        REGEXP_EXTRACT(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.agent.name"), ""), r"/agents/([0-9a-zA-Z_\-]+)"),
        REGEXP_EXTRACT(TO_JSON_STRING(jsonPayload), r"/agents/([0-9a-zA-Z_\-]+)")
      ) AS agent_id,
      COALESCE(
        JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.agent.displayName"),
        JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.displayName"),
        JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.userEvent.agentspaceInfo.agentInfo.name"),
        ""
      ) AS agent_display_name,
      COALESCE(
        NULLIF(NULLIF(TRIM(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.useriamprincipal")), "<elided>"), ""),
        NULLIF(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.userevent.userpseudoid"), ""),
        "unknown"
      ) AS author_user_id,
      timestamp AS created_at
    FROM `{project_id}.{dataset_id}.discoveryengine_googleapis_com_gemini_enterprise_user_activity`
    WHERE COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.logmetadata.methodname"), "") = "CreateAgent"
      AND (SAFE_CAST(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.status.code") AS INT64) IS NULL OR SAFE_CAST(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.status.code") AS INT64) = 0)
  )
  WHERE agent_id IS NOT NULL 
    AND agent_id != "" 
    AND agent_id != "deep_research"
    AND author_user_id NOT IN ("unknown", "<elided>")
    AND NOT author_user_id LIKE "%@gcp-sa-%.iam.gserviceaccount.com"
  GROUP BY 1, 2
),
base_user_events AS (
  SELECT * FROM (
    -- User activity stream from the Cloud Logging sink (real-time)
    SELECT
      DATE(timestamp) AS activity_date,
      timestamp,
      COALESCE(
        NULLIF(NULLIF(TRIM(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.useriamprincipal")), "<elided>"), ""),
        NULLIF(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.userevent.userpseudoid"), "")
      ) AS direct_user_id,
      COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.logmetadata.methodname"), "") AS method_name,
      COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.userevent.agentspaceinfo.agentspacepagetype"), "") AS page_type,
      COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.userevent.eventtype"), "") AS event_type,
      COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.userevent.engine"), "") AS engine,
      COALESCE(
        NULLIF(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.agentsspec.agentspecs[0].agentid"), ""),
        NULLIF(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.agentsspec.agentspecs.agentid"), ""),
        NULLIF(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.userevent.agentspaceinfo.agentinfo.agentid"), ""),
        REGEXP_EXTRACT(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.agentinfo.agent"), ""), r"/agents/([^/]+)"),
        REGEXP_EXTRACT(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.userevent.agentspaceinfo.agent"), ""), r"/agents/([^/]+)"),
        ""
      ) AS called_agent_id,
      COALESCE(
        JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.agentinfo.displayname"),
        JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.userevent.agentspaceinfo.agentinfo.name"),
        ""
      ) AS agent_display_name,
      COALESCE(
        REGEXP_EXTRACT(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.answer.name"), ""), r"/sessions/([^/]+)"),
        REGEXP_EXTRACT(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.parent"), ""), r"/sessions/([^/]+)"),
        REGEXP_EXTRACT(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.userevent.session"), ""), r"/sessions/([^/]+)"),
        REGEXP_EXTRACT(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.session"), ""), r"/sessions/([^/]+)"),
        ""
      ) AS session_id,
      trace,
      CASE 
        WHEN COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.logmetadata.methodname"), "") IN ("StreamAssist", "Assist")
         AND (
           COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.agentinfo.agent"), "") LIKE "%/agents/deep_research"
           OR TO_JSON_STRING(jsonPayload) LIKE "%deep_research%"
         )
         AND (SAFE_CAST(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.status.code") AS INT64) IS NULL OR SAFE_CAST(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.status.code") AS INT64) = 0)
         AND JSON_QUERY(TO_JSON_STRING(jsonPayload), "$.response") IS NOT NULL THEN 1 
        ELSE 0 
      END AS is_deep_research,
      CASE
        WHEN COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.logmetadata.methodname"), "") IN ("StreamAssist", "Assist")
         AND (
           LOWER(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.modelinfo.model"), "")) LIKE "%imagen%"
           OR REGEXP_CONTAINS(LOWER(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.modelinfo.model"), "")), r"(image|imagen)")
           OR LOWER(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.query.parts[0].text"), "")) LIKE "%generate%image%"
           OR COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.userevent.agentspaceinfo.agentspacepagetype"), "") = "image-generation"
           OR REGEXP_CONTAINS(LOWER(TO_JSON_STRING(jsonPayload)), r"(wygeneruj|stwórz|utwórz|zrób|generuj|generate|create|draw|narysuj|namaluj|paint)\s+(obraz|obrazek|grafik|zdjęci|image|picture|photo|illustration)")
           OR REGEXP_CONTAINS(LOWER(TO_JSON_STRING(jsonPayload)), r'"(obrazek|obraz|image|zdjęcie)\s+')
           OR LOWER(TO_JSON_STRING(jsonPayload)) LIKE "%image-generation%"
         )
         AND (SAFE_CAST(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.status.code") AS INT64) IS NULL OR SAFE_CAST(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.status.code") AS INT64) = 0) THEN 1
        ELSE 0
      END AS is_image_generation,
      CASE 
        WHEN COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.logmetadata.methodname"), "") IN ("StreamAssist", "Assist") 
         AND NOT (
           COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.agentinfo.agent"), "") LIKE "%/agents/deep_research"
           OR TO_JSON_STRING(jsonPayload) LIKE "%deep_research%"
         )
         AND NOT (
           LOWER(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.agentinfo.displayname"), "")) LIKE "%telemetry%"
           OR LOWER(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.agentinfo.displayname"), "")) LIKE "%adoption%"
         )
         -- Image generation events are counted in images_generated only (keeps total_events >= sum of actions)
         AND NOT (
           LOWER(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.modelinfo.model"), "")) LIKE "%imagen%"
           OR REGEXP_CONTAINS(LOWER(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.modelinfo.model"), "")), r"(image|imagen)")
           OR LOWER(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.query.parts[0].text"), "")) LIKE "%generate%image%"
           OR COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.userevent.agentspaceinfo.agentspacepagetype"), "") = "image-generation"
           OR REGEXP_CONTAINS(LOWER(TO_JSON_STRING(jsonPayload)), r"(wygeneruj|stwórz|utwórz|zrób|generuj|generate|create|draw|narysuj|namaluj|paint)\s+(obraz|obrazek|grafik|zdjęci|image|picture|photo|illustration)")
           OR REGEXP_CONTAINS(LOWER(TO_JSON_STRING(jsonPayload)), r'"(obrazek|obraz|image|zdjęcie)\s+')
           OR LOWER(TO_JSON_STRING(jsonPayload)) LIKE "%image-generation%"
         )
         AND (SAFE_CAST(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.status.code") AS INT64) IS NULL OR SAFE_CAST(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.status.code") AS INT64) = 0)
         AND JSON_QUERY(TO_JSON_STRING(jsonPayload), "$.response") IS NOT NULL THEN 1 
        ELSE 0 
      END AS is_assistant_query,
      CASE
        WHEN COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.logmetadata.methodname"), "") = "CreateAgent"
         AND NOT (
           COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.name"), "") LIKE "%/agents/deep_research"
           OR COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.agent.name"), "") LIKE "%/agents/deep_research"
           OR TO_JSON_STRING(jsonPayload) LIKE "%deep_research%"
         )
         AND (SAFE_CAST(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.status.code") AS INT64) IS NULL OR SAFE_CAST(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.status.code") AS INT64) = 0) THEN 1
        ELSE 0
      END AS is_custom_agent_created,
      CASE
        WHEN SAFE_CAST(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.status.code") AS INT64) IS NOT NULL 
         AND SAFE_CAST(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.status.code") AS INT64) != 0 THEN 1
        ELSE 0
      END AS is_error_event,
      insertId,
      0 AS priority
    FROM `{project_id}.{dataset_id}.discoveryengine_googleapis_com_gemini_enterprise_user_activity`

    UNION ALL

    -- Backfilled historical logs
    SELECT
      DATE(timestamp) AS activity_date,
      timestamp,
      COALESCE(
        NULLIF(NULLIF(TRIM(user_iam_principal), "<elided>"), ""),
        NULLIF(user_pseudo_id, "")
      ) AS direct_user_id,
      method_name,
      page_type,
      event_type,
      engine,
      COALESCE(
        NULLIF(agent_id, ""),
        REGEXP_EXTRACT(raw_payload, r"/agents/([0-9a-zA-Z_\-]+)"),
        ""
      ) AS called_agent_id,
      COALESCE(
        REGEXP_EXTRACT(raw_payload, r'"displayName":\s*"([^"]+)"'),
        ""
      ) AS agent_display_name,
      REGEXP_EXTRACT(COALESCE(raw_payload, ""), r"sessions/([^/\"'\s]+)") AS session_id,
      REGEXP_EXTRACT(raw_payload, r"traces/([a-zA-Z0-9]+)") AS trace,
      CASE 
        WHEN method_name IN ("StreamAssist", "Assist") 
         AND (agent_id = "deep_research" OR raw_payload LIKE "%agents/deep_research%") THEN 1 
        ELSE 0 
      END AS is_deep_research,
      CASE
        WHEN method_name IN ("StreamAssist", "Assist")
         AND (
           REGEXP_CONTAINS(LOWER(raw_payload), r'"model"\s*:\s*"[^"]*imagen[^"]*"')
           OR REGEXP_CONTAINS(LOWER(raw_payload), r'"model"\s*:\s*"[^"]*(image|imagen)[^"]*"')
           OR LOWER(COALESCE(REGEXP_EXTRACT(raw_payload, r'"text"\s*:\s*"([^"]*)"'), "")) LIKE "%generate%image%"
           OR page_type = "image-generation" 
           OR REGEXP_CONTAINS(LOWER(raw_payload), r"(wygeneruj|stwórz|utwórz|zrób|generuj|generate|create|draw|narysuj|namaluj|paint)\s+(obraz|obrazek|grafik|zdjęci|image|picture|photo|illustration)")
           OR REGEXP_CONTAINS(LOWER(raw_payload), r'"(obrazek|obraz|image|zdjęcie)\s+')
           OR LOWER(raw_payload) LIKE "%image-generation%"
         ) THEN 1
        ELSE 0
      END AS is_image_generation,
      CASE 
        WHEN method_name IN ("StreamAssist", "Assist") 
         AND NOT (agent_id = "deep_research" OR raw_payload LIKE "%agents/deep_research%")
         AND NOT (
           LOWER(COALESCE(REGEXP_EXTRACT(raw_payload, r'"displayName":\s*"([^"]+)"'), "")) LIKE "%telemetry%"
           OR LOWER(COALESCE(REGEXP_EXTRACT(raw_payload, r'"displayName":\s*"([^"]+)"'), "")) LIKE "%adoption%"
         )
         AND NOT (
           REGEXP_CONTAINS(LOWER(raw_payload), r'"model"\s*:\s*"[^"]*imagen[^"]*"')
           OR REGEXP_CONTAINS(LOWER(raw_payload), r'"model"\s*:\s*"[^"]*(image|imagen)[^"]*"')
           OR LOWER(COALESCE(REGEXP_EXTRACT(raw_payload, r'"text"\s*:\s*"([^"]*)"'), "")) LIKE "%generate%image%"
           OR page_type = "image-generation" 
           OR REGEXP_CONTAINS(LOWER(raw_payload), r"(wygeneruj|stwórz|utwórz|zrób|generuj|generate|create|draw|narysuj|namaluj|paint)\s+(obraz|obrazek|grafik|zdjęci|image|picture|photo|illustration)")
           OR REGEXP_CONTAINS(LOWER(raw_payload), r'"(obrazek|obraz|image|zdjęcie)\s+')
           OR LOWER(raw_payload) LIKE "%image-generation%"
         ) THEN 1 
        ELSE 0 
      END AS is_assistant_query,
      CASE
        WHEN method_name = "CreateAgent"
         AND NOT (
           agent_id = "deep_research"
           OR raw_payload LIKE "%agents/deep_research%"
         ) THEN 1
        ELSE 0
      END AS is_custom_agent_created,
      0 AS is_error_event,
      insert_id AS insertId,
      1 AS priority
    FROM `{project_id}.{dataset_id}.gemini_enterprise_user_activity`
  )
),
raw_user_events AS (
  SELECT
    activity_date,
    timestamp,
    COALESCE(
      direct_user_id,
      FIRST_VALUE(direct_user_id IGNORE NULLS) OVER (
        PARTITION BY session_id 
        ORDER BY timestamp 
        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
      ),
      LAST_VALUE(CASE WHEN called_agent_id != "" THEN direct_user_id END IGNORE NULLS) OVER (
        PARTITION BY called_agent_id 
        ORDER BY timestamp 
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
      ),
      FIRST_VALUE(CASE WHEN called_agent_id != "" THEN direct_user_id END IGNORE NULLS) OVER (
        PARTITION BY called_agent_id 
        ORDER BY timestamp 
        ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING
      ),
      LAST_VALUE(direct_user_id IGNORE NULLS) OVER (
        ORDER BY UNIX_SECONDS(timestamp)
        RANGE BETWEEN 30 PRECEDING AND 30 FOLLOWING
      ),
      "unassigned"
    ) AS user_id,
    method_name,
    page_type,
    event_type,
    engine,
    called_agent_id,
    agent_display_name,
    session_id,
    trace,
    is_deep_research,
    is_image_generation,
    is_assistant_query,
    is_custom_agent_created,
    is_error_event,
    insertId,
    priority
  FROM base_user_events
  QUALIFY ROW_NUMBER() OVER(
    PARTITION BY COALESCE(NULLIF(insertId, ""), CONCAT(CAST(timestamp AS STRING), "_", method_name))
    ORDER BY priority ASC, timestamp ASC
  ) = 1
),
aggregated_user_events AS (
  SELECT
    activity_date,
    user_id,
    COUNT(*) AS total_events,
    SUM(is_assistant_query) AS assistant_queries,
    COALESCE(COUNT(DISTINCT CASE WHEN is_deep_research = 1 THEN NULLIF(session_id, "") END), 0) AS deep_research_count,
    SUM(is_image_generation) AS images_generated,
    SUM(is_custom_agent_created) AS agents_created,
    COUNTIF(method_name = "UpdateAgent") AS agent_updates,
    COUNTIF(method_name = "WriteUserEvent" AND page_type = "agent") AS agent_views,
    COUNTIF(method_name = "WriteUserEvent") AS ui_page_views,
    SUM(is_error_event) AS failed_requests,
    MIN(timestamp) AS first_event,
    MAX(timestamp) AS last_event
  FROM raw_user_events
  GROUP BY 1, 2
),
agent_usage_events AS (
  SELECT
    c.activity_date,
    a.author_user_id AS user_id,
    COUNTIF(c.user_id = a.author_user_id) AS author_agent_invocations,
    COUNT(DISTINCT CASE WHEN c.user_id = a.author_user_id AND NULLIF(c.session_id, "") IS NOT NULL THEN c.session_id END) AS author_agent_sessions,
    COUNT(*) AS org_agent_invocations,
    COUNT(DISTINCT NULLIF(c.session_id, "")) AS org_agent_sessions,
    COUNT(DISTINCT c.user_id) AS org_agent_unique_callers
  FROM raw_user_events c
  JOIN agent_creators a ON c.called_agent_id = a.agent_id
  WHERE c.method_name IN ("StreamAssist", "Assist")
    AND c.called_agent_id != ""
    AND c.called_agent_id != "deep_research"
    AND c.is_error_event = 0
    AND NOT (
      LOWER(COALESCE(c.agent_display_name, "")) LIKE "%telemetry%"
      OR LOWER(COALESCE(c.agent_display_name, "")) LIKE "%adoption%"
      OR LOWER(COALESCE(a.agent_display_name, "")) LIKE "%telemetry%"
      OR LOWER(COALESCE(a.agent_display_name, "")) LIKE "%adoption%"
    )
  GROUP BY 1, 2
),
raw_audit AS (
  -- Streamed & backfilled audit events
  SELECT
    DATE(timestamp) AS activity_date,
    timestamp,
    COALESCE(
      NULLIF(principal_email, ""),
      NULLIF(JSON_VALUE(TO_JSON_STRING(protopayload_auditlog), "$.authenticationInfo.principalEmail"), ""),
      "unknown"
    ) AS user_id,
    COALESCE(method_name, JSON_VALUE(TO_JSON_STRING(protopayload_auditlog), "$.methodName"), "") AS method_name,
    COALESCE(resource_name, JSON_VALUE(TO_JSON_STRING(protopayload_auditlog), "$.resourceName"), "") AS resource_name,
    CASE 
      WHEN COALESCE(method_name, JSON_VALUE(TO_JSON_STRING(protopayload_auditlog), "$.methodName"), "") LIKE "%CreateAgent%"
       AND NOT COALESCE(resource_name, JSON_VALUE(TO_JSON_STRING(protopayload_auditlog), "$.resourceName"), "") LIKE "%/agents/deep_research"
       AND COALESCE(SAFE_CAST(JSON_VALUE(TO_JSON_STRING(protopayload_auditlog), "$.status.code") AS INT64), 0) = 0 THEN 1 
      ELSE 0 
    END AS is_custom_agent_created,
    CASE
      WHEN COALESCE(method_name, JSON_VALUE(TO_JSON_STRING(protopayload_auditlog), "$.methodName"), "") LIKE "%UpdateAgent%"
       AND COALESCE(SAFE_CAST(JSON_VALUE(TO_JSON_STRING(protopayload_auditlog), "$.status.code") AS INT64), 0) = 0 THEN 1
      ELSE 0
    END AS is_agent_updated
  FROM `{project_id}.{dataset_id}.cloudaudit_googleapis_com_activity`
  WHERE NOT COALESCE(
    NULLIF(principal_email, ""),
    NULLIF(JSON_VALUE(TO_JSON_STRING(protopayload_auditlog), "$.authenticationInfo.principalEmail"), ""),
    ""
  ) LIKE "%gserviceaccount.com"
  QUALIFY ROW_NUMBER() OVER(
    PARTITION BY COALESCE(NULLIF(insert_id, ""), NULLIF(insertId, ""), CONCAT(CAST(timestamp AS STRING), "_", COALESCE(method_name, JSON_VALUE(TO_JSON_STRING(protopayload_auditlog), "$.methodName"), "")))
    ORDER BY timestamp
  ) = 1
),
aggregated_audit AS (
  SELECT
    activity_date,
    user_id,
    COUNT(*) AS audit_events,
    SUM(is_custom_agent_created) AS agents_created,
    SUM(is_agent_updated) AS agent_updates,
    MIN(timestamp) AS first_audit,
    MAX(timestamp) AS last_audit
  FROM raw_audit
  GROUP BY 1, 2
),
raw_tokens AS (
  -- Real-time token stream from the Cloud Logging sink
  SELECT * FROM (
    SELECT
      DATE(inf.timestamp) AS activity_date,
      inf.timestamp,
      COALESCE(
        act.user_id,
        "unassigned"
      ) AS user_id,
      CAST(COALESCE(SAFE_CAST(JSON_VALUE(TO_JSON_STRING(inf.jsonPayload), "$.gen_ai_usage_input_tokens") AS FLOAT64), 0) AS INT64) AS input_tokens,
      CAST(COALESCE(SAFE_CAST(JSON_VALUE(TO_JSON_STRING(inf.jsonPayload), "$.gen_ai_usage_output_tokens") AS FLOAT64), 0) AS INT64) AS output_tokens,
      CAST(COALESCE(SAFE_CAST(JSON_VALUE(TO_JSON_STRING(inf.jsonPayload), "$.gen_ai_usage_reasoning_output_tokens") AS FLOAT64), 0) AS INT64) AS cached_tokens,
      inf.insertId,
      1 AS priority
    FROM `{project_id}.{dataset_id}.discoveryengine_googleapis_com_gen_ai_client_inference_operation_details` inf
    LEFT JOIN (
      SELECT trace, user_id FROM raw_user_events WHERE trace IS NOT NULL AND user_id != "unassigned"
      QUALIFY ROW_NUMBER() OVER(PARTITION BY trace ORDER BY timestamp DESC) = 1
    ) act
      ON inf.trace = act.trace

    UNION ALL

    -- Historical tokens from the backfill table
    SELECT
      DATE(timestamp) AS activity_date,
      timestamp,
      COALESCE(
        NULLIF(NULLIF(TRIM(user_id), "user"), ""),
        "unassigned"
      ) AS user_id,
      CAST(input_tokens AS INT64) AS input_tokens,
      CAST(output_tokens AS INT64) AS output_tokens,
      CAST(cached_tokens AS INT64) AS cached_tokens,
      insert_id AS insertId,
      0 AS priority
    FROM `{project_id}.{dataset_id}.gen_ai_client_inference_operation_details`
  )
  QUALIFY ROW_NUMBER() OVER(
    PARTITION BY COALESCE(NULLIF(insertId, ""), CAST(timestamp AS STRING))
    ORDER BY priority DESC, timestamp
  ) = 1
),
aggregated_tokens AS (
  SELECT
    activity_date,
    user_id,
    SUM(COALESCE(input_tokens, 0)) AS input_tokens,
    SUM(COALESCE(output_tokens, 0)) AS output_tokens,
    SUM(COALESCE(cached_tokens, 0)) AS cached_tokens,
    SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)) AS total_tokens,
    MIN(timestamp) AS first_token,
    MAX(timestamp) AS last_token
  FROM raw_tokens
  GROUP BY 1, 2
)
SELECT
  COALESCE(u.activity_date, a.activity_date, t.activity_date, ag.activity_date) AS activity_date,
  COALESCE(u.user_id, a.user_id, t.user_id, ag.user_id) AS user_id,
  COALESCE(u.total_events, a.audit_events, 0) AS total_events,
  COALESCE(u.assistant_queries, 0) AS assistant_queries,
  COALESCE(u.deep_research_count, 0) AS deep_research_count,
  COALESCE(u.images_generated, 0) AS images_generated,
  GREATEST(COALESCE(u.agents_created, 0), COALESCE(a.agents_created, 0)) AS agents_created,
  GREATEST(COALESCE(u.agent_updates, 0), COALESCE(a.agent_updates, 0)) AS agent_updates,
  COALESCE(u.agent_views, 0) AS agent_views,
  COALESCE(u.ui_page_views, 0) AS ui_page_views,
  COALESCE(u.failed_requests, 0) AS failed_requests,
  COALESCE(ag.author_agent_invocations, 0) AS author_agent_invocations,
  COALESCE(ag.author_agent_sessions, 0) AS author_agent_sessions,
  COALESCE(ag.org_agent_invocations, 0) AS org_agent_invocations,
  COALESCE(ag.org_agent_sessions, 0) AS org_agent_sessions,
  COALESCE(ag.org_agent_unique_callers, 0) AS org_agent_unique_callers,
  COALESCE(t.input_tokens, 0) AS input_tokens,
  COALESCE(t.output_tokens, 0) AS output_tokens,
  COALESCE(t.cached_tokens, 0) AS cached_tokens,
  COALESCE(t.total_tokens, 0) AS total_tokens,
  LEAST(
    COALESCE(u.first_event, a.first_audit, t.first_token),
    COALESCE(a.first_audit, t.first_token, u.first_event),
    COALESCE(t.first_token, u.first_event, a.first_audit)
  ) AS first_seen,
  GREATEST(
    COALESCE(u.last_event, a.last_audit, t.last_token),
    COALESCE(a.last_audit, t.last_token, u.last_event),
    COALESCE(t.last_token, u.last_event, a.last_audit)
  ) AS last_seen
FROM aggregated_user_events u
FULL OUTER JOIN aggregated_audit a 
  ON u.user_id = a.user_id AND u.activity_date = a.activity_date
FULL OUTER JOIN aggregated_tokens t
  ON COALESCE(u.user_id, a.user_id) = t.user_id 
  AND COALESCE(u.activity_date, a.activity_date) = t.activity_date
FULL OUTER JOIN agent_usage_events ag
  ON COALESCE(u.user_id, a.user_id, t.user_id) = ag.user_id
  AND COALESCE(u.activity_date, a.activity_date, t.activity_date) = ag.activity_date
WHERE COALESCE(u.user_id, a.user_id, t.user_id, ag.user_id) NOT IN ("unknown", "system", "unassigned", "<elided>")
  -- Exclude technical service accounts (Cloud Functions, Compute Engine, Logging, Google-managed SAs) from user rankings
  AND NOT REGEXP_CONTAINS(COALESCE(u.user_id, a.user_id, t.user_id, ag.user_id), r'(@.*\.gserviceaccount\.com$|@developer\.gserviceaccount\.com$)');

-- 2. Backward-compatibility view (alias for v_user_daily_utilization)
CREATE OR REPLACE VIEW `{project_id}.{dataset_id}.v_user_utilization` AS
SELECT * FROM `{project_id}.{dataset_id}.v_user_daily_utilization`;

-- 3. Aggregate per-user summary (cumulative statistics since logging began)
CREATE OR REPLACE VIEW `{project_id}.{dataset_id}.v_user_summary` AS
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
FROM `{project_id}.{dataset_id}.v_user_daily_utilization`
WHERE NOT REGEXP_CONTAINS(user_id, r'(@.*\.gserviceaccount\.com$|@developer\.gserviceaccount\.com$)')
GROUP BY user_id
ORDER BY total_events DESC, assistant_queries DESC;

-- 4. Daily organization adoption trends view (DAU, interactions, queries)
CREATE OR REPLACE VIEW `{project_id}.{dataset_id}.v_daily_adoption` AS
SELECT
  activity_date,
  COUNT(DISTINCT user_id) AS daily_active_users,
  SUM(total_events) AS total_interactions,
  SUM(assistant_queries) AS total_assistant_queries,
  SUM(deep_research_count) AS total_deep_research_queries,
  SUM(images_generated) AS total_images_generated,
  SUM(agents_created) AS total_agents_created,
  SUM(agent_updates) AS total_agent_updates,
  SUM(agent_views) AS total_agent_views,
  SUM(ui_page_views) AS total_ui_page_views,
  SUM(failed_requests) AS total_failed_requests,
  SUM(org_agent_invocations) AS total_custom_agent_invocations,
  SUM(total_tokens) AS total_tokens_burned
FROM `{project_id}.{dataset_id}.v_user_daily_utilization`
GROUP BY activity_date
ORDER BY activity_date DESC;

-- 5. Usage breakdown view by module and feature
CREATE OR REPLACE VIEW `{project_id}.{dataset_id}.v_feature_usage` AS
SELECT
  feature_name,
  COUNT(*) AS total_calls,
  COUNT(DISTINCT user_id) AS distinct_users,
  MIN(timestamp) AS earliest_invocation,
  MAX(timestamp) AS latest_invocation
FROM (
  SELECT
    timestamp,
    COALESCE(
      NULLIF(NULLIF(TRIM(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.useriamprincipal")), "<elided>"), ""),
      NULLIF(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.userevent.userpseudoid"), ""),
      "system"
    ) AS user_id,
    CASE
      WHEN COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.logmetadata.methodname"), "") IN ("StreamAssist", "Assist")
       AND (
         COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.agentinfo.agent"), "") LIKE "%/agents/deep_research"
         OR TO_JSON_STRING(jsonPayload) LIKE "%deep_research%"
       ) THEN "Deep Research"
      WHEN COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.logmetadata.methodname"), "") IN ("StreamAssist", "Assist")
       AND (
         LOWER(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.modelinfo.model"), "")) LIKE "%imagen%"
         OR REGEXP_CONTAINS(LOWER(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.modelinfo.model"), "")), r"(image|imagen)")
         OR LOWER(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.query.parts[0].text"), "")) LIKE "%generate%image%"
         OR COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.userevent.agentspaceinfo.agentspacepagetype"), "") = "image-generation"
         OR REGEXP_CONTAINS(LOWER(TO_JSON_STRING(jsonPayload)), r"(wygeneruj|stwórz|utwórz|zrób|generuj|generate|create|draw|narysuj|namaluj|paint)\s+(obraz|obrazek|grafik|zdjęci|image|picture|photo|illustration)")
         OR REGEXP_CONTAINS(LOWER(TO_JSON_STRING(jsonPayload)), r'"(obrazek|obraz|image|zdjęcie)\s+')
         OR LOWER(TO_JSON_STRING(jsonPayload)) LIKE "%image-generation%"
       ) THEN "Image Generation (Imagen models)"
      WHEN COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.logmetadata.methodname"), "") IN ("StreamAssist", "Assist")
       AND (
         COALESCE(
           NULLIF(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.agentsspec.agentspecs[0].agentid"), ""),
           NULLIF(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.agentsspec.agentspecs.agentid"), ""),
           REGEXP_EXTRACT(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.agentinfo.agent"), ""), r"/agents/([^/]+)")
         ) IS NOT NULL
         AND COALESCE(
           NULLIF(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.agentsspec.agentspecs[0].agentid"), ""),
           NULLIF(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.agentsspec.agentspecs.agentid"), ""),
           REGEXP_EXTRACT(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.agentinfo.agent"), ""), r"/agents/([^/]+)")
         ) != "deep_research"
       ) THEN "Custom Agent Conversation"
      WHEN COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.logmetadata.methodname"), "") IN ("StreamAssist", "Assist") THEN "General Assistant"
      WHEN COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.logmetadata.methodname"), "") = "CreateAgent" THEN "Custom Agent Creation"
      WHEN COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.logmetadata.methodname"), "") = "UpdateAgent" THEN "Custom Agent Edit"
      WHEN COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.userevent.agentspaceinfo.agentspacepagetype"), "") != "" 
        THEN CONCAT("UI: ", JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.userevent.agentspaceinfo.agentspacepagetype"))
      ELSE COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.logmetadata.methodname"), "Other")
    END AS feature_name
  FROM `{project_id}.{dataset_id}.discoveryengine_googleapis_com_gemini_enterprise_user_activity`
  
  UNION ALL
  
  SELECT
    timestamp,
    COALESCE(
      NULLIF(NULLIF(TRIM(user_iam_principal), "<elided>"), ""),
      NULLIF(user_pseudo_id, ""),
      "system"
    ) AS user_id,
    CASE
      WHEN method_name IN ("StreamAssist", "Assist") 
       AND (agent_id = "deep_research" OR raw_payload LIKE "%agents/deep_research%") THEN "Deep Research"
      WHEN method_name IN ("StreamAssist", "Assist") 
       AND (
         REGEXP_CONTAINS(LOWER(raw_payload), r'"model"\s*:\s*"[^"]*imagen[^"]*"')
         OR REGEXP_CONTAINS(LOWER(raw_payload), r'"model"\s*:\s*"[^"]*(image|imagen)[^"]*"')
         OR LOWER(COALESCE(REGEXP_EXTRACT(raw_payload, r'"text"\s*:\s*"([^"]*)"'), "")) LIKE "%generate%image%"
         OR page_type = "image-generation" 
         OR REGEXP_CONTAINS(LOWER(raw_payload), r"(wygeneruj|stwórz|utwórz|zrób|generuj|generate|create|draw|narysuj|namaluj|paint)\s+(obraz|obrazek|grafik|zdjęci|image|picture|photo|illustration)")
         OR REGEXP_CONTAINS(LOWER(raw_payload), r'"(obrazek|obraz|image|zdjęcie)\s+')
         OR LOWER(raw_payload) LIKE "%image-generation%"
       ) THEN "Image Generation (Imagen models)"
      WHEN method_name IN ("StreamAssist", "Assist") 
       AND agent_id IS NOT NULL AND agent_id NOT IN ("", "deep_research") THEN "Custom Agent Conversation"
      WHEN method_name IN ("StreamAssist", "Assist") THEN "General Assistant"
      WHEN method_name = "CreateAgent" THEN "Custom Agent Creation"
      WHEN method_name = "UpdateAgent" THEN "Custom Agent Edit"
      WHEN COALESCE(page_type, "") != "" THEN CONCAT("UI: ", page_type)
      ELSE COALESCE(method_name, "Other")
    END AS feature_name
  FROM `{project_id}.{dataset_id}.gemini_enterprise_user_activity`
)
WHERE user_id != "system"
GROUP BY feature_name
ORDER BY total_calls DESC;

-- 6. Distributed OpenTelemetry traces and spans view (Cloud Trace)
CREATE OR REPLACE VIEW `{project_id}.{dataset_id}.v_observability_traces` AS
WITH base_traces AS (
  SELECT
    timestamp,
    DATE(timestamp) AS trace_date,
    trace AS trace_id,
    spanId AS span_id,
    COALESCE(
      NULLIF(NULLIF(TRIM(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.useriamprincipal")), "<elided>"), ""),
      NULLIF(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.userevent.userpseudoid"), "")
    ) AS direct_user_id,
    COALESCE(
      REGEXP_EXTRACT(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.agentinfo.agent"), ""), r"/agents/([^/]+)"),
      REGEXP_EXTRACT(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.userevent.agentspaceinfo.agent"), ""), r"/agents/([^/]+)"),
      ""
    ) AS called_agent_id,
    COALESCE(
      REGEXP_EXTRACT(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.answer.name"), ""), r"/sessions/([^/]+)"),
      REGEXP_EXTRACT(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.parent"), ""), r"/sessions/([^/]+)"),
      REGEXP_EXTRACT(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.request.userevent.session"), ""), r"/sessions/([^/]+)"),
      REGEXP_EXTRACT(COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.session"), ""), r"/sessions/([^/]+)"),
      ""
    ) AS session_id,
    JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.logmetadata.methodname") AS method_name,
    JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.logmetadata.servicename") AS service_name,
    JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.answer.state") AS answer_state,
    JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.agentinfo.displayname") AS agent_display_name,
    JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.response.agentinfo.agent") AS agent_resource,
    severity,
    insertId
  FROM `{project_id}.{dataset_id}.discoveryengine_googleapis_com_gemini_enterprise_user_activity`
  WHERE trace IS NOT NULL
)
SELECT
  timestamp,
  trace_date,
  trace_id,
  span_id,
  COALESCE(
    direct_user_id,
    FIRST_VALUE(direct_user_id IGNORE NULLS) OVER (
      PARTITION BY session_id 
      ORDER BY timestamp 
      ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
    ),
    LAST_VALUE(CASE WHEN called_agent_id != "" THEN direct_user_id END IGNORE NULLS) OVER (
      PARTITION BY called_agent_id 
      ORDER BY timestamp 
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ),
    FIRST_VALUE(CASE WHEN called_agent_id != "" THEN direct_user_id END IGNORE NULLS) OVER (
      PARTITION BY called_agent_id 
      ORDER BY timestamp 
      ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING
    ),
    LAST_VALUE(direct_user_id IGNORE NULLS) OVER (
      ORDER BY UNIX_SECONDS(timestamp)
      RANGE BETWEEN 30 PRECEDING AND 30 FOLLOWING
    ),
    "anonymous_user"
  ) AS user_id,
  method_name,
  service_name,
  answer_state,
  agent_display_name,
  agent_resource,
  severity,
  insertId
FROM base_traces
ORDER BY timestamp DESC;

-- 7. Detailed LLM token consumption telemetry view (OpenTelemetry Spans & GenAI Operations)
CREATE OR REPLACE VIEW `{project_id}.{dataset_id}.v_token_telemetry` AS
SELECT
  timestamp,
  DATE(timestamp) AS activity_date,
  insertId AS insert_id,
  trace AS trace_id,
  spanId AS span_id,
  CAST(COALESCE(SAFE_CAST(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.gen_ai_usage_input_tokens") AS FLOAT64), 0) AS INT64) AS input_tokens,
  CAST(COALESCE(SAFE_CAST(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.gen_ai_usage_output_tokens") AS FLOAT64), 0) AS INT64) AS output_tokens,
  CAST(COALESCE(SAFE_CAST(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.gen_ai_usage_reasoning_output_tokens") AS FLOAT64), 0) AS INT64) AS cached_tokens,
  (CAST(COALESCE(SAFE_CAST(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.gen_ai_usage_input_tokens") AS FLOAT64), 0) AS INT64) + CAST(COALESCE(SAFE_CAST(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.gen_ai_usage_output_tokens") AS FLOAT64), 0) AS INT64)) AS total_tokens,
  COALESCE(JSON_VALUE(TO_JSON_STRING(jsonPayload), "$.gen_ai_agent_name"), "") AS agent_name,
  severity
FROM `{project_id}.{dataset_id}.discoveryengine_googleapis_com_gen_ai_client_inference_operation_details`

UNION ALL

SELECT
  timestamp,
  DATE(timestamp) AS activity_date,
  insert_id,
  NULL AS trace_id,
  NULL AS span_id,
  CAST(COALESCE(input_tokens, 0) AS INT64) AS input_tokens,
  CAST(COALESCE(output_tokens, 0) AS INT64) AS output_tokens,
  CAST(COALESCE(cached_tokens, 0) AS INT64) AS cached_tokens,
  (CAST(COALESCE(input_tokens, 0) AS INT64) + CAST(COALESCE(output_tokens, 0) AS INT64)) AS total_tokens,
  COALESCE(agent_name, "") AS agent_name,
  "DEFAULT" AS severity
FROM `{project_id}.{dataset_id}.gen_ai_client_inference_operation_details`;
