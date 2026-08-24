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
    print("📡 Fetching Hydroinnova data...")
    try:
        resp = requests.get(HYDROINNOVA_URL, timeout=30)
        resp.raise_for_status()
        print(f"   ✅ Hydroinnova response: {resp.status_code}")
    except Exception as e:
        print(f"   ❌ Hydroinnova request failed: {e}")
        return []

    # The response might have HTML tags or extra text
    text = resp.text
    
    # Try to find the CSV part
    if "UTC" in text:
        # Extract from the first "UTC" to the end
        text = text[text.index("UTC"):]
    
    # Clean up - remove any HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    
    # Split into lines
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    
    if not lines:
        print("   ❌ No data found in Hydroinnova response")
        return []
    
    # First line should be header
    header = [h.strip() for h in lines[0].split(',')]
    print(f"   Header: {header}")
    
    records = []
    for line in lines[1:]:
        # Handle rows that might have leading/trailing spaces
        parts = [p.strip() for p in line.split(',')]
        if len(parts) < len(header):
            continue
        
        # Build dict with header mapping
        row = {}
        for i, key in enumerate(header):
            if i < len(parts):
                row[key] = parts[i]
        
        # Get timestamp
        ts_str = row.get("UTC", "").strip()
        if not ts_str:
            continue
        
        try:
            # Try different timestamp formats
            timestamp = None
            for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"]:
                try:
                    timestamp = datetime.strptime(ts_str, fmt)
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            
            if timestamp is None:
                continue
                
        except Exception:
            continue
        
        # Date filter
        if timestamp < START_DATE:
            continue
        
        # Get N1 and N2 values
        n1 = row.get("N1 [cph]") or row.get("N1") or row.get("n1")
        n2 = row.get("N2 [cph]") or row.get("N2") or row.get("n2")
        
        records.append({
            "timestamp": timestamp.isoformat(),
            "N1_cph": _to_float(n1),
            "N2_cph": _to_float(n2),
            "source": "hydroinnova",
        })
    
    print(f"   ✅ Hydroinnova: {len(records)} records from {START_DATE.date()}")
    return records


# ---------------------------------------------------------------------------
# FINAPP
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
        print(f"   ✅ Login page loaded (status: {login_page.status_code})")

        # Extract CSRF token from page
        csrf_token = _extract_csrf_token(login_page.text)
        if not csrf_token:
            print("   ❌ Could not extract CSRF token")
            print(f"   Page snippet: {login_page.text[:500]}")
            return []
        
        print(f"   ✅ CSRF token: {csrf_token[:20]}...")

        # Step 2: Login
        print("   Step 2: Logging in...")
        login_data = {
            "_token": csrf_token,
            "email": FINAPP_USERNAME,
            "password": FINAPP_PASSWORD,
        }
        
        # Try with different form field names
        if 'name="email"' not in login_page.text:
            # Maybe they use different field names
            if 'name="username"' in login_page.text:
                login_data["username"] = login_data.pop("email")
            elif 'name="login"' in login_page.text:
                login_data["login"] = login_data.pop("email")
        
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
            print(f"   Response contains: {login_resp.text[:200]}")
            return []

        # Step 3: Fetch data
        print("   Step 3: Fetching data...")
        data_resp = session.get(
            FINAPP_DATA_URL,
            headers={"Accept": "application/json"},
            timeout=30
        )
        data_resp.raise_for_status()
        print(f"   ✅ Data fetched (status: {data_resp.status_code})")
        print(f"   Content-Type: {data_resp.headers.get('Content-Type', 'unknown')}")

        return _parse_finapp_response(data_resp)

    except requests.exceptions.RequestException as e:
        print(f"   ❌ Request error: {e}")
        if hasattr(e, 'response') and e.response:
            print(f"   Response: {e.response.text[:200]}")
        return []
    except Exception as e:
        print(f"   ❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return []


def _extract_csrf_token(html):
    """Extract CSRF token from Laravel page."""
    # Try meta tag first
    meta_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', html, re.IGNORECASE)
    if meta_match:
        return meta_match.group(1)
    
    # Try input field
    input_match = re.search(r'<input[^>]*name="_token"[^>]*value="([^"]+)"', html, re.IGNORECASE)
    if input_match:
        return input_match.group(1)
    
    # Try JavaScript variable
    js_match = re.search(r'csrfToken\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE)
    if js_match:
        return js_match.group(1)
    
    # Try any _token pattern
    token_match = re.search(r'_token["\']?\s*[:=]\s*["\']([^"\']+)["\']', html, re.IGNORECASE)
    if token_match:
        return token_match.group(1)
    
    return None


def _parse_finapp_response(resp):
    """Parse Finapp response with date filtering."""
    content_type = resp.headers.get("Content-Type", "")
    print(f"   Parsing Finapp response...")

    # Try to parse JSON
    try:
        payload = resp.json()
        print(f"   ✅ JSON parsed successfully")
    except json.JSONDecodeError:
        print(f"   ❌ Not JSON, Content-Type: {content_type}")
        print(f"   Response snippet: {resp.text[:300]}")
        return []

    # Handle different response structures
    rows = []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        # Try common Laravel patterns
        for key in ["data", "result", "records", "items", "installation"]:
            if key in payload and payload[key]:
                if isinstance(payload[key], list):
                    rows = payload[key]
                    break
                elif isinstance(payload[key], dict) and "data" in payload[key]:
                    rows = payload[key]["data"]
                    break
    
    # If still empty, check if payload itself is the record
    if not rows and any(k in payload for k in ["timestamp", "date", "created_at", "N1", "neutron_count"]):
        rows = [payload]
    
    print(f"   Found {len(rows)} rows to process")

    records = []
    for row in rows:
        if not row or not isinstance(row, dict):
            continue
        
        # Extract timestamp
        ts_str = None
        for key in ["timestamp", "date", "created_at", "updated_at", "time", "datetime"]:
            if key in row and row[key]:
                ts_str = row[key]
                break
        
        if not ts_str:
            continue
        
        # Parse timestamp
        try:
            if isinstance(ts_str, (int, float)):
                # Unix timestamp
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
        
        # Apply date filter
        if timestamp < START_DATE:
            continue
        
        # Extract N1 and N2
        n1 = None
        n2 = None
        
        for key in ["N1", "n1", "neutron_count", "counts", "fast_neutrons"]:
            if key in row and row[key] is not None:
                n1 = row[key]
                break
        
        for key in ["N2", "n2", "neutron_count_2", "thermal_neutrons"]:
            if key in row and row[key] is not None:
                n2 = row[key]
                break
        
        records.append({
            "timestamp": timestamp.isoformat(),
            "N1_cph": _to_float(n1),
            "N2_cph": _to_float(n2),
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
