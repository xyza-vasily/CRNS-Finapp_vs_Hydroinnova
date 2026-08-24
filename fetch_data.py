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
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

HYDROINNOVA_URL = (
    "http://nearfld.com/reguser/queryData.php"
    "?tz=1:00&vw=soil_moisture4&IM=300534060129810&fn=Marchfeld"
)

# Finapp runs on Laravel (confirmed via its CSRF meta tag + route naming),
# which has a standard login flow — see fetch_finapp() below.
FINAPP_BASE_URL = "https://data.finapptech.com"
FINAPP_LOGIN_PAGE_URL = f"{FINAPP_BASE_URL}/login"
FINAPP_DATA_URL = os.environ.get(
    "FINAPP_DATA_URL",
    f"{FINAPP_BASE_URL}/user/installation/info?idInstallation=10617&inst_name=IAEA%20SWMCNL",
)
FINAPP_USERNAME = os.environ.get("FINAPP_USERNAME", "")  # your Finapp login email
FINAPP_PASSWORD = os.environ.get("FINAPP_PASSWORD", "")

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "data.json")


# ---------------------------------------------------------------------------
# HYDROINNOVA (confirmed working, no login needed)
# ---------------------------------------------------------------------------

def fetch_hydroinnova():
    """Fetch and parse the Hydroinnova CSV-in-HTML feed into a list of records."""
    resp = requests.get(HYDROINNOVA_URL, timeout=30)
    resp.raise_for_status()
    text = resp.text

    # The page is basically raw CSV text (with a "Marchfeld" title glued on the
    # front). Strip HTML tags if any slipped through, then split into rows.
    text = re.sub(r"<[^>]+>", "", text)

    # Rows look like: "2026-08-24 08:05:00, 1704, 806, 0.0, 13.9, ..."
    # Split the header from data by finding the "UTC, N1" marker.
    if "UTC" in text:
        text = text[text.index("UTC"):]

    # Rows are concatenated without newlines in the raw fetch, but each row
    # starts with a date pattern. Insert newlines before each date pattern.
    text = re.sub(r"(?<!^)(?=\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},)", "\n", text)

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    header = [h.strip() for h in lines[0].split(",")]

    records = []
    for line in lines[1:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        row = dict(zip(header, parts))
        records.append({
            "timestamp": row.get("UTC"),
            "N1_cph": _to_float(row.get("N1 [cph]")),
            "N2_cph": _to_float(row.get("N2 [cph]")),
            "source": "hydroinnova",
        })
    return records


# ---------------------------------------------------------------------------
# FINAPP (placeholder — needs real login details)
# ---------------------------------------------------------------------------

def fetch_finapp():
    """
    Log in to Finapp (data.finapptech.com, a Laravel app) and fetch neutron
    count data for installation 10617 (IAEA SWMCNL).

    Laravel's standard session-login flow:
      1. GET the login page to obtain a CSRF token + session cookies.
      2. POST email/password + the CSRF token back to /login, in that session.
      3. Re-use the now-authenticated session to GET the data endpoint.
    """
    if not (FINAPP_USERNAME and FINAPP_PASSWORD):
        print("FINAPP_USERNAME / FINAPP_PASSWORD not set — skipping Finapp fetch.")
        return []

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    # --- Step 1: load the login page and extract the CSRF token -----------
    login_page = session.get(FINAPP_LOGIN_PAGE_URL, timeout=30)
    login_page.raise_for_status()

    csrf_token = _extract_csrf_token(login_page.text)
    if not csrf_token:
        raise RuntimeError(
            "Could not find a CSRF token on the Finapp login page — "
            "the login form's field names may differ from what this script expects. "
            "Inspect the <input name=\"_token\"> field on "
            f"{FINAPP_LOGIN_PAGE_URL} and adjust fetch_finapp() accordingly."
        )

    # --- Step 2: submit the login form -------------------------------------
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
            "Finapp login appears to have failed (still on the login page after "
            "POSTing credentials). Check FINAPP_USERNAME/FINAPP_PASSWORD, or the "
            "login form's field names may not be exactly 'email' / 'password'."
        )

    # --- Step 3: fetch the actual data using the authenticated session -----
    data_resp = session.get(FINAPP_DATA_URL, timeout=30)
    data_resp.raise_for_status()

    return _parse_finapp_response(data_resp)


def _extract_csrf_token(html):
    """Pull the CSRF token out of a Laravel page's <meta name="csrf-token"> tag
    or a hidden <input name="_token"> field, whichever is present."""
    meta_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    if meta_match:
        return meta_match.group(1)
    input_match = re.search(r'name="_token"\s+value="([^"]+)"', html)
    if input_match:
        return input_match.group(1)
    return None


def _parse_finapp_response(resp):
    """
    Parse the /user/installation/info response into a list of records.

    This endpoint's exact shape (JSON vs HTML table) isn't confirmed yet —
    this handles the JSON case (most likely, given the route naming) and
    falls back to raising a clear error with a snippet of the real response
    so we can adjust parsing once we see it.
    """
    content_type = resp.headers.get("Content-Type", "")

    if "application/json" in content_type:
        payload = resp.json()
        # Try a couple of plausible shapes; adjust once the real shape is known.
        rows = payload if isinstance(payload, list) else payload.get("data", [])
        records = []
        for row in rows:
            records.append({
                "timestamp": row.get("timestamp") or row.get("date") or row.get("created_at"),
                "N1_cph": _to_float(
                    row.get("N1") or row.get("neutron_count") or row.get("counts")
                ),
                "source": "finapp",
            })
        return records

    # Not JSON — dump a snippet so we can see the real structure and fix parsing.
    snippet = resp.text[:1500]
    raise RuntimeError(
        "Finapp data endpoint didn't return JSON as expected. "
        f"Content-Type was '{content_type}'. First 1500 chars of response:\n{snippet}"
    )


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def main():
    hydro = fetch_hydroinnova()
    fin = fetch_finapp()

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "hydroinnova": hydro,
        "finapp": fin,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote {len(hydro)} Hydroinnova records and {len(fin)} Finapp records to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
