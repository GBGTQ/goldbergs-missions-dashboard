"""
github_action_refresh.py
────────────────────────
Runs inside GitHub Actions (Ubuntu, no Chrome, no Windows).
Uses Playwright (headless browser) to log into Springshot, then fetches
30-day ATL data via the API, rebuilds the dashboard HTML, and saves it
to the repo root so GitHub Pages picks it up automatically.

Credentials are passed via environment variables (stored as GitHub Secrets):
    SPRINGSHOT_EMAIL
    SPRINGSHOT_PASSWORD

Dependencies (installed by workflow):
    pip install requests playwright
    playwright install chromium --with-deps
"""

import csv
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

# ── Paths (relative to repo root, where the Action checks out) ───────────────
REPO_ROOT      = Path(__file__).parent.parent
AUTO_DIR       = REPO_ROOT / "automation"
DASHBOARD_PATH = REPO_ROOT / "Missions_Operations_Dashboard.html"
MASTER_PATH    = REPO_ROOT / "MissionsSummary_master.csv"
BACKUP_DIR     = AUTO_DIR / "backups"

SPRINGSHOT_BASE = "https://webapp.springshot.com"
LOGIN_URL       = f"{SPRINGSHOT_BASE}/authentication/login"
API_PATTERN     = (
    "/CabinCleaningMissions/summaryWidget/5-9-84-10-176-52/487"
    "/refreshed/ajax/summary_widget/292"
    "?airportCode=ATL&startDate={start}&endDate={end}"
    "&jobs=M20-19:A20&missionTypes=5050-442-1009-699-443-4107-448"
)

def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ── Step 1: Login via Playwright (headless browser) ──────────────────────────
def springshot_login(email: str, password: str) -> requests.Session:
    """
    Use Playwright to log into Springshot in a headless browser.
    The SPA renders the login form via JavaScript (no CSRF token in raw HTML),
    so a plain HTTP POST doesn't work — Playwright handles the full JS render.

    After login succeeds we extract all cookies and transfer them into a
    requests.Session so the rest of the script can use plain HTTP calls.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log("ERROR: playwright not installed. Make sure the workflow installs it.")
        sys.exit(1)

    log("Launching headless browser …")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx     = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = ctx.new_page()

        # ── Navigate to login page ────────────────────────────────────────────
        log(f"Navigating to {LOGIN_URL} …")
        page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)

        # ── Fill in credentials — Springshot uses a two-step login ────────────
        # Step 1: fill username, click NEXT; Step 2: fill password, click SIGN IN
        username_sel = 'input[name="data[Login][username]"]'
        try:
            page.wait_for_selector(username_sel, timeout=10000)
        except PWTimeout:
            log("ERROR: Could not find username field on login page.")
            log(f"Page URL: {page.url}")
            log(f"Page title: {page.title()}")
            sys.exit(1)

        log("Filling username and clicking NEXT …")
        page.fill(username_sel, email)

        # NEXT button becomes enabled once text is entered
        try:
            page.wait_for_selector('button:has-text("NEXT")', timeout=5000)
            page.click('button:has-text("NEXT")', timeout=5000)
        except PWTimeout:
            log("ERROR: Could not find/click NEXT button.")
            log(f"Page URL: {page.url}")
            sys.exit(1)

        # Step 2: wait for password field, fill it
        pw_sel = 'input[name="data[Login][password]"]'
        try:
            page.wait_for_selector(pw_sel, timeout=10000)
        except PWTimeout:
            log("ERROR: Password field did not appear after clicking NEXT.")
            log(f"Page URL: {page.url}")
            sys.exit(1)

        log("Filling password …")
        page.fill(pw_sel, password)

        # ── Submit ────────────────────────────────────────────────────────────
        submit_selectors = [
            'button:has-text("SIGN IN")', 'button[type="submit"]',
            'input[type="submit"]', 'button:has-text("Login")',
            'button:has-text("Sign in")', 'button:has-text("Log in")',
        ]
        submitted = False
        for sel in submit_selectors:
            try:
                page.click(sel, timeout=3000)
                submitted = True
                break
            except PWTimeout:
                continue

        if not submitted:
            log("Submit button not found — pressing Enter …")
            page.keyboard.press("Enter")

        # ── Wait for redirect away from login page ────────────────────────────
        try:
            page.wait_for_url(
                lambda url: "authentication/login" not in url,
                timeout=15000,
            )
        except PWTimeout:
            log("ERROR: Still on login page after submit — check credentials.")
            log(f"Page URL: {page.url}")
            # Capture any visible error message on the page
            err_text = page.text_content("body") or ""
            err_lines = [l.strip() for l in err_text.split("\n") if l.strip()][:10]
            log("Page content (first 10 lines): " + str(err_lines))
            sys.exit(1)

        log(f"Login succeeded — landed at {page.url}")

        # ── Extract cookies into a requests.Session ───────────────────────────
        pw_cookies = ctx.cookies()
        browser.close()

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{SPRINGSHOT_BASE}/dashboard",
    })
    for c in pw_cookies:
        session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))

    log(f"Transferred {len(pw_cookies)} cookies to requests session.")
    return session


# ── Step 2: Fetch missions ────────────────────────────────────────────────────
def fetch_missions(session: requests.Session):
    end   = datetime.now()
    start = end - timedelta(days=30)
    fmt   = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S")

    url = SPRINGSHOT_BASE + API_PATTERN.format(start=fmt(start), end=fmt(end))
    log(f"Fetching ATL missions {fmt(start)} → {fmt(end)} …")

    resp = session.get(
        url,
        headers={
            "Accept":          "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer":         f"{SPRINGSHOT_BASE}/dashboard",
        },
        timeout=60,
    )

    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        log(f"API call failed: HTTP {resp.status_code}  content-type={resp.headers.get('content-type')}")
        log(f"Response preview: {resp.text[:300]}")
        sys.exit(1)

    missions = resp.json().get("data", [])
    log(f"Fetched {len(missions)} missions.")
    return missions, start, end


# ── Step 3: Build CSV ─────────────────────────────────────────────────────────
CSV_COLUMNS = [
    "Team Lead","Airline","Mission Type","Worksite","Asset",
    "Engagement","Productivity","Inbound Flight","Outbound Flight",
    "Asset Type","Location","Event",
    "Flight Arrival","Mission Assigned","Mission Accepted",
    "Team Arrival","Mission Started","Mission Completed","Flight Departure",
    "Security Search","Details","Mission Notes","Arrival Delay",
]

def _eng(m):
    s = (m.get("STATUS") or "").lower()
    if "complet" in s: return "100%"
    if any(x in s for x in ("cancel","skip","absent")): return "0%"
    if m.get("TEAM_ARRIVED_DATE") and m["TEAM_ARRIVED_DATE"] != "0000-00-00 00:00:00": return "100%"
    return "0%"

def _prod(m):
    v = m.get("OVERALL_SCORE")
    if v is None or v == "": return "N/A"
    try: return str(round(float(v))) + "%"
    except: return "N/A"

def _cell(v):
    if v is None or str(v) in ("","0000-00-00 00:00:00"): return ""
    s = str(v)
    return '"' + s.replace('"','""') + '"' if any(c in s for c in (',','"','\n')) else s

def missions_to_csv(missions):
    lines = [",".join(CSV_COLUMNS)]
    for m in missions:
        lead = (((m.get("LEAD_FIRST_NAME") or "") + " " + (m.get("LEAD_LAST_NAME") or "")).strip())
        sec  = ("-" if not m.get("HAS_SECURITY_SEARCH_TASKS")
                else ("Compliant" if m.get("SECURITY_SEARCH_IS_COMPLIANT") else "Non-compliant"))
        delay = str(round(m["ARRIVING_SEG_DELAY"])) if m.get("ARRIVING_SEG_DELAY") is not None else ""
        row = [
            lead, m.get("AIRLINE_CODE"), m.get("MISSION_TYPE_CODE"), m.get("AIRPORT_CODE"), m.get("TAIL_NUMBER"),
            _eng(m), _prod(m), m.get("ARRIVING_SEG_NUMBER"), m.get("DEPARTING_SEG_NUMBER"), m.get("VESSEL_DESCRIPTION"),
            m.get("AIRPORT_LOCATION"), m.get("EVENT_NAME") or "-",
            m.get("ARRIVAL_DATE_DISPLAY"), m.get("ASSIGNED_DATE_DISPLAY"), m.get("ACCEPTED_DATE_DISPLAY"),
            m.get("TEAM_ARRIVED_DATE_DISPLAY"), m.get("START_DATE_DISPLAY"),
            m.get("COMPLETED_DATE_DISPLAY"), m.get("DEPARTURE_DATE_DISPLAY"),
            sec, m.get("COMMENTS_NUMBER") or "", m.get("COMMENT_TEXT") or "", delay,
        ]
        lines.append(",".join(_cell(c) for c in row))
    return "﻿" + "\n".join(lines)


# ── Step 4: Merge + rebuild (reuses existing rebuild_dashboard.py) ────────────
def merge_and_rebuild(csv_text: str):
    sys.path.insert(0, str(AUTO_DIR))
    import rebuild_dashboard as rd
    rd.TEST_DIR       = REPO_ROOT
    rd.MASTER_PATH    = MASTER_PATH
    rd.DASHBOARD_PATH = DASHBOARD_PATH
    rd.BACKUP_DIR     = BACKUP_DIR

    # Write incoming CSV to a temp file
    tmp_csv = REPO_ROOT / "MissionsSummary_latest.csv"
    tmp_csv.write_text(csv_text, encoding="utf-8")

    added, skipped, total = rd.merge_incoming(tmp_csv)
    tmp_csv.unlink(missing_ok=True)

    master_rows = list(csv.DictReader(open(MASTER_PATH, encoding="utf-8-sig")))
    enriched = rd.enrich(master_rows)
    log(f"Enriched {len(enriched)} master records.")
    rd.rewrite_dashboard(enriched)
    return added, skipped, total


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    log("=" * 60)
    log(f"Springshot GitHub Actions Refresh — {datetime.now():%Y-%m-%d %H:%M UTC}")
    log("=" * 60)

    email    = os.environ.get("SPRINGSHOT_EMAIL", "").strip()
    password = os.environ.get("SPRINGSHOT_PASSWORD", "").strip()

    if not email or not password:
        log("ERROR: SPRINGSHOT_EMAIL and SPRINGSHOT_PASSWORD must be set as GitHub Secrets.")
        log("Go to: repo → Settings → Secrets and variables → Actions → New repository secret")
        sys.exit(1)

    # 1. Login
    session = springshot_login(email, password)

    # 2. Fetch
    missions, start, end = fetch_missions(session)
    non_zero = sum(1 for m in missions if m.get("ARRIVING_SEG_DELAY") and round(m["ARRIVING_SEG_DELAY"]) != 0)

    # 3. Build CSV + merge + rebuild
    csv_text = missions_to_csv(missions)
    added, skipped, total = merge_and_rebuild(csv_text)

    # 4. Stamp the actual Eastern Time into the dashboard's snapshot display
    from datetime import timezone
    from zoneinfo import ZoneInfo
    import re as _re
    _eastern = ZoneInfo("America/New_York")
    _now_et = datetime.now(timezone.utc).astimezone(_eastern)
    _snap = _now_et.strftime("%b %-d, %Y at %I:%M %p ") + _now_et.strftime("%Z")
    _html = DASHBOARD_PATH.read_text(encoding="utf-8")
    _html = _re.sub(r'(?<=id="snaptime">)[^<]*(?=<)', _snap, _html)
    DASHBOARD_PATH.write_text(_html, encoding="utf-8")
    log(f"Snapshot timestamp set to: {_snap}")

    log("=" * 60)
    log(f"DONE — {len(missions)} missions | {non_zero} delayed | +{added} new | {total} master total")
    log(f"Dashboard: {DASHBOARD_PATH}")
    log("=" * 60)


if __name__ == "__main__":
    main()
