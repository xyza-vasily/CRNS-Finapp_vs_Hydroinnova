"""
Fetch neutron-count data from Hydroinnova and Finapp.
"""

import csv
import io
import json
import os
import re
import sys
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
FINAPP_DATA_URL = (
    f"{FINAPP_BASE_URL}/user/installation/info"
    "?idInstallation=10617&inst_name=IAEA%20SWMCNL"
)
FINAPP_USERNAME = os.environ.get("FINAPP_USERNAME", "")
FINAPP_PASSWORD = os.environ.get("FINAPP_PASSWORD", "")

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "data.json")
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
    except Exception as e:
        print(f"   ❌ Hydroinnova request failed: {e}")
        return []

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
            if n1 is not None:
                records.append({
                    "timestamp": timestamp.isoformat(),
                    "N1_cph": n1,
                    "source": "hydroinnova",
                })
        
        print(f"   ✅ Hydroinnova: {len(records)} records")
        return records
    except Exception as e:
        print(f"   ❌ Hydroinnova parsing failed: {e}")
        return []


# ---------------------------------------------------------------------------
# FINAPP - Fixed session handling
# ---------------------------------------------------------------------------

def fetch_finapp():
    """Log in to Finapp and fetch neutron data with proper session handling."""
    if not (FINAPP_USERNAME and FINAPP_PASSWORD):
        print("⚠️ FINAPP_USERNAME/PASSWORD not set")
        return []

    print("📡 Fetching Finapp data...")
    
    # Create session with browser-like headers
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })

    try:
        # Step 1: Get login page and CSRF token
        print("   Step 1: Getting login page...")
        login_page = session.get(FINAPP_LOGIN_URL, timeout=30)
        login_page.raise_for_status()
        print(f"   ✅ Login page loaded (status: {login_page.status_code})")

        # Extract CSRF token using BeautifulSoup
        soup = BeautifulSoup(login_page.text, 'html.parser')
        
        # Try multiple ways to find CSRF token
        csrf_token = None
        
        # Method 1: Meta tag
        meta_tag = soup.find('meta', {'name': 'csrf-token'})
        if meta_tag:
            csrf_token = meta_tag.get('content')
        
        # Method 2: Hidden input
        if not csrf_token:
            token_input = soup.find('input', {'name': '_token'})
            if token_input:
                csrf_token = token_input.get('value')
        
        # Method 3: Find any input with token
        if not csrf_token:
            for input_tag in soup.find_all('input'):
                if 'token' in str(input_tag).lower():
                    csrf_token = input_tag.get('value')
                    break
        
        if not csrf_token:
            print("   ❌ Could not find CSRF token")
            # Save page for debugging
            with open("/tmp/finapp_login.html", "w") as f:
                f.write(login_page.text)
            print("   📄 Saved login page to /tmp/finapp_login.html")
            return []

        print(f"   ✅ CSRF token found: {csrf_token[:20]}...")

        # Step 2: Login
        print("   Step 2: Logging in...")
        
        # Prepare login data
        login_data = {
            '_token': csrf_token,
            'email': FINAPP_USERNAME,
            'password': FINAPP_PASSWORD,
        }
        
        # Add remember me if needed
        if soup.find('input', {'name': 'remember'}):
            login_data['remember'] = 'on'

        # Submit login
        login_resp = session.post(
            FINAPP_LOGIN_URL,
            data=login_data,
            headers={
                'Referer': FINAPP_LOGIN_URL,
                'Content-Type': 'application/x-www-form-urlencoded',
            },
            timeout=30,
            allow_redirects=True,
        )
        login_resp.raise_for_status()
        
        print(f"   ✅ Login response: {login_resp.status_code}")
        print(f"   📍 Final URL: {login_resp.url}")

        # Check if login succeeded
        if '/login' in login_resp.url:
            print("   ❌ Login failed - still on login page")
            print(f"   📄 Response snippet: {login_resp.text[:300]}")
            return []

        # Step 3: Get the installation data
        print("   Step 3: Fetching installation data...")
        
        # First, get the main page to establish session
        main_resp = session.get(
            f"{FINAPP_BASE_URL}/user/dashboard",
            timeout=30
        )
        main_resp.raise_for_status()
        print(f"   ✅ Dashboard loaded: {main_resp.status_code}")

        # Now get the specific installation data
        data_resp = session.get(
            FINAPP_DATA_URL,
            headers={
                'Accept': 'application/json, text/html, */*',
                'X-Requested-With': 'XMLHttpRequest',
            },
            timeout=30
        )
        data_resp.raise_for_status()
        print(f"   ✅ Data fetched: {data_resp.status_code}")
        print(f"   📄 Content-Type: {data_resp.headers.get('Content-Type', 'unknown')}")

        # Parse the response
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


def _parse_finapp_response(resp):
    """Parse Finapp response."""
    print("   🔍 Parsing Finapp response...")
    
    # Try JSON first
    try:
        payload = resp.json()
        print(f"   ✅ JSON parsed")
        print(f"   📊 Type: {type(payload)}")
        if isinstance(payload, dict):
            print(f"   📊 Keys: {list(payload.keys())}")
    except json.JSONDecodeError:
        # Try HTML parsing
        print("   📄 Response is HTML, trying to parse tables...")
        return _parse_finapp_html(resp.text)
    
    # Handle JSON response
    rows = []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        # Look for data in common locations
        for key in ['data', 'result', 'records', 'items', 'installation', 'readings']:
            if key in payload and payload[key]:
                if isinstance(payload[key], list):
                    rows = payload[key]
                    print(f"   📊 Found list in '{key}'")
                    break
                elif isinstance(payload[key], dict):
                    # Check if there's a nested data array
                    if 'data' in payload[key]:
                        rows = payload[key]['data']
                        print(f"   📊 Found data in '{key}.data'")
                        break
    
    if not rows:
        print("   ⚠️ No data rows found")
        # Save response for debugging
        with open("/tmp/finapp_response.json", "w") as f:
            json.dump(payload, f, indent=2)
        print("   💾 Saved response to /tmp/finapp_response.json")
        return []
    
    records = []
    for row in rows:
        if not row:
            continue
        
        # Extract timestamp
        ts_str = None
        for key in ['timestamp', 'date', 'created_at', 'time', 'reading_time']:
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
                if 'T' in ts_str:
                    timestamp = datetime.fromisoformat(ts_str)
                else:
                    timestamp = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        
        if timestamp < START_DATE:
            continue
        
        # Extract N1 value
        n1 = None
        for key in ['N1', 'n1', 'neutron_count', 'counts', 'fast_neutrons', 'value']:
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
    
    print(f"   ✅ Finapp: {len(records)} records")
    return records


def _parse_finapp_html(html):
    """Parse Finapp data from HTML table."""
    print("   🔍 Parsing HTML table...")
    
    try:
        soup = BeautifulSoup(html, 'html.parser')
        
        # Find tables
        tables = soup.find_all('table')
        if not tables:
            print("   ❌ No tables found in HTML")
            return []
        
        print(f"   📊 Found {len(tables)} tables")
        
        # Look for tables with neutron data
        for table in tables:
            rows = table.find_all('tr')
            if len(rows) < 2:
                continue
            
            # Check headers for neutron-related columns
            headers = [th.get_text().strip() for th in rows[0].find_all(['th', 'td'])]
            print(f"   📊 Headers: {headers}")
            
            n1_idx = -1
            time_idx = -1
            
            for i, h in enumerate(headers):
                if 'n1' in h.lower() or 'neutron' in h.lower() or 'count' in h.lower():
                    n1_idx = i
                if 'time' in h.lower() or 'date' in h.lower() or 'timestamp' in h.lower():
                    time_idx = i
            
            if n1_idx == -1 or time_idx == -1:
                continue
            
            records = []
            for row in rows[1:]:
                cols = row.find_all(['td', 'th'])
                if len(cols) <= max(n1_idx, time_idx):
                    continue
                
                ts_str = cols[time_idx].get_text().strip()
                n1_str = cols[n1_idx].get_text().strip()
                
                if not ts_str or not n1_str:
                    continue
                
                try:
                    timestamp = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                
                if timestamp < START_DATE:
                    continue
                
                n1 = _to_float(n1_str)
                if n1 is not None:
                    records.append({
                        "timestamp": timestamp.isoformat(),
                        "N1_cph": n1,
                        "source": "finapp",
                    })
            
            if records:
                print(f"   ✅ Found {len(records)} records from HTML table")
                return records
        
        return []
    except Exception as e:
        print(f"   ❌ HTML parsing failed: {e}")
        return []


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
    print("🚀 Fetching neutron data...")
    print(f"📅 Filtering from {START_DATE.date()}")
    
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
        print(f"   - Hydroinnova: {len(hydro)} records ✅")
        print(f"   - Finapp: {len(fin)} records")
        print(f"   - Output: {OUTPUT_PATH}")
        print("✅ Done!")
    except Exception as e:
        print(f"❌ Failed to write output: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
