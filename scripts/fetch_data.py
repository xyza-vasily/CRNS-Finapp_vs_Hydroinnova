"""
Fetch neutron-count data from Hydroinnova (no auth) and Finapp (login required),
merge them, and write a single JSON file that the dashboard reads.

Run manually:  python scripts/fetch_data.py
Or via the GitHub Actions "Update dashboard data" workflow (manual button click).
"""

import csv
import io
import json
import os
import re
from datetime import datetime, timezone, timedelta
import time
import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

HYDROINNOVA_URL = (
    "http://nearfld.com/reguser/queryData.php"
    "?tz=1:00&vw=soil_moisture4&IM=300534060129810&fn=Marchfeld"
)

FINAPP_BASE_URL = "https://data.finapptech.com"
FINAPP_LOGIN_PAGE_URL = f"{FINAPP_BASE_URL}/login"
FINAPP_DATA_URL = os.environ.get(
    "FINAPP_DATA_URL",
    f"{FINAPP_BASE_URL}/user/installation/info?idInstallation=10617&inst_name=IAEA%20SWMCNL",
)
FINAPP_USERNAME = os.environ.get("FINAPP_USERNAME", "")
FINAPP_PASSWORD = os.environ.get("FINAPP_PASSWORD", "")

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "data.json")

# Date filter: only keep data from August 20, 2026 onwards
START_DATE = datetime(2026, 8, 20, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# HYDROINNOVA
# ---------------------------------------------------------------------------

def fetch_hydroinnova():
    """Fetch and parse the Hydroinnova CSV feed."""
    resp = requests.get(HYDROINNOVA_URL, timeout=30)
    resp.raise_for_status()
    
    # Parse as CSV directly
    csv_data = io.StringIO(resp.text)
    reader = csv.DictReader(csv_data)
    
    records = []
    for row in reader:
        ts_str = row.get("UTC", "").strip()
        if not ts_str:
            continue
            
        try:
            # Parse timestamp (format: "2026-08-24 08:05:00")
            timestamp = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        
        # Only keep records from August 20, 2026 onwards
        if timestamp < START_DATE:
            continue
            
        records.append({
            "timestamp": timestamp.isoformat(),
            "N1_cph": _to_float(row.get("N1 [cph]")),
            "N2_cph": _to_float(row.get("N2 [cph]")),
            "source": "hydroinnova",
        })
    
    print(f"✅ Hydroinnova: {len(records)} records (filtered from {START_DATE.date()})")
    return records


# ---------------------------------------------------------------------------
# FINAPP
# ---------------------------------------------------------------------------

def fetch_finapp():
    """Log in to Finapp and fetch neutron count data."""
    if not (FINAPP_USERNAME and FINAPP_PASSWORD):
        print("⚠️ FINAPP_USERNAME / FINAPP_PASSWORD not set — skipping Finapp fetch.")
        return []

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    # Step 1: Get CSRF token
    login_page = session.get(FINAPP_LOGIN_PAGE_URL, timeout=30)
    login_page.raise_for_status()

    csrf_token = _extract_csrf_token(login_page.text)
    if not csrf_token:
        raise RuntimeError(
            "Could not find CSRF token on Finapp login page. "
            "Check the login form field names."
        )

    # Step 2: Submit login
    login_payload = {
        "_token": csrf_token,
        "email": FINAPP_USERNAME,
        "password": FINAPP_PASSWORD,
    }
    login_resp = session.post(
        FINAPP_LOGIN_PAGE_URL,
        data=login_payload,
        headers={"Referer": FINAPP_LOGIN_PAGE_URL},
        timeout=30,
    )
    login_resp.raise_for_status()

    if "/login" in login_resp.url:
        raise RuntimeError(
            "Finapp login failed. Check FINAPP_USERNAME/FINAPP_PASSWORD."
        )

    # Step 3: Fetch data
    data_resp = session.get(FINAPP_DATA_URL, timeout=30)
    data_resp.raise_for_status()

    return _parse_finapp_response(data_resp)


def _extract_csrf_token(html):
    """Extract CSRF token from Laravel page."""
    meta_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    if meta_match:
        return meta_match.group(1)
    input_match = re.search(r'name="_token"\s+value="([^"]+)"', html)
    if input_match:
        return input_match.group(1)
    return None


def _parse_finapp_response(resp):
    """Parse Finapp response with date filtering."""
    content_type = resp.headers.get("Content-Type", "")

    if "application/json" in content_type:
        payload = resp.json()
        
        # Handle different response structures
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = payload.get("data") or payload.get("result") or payload.get("records") or []
            if not rows and "installation" in payload:
                rows = payload.get("installation", {}).get("data", [])
        else:
            rows = []

        records = []
        for row in rows:
            if not row:
                continue
            
            # Get timestamp from various possible fields
            ts_str = row.get("timestamp") or row.get("date") or row.get("created_at") or row.get("time")
            if not ts_str:
                continue
                
            try:
                timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
            except (ValueError, AttributeError):
                # Try alternative format
                try:
                    timestamp = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
            
            # Only keep records from August 20, 2026 onwards
            if timestamp < START_DATE:
                continue
            
            records.append({
                "timestamp": timestamp.isoformat(),
                "N1_cph": _to_float(row.get("N1") or row.get("neutron_count") or row.get("counts")),
                "N2_cph": _to_float(row.get("N2") or row.get("neutron_count_2")),
                "source": "finapp",
            })
        
        print(f"✅ Finapp: {len(records)} records (filtered from {START_DATE.date()})")
        return records

    # Not JSON - show snippet for debugging
    snippet = resp.text[:1500]
    raise RuntimeError(
        f"Finapp returned non-JSON. Content-Type: {content_type}\n"
        f"First 1500 chars: {snippet}"
    )


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _to_float(val):
    try:
        if isinstance(val, str):
            val = val.replace(",", ".")
        return float(val)
    except (TypeError, ValueError):
        return None


def main():
    """Main execution."""
    print("🚀 Fetching neutron data...")
    
    hydro = fetch_hydroinnova()
    fin = fetch_finapp()

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "start_date": START_DATE.isoformat(),
        "hydroinnova": hydro,
        "finapp": fin,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    print(f"📊 Wrote data to {OUTPUT_PATH}")
    print(f"   - Hydroinnova: {len(hydro)} records")
    print(f"   - Finapp: {len(fin)} records")


if __name__ == "__main__":
    main()
