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

import logging
import multiprocessing
import os
import re
import shutil
import smtplib
import subprocess
import time
import zipfile
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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

# Email notification (Gmail SMTP + App Password)
NOTIFICATION_EMAIL = "malachy.m.mullin@gmail.com"
GMAIL_USER         = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

# Player surnames for tee-sheet verification (used after booking to confirm)
ALL_PLAYER_SURNAMES = ["Mullin", "Hillard", "Rutherford", "Rudge", "Lalor", "Cheney"]

# All accounts that will enter the draw (credentials from env vars or defaults)
ALL_USERS = [
    {"username": os.getenv("MIGOLF_USER_1", "2007"), "password": os.getenv("MIGOLF_PASS_1", "Golf123#")},
    {"username": os.getenv("MIGOLF_USER_2", "1107"), "password": os.getenv("MIGOLF_PASS_2", "Golf123#")},
    {"username": os.getenv("MIGOLF_USER_3", "2008"), "password": os.getenv("MIGOLF_PASS_3", "Golf123#")},
    {"username": os.getenv("MIGOLF_USER_4", "2009"), "password": os.getenv("MIGOLF_PASS_4", "Golf123#")},
    {"username": os.getenv("MIGOLF_USER_5", "2010"), "password": os.getenv("MIGOLF_PASS_5", "Golf123#")},
    {"username": os.getenv("MIGOLF_USER_6", "1101"), "password": os.getenv("MIGOLF_PASS_6", "Golf123#")},
]

# ─────────────────────────────────────────────────────────────────────────────
# URLS & TIMING
# ─────────────────────────────────────────────────────────────────────────────
LOGIN_URL      = "https://macquarielinks.miclub.com.au/security/login.msp"
EVENT_LIST_URL = "https://macquarielinks.miclub.com.au/views/members/booking/eventList.xhtml"
LOGOUT_URL     = "https://macquarielinks.miclub.com.au/security/logout.msp"

LOGIN_TIME        = (18,  0)  # login at 6:00pm Sydney
QUEUE_JOIN_TIME   = (18, 30)  # ballot opens at 6:30pm — click event link here
BOOKING_OPEN_TIME = (19,  0)  # tee sheet releases at 7:00pm
HARD_TIMEOUT_TIME = (20,  0)  # give up at 8:00pm — no earlier

OPEN_POLL_INTERVAL = 15  # seconds between event-list refreshes before draw (6 workers × 15s = low load)
BOOKING_MAX_ATTEMPTS = 999  # effectively unlimited — hard deadline is HARD_TIMEOUT_TIME

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


# ─────────────────────────────────────────────────────────────────────────────
# DATE TARGET  (next-next Saturday from Thursday)
# ─────────────────────────────────────────────────────────────────────────────
def compute_target() -> Tuple[str, str]:
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
def make_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--remote-debugging-pipe")
    driver_path = shutil.which("chromedriver")
    svc = Service(executable_path=driver_path) if driver_path else Service()
    for attempt in range(1, 3):
        try:
            drv = webdriver.Chrome(options=opts, service=svc)
            drv.set_page_load_timeout(90)
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
def login(driver: webdriver.Chrome, username: str, password: str, log: logging.Logger) -> bool:
    log.info(f"Logging in...")
    driver.get(LOGIN_URL)
    try:
        uf = WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.NAME, "user")))
        uf.clear(); uf.send_keys(username)
        pf = driver.find_element(By.NAME, "password")
        pf.clear(); pf.send_keys(password)
        driver.find_element(By.XPATH, "//input[@value='Login']").click()
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.XPATH, "//a[contains(@href,'logout')]")))
        log.info("Login successful")
        return True
    except Exception as exc:
        log.error(f"Login failed: {exc}")
        return False


def logout(driver: webdriver.Chrome, log: logging.Logger) -> None:
    try:
        driver.get(LOGOUT_URL)
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.NAME, "user")))
        log.info("Logged out")
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
) -> bool:
    """Enter draw/queue and wait for tee sheet. Does NOT refresh once in draw/queue."""
    log.info("Navigating to event list...")
    driver.get(EVENT_LIST_URL)

    in_waiting_room  = False
    draw_attempted   = False
    deadline         = hard_deadline_sydney()   # hard stop at 8pm Sydney
    last_status_log  = 0.0

    while time.time() < deadline:
        now = time.time()

        if in_waiting_room:
            if has_tee_sheet(driver):
                log.info("✅ Tee sheet visible!")
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
def _find_empty_player_inputs(driver: webdriver.Chrome) -> list:
    """
    Return empty player search input fields on the makeBooking page.
    MiClub uses PrimeFaces AutoComplete — the visible input has class 'ui-autocomplete-input'.
    Player 1 is always pre-filled (logged-in user), so we skip inputs with existing values.
    """
    try:
        # Primary: PrimeFaces AutoComplete visible input
        selectors = [
            "//input[contains(@class,'ui-autocomplete-input') and not(@type='hidden')]",
            "//input[contains(@placeholder,'Find Player') or contains(@placeholder,'find player') "
            "or contains(@placeholder,'Search') or contains(@placeholder,'Player')]",
            "//input[@type='text' and (contains(@class,'inputfield') or contains(@class,'autocomplete'))]",
        ]
        for sel in selectors:
            inputs = driver.find_elements(By.XPATH, sel)
            empties = [i for i in inputs if i.is_displayed() and not (i.get_attribute("value") or "").strip()]
            if empties:
                return empties
        return []
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
    """
    # Build a lookup: member_number → surname
    member_to_surname = {
        "2007": "Mullin",  "2008": "Hillard",
        "2009": "Rutherford", "2010": "Rudge",
        "1101": "Lalor",   "1107": "Cheney",
    }
    target_names = {member_to_surname.get(p, p).lower() for p in partners_to_add}
    added = 0
    try:
        # Checkboxes are rendered as <input type='checkbox'> with adjacent <label> text,
        # or wrapped in a <span>/<div> with the player name.
        checkboxes = driver.find_elements(
            By.XPATH,
            "//input[@type='checkbox']"
        )
        for cb in checkboxes:
            if not cb.is_displayed():
                continue
            try:
                # Get label text — look at associated label, parent text, or sibling text
                cb_id = cb.get_attribute("id") or ""
                label_text = ""
                if cb_id:
                    try:
                        label = driver.find_element(By.XPATH, f"//label[@for='{cb_id}']")
                        label_text = label.text.strip().lower()
                    except Exception:
                        pass
                if not label_text:
                    try:
                        label_text = cb.find_element(By.XPATH, "./parent::*/text()").get_attribute("textContent") or ""
                        label_text = label_text.strip().lower()
                    except Exception:
                        pass
                if not label_text:
                    try:
                        label_text = cb.find_element(By.XPATH, "./following-sibling::*[1]").text.strip().lower()
                    except Exception:
                        pass
                if not label_text:
                    try:
                        parent = cb.find_element(By.XPATH, "./parent::*")
                        label_text = parent.text.strip().lower()
                    except Exception:
                        pass

                if any(name in label_text for name in target_names):
                    if not cb.is_selected():
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", cb)
                        try:
                            cb.click()
                        except ElementClickInterceptedException:
                            driver.execute_script("arguments[0].click();", cb)
                        log.info(f"Ticked checkbox: {label_text}")
                        added += 1
                        time.sleep(0.3)
            except Exception as e:
                log.debug(f"Checkbox check error: {e}")
    except Exception as exc:
        log.warning(f"Checkbox selection error: {exc}")
    return added


def _search_and_select_player(
    driver: webdriver.Chrome,
    input_el,
    member_number: str,
    log: logging.Logger,
) -> bool:
    """Type member number into a Find Player field, wait for autocomplete, click result."""
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", input_el)
        input_el.click()
        time.sleep(0.3)
        input_el.clear()
        input_el.send_keys(member_number)
        log.info(f"Searching for player {member_number}...")

        # Wait for autocomplete dropdown
        deadline = time.time() + 10
        result = None
        while time.time() < deadline:
            # Common autocomplete patterns for JSF/PrimeFaces
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
            # Fallback: try pressing Enter (some sites accept member number directly)
            log.warning(f"No autocomplete for {member_number}, trying Enter key")
            input_el.send_keys(Keys.RETURN)
            time.sleep(0.5)
            # Check if the field now has a name (accepted)
            val = input_el.get_attribute("value") or ""
            if val and not val.isdigit():
                log.info(f"Player accepted via Enter: {val}")
                return True
            return False

        log.info(f"Selecting: {result.text.strip()}")
        try:
            result.click()
        except ElementClickInterceptedException:
            driver.execute_script("arguments[0].click();", result)
        time.sleep(0.4)
        return True

    except Exception as exc:
        log.error(f"Search/select failed for {member_number}: {exc}")
        return False


def execute_search_booking(
    driver: webdriver.Chrome,
    username: str,
    partners_to_add: List[str],
    required_slots: int,
    log: logging.Logger,
    max_attempts: int = BOOKING_MAX_ATTEMPTS,
) -> bool:
    """
    Find a slot with enough empty spaces, click Book Group, click No on the modal,
    then on makeBooking.xhtml add each partner by member-number search, and confirm.

    partners_to_add: member numbers of players to add (not including logged-in user)
    required_slots:  total players needed (4 for 4-ball, 2 for 2-ball)
    """
    log.info(f"Starting search-based booking for {required_slots}-ball. Partners: {partners_to_add}")
    deadline = hard_deadline_sydney()

    attempt = 0
    while attempt < max_attempts and time.time() < deadline:
        attempt += 1
        mins_remaining = max(0, (deadline - time.time()) / 60)
        log.info(f"Booking attempt {attempt} ({mins_remaining:.0f} min until 8pm timeout)...")

        try:
            # ── 1. Find a suitable row ─────────────────────────────────────
            if not _wait_for_tee_table(driver, log, timeout=60):
                log.warning("Tee table not ready — refreshing")
                driver.refresh()
                time.sleep(4)
                continue

            rows = driver.find_elements(By.XPATH, "//div[contains(@class,'row-time')]")
            target_row = None
            for row in rows:
                try:
                    empties = row.find_elements(By.XPATH, ".//button[contains(@class,'btn-book-me')]")
                    if len(empties) >= required_slots:
                        target_row = row
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
                log.info(f"Slot locked (alert: {alert_text}) — trying next slot")
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

            # Log reservation timer if visible
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text
                m = re.search(r"Seconds remaining.*?(\d+)", body_text)
                if m:
                    log.info(f"Reservation timer: {m.group(1)}s remaining")
            except Exception:
                pass

            # ── 5. Add partners ────────────────────────────────────────────
            # Strategy A: click the pre-configured "Select Partners" checkboxes
            # Strategy B: type member number into PrimeFaces autocomplete fields
            # Strategy A is faster and more reliable when pre-configured groups match.

            ticked = _try_select_partners_checkboxes(driver, partners_to_add, log)
            log.info(f"Checkbox strategy: ticked {ticked}/{len(partners_to_add)} partners")

            # Check how many Find Player inputs are still empty after checkboxes
            still_empty = _find_empty_player_inputs(driver)
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

            # Strategy B: autocomplete search for any partners not yet added
            remaining_partners = partners_to_add[ticked:] if ticked < len(partners_to_add) else []

            # If a member is already in the tee sheet / already booked,
            # skip them and fill the slot with the next available partner.
            skipped: List[str] = []
            for member_num in remaining_partners:
                empty_inputs = _find_empty_player_inputs(driver)
                if not empty_inputs:
                    log.info("No more empty Find Player slots — all filled.")
                    break
                ok = _search_and_select_player(driver, empty_inputs[0], member_num, log)
                if not ok:
                    log.warning(f"Could not add {member_num} (may already be booked) — skipping")
                    skipped.append(member_num)
                    continue
                time.sleep(0.5)

                # Check for "already booked" error message after adding
                try:
                    body = driver.find_element(By.TAG_NAME, "body").text
                    already_booked_phrases = [
                        "already booked", "already has a booking",
                        "existing booking", "already registered",
                    ]
                    if any(p in body.lower() for p in already_booked_phrases):
                        log.warning(f"Player {member_num} already has a booking — removing and skipping")
                        # Try to remove them (click the red X next to their name)
                        try:
                            remove_btns = driver.find_elements(
                                By.XPATH,
                                f"//input[contains(@value,'{member_num}') or "
                                f"contains(following-sibling::*,'{member_num}')]"
                                "/ancestor::*[1]//a[contains(@class,'remove') or contains(@class,'delete') or @title='Remove']"
                            )
                            if remove_btns:
                                remove_btns[0].click()
                                time.sleep(0.5)
                        except Exception:
                            pass
                        skipped.append(member_num)
                except Exception:
                    pass

            if skipped:
                log.info(f"Skipped already-booked members: {skipped}. Proceeding with remaining players.")

            # We need at least 1 partner added (plus self = 2 minimum)
            empty_remaining = _find_empty_player_inputs(driver)
            total_filled = (required_slots - 1) - len(empty_remaining)  # -1 because Player 1 is self
            if total_filled < 1 and required_slots > 1:
                log.warning("No partners could be added at all — cancelling and retrying slot")
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
                         "//a[normalize-space()='Confirm Booking'] | "
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
                return True
            if any(p in body_text.lower() for p in success_phrases):
                log.info("✅ BOOKED (page text confirms).")
                return True

            # Redirect back to tee sheet often means success
            if has_tee_sheet(driver):
                log.info("✅ Redirected to tee sheet — assuming booking succeeded.")
                snap(driver, f"attempt{attempt}_teesheet_post_confirm", log)
                return True

            # Slot may have been taken mid-booking
            if alerted and alert_text:
                log.warning(f"Alert after confirm: {alert_text} — retrying")
                driver.get(EVENT_LIST_URL)
                time.sleep(2)
                continue

            log.warning("Booking status unclear — assuming success to avoid double-booking")
            return True

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
    return False


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

    while attempt < BOOKING_MAX_ATTEMPTS and time.time() < deadline:
        attempt += 1
        log.info(f"Fallback attempt {attempt}...")

        try:
            if not _wait_for_tee_table(driver, log, timeout=60):
                driver.refresh()
                time.sleep(4)
                continue

            # Find first row with enough empty slots
            rows = driver.find_elements(By.XPATH, "//div[contains(@class,'row-time')]")
            target_row = None
            for row in rows:
                try:
                    empties = row.find_elements(By.XPATH, ".//button[contains(@class,'btn-book-me')]")
                    if len(empties) >= required_slots:
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
                log.warning(f"Fallback: slot alert ({alert_text}) — retrying")
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

                    # On makeBooking page — find player fields with errors and remove them
                    try:
                        WebDriverWait(driver, 10).until(lambda d: "makeBooking" in d.current_url)
                        time.sleep(1)
                        snap(driver, f"fallback{attempt}_makebooking_remove", log)
                        # Remove any player showing an error (red background / error class)
                        # Common patterns: the field has an error style or the row has an error indicator
                        error_fields = driver.find_elements(
                            By.XPATH,
                            "//*[contains(@class,'error') or contains(@class,'invalid') or "
                            "contains(@class,'ui-state-error')]//input | "
                            "//input[contains(@class,'error') or contains(@class,'invalid')]"
                        )
                        for ef in error_fields:
                            if ef.is_displayed():
                                # Find the remove/X button nearby
                                try:
                                    parent = ef.find_element(By.XPATH, "./ancestor::td[1] | ./ancestor::div[1]")
                                    remove = parent.find_element(
                                        By.XPATH,
                                        ".//a[contains(@class,'remove') or contains(@onclick,'remove') or "
                                        "@title='Remove'] | .//button[contains(@class,'remove')]"
                                    )
                                    remove.click()
                                    log.info("Fallback: removed already-booked player from slot")
                                    time.sleep(0.5)
                                except Exception:
                                    # Try clearing the field
                                    try:
                                        ef.clear()
                                        ef.send_keys(Keys.DELETE)
                                    except Exception:
                                        pass

                        snap(driver, f"fallback{attempt}_after_remove", log)
                        # Confirm with remaining players
                        confirm = WebDriverWait(driver, 8).until(
                            EC.element_to_be_clickable(
                                (By.XPATH,
                                 "//a[normalize-space()='Confirm Booking'] | "
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
                             "//a[normalize-space()='Confirm Booking'] | "
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
) -> None:
    log = make_worker_logger(username)
    log.info(f"Worker started. Target: {target_day} {target_date}")

    driver = None
    try:
        driver = make_driver()

        # Wait until 6:00pm Sydney before logging in
        wait_until_sydney(LOGIN_TIME[0], LOGIN_TIME[1], "Login gate", log)

        if not login(driver, username, password, log):
            log.error("Login failed — exiting worker")
            return

        if not navigate_and_wait_for_tee_sheet(driver, target_day, target_date, log):
            log.error("Could not reach tee sheet — exiting worker")
            return

        # ── Attempt 4-ball ────────────────────────────────────────────────
        if not fourball_booked.is_set():
            partners_4 = get_fourball_partners(username)
            log.info(f"Attempting 4-ball (search method). Adding: {partners_4}")
            success = execute_search_booking(driver, username, partners_4, 4, log)

            if not success and not fourball_booked.is_set():
                log.warning("Search booking failed — trying Book Group → Yes fallback")
                success = execute_bookgroup_yes_fallback(driver, username, 4, log)

            if success and not fourball_booked.is_set():
                fourball_booked.set()
                try:
                    fourball_winner_val.value = username.encode()[:64]
                except Exception:
                    pass
                log.info(f"🏆 4-ball booked by {username}!")
                logout(driver, log)
                return
            log.info("4-ball attempt complete (may have been taken by another worker)")
        else:
            log.info("4-ball already booked by another worker.")

        # 4-ball members have no role in the 2-ball — exit cleanly
        if username in FOUR_BALL_MEMBERS:
            log.info(f"{username} is a 4-ball member — nothing more to do.")
            logout(driver, log)
            return

        # ── Attempt 2-ball (only 2-ball group members reach here) ─────────
        if not twoball_booked.is_set():
            try:
                winner = fourball_winner_val.value.decode().rstrip('\x00')
            except Exception:
                winner = ""
            partners_2 = get_twoball_partner(username, winner)
            log.info(f"Attempting 2-ball (search method). Adding: {partners_2}")
            success = execute_search_booking(driver, username, partners_2, 2, log)

            if not success and not twoball_booked.is_set():
                log.warning("Search booking failed for 2-ball — trying Book Group → Yes fallback")
                success = execute_bookgroup_yes_fallback(driver, username, 2, log)

            if success and not twoball_booked.is_set():
                twoball_booked.set()
                log.info(f"🏆 2-ball booked by {username}!")
                logout(driver, log)
                return
            log.info("2-ball attempt complete — both bookings likely handled by other workers")
        else:
            log.info("2-ball already booked — nothing left to do")

        logout(driver, log)

    except Exception as exc:
        log.exception(f"Worker crashed: {exc}")
    finally:
        if driver:
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
    result = {"confirmed": [], "missing": [], "tee_times": []}

    verifier = ALL_USERS[0]  # Use first account to verify
    driver = None
    try:
        driver = make_driver()
        if not login(driver, verifier["username"], verifier["password"], log):
            log.error("Verification: could not log in")
            return result

        if not navigate_and_wait_for_tee_sheet(driver, target_day, target_date, log):
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
        snap(driver, "verify_teesheet", log)

        log.info("─── Tee sheet contents ───")
        for row in rows:
            try:
                t = row.find_element(By.TAG_NAME, "h3").text
                links = row.find_elements(By.XPATH, ".//a[contains(@href,'member')]")
                names = [l.text.strip() for l in links if l.text.strip()]
                if names:
                    entry = f"{t}: {', '.join(names)}"
                    result["tee_times"].append(entry)
                    log.info(f"  {entry}")
            except Exception:
                continue

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
                                if any(m.lower() in ["mullin","hillard","rutherford","rudge"])]
            missing_in_2ball = [m for m in result["missing"]
                                if any(m.lower() in ["lalor","cheney"])]

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
                    execute_search_booking(driver, verifier["username"], partners, len(partners) + 1, log)

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
                    execute_search_booking(driver, verifier["username"], partners, len(partners) + 1, log)

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
# EMAIL NOTIFICATION
# ─────────────────────────────────────────────────────────────────────────────

def send_confirmation_email(
    target_date: str,
    result: dict,
    fourball_ok: bool,
    twoball_ok: bool,
    log: logging.Logger,
) -> None:
    """Send a booking confirmation (or failure alert) to NOTIFICATION_EMAIL via Gmail SMTP."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        log.warning("Email: GMAIL_USER or GMAIL_APP_PASSWORD not set — skipping email")
        return

    all_confirmed = len(result.get("missing", [])) == 0
    subject = (
        f"⛳ Golf Booking {'Confirmed' if all_confirmed else 'PARTIAL/FAILED'} — {target_date}"
    )

    lines = [
        f"Golf booking run complete for {target_date}.",
        "",
        f"4-ball booked: {'✅ Yes' if fourball_ok else '❌ No'}",
        f"2-ball booked: {'✅ Yes' if twoball_ok else '❌ No'}",
        "",
        "── Tee Sheet Verification ──",
    ]
    if result.get("confirmed"):
        lines.append(f"Confirmed on sheet: {', '.join(result['confirmed'])}")
    if result.get("missing"):
        lines.append(f"⚠️  NOT found on sheet: {', '.join(result['missing'])}")
    else:
        lines.append("All 6 players confirmed on the tee sheet ✅")

    if result.get("tee_times"):
        lines += ["", "── Your tee times ──"]
        lines += [f"  {t}" for t in result["tee_times"]
                  if any(s.lower() in t.lower() for s in ALL_PLAYER_SURNAMES)]

    body = "\n".join(lines)
    log.info(f"Email body:\n{body}")

    try:
        msg = MIMEMultipart()
        msg["From"]    = GMAIL_USER
        msg["To"]      = NOTIFICATION_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, NOTIFICATION_EMAIL, msg.as_string())

        log.info(f"✉️  Confirmation email sent to {NOTIFICATION_EMAIL}")
    except Exception as exc:
        log.error(f"Email failed: {exc}")


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
    log.info(f"Logs: {RUN_DIR}")

    manager: SyncManager
    with multiprocessing.Manager() as manager:
        fourball_booked    = manager.Event()
        twoball_booked     = manager.Event()
        fourball_winner_val = manager.Value(bytes, b"")

        processes = []
        for user in ALL_USERS:
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
                ),
                name=f"worker-{user['username']}",
            )
            p.start()
            log.info(f"Started worker for {user['username']} (pid {p.pid})")
            processes.append(p)
            time.sleep(0.5)  # stagger starts slightly

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

        # Clean shutdown
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

    # Email
    send_confirmation_email(target_date, verify_result, fourball_ok, twoball_ok, log)

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
