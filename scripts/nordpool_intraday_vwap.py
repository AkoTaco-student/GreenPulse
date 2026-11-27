"""Fetch Nord Pool intraday contract statistics (includes VWAP) for Norwegian areas.

Standalone on purpose: it is NOT wired into the GreenPulse pipeline, because the
response field that carries VWAP has not been verified against a live response
(the public documentation we could read does not show the payload). The script
prints the real keys it receives and picks a VWAP field only if one exists.

Credentials (never hard-code them) come from environment variables:
    NORDPOOL_USERNAME, NORDPOOL_PASSWORD   Nord Pool Market Data API user
    NORDPOOL_SUBSCRIPTION_KEY              API-management subscription key

Usage:
    python scripts/nordpool_intraday_vwap.py 2026-11-27 NO1,NO2,NO3,NO4,NO5

Endpoints (Nord Pool Market Data API v2, as used by the Axpo reference client):
    token : POST https://sts.nordpoolgroup.com/connect/token
    data  : GET  https://data-api.nordpoolgroup.com/api/v2/Intraday/ContractStatistics/ByAreas
            query: areas=<comma list>, date=<YYYY-MM-DD>
"""
import json
import os
import sys

import requests

TOKEN_URL = "https://sts.nordpoolgroup.com/connect/token"
STATS_URL = "https://data-api.nordpoolgroup.com/api/v2/Intraday/ContractStatistics/ByAreas"
# Public client id/secret of the Nord Pool Market Data API password grant
# ("client_marketdata_api:client_marketdata_api", base64). Not a personal secret.
CLIENT_BASIC = "Basic Y2xpZW50X21hcmtldGRhdGFfYXBpOmNsaWVudF9tYXJrZXRkYXRhX2FwaQ=="


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Missing environment variable {name}")
    return value


def find_vwap_keys(obj, path=""):
    """Yield (path, value) for every key whose name contains 'vwap' (case-insensitive)."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{path}.{key}" if path else key
            if "vwap" in key.lower():
                yield here, value
            yield from find_vwap_keys(value, here)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            yield from find_vwap_keys(item, f"{path}[{i}]")


def main() -> None:
    date = sys.argv[1] if len(sys.argv) > 1 else sys.exit("Usage: nordpool_intraday_vwap.py YYYY-MM-DD NO1,NO2,...")
    areas = sys.argv[2] if len(sys.argv) > 2 else "NO1,NO2,NO3,NO4,NO5"

    token_response = requests.post(
        TOKEN_URL,
        headers={"Authorization": CLIENT_BASIC},
        data={
            "grant_type": "password",
            "scope": "marketdata_api",
            "username": env("NORDPOOL_USERNAME"),
            "password": env("NORDPOOL_PASSWORD"),
        },
        timeout=30,
    )
    token_response.raise_for_status()
    token = token_response.json()["access_token"]

    response = requests.get(
        STATS_URL,
        params={"areas": areas, "date": date},
        headers={
            "Authorization": f"Bearer {token}",
            "Ocp-Apim-Subscription-Key": env("NORDPOOL_SUBSCRIPTION_KEY"),
        },
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()

    vwap = list(find_vwap_keys(payload))
    if not vwap:
        print("No key containing 'vwap' in the response. Raw payload (truncated):", file=sys.stderr)
        print(json.dumps(payload, indent=2)[:4000], file=sys.stderr)
        sys.exit(1)
    for path, value in vwap:
        print(f"{path} = {value}")


if __name__ == "__main__":
    main()
