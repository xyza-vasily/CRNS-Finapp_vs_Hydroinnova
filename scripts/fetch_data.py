"""
Fetch neutron-count data from Hydroinnova and Finapp.
"""

import csv
import io
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

HYDROINNOVA_URL = (
    "http://nearfld.com/reguser/queryData.php"
    "?tz=1:00&vw=soil_moisture4&IM=300534060129810&fn=Marchfeld"
)

FINAPP_BASE_URL = "https://data.finapptech.com"
FINAPP_LOGIN_URL = f"{FINAPP_BASE_URL}/login"
FINAPP_INSTALLATION_ID = "10617"
FINAPP_DATA_URL = f"{FINAPP_BASE_URL}/user/installation/get-charts/{FINAPP_INSTALLATION_ID}"
FINAPP_NEUTRON_FIELD = "neutrons_count_above_dynamic_threshold"
FINAPP_USERNAME = os.environ.get("FINAPP_USERNAME", "")
FINAPP_PASSWORD = os.environ.get("FINAPP_PASSWORD", "")

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "data.json")
START_DATE = datetime(2026, 8, 20, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# HYDROINNOVA
# ---------------------------------------------------------------------------

def fetch_hydroinnova():
    """Fetch and parse the Hydroinnova CSV feed."""
    print("Fetching Hydroinnova data...")
    try:
        resp = requests.get(HYDROINNOVA_URL, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"   Hydroinnova request failed: {e}")
        return []

    try:
        text = resp.text
        if "UTC" in text:
            text = text[text.index("UTC"):]
        text = re.sub(r'<[^>]+>', '', text)

        # The raw feed has no line breaks between rows -- each row runs
        # straight into the next one (e.g. "...0.4652, 2026-08-24 16:05:00...").
        # Insert a newline right before each new row's timestamp so the
        # CSV parser can actually separate rows.
        text = re.sub(r'(?<!^)(?=\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},)', '\n', text)

        csv_data = io.StringIO(text)
        reader = csv.DictReader(csv_data, skipinitialspace=True)

        records = []
        for row in reader:
            ts_str = row.get("UTC", "").strip()
            if not ts_str:
                continue

            try:
                timestamp = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

            if timestamp < START_DATE:
                continue

            n1 = _to_float(row.get("N1 [cph]"))
            if n1 is not None:
                records.append({
                    "timestamp": timestamp.isoformat(),
                    "N1_cph": n1,
                    "source": "hydroinnova",
                })

        print(f"   Hydroinnova: {len(records)} records")
        return records
    except Exception as e:
        print(f"   Hydroinnova parsing failed: {e}")
        return []


# ---------------------------------------------------------------------------
# FINAPP - session handling
# ---------------------------------------------------------------------------

def fetch_finapp():
    """
    Log in to Finapp and fetch neutron data.

    Finapp is a Laravel + Inertia.js app. Its login does NOT use a classic
    HTML form post with a hidden _token field -- it uses a JSON API call,
    authenticated via a CSRF token that Laravel sets as an "XSRF-TOKEN"
    cookie on any page load. The browser (via axios) reads that cookie,
    URL-decodes it, and sends it back as an "X-XSRF-TOKEN" header on the
    login POST. This mirrors that exact flow.
    """
    if not (FINAPP_USERNAME and FINAPP_PASSWORD):
        print("FINAPP_USERNAME/PASSWORD not set")
        return []

    print("Fetching Finapp data...")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html, application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })

    try:
        print("   Step 1: Loading login page to obtain XSRF-TOKEN cookie...")
        login_page = session.get(FINAPP_LOGIN_URL, timeout=30)
        login_page.raise_for_status()
        print(f"   Login page loaded (status: {login_page.status_code})")
        print(f"   Cookies received: {list(session.cookies.keys())}")

        xsrf_cookie = session.cookies.get("XSRF-TOKEN")
        if not xsrf_cookie:
            print("   No XSRF-TOKEN cookie found after loading login page.")
            return []

        xsrf_token = urllib.parse.unquote(xsrf_cookie)
        print(f"   XSRF-TOKEN found: {xsrf_token[:20]}...")

        print("   Step 2: Logging in (JSON POST with X-XSRF-TOKEN header)...")
        login_resp = session.post(
            FINAPP_LOGIN_URL,
            json={
                "email": FINAPP_USERNAME,
                "password": FINAPP_PASSWORD,
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "text/html, application/xhtml+xml",
                "X-Requested-With": "XMLHttpRequest",
                "X-XSRF-TOKEN": xsrf_token,
                "Referer": FINAPP_LOGIN_URL,
                "Origin": FINAPP_BASE_URL,
            },
            timeout=30,
            allow_redirects=True,
        )

        print(f"   Login response: {login_resp.status_code}")
        print(f"   Final URL: {login_resp.url}")

        if login_resp.status_code >= 400:
            print(f"   Login request failed with status {login_resp.status_code}")
            print(f"   Response snippet: {login_resp.text[:500]}")
            return []

        if '/login' in login_resp.url:
            print("   Login failed - still on login page")
            print(f"   Response snippet: {login_resp.text[:500]}")
            return []

        print("   Login succeeded.")
        print("   Step 3: Fetching installation chart data...")

        data_resp = session.get(
            FINAPP_DATA_URL,
            headers={
                'Accept': 'application/json, text/plain, */*',
                'X-Requested-With': 'XMLHttpRequest',
            },
            timeout=30
        )
        data_resp.raise_for_status()
        print(f"   Data fetched: {data_resp.status_code}")
        print(f"   Content-Type: {data_resp.headers.get('Content-Type', 'unknown')}")

        return _parse_finapp_response(data_resp)

    except requests.exceptions.RequestException as e:
        print(f"   Request error: {e}")
        if hasattr(e, 'response') and e.response:
            print(f"   Response: {e.response.text[:200]}")
        return []
    except Exception as e:
        print(f"   Error: {e}")
        import traceback
        traceback.print_exc()
        return []


def _parse_finapp_response(resp):
    """
    Parse the /user/installation/get-charts/<id> response.

    Shape (confirmed from the live response):
      {
        "charts": [
          {
            "name": "Neutron Counts",
            "field": "neutrons_count_above_dynamic_threshold",
            "data": {
              "1": [
                {"datetime": "2026-08-20 08:14:52+00", "value": "90", "acquisition_time": 166},
                ...
              ]
            }
          },
          ... (other charts: Muon Counts, Hv Voltage, Pressure, etc.)
        ],
        "method": [...],
        "timezone": "Europe/Rome"
      }
    """
    print("   Parsing Finapp response...")

    try:
        payload = resp.json()
    except json.JSONDecodeError:
        snippet = resp.text[:1500]
        print("   Response was not JSON. First 1500 chars:")
        print(snippet)
        return []

    charts = payload.get("charts", [])
    neutron_chart = None
    for chart in charts:
        if chart.get("field") == FINAPP_NEUTRON_FIELD or chart.get("name") == "Neutron Counts":
            neutron_chart = chart
            break

    if not neutron_chart:
        available = [c.get("name") for c in charts]
        print(f"   Could not find a 'Neutron Counts' chart. Available charts: {available}")
        return []

    data_by_series = neutron_chart.get("data", {})
    if not data_by_series:
        print("   Neutron Counts chart has no data.")
        return []

    records = []
    for series_id, points in data_by_series.items():
        for point in points:
            ts_str = point.get("datetime")
            value = point.get("value")
            if not ts_str or value is None:
                continue

            try:
                # e.g. "2026-08-20 08:14:52+00" -> normalize the UTC offset
                ts_normalized = ts_str.strip()
                if ts_normalized.endswith("+00"):
                    ts_normalized = ts_normalized[:-3] + "+00:00"
                timestamp = datetime.fromisoformat(ts_normalized)
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

            if timestamp < START_DATE:
                continue

            n1 = _to_float(value)
            if n1 is not None:
                records.append({
                    "timestamp": timestamp.isoformat(),
                    "N1_cph": n1,
                    "source": "finapp",
                })

    records.sort(key=lambda r: r["timestamp"])
    print(f"   Finapp: {len(records)} records")
    return records


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _to_float(val):
    """Convert to float."""
    if val is None:
        return None
    try:
        if isinstance(val, str):
            val = val.strip().replace(',', '')
        return float(val)
    except (TypeError, ValueError):
        return None


def main():
    """Main execution."""
    print("Fetching neutron data...")
    print(f"Filtering from {START_DATE.date()}")

    hydro = fetch_hydroinnova()
    fin = fetch_finapp()

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "start_date": START_DATE.isoformat(),
        "hydroinnova": hydro,
        "finapp": fin,
    }

    try:
        os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
        with open(OUTPUT_PATH, "w") as f:
            json.dump(output, f, indent=2)

        print(f"\nSummary:")
        print(f"   - Hydroinnova: {len(hydro)} records")
        print(f"   - Finapp: {len(fin)} records")
        print(f"   - Output: {OUTPUT_PATH}")
        print("Done!")
    except Exception as e:
        print(f"Failed to write output: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
