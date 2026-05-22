"""
Goldberg's Group — Springshot ATL Dashboard Auto-Refresh
=========================================================
Reads your Chrome session cookies for springshot.com, hits the Springshot
summary API directly, generates the CSV, merges it into the master ledger,
and rebuilds Missions_Operations_Dashboard.html.

No Claude or browser automation required. Run via Task Scheduler or
double-click refresh_now.bat for an immediate refresh.

Requirements (installed by setup.bat):
    pip install requests pywin32 pycryptodome azure-storage-blob
"""

import base64
import csv
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import requests

# ── Paths ────────────────────────────────────────────────────────────────────
TEST_DIR   = Path(r"C:\Users\Tony Quach\OneDrive - Goldbergs Group\Desktop\TEST")
AUTO_DIR   = TEST_DIR / "automation"
LOG_PATH   = AUTO_DIR / "refresh.log"

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ── Chrome cookie reader ─────────────────────────────────────────────────────
def get_springshot_cookies() -> dict:
    """
    Read springshot.com cookies straight from Chrome's cookie database.
    Chrome must be installed and you must be (or have been) logged into
    Springshot in Chrome at some point so the session cookie exists.
    """
    try:
        import win32crypt
        from Crypto.Cipher import AES
    except ImportError as e:
        log.error("Missing package: %s  →  run setup.bat to install dependencies.", e)
        sys.exit(1)

    # ── Decrypt Chrome's master AES key (stored in Local State, wrapped with DPAPI)
    local_state_path = (
        Path(os.environ["LOCALAPPDATA"])
        / "Google" / "Chrome" / "User Data" / "Local State"
    )
    if not local_state_path.exists():
        log.error("Chrome Local State not found at %s", local_state_path)
        sys.exit(1)

    local_state = json.loads(local_state_path.read_text(encoding="utf-8"))
    enc_key_b64 = local_state["os_crypt"]["encrypted_key"]
    enc_key = base64.b64decode(enc_key_b64)[5:]          # strip "DPAPI" prefix
    aes_key = win32crypt.CryptUnprotectData(enc_key, None, None, None, 0)[1]

    # ── Locate the cookie SQLite database
    profile_base = Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data" / "Default"
    cookie_db = profile_base / "Network" / "Cookies"
    if not cookie_db.exists():
        cookie_db = profile_base / "Cookies"          # older Chrome layout
    if not cookie_db.exists():
        log.error("Chrome cookie database not found. Is Chrome installed?")
        sys.exit(1)

    # ── Copy DB to a temp file (Chrome keeps a write-lock on the original)
    tmp_db = tempfile.mktemp(suffix=".db")
    shutil.copy2(cookie_db, tmp_db)

    cookies: dict = {}
    try:
        conn = sqlite3.connect(tmp_db)
        rows = conn.execute(
            "SELECT name, encrypted_value FROM cookies WHERE host_key LIKE '%springshot.com%'"
        ).fetchall()
        conn.close()
    finally:
        try:
            os.unlink(tmp_db)
        except OSError:
            pass

    for name, enc_val in rows:
        try:
            if enc_val[:3] == b"v10":
                # Chrome 80+: AES-256-GCM
                nonce      = enc_val[3:15]
                ciphertext = enc_val[15:]
                plaintext  = AES.new(aes_key, AES.MODE_GCM, nonce=nonce).decrypt(ciphertext)
                value      = plaintext[:-16].decode("utf-8")   # strip 16-byte GCM tag
            else:
                # Legacy: DPAPI-encrypted directly
                value = win32crypt.CryptUnprotectData(enc_val, None, None, None, 0)[1].decode("utf-8")
            cookies[name] = value
        except Exception:
            pass   # skip individual cookies that fail to decrypt

    if not cookies:
        log.error(
            "No Springshot cookies found in Chrome. "
            "Please open Chrome, log into https://webapp.springshot.com, then re-run."
        )
        sys.exit(1)

    log.info("[auth] %d Springshot cookies loaded from Chrome", len(cookies))
    return cookies


# ── Springshot API fetch ─────────────────────────────────────────────────────
SPRINGSHOT_BASE = "https://webapp.springshot.com"
API_PATTERN = (
    "/CabinCleaningMissions/summaryWidget/5-9-84-10-176-52/487"
    "/refreshed/ajax/summary_widget/292"
    "?airportCode=ATL&startDate={start}&endDate={end}"
    "&jobs=M20-19:A20&missionTypes=5050-442-1009-699-443-4107-448"
)

def fetch_missions(cookies: dict):
    end   = datetime.now()
    start = end - timedelta(days=30)
    fmt   = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S")

    url = SPRINGSHOT_BASE + API_PATTERN.format(start=fmt(start), end=fmt(end))

    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update({
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":          "application/json, text/javascript, */*; q=0.01",
        "Referer":         SPRINGSHOT_BASE + "/dashboard",
        "X-Requested-With": "XMLHttpRequest",
    })

    log.info("[fetch] Requesting %s ... %s (ATL, 30 days)", fmt(start), fmt(end))
    resp = session.get(url, timeout=60)

    content_type = resp.headers.get("content-type", "")
    if resp.status_code != 200 or "json" not in content_type:
        log.error(
            "Springshot returned HTTP %s (%s). "
            "Session may have expired — log into Chrome and re-run.",
            resp.status_code, content_type,
        )
        sys.exit(1)

    missions = resp.json().get("data", [])
    log.info("[fetch] %d missions received", len(missions))
    return missions, start, end


# ── CSV builder (matches JS export column order exactly) ────────────────────
CSV_COLUMNS = [
    "Team Lead", "Airline", "Mission Type", "Worksite", "Asset",
    "Engagement", "Productivity", "Inbound Flight", "Outbound Flight",
    "Asset Type", "Location", "Event",
    "Flight Arrival", "Mission Assigned", "Mission Accepted",
    "Team Arrival", "Mission Started", "Mission Completed", "Flight Departure",
    "Security Search", "Details", "Mission Notes", "Arrival Delay",
]

def _eng_pct(m) -> str:
    s = (m.get("STATUS") or "").lower()
    if "complet" in s:
        return "100%"
    if any(x in s for x in ("cancel", "skip", "absent")):
        return "0%"
    if m.get("TEAM_ARRIVED_DATE") and m["TEAM_ARRIVED_DATE"] != "0000-00-00 00:00:00":
        return "100%"
    return "0%"

def _prod_pct(m) -> str:
    v = m.get("OVERALL_SCORE")
    if v is None or v == "":
        return "N/A"
    try:
        return str(round(float(v))) + "%"
    except (TypeError, ValueError):
        return "N/A"

def _fmt_cell(v) -> str:
    if v is None or str(v) in ("", "0000-00-00 00:00:00"):
        return ""
    s = str(v)
    if any(c in s for c in (",", '"', "\n")):
        return '"' + s.replace('"', '""') + '"'
    return s

def missions_to_csv(missions: list) -> str:
    lines = [",".join(CSV_COLUMNS)]
    for m in missions:
        lead = (
            ((m.get("LEAD_FIRST_NAME") or "") + " " + (m.get("LEAD_LAST_NAME") or "")).strip()
        )
        sec = (
            "-" if not m.get("HAS_SECURITY_SEARCH_TASKS")
            else ("Compliant" if m.get("SECURITY_SEARCH_IS_COMPLIANT") else "Non-compliant")
        )
        delay = (
            str(round(m["ARRIVING_SEG_DELAY"]))
            if m.get("ARRIVING_SEG_DELAY") is not None else ""
        )
        row = [
            lead,
            m.get("AIRLINE_CODE"), m.get("MISSION_TYPE_CODE"), m.get("AIRPORT_CODE"), m.get("TAIL_NUMBER"),
            _eng_pct(m), _prod_pct(m),
            m.get("ARRIVING_SEG_NUMBER"), m.get("DEPARTING_SEG_NUMBER"), m.get("VESSEL_DESCRIPTION"),
            m.get("AIRPORT_LOCATION"), m.get("EVENT_NAME") or "-",
            m.get("ARRIVAL_DATE_DISPLAY"), m.get("ASSIGNED_DATE_DISPLAY"), m.get("ACCEPTED_DATE_DISPLAY"),
            m.get("TEAM_ARRIVED_DATE_DISPLAY"), m.get("START_DATE_DISPLAY"),
            m.get("COMPLETED_DATE_DISPLAY"), m.get("DEPARTURE_DATE_DISPLAY"),
            sec,
            m.get("COMMENTS_NUMBER") or "", m.get("COMMENT_TEXT") or "",
            delay,
        ]
        lines.append(",".join(_fmt_cell(c) for c in row))

    # UTF-8 BOM so Excel opens it correctly
    return "﻿" + "\n".join(lines)


def save_csv(csv_text: str) -> Path:
    csv_path = TEST_DIR / "MissionsSummary_latest.csv"
    if csv_path.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(csv_path, TEST_DIR / f"MissionsSummary_latest_archive_{stamp}.csv")
    csv_path.write_text(csv_text, encoding="utf-8")
    log.info("[csv] Saved %s (%s bytes)", csv_path.name, f"{csv_path.stat().st_size:,}")
    return csv_path


# ── GitHub Pages upload ───────────────────────────────────────────────────────
def upload_to_github(html_path: Path) -> str | None:
    """
    Push the dashboard HTML to a GitHub repository via the Contents API.
    GitHub Pages then serves it at:
        https://<GITHUB_USERNAME>.github.io/<GITHUB_REPO>/<filename>

    No git installation required — uses the requests library only.
    Configure GITHUB_USERNAME, GITHUB_REPO, and GITHUB_TOKEN in github_config.py.
    """
    sys.path.insert(0, str(AUTO_DIR))
    try:
        from github_config import GITHUB_USERNAME, GITHUB_REPO, GITHUB_TOKEN
    except ImportError:
        log.warning("[github] github_config.py not found — skipping upload.")
        return None

    if not all([GITHUB_USERNAME, GITHUB_REPO, GITHUB_TOKEN]):
        log.info("[github] GitHub not configured in github_config.py — skipping upload.")
        return None

    import base64 as _b64

    filename    = html_path.name
    api_url     = f"https://api.github.com/repos/{GITHUB_USERNAME}/{GITHUB_REPO}/contents/{filename}"
    headers     = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept":        "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    content_b64 = _b64.b64encode(html_path.read_bytes()).decode("ascii")

    # Check if the file already exists (need its SHA to update it)
    sha = None
    check = requests.get(api_url, headers=headers, timeout=30)
    if check.status_code == 200:
        sha = check.json().get("sha")
    elif check.status_code not in (404,):
        log.error("[github] Could not check existing file: HTTP %s — %s", check.status_code, check.text[:200])
        return None

    payload = {
        "message": f"Dashboard refresh — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "content": content_b64,
        "branch":  "main",
    }
    if sha:
        payload["sha"] = sha   # required when updating an existing file

    resp = requests.put(api_url, headers=headers, json=payload, timeout=60)
    if resp.status_code in (200, 201):
        pub_url = f"https://{GITHUB_USERNAME}.github.io/{GITHUB_REPO}/{filename}"
        log.info("[github] Published → %s", pub_url)
        return pub_url
    else:
        log.error("[github] Upload failed: HTTP %s — %s", resp.status_code, resp.text[:300])
        return None


# ── Azure upload ─────────────────────────────────────────────────────────────
def upload_to_azure(html_path: Path) -> str | None:
    """
    Upload the dashboard HTML to Azure Blob Storage static website.
    Returns the public URL on success, or None if Azure is not configured.

    Requires azure_config.py to have AZURE_CONNECTION_STRING set.
    The storage account must have Static Website enabled:
        Azure Portal → Storage Account → Data Management → Static website
        → Enabled, Index document = Missions_Operations_Dashboard.html
    """
    sys.path.insert(0, str(AUTO_DIR))
    try:
        from azure_config import AZURE_CONNECTION_STRING, AZURE_STORAGE_ACCOUNT_NAME
    except ImportError:
        log.warning("[azure] azure_config.py not found — skipping upload.")
        return None

    if not AZURE_CONNECTION_STRING:
        log.info("[azure] No connection string configured — skipping upload.")
        return None

    try:
        from azure.storage.blob import BlobServiceClient, ContentSettings
    except ImportError:
        log.error("[azure] azure-storage-blob not installed — run setup.bat to fix.")
        return None

    try:
        client     = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
        container  = client.get_container_client("$web")   # static website container
        blob_name  = html_path.name

        with open(html_path, "rb") as f:
            container.upload_blob(
                name=blob_name,
                data=f,
                overwrite=True,
                content_settings=ContentSettings(content_type="text/html; charset=utf-8"),
            )

        # Build the public static-website URL
        if AZURE_STORAGE_ACCOUNT_NAME:
            # Azure static website URL format varies by region suffix;
            # we probe the account's primary endpoint from the service properties.
            props    = client.get_service_properties()
            acct_url = client.url.replace(".blob.", ".z13.web.")   # best-effort fallback
            pub_url  = acct_url.rstrip("/") + "/" + blob_name
        else:
            pub_url = "(configure AZURE_STORAGE_ACCOUNT_NAME in azure_config.py for URL)"

        log.info("[azure] Uploaded %s → %s", blob_name, pub_url)
        return pub_url

    except Exception as exc:
        log.error("[azure] Upload failed: %s", exc)
        return None


# ── Entry point ──────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("Springshot ATL Dashboard Refresh — %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    log.info("=" * 60)

    # Patch rebuild_dashboard to use Windows paths
    sys.path.insert(0, str(AUTO_DIR))
    import rebuild_dashboard as rd
    rd.TEST_DIR     = TEST_DIR
    rd.MASTER_PATH  = TEST_DIR / "MissionsSummary_master.csv"
    rd.DASHBOARD_PATH = TEST_DIR / "Missions_Operations_Dashboard.html"
    rd.BACKUP_DIR   = TEST_DIR / "automation" / "backups"

    # 1. Auth
    cookies = get_springshot_cookies()

    # 2. Fetch
    missions, start, end = fetch_missions(cookies)
    non_zero_delays = sum(
        1 for m in missions
        if m.get("ARRIVING_SEG_DELAY") is not None and round(m["ARRIVING_SEG_DELAY"]) != 0
    )

    # 3. CSV
    csv_path = save_csv(missions_to_csv(missions))

    # 4. Merge + rebuild
    added, skipped, total = rd.merge_incoming(csv_path)
    master_rows = list(csv.DictReader(open(rd.MASTER_PATH, encoding="utf-8-sig")))
    enriched = rd.enrich(master_rows)
    log.info("[run] enriched %d master records", len(enriched))
    rd.rewrite_dashboard(enriched)

    # 5. Publish (GitHub Pages preferred; Azure as fallback if configured)
    pub_url = upload_to_github(rd.DASHBOARD_PATH) or upload_to_azure(rd.DASHBOARD_PATH)

    log.info("=" * 60)
    log.info(
        "DONE — %d missions fetched | %d non-zero delays | %d new rows | %d master total",
        len(missions), non_zero_delays, added, total,
    )
    log.info("Dashboard (local): %s", rd.DASHBOARD_PATH)
    if pub_url:
        log.info("Dashboard (web):   %s", pub_url)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
