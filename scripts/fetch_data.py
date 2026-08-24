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

    # Parse the CSV data directly
    try:
        text = resp.text
        if "UTC" in text:
            text = text[text.index("UTC"):]
        
        text = re.sub(r'<[^>]+>', '', text)
        
        csv_data = io.StringIO(text)
        reader = csv.DictReader(csv_data)
        
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
            n2 = _to_float(row.get("N2 [cph]"))
            
            if n1 is not None:
                records.append({
                    "timestamp": timestamp.isoformat(),
                    "N1_cph": n1,
                    "N2_cph": n2,
                    "source": "hydroinnova",
                })
        
        print(f"   ✅ Hydroinnova: {len(records)} records from {START_DATE.date()}")
        return records
        
    except Exception as e:
        print(f"   ❌ Hydroinnova parsing failed: {e}")
        return []


# ---------------------------------------------------------------------------
# FINAPP - WITH BETTER DEBUGGING
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

        # DEBUG: Save the login page HTML to see what's on it
        with open("/tmp/finapp_login.html", "w") as f:
            f.write(login_page.text)
        print("   📄 Login page saved to /tmp/finapp_login.html")

        # Extract CSRF token - try multiple methods
        csrf_token = _extract_csrf_token(login_page.text)
        if not csrf_token:
            print("   ❌ Could not extract CSRF token")
            print("   🔍 Looking for CSRF patterns in page...")
            
            # Search for common patterns
            if 'csrf' in login_page.text.lower():
                print("   🔍 'csrf' found in page - checking around it:")
                import re
                for match in re.finditer(r'.{0,50}csrf.{0,50}', login_page.text, re.IGNORECASE):
                    print(f"      ...{match.group()}...")
            
            # Check if it's a different login system
            if 'laravel' in login_page.text.lower():
                print("   ℹ️  Page appears to be Laravel")
            if 'finapp' in login_page.text.lower():
                print("   ℹ️  Page appears to be Finapp")
            
            return []
        
        print(f"   ✅ CSRF token: {csrf_token[:20]}...")

        # Step 2: Login
        print("   Step 2: Logging in...")
        
        # Try to detect the actual form field names
        form_fields = _detect_login_fields(login_page.text)
        print(f"   🔍 Detected form fields: {form_fields}")
        
        login_data = {
            "_token": csrf_token,
        }
        
        # Map detected fields to our credentials
        if "email" in form_fields:
            login_data["email"] = FINAPP_USERNAME
        elif "username" in form_fields:
            login_data["username"] = FINAPP_USERNAME
        elif "login" in form_fields:
            login_data["login"] = FINAPP_USERNAME
        else:
            # Default to email
            login_data["email"] = FINAPP_USERNAME
        
        if "password" in form_fields:
            login_data["password"] = FINAPP_PASSWORD
        
        print(f"   📤 Login payload: { {k: v[:3]+'***' if k != '_token' else v[:20]+'...' for k,v in login_data.items()} }")
        
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
        print(f"   📍 Current URL: {login_resp.url}")

        # Check if login succeeded
        if "/login" in login_resp.url:
            print("   ❌ Login failed - still on login page")
            print(f"   📄 Response snippet: {login_resp.text[:300]}")
            return []

        # Step 3: Fetch data
        print("   Step 3: Fetching data...")
        
        # Try with and without Accept header
        headers = {"Accept": "application/json"}
        data_resp = session.get(
            FINAPP_DATA_URL,
            headers=headers,
            timeout=30
        )
        data_resp.raise_for_status()
        print(f"   ✅ Data fetched (status: {data_resp.status_code})")
        print(f"   📄 Content-Type: {data_resp.headers.get('Content-Type', 'unknown')}")

        return _parse_finapp_response(data_resp)

    except requests.exceptions.RequestException as e:
        print(f"   ❌ Request error: {e}")
        if hasattr(e, 'response') and e.response:
            print(f"   📄 Response: {e.response.text[:200]}")
        return []
    except Exception as e:
        print(f"   ❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return []


def _extract_csrf_token(html):
    """Extract CSRF token from Laravel page using multiple methods."""
    # Method 1: Meta tag
    meta_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', html, re.IGNORECASE)
    if meta_match:
        return meta_match.group(1)
    
    # Method 2: Input field
    input_match = re.search(r'<input[^>]*name="_token"[^>]*value="([^"]+)"', html, re.IGNORECASE)
    if input_match:
        return input_match.group(1)
    
    # Method 3: JavaScript variable
    js_match = re.search(r'csrfToken\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE)
    if js_match:
        return js_match.group(1)
    
    # Method 4: Any _token pattern
    token_match = re.search(r'_token["\']?\s*[:=]\s*["\']([^"\']+)["\']', html, re.IGNORECASE)
    if token_match:
        return token_match.group(1)
    
    return None


def _detect_login_fields(html):
    """Detect form field names for login."""
    fields = []
    
    # Look for input fields in forms
    form_matches = re.findall(r'<input[^>]*name="([^"]+)"[^>]*>', html, re.IGNORECASE)
    for name in form_matches:
        if name.lower() in ['email', 'username', 'login', 'password', '_token']:
            fields.append(name)
    
    return fields


def _parse_finapp_response(resp):
    """Parse Finapp response with date filtering."""
    content_type = resp.headers.get("Content-Type", "")
    print(f"   🔍 Parsing response (Content-Type: {content_type})")

    # Try to parse JSON
    try:
        payload = resp.json()
        print(f"   ✅ JSON parsed successfully")
        print(f"   📊 Response structure: {type(payload)}")
        if isinstance(payload, dict):
            print(f"   📊 Top-level keys: {list(payload.keys())}")
    except json.JSONDecodeError as e:
        print(f"   ❌ Not JSON: {e}")
        print(f"   📄 Response snippet: {resp.text[:300]}")
        return []

    # Handle different response structures
    rows = []
    if isinstance(payload, list):
        rows = payload
        print(f"   📊 Found list with {len(rows)} items")
    elif isinstance(payload, dict):
        # Try common Laravel patterns
        for key in ["data", "result", "records", "items", "installation"]:
            if key in payload and payload[key]:
                if isinstance(payload[key], list):
                    rows = payload[key]
                    print(f"   📊 Found list in '{key}' with {len(rows)} items")
                    break
                elif isinstance(payload[key], dict) and "data" in payload[key]:
                    rows = payload[key]["data"]
                    print(f"   📊 Found data in '{key}.data' with {len(rows)} items")
                    break
    
    # If still empty, check if payload itself is the record
    if not rows and any(k in payload for k in ["timestamp", "date", "created_at", "N1", "neutron_count"]):
        rows = [payload]
        print(f"   📊 Using payload as single record")

    if not rows:
        print(f"   ⚠️ No data rows found")
        return []

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
        
        # Extract N1
        n1 = None
        for key in ["N1", "n1", "neutron_count", "counts", "fast_neutrons"]:
            if key in row and row[key] is not None:
                n1 = _to_float(row[key])
                if n1 is not None:
                    break
        
        if n1 is not None:
            records.append({
                "timestamp": timestamp.isoformat(),
                "N1_cph": n1,
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
