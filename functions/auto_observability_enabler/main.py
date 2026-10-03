import base64
import json
import logging
import os
import re
import urllib.request
import functions_framework
import google.auth
from google.auth.transport.requests import Request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("auto_observability_enabler")

def get_auth_token():
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(Request())
    return creds.token

def patch_agent_observability(agent_resource_name, location="eu", project_id=None):
    """
    Calls the Discovery Engine API PATCH method to enable observabilityConfig.observabilityEnabled: true.
    agent_resource_name has the form:
    projects/{proj}/locations/{loc}/collections/default_collection/engines/{engine}/assistants/default_assistant/agents/{agent_id}
    """
    token = get_auth_token()
    
    # Determine the location from the resource name, or fall back to the default
    loc_match = re.search(r"locations/([a-zA-Z0-9_\-]+)", agent_resource_name)
    loc = loc_match.group(1) if loc_match else location
    
    api_host = f"{loc}-discoveryengine.googleapis.com" if loc != "global" else "discoveryengine.googleapis.com"
    url = f"https://{api_host}/v1alpha/{agent_resource_name}?updateMask=observabilityConfig"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    if project_id:
        headers["X-Goog-User-Project"] = project_id
    
    payload = {
        "observabilityConfig": {
            "observabilityEnabled": True,
            "sensitiveLoggingEnabled": True
        }
    }
    
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="PATCH")
    try:
        with urllib.request.urlopen(req) as resp:
            logger.info(f"Successfully enabled observability for agent: {agent_resource_name} (Status: {resp.status})")
            return True
    except Exception as e:
        logger.error(f"Error while enabling observability for {agent_resource_name}: {e}")
        raise e

def reconcile_all_agents(project_id, location, engine_id):
    """
    Lists all agents in the engine and enables observability on every agent that has observabilityEnabled != True or sensitiveLoggingEnabled != True.
    """
    token = get_auth_token()
    api_host = f"{location}-discoveryengine.googleapis.com" if location != "global" else "discoveryengine.googleapis.com"
    url = f"https://{api_host}/v1alpha/projects/{project_id}/locations/{location}/collections/default_collection/engines/{engine_id}/assistants/default_assistant/agents"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "X-Goog-User-Project": project_id})
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            agents = data.get("agents", [])
            logger.info(f"Checking {len(agents)} agents in engine {engine_id}...")
            count_patched = 0
            for a in agents:
                a_name = a.get("name")
                obs = a.get("observabilityConfig") or {}
                if not (obs.get("observabilityEnabled") and obs.get("sensitiveLoggingEnabled")):
                    logger.info(f"Agent {a_name} ({a.get('displayName')}) does not have full observability. Enabling...")
                    patch_agent_observability(a_name, location=location, project_id=project_id)
                    count_patched += 1
                else:
                    logger.info(f"Agent {a_name} ({a.get('displayName')}) already has full observability enabled.")
            logger.info(f"Verification complete. Agents updated: {count_patched}")
            return count_patched
    except Exception as e:
        logger.error(f"Error while listing agents: {e}")
        raise e

@functions_framework.cloud_event
def auto_enable_observability(cloud_event):
    """
    Pub/Sub trigger: receives a CreateAgent log from Cloud Audit Logs and enables observability on the agent.
    """
    try:
        pubsub_message = cloud_event.data.get("message", {})
        data_encoded = pubsub_message.get("data")
        if not data_encoded:
            logger.warning("No data in the Pub/Sub message")
            return
            
        data_str = base64.b64decode(data_encoded).decode("utf-8")
        payload = json.loads(data_str)
        
        # Check whether this is a reconciliation command
        if payload.get("action") == "reconcile":
            proj = payload.get("project_id") or os.environ.get("GCP_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
            loc = payload.get("location") or os.environ.get("LOCATION") or "eu"
            eng = payload.get("engine_id") or os.environ.get("ENGINE_ID")
            if proj and eng:
                reconcile_all_agents(proj, loc, eng)
                return
        
        proto_payload = payload.get("protoPayload", {})
        method_name = proto_payload.get("methodName", "")
        caller = proto_payload.get("authenticationInfo", {}).get("principalEmail", "")
        
        # Event loop protection: ignore calls made by the function's own service account
        if "sa-ge-auto-obs" in caller:
            logger.info("Ignored event triggered by sa-ge-auto-obs (loop protection).")
            return
            
        if not any(m in method_name for m in ["AgentService.CreateAgent", "AgentService.UpdateAgent"]):
            logger.info(f"Ignored method: {method_name}")
            return
            
        # Extract the agent resource name from the response or resourceName
        resp = proto_payload.get("response", {})
        agent_name = resp.get("name") or proto_payload.get("resourceName")
        
        if not agent_name or "/agents/" not in agent_name:
            logger.warning(f"No valid agent path in protoPayload (name={agent_name}).")
            return
            
        # Skip the redundant PATCH only when BOTH flags are already set. The Gemini Enterprise web console
        # resets sensitiveLoggingEnabled on UpdateAgent while keeping observabilityEnabled, so both must be checked.
        obs_config = resp.get("observabilityConfig") or {}
        if obs_config.get("observabilityEnabled") is True and obs_config.get("sensitiveLoggingEnabled") is True:
            logger.info(f"Agent {agent_name} already has full observability (observabilityEnabled + sensitiveLoggingEnabled). Skipping.")
            return

        project_id = payload.get("resource", {}).get("labels", {}).get("project_id") or os.environ.get("GCP_PROJECT")
        logger.info(f"Detected {method_name} event for agent: {agent_name} (author: {caller}). Enabling observability...")
        patch_agent_observability(agent_name, project_id=project_id)
        
    except Exception as e:
        logger.error(f"Unexpected error in the auto_enable_observability function: {e}")
        raise e
