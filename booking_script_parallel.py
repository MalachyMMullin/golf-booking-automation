"""Golf booking automation - parallel multi-user version.

Runs 6 Chrome workers simultaneously. All enter the 6:30pm draw independently
(maximising queue position odds). Once the tee sheet opens, all race to secure
the 4-ball slot; the winner adds partners by member-number search.  The second
worker through secures the 2-ball.  The rest exit cleanly.

Coordination uses multiprocessing.Manager events so workers on the same process
can signal each other without any external service.

CONFIG (top of file) ─ update before each season if group changes:
  FOUR_BALL_MEMBERS  – member numbers for the 4-person group (order doesn't matter)
  TWO_BALL_MEMBERS   – member numbers for the 2-person group
  ALL_USERS          – credentials for every account that will enter the draw
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import random
import re

import subprocess
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from multiprocessing.managers import SyncManager
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import zoneinfo
    SYDNEY_TZ = zoneinfo.ZoneInfo("Australia/Sydney")
except Exception:
    SYDNEY_TZ = timezone.utc

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoAlertPresentException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG  ← edit these before the session
# ─────────────────────────────────────────────────────────────────────────────

# Member numbers for fixed groups.
FOUR_BALL_MEMBERS: List[str] = ["2007", "2008", "2009", "2010"]
# Mullin=2007, Hillard=2008, Rutherford=2009, Rudge=2010

TWO_BALL_MEMBERS: List[str] = ["1101", "1107"]
# Lalor=1101, Cheney=1107

# Player surnames for tee-sheet verification (used after booking to confirm)
ALL_PLAYER_SURNAMES = ["Mullin", "Hillard", "Rutherford", "Rudge", "Lalor", "Cheney"]

# Member number → surname mapping (used for partner selection and error detection)
MEMBER_TO_SURNAME = {
    "2007": "Mullin",  "2008": "Hillard",
    "2009": "Rutherford", "2010": "Rudge",
    "1101": "Lalor",   "1107": "Cheney",
}
SURNAME_TO_MEMBER = {v.lower(): k for k, v in MEMBER_TO_SURNAME.items()}

# First names for Discord notifications (friendlier than surnames)
MEMBER_TO_FIRST = {
    "2007": "Malachy", "2008": "Gareth",
    "2009": "Tommy",   "2010": "Dan",
    "1101": "Lalor",   "1107": "Cheney",
}

# All accounts that will enter the draw (credentials from env vars or defaults)
# Worker order: 2-ball members interleaved early so they're on tee sheet
# in time to grab the 2-ball after the 4-ball winner is decided.
# Stagger: 0s, 2m, 4m, 6m, 8m, 10m → all logged in by ~6:10pm
ALL_USERS = [
    {"username": os.getenv("MIGOLF_USER_1", "2007"), "password": os.getenv("MIGOLF_PASS_1", "Golf123#")},
    {"username": os.getenv("MIGOLF_USER_2", "1101"), "password": os.getenv("MIGOLF_PASS_2", "Golf123#")},
    {"username": os.getenv("MIGOLF_USER_3", "2008"), "password": os.getenv("MIGOLF_PASS_3", "Golf123#")},
    {"username": os.getenv("MIGOLF_USER_4", "1107"), "password": os.getenv("MIGOLF_PASS_4", "Golf123#")},
    {"username": os.getenv("MIGOLF_USER_5", "2009"), "password": os.getenv("MIGOLF_PASS_5", "Golf123#")},
    {"username": os.getenv("MIGOLF_USER_6", "2010"), "password": os.getenv("MIGOLF_PASS_6", "Golf123#")},
]

# ─────────────────────────────────────────────────────────────────────────────
# URLS & TIMING
# ─────────────────────────────────────────────────────────────────────────────
LOGIN_URL      = "https://macquarielinks.miclub.com.au/security/login.msp"
EVENT_LIST_URL = "https://macquarielinks.miclub.com.au/views/members/booking/eventList.xhtml"
HOME_URL       = "https://macquarielinks.miclub.com.au/views/members/home.xhtml"
LOGOUT_URL     = "https://macquarielinks.miclub.com.au/security/logout.msp"

LOGIN_TIME        = (18,  0)  # login at 6:00pm Sydney — all workers logged in by ~6:10pm
QUEUE_JOIN_TIME   = (18, 30)  # ballot opens at 6:30pm — click event link here
BOOKING_OPEN_TIME = (19,  0)  # tee sheet releases at 7:00pm
HARD_TIMEOUT_TIME = (20,  0)  # give up at 8:00pm — no earlier

LOGIN_STAGGER_SECS = int(os.getenv("LOGIN_STAGGER_SECS", "120"))      # default 2 min between workers; override via env
MAX_LOGIN_RETRIES  = 8        # up from 3
LOGIN_BASE_BACKOFF = 30       # seconds (up from 10)
LOGIN_MAX_BACKOFF  = 300      # 5-min cap

OPEN_POLL_INTERVAL = 15  # seconds between event-list refreshes before draw (6 workers × 15s = low load)
KEEPALIVE_INTERVAL = 300  # seconds between session keepalive navigations (5 min)
BOOKING_MAX_ATTEMPTS = 999  # effectively unlimited — hard deadline is HARD_TIMEOUT_TIME

# Anti-detection: diverse browser fingerprints
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]

WINDOW_SIZES = [
    (1920, 1080),
    (1366, 768),
    (1440, 900),
    (1536, 864),
    (1280, 720),
    (1600, 900),
]

# Discord notifications (bot token + channel)
DISCORD_BOT_TOKEN  = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID", "1481476007176306708")

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING  (per-worker files)
# ─────────────────────────────────────────────────────────────────────────────
RUN_ROOT_ENV = os.getenv("GOLFBOT_RUN_ROOT")
RUN_ROOT = Path(RUN_ROOT_ENV).expanduser() if RUN_ROOT_ENV else Path.home() / "golfbot_logs"
RUN_ROOT.mkdir(parents=True, exist_ok=True)
RUN_ID   = datetime.now().strftime("parallel_%Y-%m-%d_%H-%M-%S")
RUN_DIR  = RUN_ROOT / RUN_ID
RUN_DIR.mkdir(parents=True, exist_ok=True)


def make_worker_logger(username: str) -> logging.Logger:
    logger = logging.getLogger(f"worker_{username}")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(f"[%(asctime)s][{username}] %(message)s", datefmt="%H:%M:%S")
    # File handler
    fh = logging.FileHandler(RUN_DIR / f"worker_{username}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    # Console (shows up in GitHub Actions log)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def now_sydney() -> datetime:
    return datetime.now(timezone.utc).astimezone(SYDNEY_TZ)


def hard_deadline_sydney() -> float:
    """Unix timestamp for today's HARD_TIMEOUT_TIME in Sydney."""
    now = now_sydney()
    target = now.replace(
        hour=HARD_TIMEOUT_TIME[0], minute=HARD_TIMEOUT_TIME[1], second=0, microsecond=0
    )
    return target.timestamp()


def wait_until_sydney(hour: int, minute: int, label: str, log: logging.Logger) -> None:
    """Sleep until the given Sydney wall-clock time (if not already past it)."""
    now = now_sydney()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now >= target:
        log.info(f"{label}: already past {hour:02d}:{minute:02d} Sydney — continuing immediately.")
        return
    log.info(f"{label}: waiting until {hour:02d}:{minute:02d} Sydney. Currently {now:%H:%M:%S}.")
    while True:
        now = now_sydney()
        if now >= target:
            log.info(f"{label}: reached {hour:02d}:{minute:02d}. Continuing.")
            return
        secs_left = (target - now).total_seconds()
        sleep_for = min(max(1, secs_left - 2), 60 if secs_left > 120 else 10)
        time.sleep(sleep_for)


def snap(driver: webdriver.Chrome, name: str, log: logging.Logger) -> None:
    p = RUN_DIR / f"{name}.png"
    try:
        driver.save_screenshot(str(p))
        log.info(f"Screenshot: {p.name}")
    except Exception as exc:
        log.warning(f"Screenshot failed ({name}): {exc}")


def discord_notify(message: str, log: Optional[logging.Logger] = None) -> None:
    """Post a message to the #golf-booking Discord channel via bot API."""
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        return
    try:
        data = json.dumps({"content": message}).encode("utf-8")
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages",
            data=data,
            headers={
                "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
                "Content-Type": "application/json",
                "User-Agent": "GolfBookingBot/1.0",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        if log:
            log.warning(f"Discord notify failed: {exc}")


def discord_upload_screenshot(filepath: str, caption: str, log: Optional[logging.Logger] = None) -> None:
    """Upload a screenshot to the #golf-booking Discord channel."""
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        return
    try:
        import io
        p = Path(filepath)
        if not p.exists() or p.stat().st_size == 0:
            return
        boundary = f"----GolfBot{random.randint(100000, 999999)}"
        body = io.BytesIO()
        # JSON payload part
        body.write(f"--{boundary}\r\n".encode())
        body.write(b'Content-Disposition: form-data; name="payload_json"\r\nContent-Type: application/json\r\n\r\n')
        body.write(json.dumps({"content": caption}).encode())
        body.write(b"\r\n")
        # File part
        body.write(f"--{boundary}\r\n".encode())
        body.write(f'Content-Disposition: form-data; name="files[0]"; filename="{p.name}"\r\n'.encode())
        body.write(b"Content-Type: image/png\r\n\r\n")
        body.write(p.read_bytes())
        body.write(f"\r\n--{boundary}--\r\n".encode())
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages",
            data=body.getvalue(),
            headers={
                "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "GolfBookingBot/1.0",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=30)
    except Exception as exc:
        if log:
            log.warning(f"Discord screenshot upload failed: {exc}")


def safe_accept_alert(driver: webdriver.Chrome) -> Tuple[bool, str]:
    try:
        alert = driver.switch_to.alert
        text  = alert.text
        alert.accept()
        return True, text
    except NoAlertPresentException:
        return False, ""


def wait_ready(driver: webdriver.Chrome, timeout: int = 15) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if driver.execute_script("return document.readyState") == "complete":
                return
        except Exception:
            pass
        time.sleep(0.1)


# ─────────────────────────────────────────────────────────────────────────────
# DRAW / QUEUE DETECTION  (ported from booking_script_thursday.py)
# ─────────────────────────────────────────────────────────────────────────────
def detect_draw(driver: webdriver.Chrome) -> Tuple[bool, Optional[int]]:
    try:
        body = driver.find_element(By.TAG_NAME, "body").text
        if "You are in the draw" not in body and "in the draw to access" not in body:
            return False, None
        m = re.search(r"Opens\s+in\s+(\d{1,2}):(\d{2}):(\d{2})", body)
        if m:
            return True, int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        m = re.search(r"Opens\s+in\s+(\d{1,2}):(\d{2})", body)
        if m:
            return True, int(m.group(1)) * 60 + int(m.group(2))
        return True, None
    except Exception:
        return False, None


def detect_queue(driver: webdriver.Chrome) -> Tuple[bool, Optional[int], Optional[int]]:
    try:
        body = driver.find_element(By.TAG_NAME, "body").text
        if "Current Position" not in body and "placed in a queue" not in body:
            return False, None, None
        pos = avail = None
        m = re.search(r"Current\s+Position\s*:\s*(\d+)", body)
        if m:
            pos = int(m.group(1))
        m = re.search(r"Approximate\s+Bookings\s+Available\s*:\s*~?(\d+)", body)
        if m:
            avail = int(m.group(1))
        return True, pos, avail
    except Exception:
        return False, None, None


def has_tee_sheet(driver: webdriver.Chrome) -> bool:
    try:
        table = driver.find_element(By.CLASS_NAME, "teetime-day-table")
        return bool(table.find_elements(By.XPATH, ".//div[contains(@class,'row-time')]"))
    except Exception:
        return False


def is_confirmed_logged_out(driver: webdriver.Chrome) -> bool:
    """Return True only when logout is strongly confirmed.

    Absence of the usual member nav is not enough; MiClub pages can render
    differently during keepalive hops while the session is still valid.
    """
    try:
        url = (driver.current_url or "").lower()
    except Exception:
        url = ""
    try:
        title = (driver.title or "").lower()
    except Exception:
        title = ""
    try:
        body = driver.find_element(By.TAG_NAME, "body").text.lower()
    except Exception:
        body = ""

    if "security/login" in url:
        return True
    if "your session has expired" in body or "session expired" in body:
        return True
    if ("log in" in body or "login" in title) and (
        "password" in body or "member number" in body or "username" in body
    ):
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# DATE TARGET  (next-next Saturday from Thursday)
# ─────────────────────────────────────────────────────────────────────────────
def compute_target() -> Tuple[str, str]:
    # Allow env var override for manual/test runs (e.g. OVERRIDE_TARGET_DAY=Sun OVERRIDE_TARGET_DATE="1 Mar")
    override_day  = os.getenv("OVERRIDE_TARGET_DAY", "").strip()
    override_date = os.getenv("OVERRIDE_TARGET_DATE", "").strip()
    if override_day and override_date:
        return override_day, override_date

    # Default: next-next Saturday from the current Thursday
    now = now_sydney()
    days_to_sat = (5 - now.weekday() + 7) % 7 or 7
    upcoming_sat = now + timedelta(days=days_to_sat)
    target = upcoming_sat + timedelta(days=7)
    dayname = target.strftime("%a")
    try:
        combo = target.strftime("%-d %b")
    except Exception:
        combo = target.strftime("%d %b").lstrip("0")
    return dayname, combo


# ─────────────────────────────────────────────────────────────────────────────
# SELENIUM SETUP
# ─────────────────────────────────────────────────────────────────────────────
def make_driver(log: Optional[logging.Logger] = None, worker_index: int = 0) -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--ignore-certificate-errors")

    # Per-worker fingerprint diversification
    ua = USER_AGENTS[worker_index % len(USER_AGENTS)]
    w, h = WINDOW_SIZES[worker_index % len(WINDOW_SIZES)]
    opts.add_argument(f"--window-size={w},{h}")
    opts.add_argument(f"--user-agent={ua}")

    # Anti-automation detection flags
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    svc = Service()  # Selenium Manager auto-downloads matching chromedriver
    for attempt in range(1, 3):
        try:
            drv = webdriver.Chrome(options=opts, service=svc)
            drv.set_page_load_timeout(90)

            # Override navigator.webdriver flag via CDP
            drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-AU', 'en']});
window.chrome = {runtime: {}};
"""
            })

            if log:
                caps = drv.capabilities
                log.info(f"Chrome version: {caps.get('browserVersion', 'unknown')}")
                log.info(f"ChromeDriver version: {caps.get('chrome', {}).get('chromedriverVersion', 'unknown')}")
                log.info(f"Worker fingerprint: UA={ua[:50]}... Window={w}x{h}")
            return drv
        except Exception as exc:
            if attempt == 2:
                raise RuntimeError(f"Chrome failed after retries: {exc}") from exc
            subprocess.run(["pkill", "-f", "chromedriver"], check=False)
            time.sleep(3)
    raise RuntimeError("unreachable")


# ─────────────────────────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────────────────────────
def _human_type(element, text: str) -> None:
    """Type text character-by-character with human-like delays."""
    element.clear()
    for ch in text:
        element.send_keys(ch)
        time.sleep(random.uniform(0.05, 0.15))


def login(driver: webdriver.Chrome, username: str, password: str, log: logging.Logger) -> bool:
    log.info(f"Logging in...")
    consecutive_fails = 0

    for attempt in range(1, MAX_LOGIN_RETRIES + 1):
        # Circuit breaker: stop after 5 consecutive failures to protect IP
        if consecutive_fails >= 5:
            log.error("Circuit breaker: 5 consecutive login failures — aborting to protect IP")
            discord_notify(f"🛑 {MEMBER_TO_FIRST.get(username, username)}: circuit breaker tripped after 5 login failures", log)
            return False

        # Load login page
        try:
            driver.get(LOGIN_URL)
            time.sleep(random.uniform(1.5, 3.0))
        except Exception as nav_exc:
            err_str = str(nav_exc)
            if "ERR_CONNECTION_REFUSED" in err_str or "net::ERR_" in err_str:
                log.warning(f"Connection error (attempt {attempt}/{MAX_LOGIN_RETRIES}): {err_str[:100]}")
                consecutive_fails += 1
                backoff = min(LOGIN_BASE_BACKOFF * (2 ** (attempt - 1)), LOGIN_MAX_BACKOFF)
                jitter = random.uniform(0, backoff * 0.3)
                log.info(f"Backing off {backoff + jitter:.0f}s")
                time.sleep(backoff + jitter)
                continue
            raise

        # Check for 403 Forbidden
        try:
            page_text = driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            page_text = ""
        if "Forbidden" in page_text or "403" in driver.title:
            consecutive_fails += 1
            backoff = min(LOGIN_BASE_BACKOFF * (2 ** (attempt - 1)), LOGIN_MAX_BACKOFF)
            jitter = random.uniform(0, backoff * 0.3)
            log.warning(f"403 Forbidden (attempt {attempt}/{MAX_LOGIN_RETRIES}) — backing off {backoff + jitter:.0f}s")
            time.sleep(backoff + jitter)
            continue

        # Attempt login with human-like typing
        try:
            uf = WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.NAME, "user")))
            _human_type(uf, username)
            time.sleep(random.uniform(0.3, 0.8))  # pause between fields
            pf = driver.find_element(By.NAME, "password")
            _human_type(pf, password)
            time.sleep(random.uniform(0.2, 0.5))  # pause before click
            driver.find_element(By.XPATH, "//input[@value='Login']").click()
            WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.XPATH, "//a[contains(@href,'logout')]")))
            log.info("Login successful")
            snap(driver, f"login_ok_{username}", log)
            return True
        except Exception as exc:
            consecutive_fails += 1
            log.warning(f"Login attempt {attempt}/{MAX_LOGIN_RETRIES} failed: {exc}")
            snap(driver, f"login_fail_{username}_attempt{attempt}", log)
            if attempt == MAX_LOGIN_RETRIES:
                log.error(f"Login failed after {MAX_LOGIN_RETRIES} attempts")
                try:
                    src_path = RUN_DIR / f"login_fail_{username}.html"
                    src_path.write_text(driver.page_source, encoding="utf-8")
                    log.info(f"Page source saved: {src_path.name}")
                except Exception as src_exc:
                    log.warning(f"Could not save page source: {src_exc}")
                return False
            backoff = min(LOGIN_BASE_BACKOFF * (2 ** (attempt - 1)), LOGIN_MAX_BACKOFF)
            jitter = random.uniform(0, backoff * 0.3)
            log.info(f"Retrying in {backoff + jitter:.0f}s")
            time.sleep(backoff + jitter)

    return False


def logout(driver: webdriver.Chrome, log: logging.Logger, username: str = "") -> None:
    try:
        driver.get(LOGOUT_URL)
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.NAME, "user")))
        log.info("Logged out")
        if username:
            discord_notify(f"🚪 {MEMBER_TO_FIRST.get(username, username)} logged out", log)
    except Exception as exc:
        log.warning(f"Logout incomplete: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# DRAW / QUEUE NAVIGATION
# ─────────────────────────────────────────────────────────────────────────────
def navigate_and_wait_for_tee_sheet(
    driver: webdriver.Chrome,
    target_day: str,
    target_date: str,
    log: logging.Logger,
    username: str = "",
    password: str = "",
) -> bool:
    """Enter draw/queue and wait for tee sheet. Does NOT refresh once in draw/queue."""
    # Store credentials for keepalive re-login
    _keepalive_username = username
    _keepalive_password = password
    log.info("Navigating to event list...")
    driver.get(EVENT_LIST_URL)

    in_waiting_room  = False
    draw_attempted   = False
    deadline         = hard_deadline_sydney()   # hard stop at 8pm Sydney
    last_status_log  = 0.0
    last_keepalive   = time.time()
    last_notified_pos = None  # track queue position for Discord updates

    while time.time() < deadline:
        now = time.time()

        if in_waiting_room:
            if has_tee_sheet(driver):
                log.info("✅ Tee sheet visible!")
                snap(driver, f"tee_sheet_visible_{username}", log)
                discord_notify(f"👀 {MEMBER_TO_FIRST.get(username, username)}: tee sheet visible — starting booking!", log)
                return True

            in_draw, countdown = detect_draw(driver)
            if in_draw:
                if now - last_status_log > 10:
                    log.info(f"In draw — countdown {countdown}s. Not refreshing.")
                    last_status_log = now
                if countdown:
                    deadline = max(deadline, time.time() + countdown + 60)
                time.sleep(1)
                continue

            in_queue, pos, avail = detect_queue(driver)
            if in_queue:
                if now - last_status_log > 5:
                    log.info(f"In queue — position {pos}, ~{avail} available. Not refreshing.")
                    last_status_log = now
                if pos != last_notified_pos:
                    last_notified_pos = pos
                    discord_notify(f"📊 {MEMBER_TO_FIRST.get(username, username)}: queue position {pos} (~{avail} available)", log)
                deadline = max(deadline, time.time() + 300)
                time.sleep(0.5)
                continue

            # Transitioning
            time.sleep(0.5)
            continue

        # Not yet in waiting room — try to enter
        try:
            xpath = (
                f"//div[contains(@class,'full') and "
                f".//span[contains(.,'{target_day}')] and "
                f".//span[contains(.,'{target_date}')]]"
            )
            div  = WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.XPATH, xpath)))
            link = div.find_element(By.TAG_NAME, "a")
            classes  = link.get_attribute("class") or ""
            href     = link.get_attribute("href") or ""

            local_now  = now_sydney()
            draw_open  = local_now.replace(hour=QUEUE_JOIN_TIME[0], minute=QUEUE_JOIN_TIME[1], second=0, microsecond=0)

            if "eventStatusOpen" in classes or (local_now >= draw_open and not draw_attempted):
                if not draw_attempted:
                    log.info(f"Attempting to enter draw for {target_date}...")
                    draw_attempted = True
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", link)
                    link.click()
                except ElementClickInterceptedException:
                    driver.execute_script("arguments[0].click();", link)

                time.sleep(1)

                if has_tee_sheet(driver):
                    log.info("Tee sheet loaded immediately (no queue).")
                    return True

                in_draw, _ = detect_draw(driver)
                in_queue, pos, _ = detect_queue(driver)
                if in_draw or in_queue:
                    state = "draw" if in_draw else f"queue (pos {pos})"
                    log.info(f"Entered {state}.")
                    snap(driver, f"entered_{state.replace(' ', '_')}_{username}", log)
                    discord_notify(f"⏳ {MEMBER_TO_FIRST.get(username, username)} entered {state}", log)
                    in_waiting_room = True
                    continue

                # Try direct URL
                if href and not href.lower().startswith("javascript"):
                    driver.get(href)
                    time.sleep(1)
                    in_draw, _ = detect_draw(driver)
                    in_queue, pos, _ = detect_queue(driver)
                    if in_draw or in_queue:
                        log.info(f"Entered via direct URL.")
                        in_waiting_room = True
                        continue

                log.info("Not in draw/queue yet — returning to event list.")
                driver.get(EVENT_LIST_URL)
                draw_attempted = False
                time.sleep(2)
                continue

            # Not yet draw time — poll slowly when far away, tighten near 6:30
            local_now  = now_sydney()
            draw_open  = local_now.replace(hour=QUEUE_JOIN_TIME[0], minute=QUEUE_JOIN_TIME[1], second=0, microsecond=0)
            secs_to_draw = (draw_open - local_now).total_seconds()
            if secs_to_draw > 120:
                poll_interval = OPEN_POLL_INTERVAL  # 15s when >2 min away
            else:
                poll_interval = 2  # tight polling in final 2 minutes

            if now - last_status_log > 30:
                log.info(f"Waiting for draw time ({QUEUE_JOIN_TIME[0]:02d}:{QUEUE_JOIN_TIME[1]:02d}) — {secs_to_draw:.0f}s away...")
                last_status_log = now

            # Session keepalive: navigate to home page and back every 5 min
            # to prevent MiClub from expiring idle sessions.
            # Important: do NOT panic re-login on weak signals. Only re-login
            # when logout is confirmed. Once the draw/queue is entered, this
            # keepalive block is bypassed entirely by the in_waiting_room path.
            if now - last_keepalive > KEEPALIVE_INTERVAL and secs_to_draw > 60:
                log.info("Session keepalive: navigating to home page and back")
                try:
                    driver.get(HOME_URL)
                    time.sleep(2)

                    if is_confirmed_logged_out(driver):
                        local_now = now_sydney()
                        relogin_cutoff = local_now.replace(hour=18, minute=57, second=0, microsecond=0)
                        if local_now <= relogin_cutoff:
                            log.warning("Confirmed logged out during keepalive — re-logging in")
                            if not login(driver, _keepalive_username, _keepalive_password, log):
                                log.error("Re-login failed during keepalive")
                        else:
                            log.warning("Confirmed logged out during keepalive, but skipping re-login after 18:57 cutoff")
                    else:
                        log.info("Keepalive OK — no confirmed logout")

                    driver.get(EVENT_LIST_URL)
                    time.sleep(2)
                except Exception as exc:
                    log.warning(f"Keepalive navigation error: {exc}")
                    driver.get(EVENT_LIST_URL)
                    time.sleep(2)
                last_keepalive = now
                continue

            time.sleep(poll_interval)
            driver.refresh()
            safe_accept_alert(driver)

        except TimeoutException:
            if now - last_status_log > 30:
                log.warning("Event not found — refreshing.")
                last_status_log = now
            time.sleep(3)
            driver.get(EVENT_LIST_URL)
        except Exception as exc:
            log.warning(f"Navigation error: {exc}")
            time.sleep(3)
            try:
                driver.refresh()
            except Exception:
                driver.get(EVENT_LIST_URL)

    log.error("Timed out waiting for tee sheet.")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# NEW: SEARCH-BASED BOOKING  (makeBooking.xhtml flow)
# ─────────────────────────────────────────────────────────────────────────────
def _reveal_player_inputs(driver: webdriver.Chrome, log: Optional[logging.Logger] = None) -> None:
    """
    Reveal hidden autocomplete inputs on the makeBooking page.
    MiClub hides the <span class="ui-autocomplete autocomplete"> wrapper
    (display:none) until the user clicks the "Find Player" icon/text.
    Native clicks are unreliable in headless mode, so we force display via JS.
    """
    try:
        revealed = driver.execute_script("""
            var spans = document.querySelectorAll('span.ui-autocomplete.autocomplete');
            var count = 0;
            for (var i = 0; i < spans.length; i++) {
                if (window.getComputedStyle(spans[i]).display === 'none') {
                    spans[i].style.display = 'inline-block';
                    count++;
                }
            }
            return count;
        """)
        if log:
            log.info(f"_reveal_player_inputs: revealed {revealed} hidden autocomplete spans via JS")
    except Exception as exc:
        if log:
            log.warning(f"_reveal_player_inputs failed: {exc}")


def _find_empty_player_inputs(driver: webdriver.Chrome, log: Optional[logging.Logger] = None) -> list:
    """
    Return empty player search input fields on the makeBooking page.
    MiClub (PrimeFaces) uses class 'ui-autocomplete-input' with placeholder 'Type Name'.
    Player 1 is always pre-filled (logged-in user), so we skip inputs with existing values.
    """
    try:
        # Primary: PrimeFaces AutoComplete input with 'Type Name' placeholder
        inputs = driver.find_elements(
            By.CSS_SELECTOR, "input.ui-autocomplete-input[placeholder='Type Name']"
        )
        empties = [i for i in inputs if not (i.get_attribute("value") or "").strip()]
        if log:
            log.info(f"_find_empty_player_inputs: {len(inputs)} Type Name inputs, {len(empties)} empty")
        if empties:
            return empties

        # Fallback: any autocomplete input that isn't 'Select Club'
        inputs = driver.find_elements(
            By.CSS_SELECTOR, "input.ui-autocomplete-input"
        )
        empties = [
            i for i in inputs
            if not (i.get_attribute("value") or "").strip()
            and (i.get_attribute("placeholder") or "").lower() not in ("select club", "")
        ]
        if log:
            log.info(f"_find_empty_player_inputs fallback: {len(inputs)} autocomplete inputs, {len(empties)} empty")
        return empties
    except Exception:
        return []


def _try_select_partners_checkboxes(
    driver: webdriver.Chrome,
    partners_to_add: List[str],
    log: logging.Logger,
) -> int:
    """
    Click the pre-configured 'Select Partners' checkboxes that match the
    required partners by member number or surname.  Returns number of players
    successfully ticked.

    MiClub uses PrimeFaces which hides native <input type="checkbox"> inside
    <div class="ui-helper-hidden-accessible">.  The visible clickable element
    is a sibling <div class="ui-chkbox-box">.  We find checkboxes via their
    <label> text (e.g. "Gareth Hillard") and click the PrimeFaces box.
    """
    target_names = {MEMBER_TO_SURNAME.get(p, p).lower() for p in partners_to_add}
    added = 0
    try:
        # Strategy 1 (Primary): Find PrimeFaces checkbox wrappers via labels.
        # DOM structure:
        #   <td>
        #     <div class="ui-chkbox ui-widget">
        #       <div class="ui-helper-hidden-accessible">
        #         <input type="checkbox" id="bookForm:...:0" value="2008">
        #       </div>
        #       <div class="ui-chkbox-box ...">  ← click this
        #         <span class="ui-chkbox-icon ..."></span>
        #       </div>
        #     </div>
        #     <label for="bookForm:...:0">Gareth Hillard</label>
        #   </td>
        labels = driver.find_elements(By.XPATH, "//label")
        for label_el in labels:
            try:
                label_text = label_el.text.strip().lower()
                if not label_text:
                    continue
                if not any(name in label_text for name in target_names):
                    continue

                # Found a matching label — find the associated checkbox
                label_for = label_el.get_attribute("for") or ""
                clicked = False

                # Method A: find the PrimeFaces chkbox-box in the same <td> or parent container
                if label_for:
                    try:
                        cb_input = driver.find_element(By.ID, label_for)
                        pf_box = cb_input.find_element(
                            By.XPATH,
                            "./ancestor::div[contains(@class,'ui-chkbox')][1]"
                            "//div[contains(@class,'ui-chkbox-box')]"
                        )
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", pf_box)
                        try:
                            pf_box.click()
                        except ElementClickInterceptedException:
                            driver.execute_script("arguments[0].click();", pf_box)
                        clicked = True
                    except Exception:
                        pass

                # Method B: find ui-chkbox-box as preceding sibling of the label's parent
                if not clicked:
                    try:
                        pf_box = label_el.find_element(
                            By.XPATH,
                            "./preceding-sibling::div[contains(@class,'ui-chkbox')]"
                            "//div[contains(@class,'ui-chkbox-box')]"
                        )
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", pf_box)
                        try:
                            pf_box.click()
                        except ElementClickInterceptedException:
                            driver.execute_script("arguments[0].click();", pf_box)
                        clicked = True
                    except Exception:
                        pass

                # Method C: click the label itself (fires associated checkbox)
                if not clicked:
                    try:
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", label_el)
                        label_el.click()
                        clicked = True
                    except Exception:
                        pass

                if clicked:
                    log.info(f"Ticked checkbox: {label_text}")
                    added += 1
                    time.sleep(0.3)
            except Exception as e:
                log.debug(f"Checkbox label check error: {e}")

        # Strategy 2 (Fallback): try native checkbox inputs if Strategy 1 found nothing
        if added == 0:
            checkboxes = driver.find_elements(By.XPATH, "//input[@type='checkbox']")
            for cb in checkboxes:
                try:
                    cb_id = cb.get_attribute("id") or ""
                    label_text = ""
                    label_el = None
                    if cb_id:
                        try:
                            label_el = driver.find_element(By.XPATH, f"//label[@for='{cb_id}']")
                            label_text = label_el.text.strip().lower()
                        except Exception:
                            pass
                    if not label_text:
                        try:
                            parent = cb.find_element(By.XPATH, "./ancestor::td[1]")
                            label_text = parent.text.strip().lower()
                        except Exception:
                            pass

                    if label_text and any(name in label_text for name in target_names):
                        if not cb.is_selected():
                            driver.execute_script("arguments[0].click();", cb)
                            log.info(f"Ticked checkbox (native fallback): {label_text}")
                            added += 1
                            time.sleep(0.3)
                except Exception as e:
                    log.debug(f"Checkbox fallback error: {e}")
    except Exception as exc:
        log.warning(f"Checkbox selection error: {exc}")

    # If we ticked any checkboxes, click the "Select Partners" button to confirm them
    if added > 0:
        try:
            select_btn = driver.find_element(
                By.XPATH,
                "//button[normalize-space()='Select Partners'] | "
                "//a[normalize-space()='Select Partners'] | "
                "//input[@value='Select Partners']"
            )
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", select_btn)
            try:
                select_btn.click()
            except ElementClickInterceptedException:
                driver.execute_script("arguments[0].click();", select_btn)
            log.info(f"Clicked 'Select Partners' button ({added} partners ticked)")
            time.sleep(1.0)
        except Exception as e:
            log.debug(f"'Select Partners' button not found or click failed: {e}")

    return added


def _find_error_player_slots(driver: webdriver.Chrome, log: logging.Logger) -> list:
    """
    Detect player slots on makeBooking.xhtml that have 'already booked' errors.
    Returns a list of (error_element, nearby_input_or_container) tuples.

    Detection methods (in priority order):
      1. Text nodes containing 'already booked' / 'already has a booking' / 'existing booking'
      2. PrimeFaces message components: div.ui-message-error, span.ui-message-error-detail
      3. PrimeFaces autocomplete wrappers with ui-state-error class
    """
    results = []
    already_booked_phrases = [
        "already booked", "already has a booking",
        "existing booking", "already registered",
        "member is already",
    ]
    try:
        # Method 1: Text-based detection
        error_elements = driver.find_elements(
            By.XPATH,
            "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
            "'already booked') or "
            "contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
            "'already has a booking') or "
            "contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
            "'existing booking') or "
            "contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
            "'member is already')]"
        )
        for el in error_elements:
            if el.is_displayed():
                results.append(el)

        # Method 2: PrimeFaces error message components
        pf_errors = driver.find_elements(
            By.XPATH,
            "//div[contains(@class,'ui-message-error')] | "
            "//span[contains(@class,'ui-message-error-detail')] | "
            "//div[contains(@class,'ui-messages-error')]"
        )
        for el in pf_errors:
            if el.is_displayed() and any(p in el.text.lower() for p in already_booked_phrases):
                if el not in results:
                    results.append(el)

        # Method 3: Autocomplete wrappers with error state
        error_wrappers = driver.find_elements(
            By.XPATH,
            "//span[contains(@class,'ui-autocomplete') and contains(@class,'ui-state-error')]"
        )
        for el in error_wrappers:
            if el.is_displayed() and el not in results:
                results.append(el)

    except Exception as exc:
        log.debug(f"_find_error_player_slots error: {exc}")
    return results


def _remove_player_by_name(driver: webdriver.Chrome, surname: str, log: logging.Logger) -> bool:
    """
    Remove a player from the makeBooking page by finding their recordContainer
    and clicking the glyphicon-remove icon.

    MiClub DOM structure for a filled player slot:
      <div class="recordContainer recordMade" id="bookForm:record_N">
        <a class="ui-commandlink ui-widget">
          <span class="glyphicon glyphicon-remove removeIcon"></span>
        </a>
        <span class="booking-name">Hillard, Gareth</span>
      </div>

    Returns True if removal was successful.
    """
    surname_lower = surname.lower()
    try:
        # Find all recordContainers with a booking-name span
        records = driver.find_elements(
            By.XPATH,
            "//div[contains(@class,'recordContainer')]"
        )
        for record in records:
            try:
                name_spans = record.find_elements(
                    By.XPATH, ".//span[contains(@class,'booking-name')]"
                )
                for name_span in name_spans:
                    if surname_lower in name_span.text.strip().lower():
                        # Found the player — click the remove icon
                        remove_link = record.find_element(
                            By.XPATH,
                            ".//a[.//span[contains(@class,'removeIcon')]] | "
                            ".//a[.//span[contains(@class,'glyphicon-remove')]]"
                        )
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", remove_link)
                        try:
                            remove_link.click()
                        except ElementClickInterceptedException:
                            driver.execute_script("arguments[0].click();", remove_link)
                        log.info(f"Removed player '{name_span.text.strip()}' via removeIcon")
                        time.sleep(0.5)
                        return True
            except Exception:
                continue
    except Exception as exc:
        log.debug(f"_remove_player_by_name failed: {exc}")

    return False


def _remove_player_from_slot(driver: webdriver.Chrome, element, log: logging.Logger) -> bool:
    """
    Given an error element or player slot element, find and click the remove/close button.
    Tries multiple strategies for MiClub's PrimeFaces-based booking page.
    Returns True if removal was successful.
    """
    # Walk up through ancestors looking for a remove/close control
    ancestor_xpaths = [
        "./ancestor::div[contains(@class,'recordContainer')][1]",
        "./ancestor::div[contains(@class,'playerCont')][1]",
        "./ancestor::span[contains(@class,'ui-autocomplete')][1]",
        "./ancestor::td[1]",
        "./ancestor::div[contains(@class,'player') or contains(@class,'slot') or contains(@class,'booking')][1]",
        "./ancestor::div[1]",
    ]

    remove_selectors = [
        # MiClub-specific: glyphicon remove icon (primary)
        ".//span[contains(@class,'removeIcon')]/..",
        ".//span[contains(@class,'glyphicon-remove')]/..",
        # PrimeFaces command link containing remove icon
        ".//a[contains(@class,'ui-commandlink') and .//span[contains(@class,'removeIcon')]]",
        # PrimeFaces autocomplete close icon
        ".//span[contains(@class,'ui-icon-close')]/..",
        ".//span[contains(@class,'ui-autocomplete-close')]",
        # Generic remove buttons
        ".//a[contains(@class,'remove') or contains(@onclick,'remove') or @title='Remove']",
        ".//button[contains(@class,'remove') or contains(@class,'delete')]",
        ".//a[contains(@class,'close') or @title='Close']",
    ]

    for anc_xpath in ancestor_xpaths:
        try:
            container = element.find_element(By.XPATH, anc_xpath)
        except Exception:
            continue

        for sel in remove_selectors:
            try:
                btns = container.find_elements(By.XPATH, sel)
                for btn in btns:
                    if btn.is_displayed():
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                        try:
                            btn.click()
                        except ElementClickInterceptedException:
                            driver.execute_script("arguments[0].click();", btn)
                        log.info("Removed player via close/remove button")
                        time.sleep(0.5)
                        return True
            except Exception:
                continue

    # Nuclear fallback: find the nearest autocomplete input and clear it via JS
    try:
        for anc_xpath in ancestor_xpaths[:3]:
            try:
                container = element.find_element(By.XPATH, anc_xpath)
                inputs = container.find_elements(By.XPATH, ".//input[contains(@class,'ui-autocomplete-input')]")
                for inp in inputs:
                    if inp.is_displayed() and (inp.get_attribute("value") or "").strip():
                        driver.execute_script(
                            "arguments[0].value = ''; "
                            "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
                            inp
                        )
                        log.info("Cleared player slot via JS value reset")
                        time.sleep(0.5)
                        return True
            except Exception:
                continue
    except Exception:
        pass

    return False


def _clear_already_booked_slots(driver: webdriver.Chrome, log: logging.Logger) -> int:
    """
    On makeBooking.xhtml, detect player slots showing 'already booked' errors
    and remove them. Returns number of slots cleared.
    """
    error_elements = _find_error_player_slots(driver, log)
    if not error_elements:
        return 0

    cleared = 0
    for err in error_elements:
        err_text = err.text.strip()[:120]
        log.info(f"Found already-booked error: '{err_text}'")

        # Try to extract the player surname from the error message
        # e.g. "Hillard, Gareth - Member is already booked" → surname = "Hillard"
        removed = False
        for surname in MEMBER_TO_SURNAME.values():
            if surname.lower() in err_text.lower():
                removed = _remove_player_by_name(driver, surname, log)
                if removed:
                    break

        # Fallback: ancestor-walk removal from the error element
        if not removed:
            removed = _remove_player_from_slot(driver, err, log)

        if removed:
            cleared += 1
        else:
            log.warning("Could not remove already-booked player — slot may still be occupied")

    return cleared


def _search_and_select_player(
    driver: webdriver.Chrome,
    input_el,
    member_number: str,
    log: logging.Logger,
) -> str:
    """
    Type member number into a Find Player field, wait for autocomplete, click result.

    Returns:
      "ok"             — player added successfully
      "already_booked" — player found but already has a booking (slot auto-cleared)
      "not_found"      — no autocomplete result appeared
      "error"          — unexpected failure
    """
    surname = MEMBER_TO_SURNAME.get(member_number, member_number)
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", input_el)
        driver.execute_script("arguments[0].click();", input_el)
        time.sleep(0.3)
        input_el.clear()
        input_el.send_keys(member_number)
        log.info(f"Searching for player {member_number} ({surname})...")

        # Wait for autocomplete dropdown
        deadline = time.time() + 10
        result = None
        while time.time() < deadline:
            candidates = driver.find_elements(
                By.XPATH,
                "//*[contains(@class,'ui-autocomplete-item') or "
                "contains(@class,'ac_results') or "
                "contains(@class,'autocomplete-result') or "
                "contains(@class,'ui-menu-item')]"
                "[not(contains(@style,'display:none')) and not(contains(@style,'display: none'))]"
            )
            visible = [c for c in candidates if c.is_displayed() and c.text.strip()]
            if visible:
                result = visible[0]
                break
            time.sleep(0.2)

        if result is None:
            log.warning(f"No autocomplete for {member_number}, trying Enter key")
            input_el.send_keys(Keys.RETURN)
            time.sleep(0.5)
            val = input_el.get_attribute("value") or ""
            if val and not val.isdigit():
                log.info(f"Player accepted via Enter: {val}")
            else:
                return "not_found"
        else:
            log.info(f"Selecting: {result.text.strip()}")
            try:
                result.click()
            except ElementClickInterceptedException:
                driver.execute_script("arguments[0].click();", result)

        # Post-selection: wait briefly then check for "already booked" errors
        time.sleep(1.0)

        already_booked_phrases = [
            "already booked", "already has a booking",
            "existing booking", "already registered",
            "member is already",
        ]

        # Check page body and nearby error messages
        try:
            body = driver.find_element(By.TAG_NAME, "body").text.lower()
        except Exception:
            body = ""

        is_error = any(p in body for p in already_booked_phrases)

        # Also check PrimeFaces inline error messages
        if not is_error:
            try:
                pf_msgs = driver.find_elements(
                    By.XPATH,
                    "//div[contains(@class,'ui-message-error')] | "
                    "//span[contains(@class,'ui-message-error-detail')] | "
                    "//div[contains(@class,'ui-messages-error')]"
                )
                for msg in pf_msgs:
                    if msg.is_displayed() and any(p in msg.text.lower() for p in already_booked_phrases):
                        is_error = True
                        break
            except Exception:
                pass

        if is_error:
            log.warning(f"Player {member_number} ({surname}) is already booked — clearing slot")
            snap(driver, f"already_booked_{member_number}", log)

            # Try to remove this player from the slot
            # Method 1: Direct removal via recordContainer + removeIcon (MiClub-specific)
            removed = _remove_player_by_name(driver, surname, log)

            # Method 2: Find the autocomplete input containing the surname and remove via ancestor walk
            if not removed:
                try:
                    filled_inputs = driver.find_elements(
                        By.XPATH,
                        f"//input[contains(@class,'ui-autocomplete-input') and "
                        f"contains(translate(@value,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                        f"'{surname.lower()}')]"
                    )
                    for inp in filled_inputs:
                        if inp.is_displayed():
                            if _remove_player_from_slot(driver, inp, log):
                                removed = True
                            break
                except Exception as rm_exc:
                    log.debug(f"Could not clear already-booked slot for {surname}: {rm_exc}")

            if not removed:
                log.warning(f"Could not remove already-booked player {surname} — slot may still be occupied")

            return "already_booked"

        # Verify the input actually has a value now
        try:
            val = input_el.get_attribute("value") or ""
            if val and not val.isdigit():
                log.info(f"Player {member_number} ({surname}) added: {val}")
                return "ok"
            # Input might have moved — check if any input now contains the surname
            filled = driver.find_elements(
                By.XPATH,
                f"//input[contains(@class,'ui-autocomplete-input') and "
                f"contains(translate(@value,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                f"'{surname.lower()}')]"
            )
            if filled:
                log.info(f"Player {member_number} ({surname}) confirmed in slot")
                return "ok"
        except Exception:
            pass

        log.info(f"Player {member_number} selection status unclear — assuming ok")
        return "ok"

    except Exception as exc:
        log.error(f"Search/select failed for {member_number}: {exc}")
        return "error"


def _get_row_id(row) -> str:
    """Extract booking_row_id from a tee-sheet row.

    MiClub HTML structure (confirmed from live page):
      <div id="row-9454" class="row row-time ...">
        <button id="btn-book-group-9454" onclick="javascript:checkAutomaticBook(261,9454,1,...)">

    Three reliable sources, tried in order:
      1. Row div id:   id="row-9454"           → strip "row-" prefix
      2. Button onclick: checkAutomaticBook(261,9454,...) → 2nd argument
      3. Button id:    id="btn-book-group-9454" → strip "btn-book-group-" prefix
    """
    # 1) Row div id — most direct: id="row-9454"
    try:
        row_id_attr = row.get_attribute("id") or ""
        m = re.match(r"^row-(\d+)$", row_id_attr)
        if m:
            return m.group(1)
    except Exception:
        pass

    btn = None
    try:
        btn = row.find_element(By.XPATH, ".//button[contains(@class,'btn-book-group')]")
    except Exception:
        pass

    # 2) Button onclick: checkAutomaticBook(261,9454,1,...) — 2nd arg is row id
    if btn is not None:
        try:
            onclick = btn.get_attribute("onclick") or ""
            m = re.search(r"checkAutomaticBook\(\s*\d+\s*,\s*(\d+)\s*,", onclick)
            if m:
                return m.group(1)
        except Exception:
            pass

    # 3) Button id: id="btn-book-group-9454"
    if btn is not None:
        try:
            btn_id = btn.get_attribute("id") or ""
            m = re.match(r"^btn-book-group-(\d+)$", btn_id)
            if m:
                return m.group(1)
        except Exception:
            pass

    return ""


def execute_search_booking(
    driver: webdriver.Chrome,
    username: str,
    partners_to_add: List[str],
    required_slots: int,
    log: logging.Logger,
    max_attempts: int = BOOKING_MAX_ATTEMPTS,
    skip_row_ids: Optional[set] = None,
    cancel_event: Optional[multiprocessing.Event] = None,
) -> Tuple[bool, str]:
    """
    Find a slot with enough empty spaces, click Book Group, click No on the modal,
    then on makeBooking.xhtml add each partner by member-number search, and confirm.

    partners_to_add: member numbers of players to add (not including logged-in user)
    required_slots:  total players needed (4 for 4-ball, 2 for 2-ball)
    cancel_event:    if set by another worker, abort early
    """
    log.info(f"Starting search-based booking for {required_slots}-ball. Partners: {partners_to_add}")
    deadline = hard_deadline_sydney()

    locked_row_ids: set = set()          # rows locked by other users (cleared periodically)
    locked_clear_time = time.time() + 30  # clear locked set every 30s

    attempt = 0
    while attempt < max_attempts and time.time() < deadline:
        if cancel_event and cancel_event.is_set():
            log.info("Another worker already completed this booking — aborting.")
            return False, ""
        attempt += 1
        row_id = ""
        mins_remaining = max(0, (deadline - time.time()) / 60)
        log.info(f"Booking attempt {attempt} ({mins_remaining:.0f} min until 8pm timeout)...")

        # Periodically clear locked-row memory so we retry released rows
        if time.time() > locked_clear_time:
            if locked_row_ids:
                log.info(f"Clearing {len(locked_row_ids)} locked-row entries (30s cooldown)")
            locked_row_ids.clear()
            locked_clear_time = time.time() + 30

        try:
            # ── 1. Find a suitable row ─────────────────────────────────────
            if not _wait_for_tee_table(driver, log, timeout=10):
                # Not on tee sheet — likely on event list after a cancel/redirect.
                # Re-navigate into the event by clicking the target day link.
                log.warning("Tee table not ready — re-navigating into event")
                cur_url = driver.current_url or ""
                if "makeBooking" in cur_url or "eventList" in cur_url or "event.msp" not in cur_url:
                    driver.get(EVENT_LIST_URL)
                    time.sleep(2)
                    try:
                        # Click the first available event link for the target day
                        event_links = driver.find_elements(
                            By.XPATH,
                            "//div[contains(@class,'full')]//a[contains(@href,'booking_event_id')]"
                        )
                        if event_links:
                            event_links[0].click()
                            time.sleep(2)
                    except Exception:
                        pass
                else:
                    driver.refresh()
                    time.sleep(3)
                if not _wait_for_tee_table(driver, log, timeout=30):
                    log.warning("Still no tee table after re-navigation — will retry")
                    time.sleep(2)
                continue

            rows = driver.find_elements(By.XPATH, "//div[contains(@class,'row-time')]")
            target_row = None
            for row in rows:
                try:
                    empties = row.find_elements(By.XPATH, ".//button[contains(@class,'btn-book-me')]")
                    if len(empties) < required_slots:
                        continue
                    candidate_row_id = _get_row_id(row)
                    if skip_row_ids and candidate_row_id and candidate_row_id in skip_row_ids:
                        log.info(
                            f"Skipping row_id={candidate_row_id} (already booked by another group) — trying next row"
                        )
                        continue
                    if candidate_row_id and candidate_row_id in locked_row_ids:
                        log.info(f"Skipping row_id={candidate_row_id} (locked by another user) — trying next row")
                        continue
                    target_row = row
                    row_id = candidate_row_id
                    break
                except StaleElementReferenceException:
                    continue

            if not target_row:
                log.info("No suitable slot found — refreshing")
                snap(driver, f"attempt{attempt}_no_slot", log)
                driver.refresh()
                time.sleep(3)
                continue

            try:
                time_text = target_row.find_element(By.TAG_NAME, "h3").text
            except Exception:
                time_text = "(unknown)"
            log.info(f"Target slot: {time_text}")
            snap(driver, f"attempt{attempt}_target_row", log)

            # ── 2. Click Book Group ────────────────────────────────────────
            btn = target_row.find_element(By.XPATH, ".//button[contains(@class,'btn-book-group')]")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            try:
                btn.click()
            except ElementClickInterceptedException:
                driver.execute_script("arguments[0].click();", btn)
            time.sleep(1)

            # Check for alert (slot locked by someone else)
            alerted, alert_text = safe_accept_alert(driver)
            if alerted:
                if row_id:
                    locked_row_ids.add(row_id)
                log.info(f"Slot locked (alert: {alert_text}) — skipping row_id={row_id}, trying next slot")
                discord_notify(f"🔒 {MEMBER_TO_FIRST.get(username, username)}: slot {time_text} locked, trying next", log)
                driver.refresh()
                time.sleep(2)
                continue

            # ── 3. Click No on the group modal ─────────────────────────────
            try:
                no_btn = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable(
                        (By.XPATH, "//button[normalize-space()='No'] | //a[normalize-space()='No']")
                    )
                )
                snap(driver, f"attempt{attempt}_group_modal", log)
                try:
                    no_btn.click()
                except ElementClickInterceptedException:
                    driver.execute_script("arguments[0].click();", no_btn)
                log.info("Clicked 'No' on group modal — heading to makeBooking page")
                time.sleep(1.5)
            except TimeoutException:
                log.warning("Group modal didn't appear — might have gone direct to booking page")

            # ── 4. Wait for makeBooking.xhtml ──────────────────────────────
            try:
                WebDriverWait(driver, 15).until(lambda d: "makeBooking" in d.current_url)
            except TimeoutException:
                # Some cases: slot already booked or redirect didn't happen
                log.warning(f"makeBooking URL not reached — current: {driver.current_url}")
                snap(driver, f"attempt{attempt}_no_makebooking", log)
                driver.get(EVENT_LIST_URL)
                time.sleep(3)
                continue

            log.info(f"makeBooking page loaded: {driver.current_url}")
            snap(driver, f"attempt{attempt}_makebooking_loaded", log)

            # Double-check: extract row_id from URL and verify it's not in skip set
            if skip_row_ids:
                url_match = re.search(r"booking_row_id=(\d+)", driver.current_url)
                if url_match and url_match.group(1) in skip_row_ids:
                    log.warning(f"URL row_id={url_match.group(1)} is in skip set — "
                                f"cancelling and trying a different row")
                    driver.get(EVENT_LIST_URL)
                    time.sleep(3)
                    continue

            # Log reservation timer if visible
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text
                m = re.search(r"Seconds remaining.*?(\d+)", body_text)
                if m:
                    log.info(f"Reservation timer: {m.group(1)}s remaining")
            except Exception:
                pass

            # Clear any pre-filled slots with "already booked" errors
            cleared = _clear_already_booked_slots(driver, log)
            if cleared:
                log.info(f"Cleared {cleared} already-booked slot(s) — page reset")
                time.sleep(0.5)

            # ── 5. Add partners ────────────────────────────────────────────
            # Strategy A: click the pre-configured "Select Partners" checkboxes
            # Strategy B: type member number into PrimeFaces autocomplete fields
            # If a partner is already booked, remove them and try alternates.

            # Reveal hidden autocomplete inputs by clicking "Find Player" icons
            _reveal_player_inputs(driver, log)
            time.sleep(0.5)

            ticked = _try_select_partners_checkboxes(driver, partners_to_add, log)
            log.info(f"Checkbox strategy: ticked {ticked}/{len(partners_to_add)} partners")

            # Check how many Find Player inputs are still empty after checkboxes
            still_empty = _find_empty_player_inputs(driver, log)
            log.info(f"Empty Find Player inputs remaining after checkboxes: {len(still_empty)} "
                     f"(need {len(partners_to_add) - ticked} more via search)")

            # Diagnostic: log what the current page inputs look like
            try:
                all_inputs = driver.find_elements(By.XPATH, "//input[@type='text' and not(@disabled)]")
                log.info(f"DEBUG: Found {len(all_inputs)} visible text inputs on makeBooking page")
                for inp in all_inputs[:8]:
                    log.info(f"  input class='{inp.get_attribute('class')}' "
                             f"placeholder='{inp.get_attribute('placeholder')}' "
                             f"value='{inp.get_attribute('value')}'")
            except Exception:
                pass

            # Strategy B: autocomplete search — build a queue of partners to try.
            # Primary partners first, then alternates (other group members not in this booking).
            remaining_partners = partners_to_add[ticked:] if ticked < len(partners_to_add) else []

            # Build alternate partner pool: all known members except the logged-in user
            # and those already in the primary partner list
            primary_set = set(partners_to_add) | {username}
            alternate_partners = [m for m in MEMBER_TO_SURNAME.keys()
                                  if m not in primary_set]
            partner_queue = list(remaining_partners) + alternate_partners

            partners_added_by_search = 0
            attempted: set = set()
            skipped: List[str] = []

            for member_num in partner_queue:
                if member_num in attempted:
                    continue
                attempted.add(member_num)

                empty_inputs = _find_empty_player_inputs(driver, log)
                if not empty_inputs:
                    log.info("No more empty Find Player slots — all filled.")
                    break

                # We've added enough partners already
                needed = len(partners_to_add) - ticked - partners_added_by_search
                if needed <= 0:
                    break

                result = _search_and_select_player(driver, empty_inputs[0], member_num, log)

                if result == "ok":
                    partners_added_by_search += 1
                    time.sleep(0.3)
                elif result == "already_booked":
                    log.warning(f"Player {member_num} already booked — trying next in queue")
                    skipped.append(member_num)
                    # _search_and_select_player already attempted slot cleanup;
                    # double-check and clear any lingering errors
                    _clear_already_booked_slots(driver, log)
                    continue
                elif result == "not_found":
                    log.warning(f"Player {member_num} not found in autocomplete — skipping")
                    skipped.append(member_num)
                    continue
                else:  # "error"
                    log.warning(f"Error adding {member_num} — skipping")
                    skipped.append(member_num)
                    continue

            if skipped:
                log.info(f"Skipped members: {skipped}. Proceeding with {partners_added_by_search} added by search.")

            # Safety check: use explicit count of partners added, not empty-input inference
            total_partners_added = ticked + partners_added_by_search
            log.info(f"Partner tally: {total_partners_added}/{len(partners_to_add)} "
                     f"(checkbox={ticked}, search={partners_added_by_search})")
            if total_partners_added < 1 and required_slots > 1:
                log.warning(f"Only {total_partners_added} partners added (need at least 1) "
                            f"— cancelling and retrying slot")
                try:
                    cancel = driver.find_element(
                        By.XPATH,
                        "//a[normalize-space()='CANCEL'] | //button[normalize-space()='Cancel']"
                    )
                    cancel.click()
                except Exception:
                    driver.get(EVENT_LIST_URL)
                time.sleep(2)
                continue

            snap(driver, f"attempt{attempt}_partners_added", log)

            # ── 6. Confirm Booking ──────────────────────────────────────────
            try:
                confirm_btn = WebDriverWait(driver, 8).until(
                    EC.element_to_be_clickable(
                        (By.XPATH,
                         "//button[.//span[contains(normalize-space(.),'Confirm Booking')]] | "
                         "//a[contains(normalize-space(.),'Confirm Booking')] | "
                         "//button[normalize-space()='Confirm Booking']")
                    )
                )
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", confirm_btn)
                try:
                    confirm_btn.click()
                except ElementClickInterceptedException:
                    driver.execute_script("arguments[0].click();", confirm_btn)
                log.info("Clicked Confirm Booking")
                time.sleep(1.5)
            except TimeoutException:
                log.error("Confirm Booking button not found")
                snap(driver, f"attempt{attempt}_no_confirm_btn", log)
                driver.get(EVENT_LIST_URL)
                time.sleep(3)
                continue

            # ── 7. Verify success ───────────────────────────────────────────
            snap(driver, f"attempt{attempt}_post_confirm", log)
            alerted, alert_text = safe_accept_alert(driver)

            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text
            except Exception:
                body_text = ""

            success_phrases = [
                "booking has been made", "successfully booked",
                "booking successful", "booking confirmed",
            ]
            if alerted and any(p in alert_text.lower() for p in success_phrases):
                log.info(f"✅ BOOKED via alert: {alert_text}")
                return True, row_id
            if any(p in body_text.lower() for p in success_phrases):
                log.info("✅ BOOKED (page text confirms).")
                return True, row_id

            # Redirect back to tee sheet often means success
            if has_tee_sheet(driver):
                log.info("✅ Redirected to tee sheet — assuming booking succeeded.")
                snap(driver, f"attempt{attempt}_teesheet_post_confirm", log)
                return True, row_id

            # Slot may have been taken mid-booking
            if alerted and alert_text:
                log.warning(f"Alert after confirm: {alert_text} — retrying")
                driver.get(EVENT_LIST_URL)
                time.sleep(2)
                continue

            log.warning("Booking status unclear — assuming success to avoid double-booking")
            return True, row_id

        except StaleElementReferenceException:
            log.warning("Stale element — refreshing")
            driver.refresh()
            time.sleep(3)
        except TimeoutException:
            log.warning("Timeout — refreshing")
            snap(driver, f"attempt{attempt}_timeout", log)
            driver.refresh()
            time.sleep(5)
        except Exception as exc:
            log.error(f"Unexpected error: {exc}")
            snap(driver, f"attempt{attempt}_error", log)
            try:
                driver.refresh()
            except Exception:
                driver.get(EVENT_LIST_URL)
            time.sleep(5)

    log.error(f"Failed to book after {max_attempts} attempts.")
    return False, ""


def _wait_for_tee_table(driver: webdriver.Chrome, log: logging.Logger, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if has_tee_sheet(driver):
            return True
        in_draw, _ = detect_draw(driver)
        if in_draw:
            time.sleep(1)
            continue
        in_queue, pos, _ = detect_queue(driver)
        if in_queue:
            time.sleep(0.5)
            continue
        time.sleep(0.25)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# FALLBACK: Book Group → Yes  (pre-configured group, handle already-booked)
# ─────────────────────────────────────────────────────────────────────────────

def execute_bookgroup_yes_fallback(
    driver: webdriver.Chrome,
    username: str,
    required_slots: int,
    log: logging.Logger,
    skip_row_ids: Optional[set] = None,
    cancel_event: Optional[multiprocessing.Event] = None,
) -> bool:
    """
    Fallback booking method: click BOOK GROUP → Yes (uses MiClub pre-configured group).

    If MiClub raises an alert that a member is already booked:
      - Dismiss the alert
      - Navigate to the makeBooking page for the same row
      - Find and remove the problematic player (click their red X)
      - Confirm with the remaining players

    Returns True on success.
    """
    log.info("▶ FALLBACK: Book Group → Yes method")
    deadline = hard_deadline_sydney()
    attempt  = 0
    locked_row_ids: set = set()
    locked_clear_time = time.time() + 30

    while attempt < BOOKING_MAX_ATTEMPTS and time.time() < deadline:
        if cancel_event and cancel_event.is_set():
            log.info("Another worker already completed this booking — aborting fallback.")
            return False
        attempt += 1
        log.info(f"Fallback attempt {attempt}...")

        # Clear locked rows periodically
        if time.time() > locked_clear_time:
            locked_row_ids.clear()
            locked_clear_time = time.time() + 30

        try:
            if not _wait_for_tee_table(driver, log, timeout=10):
                # Re-navigate into the event
                log.warning("Fallback: tee table not ready — re-navigating into event")
                cur_url = driver.current_url or ""
                if "event.msp" not in cur_url or "makeBooking" in cur_url or "eventList" in cur_url:
                    driver.get(EVENT_LIST_URL)
                    time.sleep(2)
                    try:
                        event_links = driver.find_elements(
                            By.XPATH,
                            "//div[contains(@class,'full')]//a[contains(@href,'booking_event_id')]"
                        )
                        if event_links:
                            event_links[0].click()
                            time.sleep(2)
                    except Exception:
                        pass
                else:
                    driver.refresh()
                    time.sleep(3)
                if not _wait_for_tee_table(driver, log, timeout=30):
                    log.warning("Fallback: still no tee table after re-navigation")
                    time.sleep(2)
                continue

            # Find first row with enough empty slots
            rows = driver.find_elements(By.XPATH, "//div[contains(@class,'row-time')]")
            target_row = None
            for row in rows:
                try:
                    empties = row.find_elements(By.XPATH, ".//button[contains(@class,'btn-book-me')]")
                    if len(empties) >= required_slots:
                        candidate_id = _get_row_id(row)
                        if skip_row_ids and candidate_id and candidate_id in skip_row_ids:
                            log.info(f"Fallback: skipping row_id={candidate_id} (used by another group)")
                            continue
                        if candidate_id and candidate_id in locked_row_ids:
                            log.info(f"Fallback: skipping row_id={candidate_id} (locked by another user)")
                            continue
                        target_row = row
                        break
                except StaleElementReferenceException:
                    continue

            if not target_row:
                log.info("Fallback: no suitable row — refreshing")
                driver.refresh()
                time.sleep(3)
                continue

            try:
                time_text = target_row.find_element(By.TAG_NAME, "h3").text
            except Exception:
                time_text = "(unknown)"
            log.info(f"Fallback target slot: {time_text}")
            snap(driver, f"fallback{attempt}_target_row", log)

            # Click BOOK GROUP
            btn = target_row.find_element(By.XPATH, ".//button[contains(@class,'btn-book-group')]")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            try:
                btn.click()
            except ElementClickInterceptedException:
                driver.execute_script("arguments[0].click();", btn)
            time.sleep(1)

            # Dismiss any unexpected alert (slot taken)
            alerted, alert_text = safe_accept_alert(driver)
            if alerted:
                fallback_rid = _get_row_id(target_row) if target_row else ""
                if fallback_rid:
                    locked_row_ids.add(fallback_rid)
                log.warning(f"Fallback: slot alert ({alert_text}) — skipping row_id={fallback_rid}, retrying")
                driver.refresh()
                time.sleep(2)
                continue

            # Click Yes on the "Book Your Playing Partners?" modal
            try:
                yes_btn = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable(
                        (By.XPATH, "//button[normalize-space()='Yes'] | //a[normalize-space()='Yes']")
                    )
                )
                snap(driver, f"fallback{attempt}_group_modal", log)
                try:
                    yes_btn.click()
                except ElementClickInterceptedException:
                    driver.execute_script("arguments[0].click();", yes_btn)
                log.info("Fallback: clicked Yes on group modal")
                time.sleep(1.5)
            except TimeoutException:
                log.warning("Fallback: group modal not found — may have gone direct")

            # Check for "already booked" alert
            alerted, alert_text = safe_accept_alert(driver)
            if alerted:
                already_booked_phrases = [
                    "already booked", "already has a booking",
                    "existing booking", "already registered",
                ]
                if any(p in alert_text.lower() for p in already_booked_phrases):
                    log.warning(f"Fallback: member already booked ({alert_text}). Switching to makeBooking page...")
                    snap(driver, f"fallback{attempt}_already_booked_alert", log)
                    # If we're still on the tee sheet, try again via No → manual remove
                    if has_tee_sheet(driver) or "makeBooking" not in driver.current_url:
                        # Click Book Group on the same/next slot and go via No path
                        try:
                            rows2 = driver.find_elements(By.XPATH, "//div[contains(@class,'row-time')]")
                            for row2 in rows2:
                                empties2 = row2.find_elements(By.XPATH, ".//button[contains(@class,'btn-book-me')]")
                                if len(empties2) >= required_slots:
                                    btn2 = row2.find_element(By.XPATH, ".//button[contains(@class,'btn-book-group')]")
                                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn2)
                                    btn2.click()
                                    time.sleep(1)
                                    safe_accept_alert(driver)
                                    no_btn = WebDriverWait(driver, 8).until(
                                        EC.element_to_be_clickable(
                                            (By.XPATH, "//button[normalize-space()='No'] | //a[normalize-space()='No']")
                                        )
                                    )
                                    no_btn.click()
                                    log.info("Fallback: switched to makeBooking via No path to remove already-booked player")
                                    break
                        except Exception as e2:
                            log.warning(f"Fallback: could not switch to No path: {e2}")
                            driver.get(EVENT_LIST_URL)
                            time.sleep(2)
                            continue

                    # On makeBooking page — find and remove error player slots
                    try:
                        WebDriverWait(driver, 10).until(lambda d: "makeBooking" in d.current_url)
                        time.sleep(1)
                        snap(driver, f"fallback{attempt}_makebooking_remove", log)

                        # Use shared error detection and removal helpers
                        cleared = _clear_already_booked_slots(driver, log)
                        log.info(f"Fallback: cleared {cleared} already-booked slot(s)")

                        snap(driver, f"fallback{attempt}_after_remove", log)
                        # Confirm with remaining players
                        confirm = WebDriverWait(driver, 8).until(
                            EC.element_to_be_clickable(
                                (By.XPATH,
                                 "//button[.//span[contains(normalize-space(.),'Confirm Booking')]] | "
                                 "//a[contains(normalize-space(.),'Confirm Booking')] | "
                                 "//button[normalize-space()='Confirm Booking']")
                            )
                        )
                        confirm.click()
                        time.sleep(1.5)
                        snap(driver, f"fallback{attempt}_post_confirm_remove", log)
                        alerted2, alert_text2 = safe_accept_alert(driver)
                        if has_tee_sheet(driver) or alerted2:
                            log.info("✅ Fallback: booked (after removing already-booked player)")
                            return True
                    except Exception as e3:
                        log.warning(f"Fallback: remove-and-rebook failed: {e3}")
                        driver.get(EVENT_LIST_URL)
                        time.sleep(2)
                        continue
                else:
                    log.warning(f"Fallback: unexpected alert after Yes: {alert_text}")
                    driver.get(EVENT_LIST_URL)
                    time.sleep(2)
                    continue

            # No alert — check for success
            snap(driver, f"fallback{attempt}_post_yes", log)
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text
            except Exception:
                body_text = ""
            success_phrases = [
                "booking has been made", "successfully booked",
                "booking successful", "booking confirmed",
            ]
            if any(p in body_text.lower() for p in success_phrases):
                log.info("✅ Fallback: booking confirmed (page text).")
                return True
            if has_tee_sheet(driver):
                log.info("✅ Fallback: redirected to tee sheet — assuming success.")
                return True
            # Might be on makeBooking page (modal went direct to booking form)
            if "makeBooking" in driver.current_url:
                try:
                    confirm = WebDriverWait(driver, 8).until(
                        EC.element_to_be_clickable(
                            (By.XPATH,
                             "//button[.//span[contains(normalize-space(.),'Confirm Booking')]] | "
                             "//a[contains(normalize-space(.),'Confirm Booking')] | "
                             "//button[normalize-space()='Confirm Booking']")
                        )
                    )
                    confirm.click()
                    time.sleep(1.5)
                    alerted3, _ = safe_accept_alert(driver)
                    if has_tee_sheet(driver) or alerted3:
                        log.info("✅ Fallback: booking confirmed via makeBooking confirm.")
                        return True
                except Exception:
                    pass

            driver.get(EVENT_LIST_URL)
            time.sleep(2)

        except Exception as exc:
            log.error(f"Fallback attempt {attempt} error: {exc}")
            snap(driver, f"fallback{attempt}_crash", log)
            try:
                driver.refresh()
            except Exception:
                driver.get(EVENT_LIST_URL)
            time.sleep(5)

    log.error("Fallback booking also failed.")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# GROUP HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def get_fourball_partners(username: str) -> List[str]:
    """Return member numbers to ADD (everyone in 4-ball group except self)."""
    base = list(FOUR_BALL_MEMBERS)
    if username in base:
        base.remove(username)
    else:
        # User is from 2-ball group but won the 4-ball race.
        # Replace ourselves into the 4-ball and drop the last 4-ball member.
        # Keeps the slot filled with 4 people.
        base = base[:-1]  # drop one 4-ball member to make room for self
    return base


def get_twoball_partner(username: str, fourball_winner: str) -> List[str]:
    """Return the partner(s) for the 2-ball, given who won the 4-ball."""
    # The 4 people in the 4-ball are: fourball_winner + get_fourball_partners(fourball_winner)
    in_fourball = {fourball_winner} | set(get_fourball_partners(fourball_winner))
    # Everyone else should be in the 2-ball
    remaining = [u["username"] for u in ALL_USERS if u["username"] not in in_fourball and u["username"] != username]
    return remaining[:1]  # Only need 1 partner for a 2-ball


# ─────────────────────────────────────────────────────────────────────────────
# WORKER PROCESS
# ─────────────────────────────────────────────────────────────────────────────
def worker(
    username: str,
    password: str,
    target_day: str,
    target_date: str,
    fourball_booked: multiprocessing.Event,
    twoball_booked: multiprocessing.Event,
    fourball_winner_val: multiprocessing.Value,
    fourball_row_id_val: multiprocessing.Value,
    worker_index: int = 0,
    login_delay: float = 0.0,
) -> None:
    log = make_worker_logger(username)
    log.info(f"Worker started (index={worker_index}, delay={login_delay:.0f}s). Target: {target_day} {target_date}")

    driver = None
    try:
        # Wait until login time then apply per-worker stagger BEFORE creating
        # the browser — this prevents Chrome from sitting idle for long periods
        # and crashing (the root cause of "Connection refused" worker deaths).
        wait_until_sydney(LOGIN_TIME[0], LOGIN_TIME[1], "Login gate", log)
        if login_delay > 0:
            jitter = random.uniform(0, 30)
            total_delay = login_delay + jitter
            log.info(f"Stagger delay: {login_delay:.0f}s + {jitter:.0f}s jitter = {total_delay:.0f}s")
            time.sleep(total_delay)

        # Create browser just before login — keeps session fresh
        driver = make_driver(log=log, worker_index=worker_index)

        if not login(driver, username, password, log):
            log.error("Login failed — exiting worker")
            discord_notify(f"❌ {MEMBER_TO_FIRST.get(username, username)}: login failed after {MAX_LOGIN_RETRIES} attempts", log)
            return

        discord_notify(f"🔑 {MEMBER_TO_FIRST.get(username, username)} logged in", log)

        if not navigate_and_wait_for_tee_sheet(driver, target_day, target_date, log, username, password):
            log.error("Could not reach tee sheet — exiting worker")
            return

        # ── Attempt 4-ball ────────────────────────────────────────────────
        if not fourball_booked.is_set():
            partners_4 = get_fourball_partners(username)
            row_id = ""

            # Only 4-ball members (2007-2010) have pre-configured default partners
            # matching the group, so they should use the fast "Yes" path first.
            is_fourball_member = username in FOUR_BALL_MEMBERS
            if is_fourball_member:
                log.info(f"Attempting 4-ball (Book Group → Yes — fast path). Partners: {partners_4}")
                discord_notify(f"🎯 {MEMBER_TO_FIRST.get(username, username)} attempting 4-ball booking (fast)...", log)
                success = execute_bookgroup_yes_fallback(driver, username, 4, log,
                                                          cancel_event=fourball_booked)
                if success:
                    # extract row_id from current URL if on makeBooking/tee sheet
                    try:
                        url_match = re.search(r"booking_row_id=(\d+)", driver.current_url)
                        if url_match:
                            row_id = url_match.group(1)
                    except Exception:
                        pass
            else:
                success = False

            if not success and not fourball_booked.is_set():
                log.info(f"Attempting 4-ball (search method — manual partner add). Adding: {partners_4}")
                discord_notify(f"🎯 {MEMBER_TO_FIRST.get(username, username)} attempting 4-ball booking (search)...", log)
                success, row_id = execute_search_booking(driver, username, partners_4, 4, log,
                                                          cancel_event=fourball_booked)

            if success and not fourball_booked.is_set():
                fourball_booked.set()
                try:
                    fourball_winner_val.value = username.encode()[:64]
                except Exception:
                    pass
                try:
                    fourball_row_id_val.value = row_id.encode()[:64]
                except Exception:
                    pass
                log.info(f"🏆 4-ball booked by {username}!")
                fourball_partners = get_fourball_partners(username)
                all_fourball = [username] + fourball_partners
                names = [MEMBER_TO_FIRST.get(m, m) for m in all_fourball]
                caption = f"✅ 4-ball BOOKED by {MEMBER_TO_FIRST.get(username, username)}!\nPlayers: {', '.join(names)}"
                discord_notify(caption, log)
                # Upload tee sheet screenshot showing the booking
                ss_path = RUN_DIR / f"fourball_confirmed_{username}.png"
                try:
                    driver.save_screenshot(str(ss_path))
                    discord_upload_screenshot(str(ss_path), "4-ball booking confirmed — tee sheet:", log)
                except Exception as exc:
                    log.warning(f"Failed to capture/upload 4-ball screenshot: {exc}")
                # Only the winning worker logs out — others stay on tee sheet
                # in case they're ahead in queue for other bookings
                logout(driver, log, username)
                return
            log.info("4-ball attempt complete (may have been taken by another worker)")
        else:
            log.info("4-ball already booked by another worker.")

        # ── Attempt 2-ball (ANY worker who is through can attempt this) ────
        if twoball_booked.is_set():
            log.info("2-ball already booked — nothing left to do.")
            logout(driver, log, username)
            return

        if not twoball_booked.is_set():
            try:
                winner = fourball_winner_val.value.decode().rstrip('\x00')
            except Exception:
                winner = ""
            partners_2 = get_twoball_partner(username, winner)
            try:
                skip_ids = set()
                raw = fourball_row_id_val.value
                log.info(f"Reading fourball_row_id_val: raw={raw!r}")
                frid = raw.decode().rstrip("\x00") if raw else ""
                if frid:
                    skip_ids.add(frid)
                    log.info(f"2-ball will skip row_id={frid} (used by 4-ball)")
                else:
                    log.warning("fourball_row_id_val is empty — cannot skip 4-ball row")
            except Exception as exc:
                log.warning(f"Failed to read fourball_row_id_val: {exc}")
                skip_ids = set()
            log.info(f"Attempting 2-ball (search method). Adding: {partners_2}")
            discord_notify(f"🎯 {MEMBER_TO_FIRST.get(username, username)} attempting 2-ball booking...", log)
            success, _ = execute_search_booking(
                driver,
                username,
                partners_2,
                2,
                log,
                skip_row_ids=skip_ids,
                cancel_event=twoball_booked,
            )

            if not success and not twoball_booked.is_set():
                log.warning("Search booking failed for 2-ball — trying Book Group → Yes fallback")
                success = execute_bookgroup_yes_fallback(driver, username, 2, log,
                                                          skip_row_ids=skip_ids,
                                                          cancel_event=twoball_booked)

            if success and not twoball_booked.is_set():
                twoball_booked.set()
                log.info(f"🏆 2-ball booked by {username}!")
                twoball_names = [MEMBER_TO_FIRST.get(username, username)] + [MEMBER_TO_FIRST.get(p, p) for p in partners_2]
                caption = f"✅ 2-ball BOOKED by {MEMBER_TO_FIRST.get(username, username)}!\nPlayers: {', '.join(twoball_names)}"
                discord_notify(caption, log)
                # Upload tee sheet screenshot showing the booking
                ss_path = RUN_DIR / f"twoball_confirmed_{username}.png"
                try:
                    driver.save_screenshot(str(ss_path))
                    discord_upload_screenshot(str(ss_path), "2-ball booking confirmed — tee sheet:", log)
                except Exception as exc:
                    log.warning(f"Failed to capture/upload 2-ball screenshot: {exc}")
                logout(driver, log, username)
                return
            log.info("2-ball attempt complete — both bookings likely handled by other workers")

        logout(driver, log, username)

    except Exception as exc:
        log.exception(f"Worker crashed: {exc}")
        discord_notify(f"💥 {MEMBER_TO_FIRST.get(username, username)} crashed: {exc}", log)
    finally:
        if driver:
            try:
                logout(driver, log, username)
            except Exception:
                pass
            try:
                driver.quit()
            except Exception:
                pass
        log.info("Worker finished.")


# ─────────────────────────────────────────────────────────────────────────────
# VERIFICATION  (checks tee sheet after booking, retries if missing players)
# ─────────────────────────────────────────────────────────────────────────────

def verify_bookings(
    target_day: str,
    target_date: str,
    log: logging.Logger,
    retry_fourball: bool = True,
    retry_twoball: bool = True,
) -> dict:
    """
    Log in as the first user, navigate to the target tee sheet, and check
    that all 6 players appear. Returns a dict with confirmed/missing lists.
    If players are missing, attempts to rebook them.
    """
    log.info("=== VERIFICATION: Checking tee sheet for all 6 players ===")
    result = {"confirmed": [], "missing": [], "tee_times": [], "screenshots": []}

    verifier = ALL_USERS[0]  # Use first account to verify
    driver = None
    try:
        driver = make_driver(log=log)
        if not login(driver, verifier["username"], verifier["password"], log):
            log.error("Verification: could not log in")
            return result

        if not navigate_and_wait_for_tee_sheet(driver, target_day, target_date, log,
                                               verifier["username"], verifier["password"]):
            log.error("Verification: tee sheet not reachable")
            return result

        # Read all rows and look for player names
        try:
            table = driver.find_element(By.CLASS_NAME, "teetime-day-table")
            rows  = table.find_elements(By.XPATH, ".//div[contains(@class,'row-time')]")
        except Exception as exc:
            log.error(f"Verification: could not read tee sheet: {exc}")
            return result

        sheet_text = table.text

        log.info("─── Tee sheet contents ───")
        our_row_idx = 0
        for row in rows:
            try:
                t = row.find_element(By.TAG_NAME, "h3").text
                links = row.find_elements(By.XPATH, ".//a[contains(@href,'member')]")
                names = [l.text.strip() for l in links if l.text.strip()]
                if names:
                    entry = f"{t}: {', '.join(names)}"
                    result["tee_times"].append(entry)
                    log.info(f"  {entry}")

                    # Screenshot rows containing any of our players
                    if any(s.lower() in " ".join(names).lower() for s in ALL_PLAYER_SURNAMES):
                        our_row_idx += 1
                        try:
                            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", row)
                            time.sleep(0.3)
                            shot_path = RUN_DIR / f"verify_booking_{our_row_idx}.png"
                            row.screenshot(str(shot_path))
                            result["screenshots"].append(str(shot_path))
                            log.info(f"  Screenshot: {shot_path.name}")
                        except Exception as ss_exc:
                            log.warning(f"  Row screenshot failed: {ss_exc}")
            except Exception:
                continue

        # Also take a full tee sheet screenshot
        snap(driver, "verify_teesheet_full", log)
        result["screenshots"].append(str(RUN_DIR / "verify_teesheet_full.png"))

        # Check each player surname
        for surname in ALL_PLAYER_SURNAMES:
            if surname.lower() in sheet_text.lower():
                result["confirmed"].append(surname)
                log.info(f"  ✅ {surname} — confirmed on tee sheet")
            else:
                result["missing"].append(surname)
                log.warning(f"  ❌ {surname} — NOT found on tee sheet")

        # Retry missing players if any
        if result["missing"] and (retry_fourball or retry_twoball):
            log.warning(f"Missing players: {result['missing']} — attempting to rebook")
            # Determine which group they belong to and attempt re-booking
            # Use current driver (already logged in as verifier)
            missing_in_4ball = [m for m in result["missing"]
                                if m.lower() in ["mullin","hillard","rutherford","rudge"]]
            missing_in_2ball = [m for m in result["missing"]
                                if m.lower() in ["lalor","cheney"]]

            if missing_in_4ball and retry_fourball:
                log.info(f"Re-attempting 4-ball for missing: {missing_in_4ball}")
                # Map surnames back to member numbers for re-booking
                name_to_member = {
                    "mullin": "2007", "hillard": "2008",
                    "rutherford": "2009", "rudge": "2010",
                    "lalor": "1101", "cheney": "1107",
                }
                partners = [name_to_member[n.lower()] for n in missing_in_4ball
                           if n.lower() in name_to_member and name_to_member[n.lower()] != verifier["username"]]
                if partners:
                    _, _ = execute_search_booking(driver, verifier["username"], partners, len(partners) + 1, log)

            if missing_in_2ball and retry_twoball:
                log.info(f"Re-attempting 2-ball for missing: {missing_in_2ball}")
                name_to_member = {
                    "mullin": "2007", "hillard": "2008",
                    "rutherford": "2009", "rudge": "2010",
                    "lalor": "1101", "cheney": "1107",
                }
                partners = [name_to_member[n.lower()] for n in missing_in_2ball
                           if n.lower() in name_to_member and name_to_member[n.lower()] != verifier["username"]]
                if partners:
                    _, _ = execute_search_booking(driver, verifier["username"], partners, len(partners) + 1, log)

        logout(driver, log)

    except Exception as exc:
        log.exception(f"Verification error: {exc}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    return result


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    log = logging.getLogger("main")

    target_day, target_date = compute_target()
    log.info(f"=== PARALLEL GOLF BOOKING BOT ===")
    log.info(f"Target date: {target_day} {target_date}")
    log.info(f"4-ball group: {FOUR_BALL_MEMBERS}")
    log.info(f"2-ball group: {TWO_BALL_MEMBERS}")
    log.info(f"Workers: {[u['username'] for u in ALL_USERS]}")
    log.info(f"Login stagger: {LOGIN_STAGGER_SECS}s between workers")
    log.info(f"Logs: {RUN_DIR}")

    discord_notify(f"🏌️ Booking run started — targeting {target_day} {target_date}", log)

    manager: SyncManager
    with multiprocessing.Manager() as manager:
        fourball_booked    = manager.Event()
        twoball_booked     = manager.Event()
        fourball_winner_val = manager.Value(bytes, b"")
        fourball_row_id_val = manager.Value(bytes, b"")

        processes = []
        for idx, user in enumerate(ALL_USERS):
            delay = idx * LOGIN_STAGGER_SECS  # 0s, 2m, 4m, 6m, 8m, 10m
            p = multiprocessing.Process(
                target=worker,
                args=(
                    user["username"],
                    user["password"],
                    target_day,
                    target_date,
                    fourball_booked,
                    twoball_booked,
                    fourball_winner_val,
                    fourball_row_id_val,
                    idx,
                    float(delay),
                ),
                name=f"worker-{user['username']}",
            )
            p.start()
            log.info(f"Started worker for {user['username']} (pid {p.pid}, delay={delay}s)")
            processes.append(p)

        # Wait for both bookings to complete, or all workers to finish — hard stop at 8pm
        deadline = hard_deadline_sydney()
        while time.time() < deadline:
            if fourball_booked.is_set() and twoball_booked.is_set():
                log.info("✅ Both bookings complete!")
                break
            alive = [p for p in processes if p.is_alive()]
            if not alive:
                log.info("All workers finished.")
                break
            time.sleep(5)

        # Capture states BEFORE manager shuts down
        fourball_ok = fourball_booked.is_set()
        twoball_ok  = twoball_booked.is_set()

        # Give workers time to detect events and log out cleanly
        alive = [p for p in processes if p.is_alive()]
        if alive and fourball_booked.is_set() and twoball_booked.is_set():
            log.info(f"Waiting up to 30s for {len(alive)} remaining workers to log out...")
            for _ in range(15):
                alive = [p for p in processes if p.is_alive()]
                if not alive:
                    break
                time.sleep(2)

        # Force-terminate any workers that didn't exit cleanly
        for p in processes:
            if p.is_alive():
                log.info(f"Terminating {p.name}")
                p.terminate()
                p.join(timeout=10)

    # Summary
    log.info("=== SUMMARY ===")
    log.info(f"4-ball booked: {fourball_ok}")
    log.info(f"2-ball booked: {twoball_ok}")
    log.info(f"Log directory: {RUN_DIR}")

    # Verification — confirm all 6 players appear on the tee sheet
    verify_result = {"confirmed": [], "missing": [], "tee_times": []}
    if fourball_ok or twoball_ok:
        log.info("Waiting 30s for bookings to propagate before verifying...")
        time.sleep(30)
        verify_result = verify_bookings(target_day, target_date, log)
    else:
        log.warning("No bookings confirmed — skipping verification")

    # Discord final summary
    four_emoji = "✅" if fourball_ok else "❌"
    two_emoji = "✅" if twoball_ok else "❌"
    confirmed = verify_result.get("confirmed", [])
    missing = verify_result.get("missing", [])
    summary = f"📊 Final: 4-ball {four_emoji} | 2-ball {two_emoji}"
    if confirmed:
        summary += f" | Verified: {', '.join(confirmed)}"
    if missing:
        summary += f" | Missing: {', '.join(missing)}"
    discord_notify(summary, log)

    # Upload key screenshots to Discord
    for ss_path in verify_result.get("screenshots", [])[:3]:
        discord_upload_screenshot(ss_path, "📸 Tee sheet verification", log)

    # Zip logs
    zip_path = RUN_ROOT / f"{RUN_ID}.zip"
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in RUN_DIR.rglob("*"):
                zf.write(f, arcname=f.relative_to(RUN_DIR))
        log.info(f"Evidence bundle: {zip_path}")
    except Exception as exc:
        log.warning(f"ZIP failed: {exc}")


if __name__ == "__main__":
    main()
