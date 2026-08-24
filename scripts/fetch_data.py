"""
Fetch neutron-count data from Hydroinnova (no auth) and Finapp (login required),
merge them, and write a single JSON file that the dashboard reads.
"""

import csv
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# Try multiple Hydroinnova URLs to find neutron data
HYDROINNOVA_URLS = [
    "http://nearfld.com/reguser/queryData.php?tz=1:00&vw=neutron1&IM=300534060129810&fn=Marchfeld",
    "http://nearfld.com/reguser/queryData.php?tz=1:00&vw=neutron&IM=300534060129810&fn=Marchfeld",
    "http://nearfld.com/reguser/queryData.php?tz=1:00&vw=raw&IM=300534060129810&fn=Marchfeld",
    "http://nearfld.com/reguser/queryData.php?tz=1:00&vw=soil_moisture&IM=300534060129810&fn=Marchfeld",
]

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
# HYDROINNOVA - Try multiple URLs
# ---------------------------------------------------------------------------

def fetch_hydroinnova():
    """Try multiple URLs to find neutron data from Hydroinnova."""
    print("📡 Fetching Hydroinnova data...")
    
    for i, url in enumerate(HYDROINNOVA_URLS, 1):
        print(f"   Trying URL {i}/{len(HYDROINNOVA_URLS)}: {url}")
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            
            records = _parse_hydroinnova_response(resp.text)
            if records:
                print(f"   ✅ Found {len(records)} neutron records from URL {i}")
                return records
            else:
                print(f"   ⚠️ No records found, trying next URL...")
                
        except Exception as e:
            print(f"   ❌ Error: {e}")
            continue
    
    print("   ❌ No neutron data found in any URL")
    return []


def _parse_hydroinnova_response(text):
    """Parse Hydroinnova response and extract neutron data."""
    # Check if it's CSV data
    if "UTC" not in text:
        return []
    
    # Extract CSV part
    text = text[text.index("UTC"):]
    text = re.sub(r'<[^>]+>', '', text)
    
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if len(lines) < 2:
        return []
    
    # Parse header
    header = [h.strip() for h in lines[0].split(',')]
    
    # Check if we have neutron columns
    has_n1 = any('N1' in h for h in header)
    has_n2 = any('N2' in h for h in header)
    
    if not has_n1:
        print(f"   ⚠️ No N1 column found. Header: {header}")
        return []
    
    records = []
    for line in lines[1:]:
        parts = [p.strip() for p in line.split(',')]
        if len(parts) < len(header):
            continue
        
        row = dict(zip(header, parts))
        
        # Get timestamp
        ts_str = row.get("UTC", "").strip()
        if not ts_str:
            continue
        
        try:
            timestamp = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        
        # Filter by date
        if timestamp < START_DATE:
            continue
        
        # Get N1 and N2 values
        n1 = None
        n2 = None
        
        for key, val in row.items():
            if 'N1' in key and val:
                n1 = _to_float(val)
            elif 'N2' in key and val:
                n2 = _to_float(val)
        
        # Only add if we have at least N1
        if n1 is not None:
            records.append({
                "timestamp": timestamp.isoformat(),
                "N1_cph": n1,
                "N2_cph": n2,
                "source": "hydroinnova",
            })
    
    return records


# ---------------------------------------------------------------------------
# FINAPP (simplified for now)
# ---------------------------------------------------------------------------

def fetch_finapp():
    """Log in to Finapp and fetch neutron count data."""
    if not (FINAPP_USERNAME and FINAPP_PASSWORD):
        print("⚠️ FINAPP_USERNAME / FINAPP_PASSWORD not set — skipping Finapp fetch.")
        return []

    print("📡 Fetching Finapp data...")
    print(f"   Username: {FINAPP_USERNAME[:3]}***")
    
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })

    try:
        # Step 1: Get login page with CSRF token
        print("   Step 1: Getting login page...")
        login_page = session.get(FINAPP_LOGIN_PAGE_URL, timeout=30)
        login_page.raise_for_status()
        print(f"   ✅ Login page loaded")

        # Extract CSRF token
        csrf_token = _extract_csrf_token(login_page.text)
        if not csrf_token:
            print("   ❌ Could not extract CSRF token")
            return []
        
        print(f"   ✅ CSRF token: {csrf_token[:20]}...")

        # Step 2: Login
        print("   Step 2: Logging in...")
        login_data = {
            "_token": csrf_token,
            "email": FINAPP_USERNAME,
            "password": FINAPP_PASSWORD,
        }
        
        login_resp = session.post(
            FINAPP_LOGIN_PAGE_URL,
            data=login_data,
            headers={
                "Referer": FINAPP_LOGIN_PAGE_URL,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30,
            allow_redirects=True,
        )
        login_resp.raise_for_status()
        print(f"   ✅ Login response: {login_resp.status_code}")
        print(f"   Current URL: {login_resp.url}")

        # Check if login succeeded
        if "/login" in login_resp.url:
            print("   ❌ Login failed - still on login page")
            return []

        # Step 3: Fetch data
        print("   Step 3: Fetching data...")
        data_resp = session.get(
            FINAPP_DATA_URL,
            headers={"Accept": "application/json"},
            timeout=30
        )
        data_resp.raise_for_status()
        print(f"   ✅ Data fetched")

        return _parse_finapp_response(data_resp)

    except requests.exceptions.RequestException as e:
        print(f"   ❌ Request error: {e}")
        return []
    except Exception as e:
        print(f"   ❌ Error: {e}")
        return []


def _extract_csrf_token(html):
    """Extract CSRF token from Laravel page."""
    meta_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', html, re.IGNORECASE)
    if meta_match:
        return meta_match.group(1)
    
    input_match = re.search(r'<input[^>]*name="_token"[^>]*value="([^"]+)"', html, re.IGNORECASE)
    if input_match:
        return input_match.group(1)
    
    return None


def _parse_finapp_response(resp):
    """Parse Finapp response with date filtering."""
    try:
        payload = resp.json()
        print(f"   ✅ JSON parsed successfully")
    except json.JSONDecodeError:
        print(f"   ❌ Not JSON")
        return []

    # Handle different response structures
    rows = []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ["data", "result", "records", "items"]:
            if key in payload and payload[key]:
                if isinstance(payload[key], list):
                    rows = payload[key]
                    break
    
    if not rows:
        print(f"   ⚠️ No data found in response")
        return []

    records = []
    for row in rows:
        if not row:
            continue
        
        # Extract timestamp
        ts_str = None
        for key in ["timestamp", "date", "created_at", "time"]:
            if key in row and row[key]:
                ts_str = row[key]
                break
        
        if not ts_str:
            continue
        
        try:
            if isinstance(ts_str, (int, float)):
                timestamp = datetime.fromtimestamp(ts_str, tz=timezone.utc)
            else:
                ts_str = str(ts_str).replace('Z', '+00:00')
                timestamp = datetime.fromisoformat(ts_str)
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            try:
                timestamp = datetime.strptime(str(ts_str), "%Y-%m-%d %H:%M:%S")
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        
        if timestamp < START_DATE:
            continue
        
        # Extract N1 and N2
        n1 = None
        n2 = None
        
        for key in ["N1", "n1", "neutron_count", "counts"]:
            if key in row and row[key] is not None:
                n1 = _to_float(row[key])
                break
        
        for key in ["N2", "n2", "neutron_count_2"]:
            if key in row and row[key] is not None:
                n2 = _to_float(row[key])
                break
        
        if n1 is not None:
            records.append({
                "timestamp": timestamp.isoformat(),
                "N1_cph": n1,
                "N2_cph": n2,
                "source": "finapp",
            })
    
    print(f"   ✅ Finapp: {len(records)} records from {START_DATE.date()}")
    return records


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _to_float(val):
    """Convert value to float, handling various formats."""
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
    print("🚀 Fetching neutron data...")
    print(f"📅 Filtering data from {START_DATE.date()} onwards")
    
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
        
        print(f"\n📊 Summary:")
        print(f"   - Hydroinnova: {len(hydro)} records")
        print(f"   - Finapp: {len(fin)} records")
        print(f"   - Output: {OUTPUT_PATH}")
        print("✅ Done!")
    except Exception as e:
        print(f"❌ Failed to write output: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
