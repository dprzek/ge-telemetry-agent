#!/usr/bin/env python3
"""
Gemini Enterprise license management module (Discovery Engine License API).
Allows listing license pools, checking assigned users,
and automatically assigning licenses (pre-provisioning) without waiting for the first login.
"""

import sys
import os
import json
import argparse
import urllib.request
import google.auth
from google.auth.transport.requests import Request

def get_auth_token():
    creds, _ = google.auth.default()
    creds.refresh(Request())
    return creds.token

def get_api_host(location):
    return f"{location}-discoveryengine.googleapis.com" if location != "global" else "discoveryengine.googleapis.com"

def list_license_configs(project_id, location, token):
    api_host = get_api_host(location)
    url = f"https://{api_host}/v1alpha/projects/{project_id}/locations/{location}/licenseConfigs"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "X-Goog-User-Project": project_id})
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("licenseConfigs", [])
    except Exception as e:
        print(f"[!] Error fetching license configurations: {e}")
        return []

def list_user_licenses(project_id, location, token):
    api_host = get_api_host(location)
    url = f"https://{api_host}/v1alpha/projects/{project_id}/locations/{location}/userStores/default_user_store/userLicenses"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "X-Goog-User-Project": project_id})
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("userLicenses", [])
    except Exception as e:
        print(f"[!] Error fetching user licenses: {e}")
        return []

def batch_assign_licenses(project_id, location, user_emails, license_config_name, token):
    api_host = get_api_host(location)
    url = f"https://{api_host}/v1alpha/projects/{project_id}/locations/{location}/userStores/default_user_store:batchUpdateUserLicenses"
    
    user_licenses_payload = [
        {
            "userPrincipal": email.strip(),
            "licenseConfig": license_config_name
        }
        for email in user_emails if email.strip()
    ]
    
    body = {
        "inlineSource": {
            "userLicenses": user_licenses_payload
        }
    }
    
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "X-Goog-User-Project": project_id,
            "Content-Type": "application/json"
        },
        method="POST"
    )
    
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[!] Error assigning licenses: {e}")
        return None

def main():
    parser = argparse.ArgumentParser(description="Manage Gemini Enterprise user licenses.")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"), help="GCP project ID (defaults to $GOOGLE_CLOUD_PROJECT or the ADC project)")
    parser.add_argument("--location", default="eu", help="Location (e.g. eu, us, global)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: configs
    subparsers.add_parser("configs", help="Lists the available license pools")

    # Subcommand: users
    subparsers.add_parser("users", help="Lists users with assigned licenses")

    # Subcommand: assign
    assign_p = subparsers.add_parser("assign", help="Assigns a license to the given users")
    assign_p.add_argument("--emails", required=True, help="Comma-separated e-mail addresses")
    assign_p.add_argument("--license-config", help="Full licenseConfig resource name (if omitted, the first active one is used)")

    args = parser.parse_args()
    if not args.project:
        try:
            _, args.project = google.auth.default()
        except Exception:
            pass
    if not args.project:
        print("[!] Error: no GCP project ID. Use --project <PROJECT_ID> or set GOOGLE_CLOUD_PROJECT.")
        sys.exit(1)
    token = get_auth_token()

    if args.command == "configs":
        configs = list_license_configs(args.project, args.location, token)
        print(f"\n=== License Configurations in Project '{args.project}' ({args.location}) ===")
        for c in configs:
            print(f"- Name: {c.get('name')}")
            print(f"  Pool: {c.get('licenseCount')} licenses | State: {c.get('state')} | Tier: {c.get('subscriptionTier')}")
            print(f"  Validity: {c.get('startDate')} to {c.get('endDate')}")

    elif args.command == "users":
        users = list_user_licenses(args.project, args.location, token)
        print(f"\n=== Users with Assigned Licenses ({len(users)} users) ===")
        print(f"{'Identifier / E-mail':<32} | {'State':<10} | {'Last Login':<26}")
        print("-" * 72)
        for u in users:
            last_login = u.get("lastLoginTime", "Not logged in yet")
            print(f"{u.get('userPrincipal'):<32} | {u.get('licenseAssignmentState'):<10} | {last_login:<26}")

    elif args.command == "assign":
        emails = [e.strip() for e in args.emails.split(",") if e.strip()]
        cfg = args.license_config
        if not cfg:
            configs = list_license_configs(args.project, args.location, token)
            if not configs:
                print("[!] No license configurations available!")
                sys.exit(1)
            cfg = configs[0].get("name")
        print(f"[*] Assigning license '{cfg}' to {len(emails)} users...")
        res = batch_assign_licenses(args.project, args.location, emails, cfg, token)
        if res and res.get("done"):
            print("✔ Licenses assigned successfully!")
            for ul in res.get("response", {}).get("userLicenses", []):
                print(f"  - {ul.get('userPrincipal')}: {ul.get('licenseAssignmentState')}")
        else:
            print(f"[!] Operation result: {res}")

if __name__ == "__main__":
    main()
