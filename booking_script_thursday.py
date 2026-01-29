"""Golf booking automation script for PythonAnywhere (Thursday variant).

This module provisions a Selenium-based job that monitors the Macquarie Links
MiClub portal and attempts to reserve tee-times for pre-defined player groups.
The code is intentionally verbose and defensive so that it can operate in a
headless, scheduled environment such as PythonAnywhere.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Tuple

try:
    import zoneinfo
except Exception:  # pragma: no cover - zoneinfo always available on Py3.9+
    zoneinfo = None

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoAlertPresentException,
    StaleElementReferenceException,
    TimeoutException,
    UnexpectedAlertPresentException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


from selenium.webdriver.remote.webelement import WebElement

import re

# ------------------------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------------------------
LOGIN_URL = "https://macquarielinks.miclub.com.au/security/login.msp"
EVENT_LIST_URL = "https://macquarielinks.miclub.com.au/views/members/booking/eventList.xhtml"
LOGOUT_URL = "https://macquarielinks.miclub.com.au/security/logout.msp"

BOOKER_1_USERNAME = os.getenv("MIGOLF_USER_1", "2007")
BOOKER_1_PASSWORD = os.getenv("MIGOLF_PASS_1", "Golf123#")
GROUP_1_SIZE = 4
GROUP_1_MAX_ATTEMPTS = 60  # ~5 minutes

BOOKER_2_USERNAME = os.getenv("MIGOLF_USER_2", "1107")
BOOKER_2_PASSWORD = os.getenv("MIGOLF_PASS_2", "Golf123#")
GROUP_2_SIZE = 2
GROUP_2_MAX_ATTEMPTS = 36  # ~3 minutes

BOOKER_3_USERNAME = os.getenv("MIGOLF_USER_3", "2008")
BOOKER_3_PASSWORD = os.getenv("MIGOLF_PASS_3", "Golf123#")
GROUP_3_SIZE = 4
GROUP_3_MAX_ATTEMPTS = 180  # ~15 minutes (backup)

PLAYERS_TO_VERIFY = [
    "Mullin",
    "Hillard",
    "Rutherford",
    "Rudge",
    "Lalor",
    "Cheney",
]

EXPECTED_GROUPS = {
    "2007": ["Mullin", "Hillard", "Rutherford", "Rudge"],
    "1107": ["Cheney", "Lalor"],
    # "2008": [...],  # add if you want modal validation for fallback too
}


def expected_group_for(booker_username: str) -> List[str]:
    return EXPECTED_GROUPS.get(booker_username, [])


OPEN_POLL_INTERVAL_SEC = 2
YES_BUTTON_WAIT_SEC = 15
TEE_SHEET_WAIT_FIRST = 120  # after 19:00 rush
TEE_SHEET_WAIT_SUBSEQUENT = 60

SYDNEY_TZ = zoneinfo.ZoneInfo("Australia/Sydney") if zoneinfo else timezone.utc
QUEUE_ACCESS_START_TIME = (18, 0)  # 6:00pm Sydney – begin polling event list
QUEUE_JOIN_TIME = (18, 30)  # 6:30pm Sydney queue unlock
BOOKING_OPEN_TIME = (19, 0)  # 7:00pm Sydney booking release

# Logging/snapshot paths
RUN_ROOT_ENV = os.getenv("GOLFBOT_RUN_ROOT")
RUN_ROOT = Path(RUN_ROOT_ENV).expanduser() if RUN_ROOT_ENV else Path.home() / "golfbot_logs"
RUN_ROOT.mkdir(parents=True, exist_ok=True)
RUN_ID = datetime.now().strftime("run_%Y-%m-%d_%H-%M-%S")
RUN_DIR = RUN_ROOT / RUN_ID
RUN_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = RUN_DIR / "run.log"
ZIP_PATH = RUN_ROOT / f"{RUN_ID}.zip"


# ------------------------------------------------------------------------------------
# UTIL: logging + snapshots
# ------------------------------------------------------------------------------------
def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log(msg: str) -> None:
    line = f"[{ts()}] {msg}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def snap_png(driver: webdriver.Chrome, name: str) -> None:
    path = RUN_DIR / f"{name}.png"
    try:
        driver.save_screenshot(str(path))
        log(f"Saved screenshot: {path}")
    except Exception as exc:  # noqa: BLE001 - logging for diagnostics
        log(f"WARNING: Failed to save screenshot ({name}): {exc}")


def snap_html(driver: webdriver.Chrome, name: str) -> None:
    path = RUN_DIR / f"{name}.html"
    try:
        html = driver.page_source
        path.write_text(html, encoding="utf-8")
        log(f"Saved HTML snapshot: {path}")
    except Exception as exc:  # noqa: BLE001 - logging for diagnostics
        log(f"WARNING: Failed to save HTML ({name}): {exc}")


def now_in_sydney() -> datetime:
    """Return the current time in the Australia/Sydney timezone."""

    now_utc = datetime.now(timezone.utc)
    return now_utc.astimezone(SYDNEY_TZ) if SYDNEY_TZ is not timezone.utc else now_utc


def wait_until_local_time(target_hour: int, target_minute: int, label: str) -> None:
    """Sleep until the requested Sydney time is reached (if not already past)."""

    local_now = now_in_sydney()
    target = local_now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
    if local_now >= target:
        log(
            f"{label}: already at/past {target_hour:02d}:{target_minute:02d} "
            f"{local_now.tzname() or 'local'} (current {local_now:%H:%M:%S})."
        )
        return

    log(
        f"{label}: waiting until {target:%H:%M %Z}. "
        f"Current time {local_now:%H:%M:%S %Z}."
    )
    while True:
        local_now = now_in_sydney()
        if local_now >= target:
            log(
                f"{label}: reached {target:%H:%M %Z}. Continuing workflow "
                f"(current {local_now:%H:%M:%S %Z})."
            )
            return
        seconds_left = (target - local_now).total_seconds()
        if seconds_left <= 5:
            time.sleep(seconds_left)
            continue
        if seconds_left > 600:
            sleep_for = 300
        elif seconds_left > 180:
            sleep_for = 60
        elif seconds_left > 60:
            sleep_for = 30
        else:
            sleep_for = 10
        log(
            f"{label}: {seconds_left/60:.1f} min remaining "
            f"(sleeping {sleep_for}s; current {local_now:%H:%M:%S %Z})."
        )
        time.sleep(sleep_for)


def hold_until_queue_poll_window(driver: webdriver.Chrome) -> None:
    """Keep the session warm until the 18:00 Sydney queue polling window."""

    local_now = now_in_sydney()
    target = local_now.replace(
        hour=QUEUE_ACCESS_START_TIME[0],
        minute=QUEUE_ACCESS_START_TIME[1],
        second=0,
        microsecond=0,
    )
    if local_now >= target:
        log(
            f"Queue polling window ({QUEUE_ACCESS_START_TIME[0]:02d}:{QUEUE_ACCESS_START_TIME[1]:02d}) "
            "already open. Proceeding immediately."
        )
        driver.get(EVENT_LIST_URL)
        return

    log(
        "Holding event list page until "
        f"{QUEUE_ACCESS_START_TIME[0]:02d}:{QUEUE_ACCESS_START_TIME[1]:02d} {local_now.tzname() or 'local'} "
        "before beginning queue polling."
    )
    driver.get(EVENT_LIST_URL)
    while True:
        local_now = now_in_sydney()
        if local_now >= target:
            log(
                f"Queue polling window reached at {local_now:%H:%M:%S %Z}. "
                "Starting queue navigation."
            )
            return
        seconds_left = (target - local_now).total_seconds()
        if seconds_left > 300:
            sleep_for = 60
        elif seconds_left > 60:
            sleep_for = 30
        else:
            # wake slightly before the unlock so we can refresh promptly
            sleep_for = max(5, seconds_left - 5)
            sleep_for = min(sleep_for, seconds_left)
        log(
            f"Queue wait: {seconds_left/60:.1f} min remaining until "
            f"{target:%H:%M %Z} (sleeping {int(sleep_for)}s)."
        )
        time.sleep(sleep_for)
        try:
            driver.refresh()
            _safe_accept_alert(driver)
        except Exception as exc:  # noqa: BLE001 - keep waiting despite refresh issues
            log(f"WARNING: Refresh while waiting for queue failed: {exc}")
            time.sleep(5)


def hold_until_booking_release(label: str = "Booking release gate (7:00pm Sydney)") -> None:
    """Block until the 19:00 release window, logging progress."""

    wait_until_local_time(
        BOOKING_OPEN_TIME[0],
        BOOKING_OPEN_TIME[1],
        label,
    )


def zip_run_folder() -> None:
    try:
        with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in RUN_DIR.rglob("*"):
                zf.write(path, arcname=path.relative_to(RUN_DIR))
        log(f"Created evidence bundle: {ZIP_PATH}")
    except Exception as exc:  # noqa: BLE001 - logging for diagnostics
        log(f"WARNING: Could not create ZIP: {exc}")


# ------------------------------------------------------------------------------------
# DATE/TARGET
# ------------------------------------------------------------------------------------
def compute_target_date() -> Tuple[datetime, datetime, datetime, str, str]:
    local_now = now_in_sydney()

    weekday = local_now.weekday()
    days_until_upcoming_sat = (5 - weekday + 7) % 7
    if days_until_upcoming_sat == 0:
        days_until_upcoming_sat = 7  # always look at least one week ahead

    upcoming_saturday = local_now + timedelta(days=days_until_upcoming_sat)
    target = upcoming_saturday + timedelta(days=7)  # following Saturday (≈9 days ahead on Thu)
    dayname = target.strftime("%a")
    try:
        combo = target.strftime("%-d %b")
    except Exception:
        combo = target.strftime("%d %b").lstrip("0")
    return local_now, upcoming_saturday, target, dayname, combo


local_now, upcoming_saturday, target_date, target_day_name, target_date_combo = (
    compute_target_date()
)
log("--- RUNNING IN LIVE AUTOMATIC MODE ---")
log(f"The upcoming Saturday is: {upcoming_saturday.strftime('%Y-%m-%d')}")
log(
    "Therefore, the script is targeting the following Saturday: "
    f"{target_date.strftime('%Y-%m-%d')} ({target_day_name} {target_date_combo})"
)


# ------------------------------------------------------------------------------------
# SELENIUM / HELPERS
# ------------------------------------------------------------------------------------
def make_driver() -> webdriver.Chrome:
    """Start Chrome/Chromedriver robustly with retries and safer flags."""

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--remote-debugging-pipe")
    # If you ever install your own Chromium, point to it here:
    # opts.binary_location = "/home/youruser/bin/chromium/chrome"

    driver_path = shutil.which("chromedriver")
    service = Service(executable_path=driver_path) if driver_path else Service()

    last_err: Exception | None = None
    for attempt in range(1, 3):
        try:
            print(
                f"[make_driver] Launch attempt {attempt} "
                f"(using {driver_path or 'auto-managed'} driver)"
            )
            drv = webdriver.Chrome(options=opts, service=service)
            drv.set_page_load_timeout(90)
            return drv
        except Exception as exc:  # noqa: BLE001 - we retry after cleanup
            last_err = exc
            print(f"[make_driver] Launch failed (attempt {attempt}): {exc}")
            try:
                subprocess.run(["pkill", "-f", "chromedriver"], check=False)
                subprocess.run(["pkill", "-f", "chrome.*--headless"], check=False)
                subprocess.run(["pkill", "-f", "chrome.*for-testing"], check=False)
            except Exception:
                pass
            time.sleep(3)

    raise RuntimeError(f"Chrome/driver failed to start after retries: {last_err}")


def _safe_accept_alert(driver: webdriver.Chrome) -> Tuple[bool, str]:
    try:
        alert = driver.switch_to.alert
        text = alert.text
        alert.accept()
        log(f" -> ALERT dismissed: {text}")
        return True, text
    except NoAlertPresentException:
        return False, ""


def _wait_ready_state_complete(driver: webdriver.Chrome, timeout: int = 30) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if driver.execute_script("return document.readyState") == "complete":
                return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


# ------------------------------------------------------------------------------------
# DRAW / QUEUE DETECTION (MiClub waiting room)
# ------------------------------------------------------------------------------------
def _detect_draw_state(driver: webdriver.Chrome) -> tuple[bool, int | None]:
    """Detect the MiClub draw/waiting room with countdown timer.

    Returns:
        (is_in_draw, seconds_remaining)
        - is_in_draw: True if "You are in the draw" banner is detected
        - seconds_remaining: Parsed countdown in seconds, or None if not parseable
    """
    try:
        # Look for the draw banner text
        body_text = driver.find_element(By.TAG_NAME, "body").text
        if "You are in the draw" not in body_text and "in the draw to access" not in body_text:
            return False, None

        # Try to parse the countdown "Opens in HH:MM:SS" or "Opens in MM:SS"
        match = re.search(r"Opens\s+in\s+(\d{1,2}):(\d{2}):(\d{2})", body_text)
        if match:
            hours, mins, secs = int(match.group(1)), int(match.group(2)), int(match.group(3))
            total_seconds = hours * 3600 + mins * 60 + secs
            return True, total_seconds

        # Try MM:SS format
        match = re.search(r"Opens\s+in\s+(\d{1,2}):(\d{2})", body_text)
        if match:
            mins, secs = int(match.group(1)), int(match.group(2))
            total_seconds = mins * 60 + secs
            return True, total_seconds

        # Draw detected but couldn't parse countdown
        return True, None
    except Exception:
        return False, None


def _detect_queue_position(driver: webdriver.Chrome) -> tuple[bool, int | None, int | None]:
    """Detect the MiClub queue state with position number.

    Returns:
        (is_in_queue, position, approx_bookings_available)
        - is_in_queue: True if queue banner detected
        - position: Current queue position, or None if not parseable
        - approx_bookings_available: Approximate bookings still available, or None
    """
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text

        # Check for queue indicators
        if "Current Position" not in body_text and "placed in a queue" not in body_text:
            return False, None, None

        position = None
        approx_available = None

        # Parse "Current Position : X" (note spaces around colon)
        match = re.search(r"Current\s+Position\s*:\s*(\d+)", body_text)
        if match:
            position = int(match.group(1))

        # Parse "Approximate Bookings Available : ~X"
        match = re.search(r"Approximate\s+Bookings\s+Available\s*:\s*~?(\d+)", body_text)
        if match:
            approx_available = int(match.group(1))

        return True, position, approx_available
    except Exception:
        return False, None, None


def _has_tee_sheet(driver: webdriver.Chrome) -> bool:
    """Return True if the tee-sheet table (with rows) is present."""
    try:
        table = driver.find_element(By.CLASS_NAME, "teetime-day-table")
        rows = table.find_elements(By.XPATH, ".//div[contains(@class, 'row-time')]")
        return bool(rows)
    except Exception:
        return False


def _wait_teetime_table(driver: webdriver.Chrome, timeout: int) -> bool:
    """Wait for tee sheet to appear, handling draw and queue states.

    IMPORTANT: This function does NOT refresh when in draw or queue state
    to avoid losing queue position. It waits for automatic redirect.
    """
    _wait_ready_state_complete(driver, timeout=min(10, timeout))
    deadline = time.time() + timeout
    last_err: Exception | None = None
    last_status_log = 0.0

    while time.time() < deadline:
        # Check if tee sheet is available
        try:
            table = driver.find_element(By.CLASS_NAME, "teetime-day-table")
            rows = table.find_elements(By.XPATH, ".//div[contains(@class, 'row-time')]")
            if rows:
                return True
        except Exception as exc:  # noqa: BLE001 - we retry until timeout
            last_err = exc

        now = time.time()

        # Check for draw state (countdown before 7pm)
        in_draw, countdown_secs = _detect_draw_state(driver)
        if in_draw:
            if now - last_status_log > 10:
                if countdown_secs is not None:
                    log(f" -> In draw, countdown: {countdown_secs}s remaining. NOT refreshing.")
                else:
                    log(" -> In draw (countdown not parsed). NOT refreshing.")
                last_status_log = now
            # Extend deadline - we must wait for draw to complete
            if countdown_secs is not None:
                deadline = max(deadline, time.time() + countdown_secs + 60)
            else:
                deadline = max(deadline, time.time() + 120)
            time.sleep(1)  # Poll frequently during draw
            continue

        # Check for queue state (after 7pm, waiting for turn)
        in_queue, position, approx_available = _detect_queue_position(driver)
        if in_queue:
            if now - last_status_log > 5:
                pos_str = str(position) if position is not None else "unknown"
                avail_str = f"~{approx_available}" if approx_available is not None else "unknown"
                log(f" -> In queue, position: {pos_str}, available: {avail_str}. NOT refreshing.")
                last_status_log = now
            # Extend deadline significantly - queue can take time
            deadline = max(deadline, time.time() + 300)
            time.sleep(0.5)  # Poll frequently, waiting for auto-redirect
            continue

        # Not in draw or queue - small sleep before retry
        time.sleep(0.25)

    log(f" -> TIMEOUT waiting for tee sheet (no rows). Last error: {last_err}")
    return False


def _wait_confirm_or_alert(driver: webdriver.Chrome, timeout: int) -> Tuple[str, object | None]:
    end = time.time() + timeout
    while time.time() < end:
        ok, text = _safe_accept_alert(driver)
        if ok:
            return "alert", text
        try:
            button = driver.find_element(
                By.XPATH,
                "//button[normalize-space()='Yes' or normalize-space()='YES' "
                "or normalize-space()='Confirm']",
            )
            if button.is_enabled() and button.is_displayed():
                return "modal", button
        except Exception:
            pass
        time.sleep(0.12)
    return "timeout", None


# --- Modal readers/validators -------------------------------------------------------
def _read_confirm_modal(
    driver: webdriver.Chrome, timeout: int = 8
) -> Tuple[object, str, object | None, object | None]:
    """Fetch the confirmation modal and key controls."""

    modal = WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//div[contains(@class,'modal') and "
                "(contains(@class,'show') or contains(@style,'display'))]",
            )
        )
    )
    text = modal.text or ""

    try:
        yes_btn = modal.find_element(
            By.XPATH,
            ".//button[normalize-space()='Yes' or normalize-space()='YES' "
            "or normalize-space()='Confirm']",
        )
    except Exception:
        yes_btn = None

    cancel_btn = None
    for xp in [
        ".//button[normalize-space()='No']",
        ".//button[normalize-space()='Cancel']",
        ".//button[contains(@class,'btn-secondary')]",
        ".//button[contains(@class,'btn-default')]",
    ]:
        elements = modal.find_elements(By.XPATH, xp)
        if elements:
            cancel_btn = elements[0]
            break

    return modal, text, yes_btn, cancel_btn


def _modal_contains_expected_names(
    modal_text: str, expected_surnames: List[str]
) -> Tuple[bool, List[str]]:
    missing = [surname for surname in expected_surnames if surname not in modal_text]
    return len(missing) == 0, missing


# ------------------------------------------------------------------------------------
# CORE ACTIONS
# ------------------------------------------------------------------------------------
def login(driver: webdriver.Chrome, username: str, password: str) -> bool:
    log(f"\nAttempting to log in as user: {username}")
    start = time.time()
    driver.get(LOGIN_URL)
    user_field = WebDriverWait(driver, 30).until(
        EC.presence_of_element_located((By.NAME, "user"))
    )
    user_field.clear()
    user_field.send_keys(username)
    pw_field = driver.find_element(By.NAME, "password")
    pw_field.clear()
    pw_field.send_keys(password)
    driver.find_element(By.XPATH, "//input[@value='Login']").click()
    WebDriverWait(driver, 30).until(
        EC.presence_of_element_located((By.XPATH, "//a[contains(@href, 'logout')]"))
    )
    log(f"Login successful for user: {username} (took {time.time() - start:.2f}s)")
    return True


def logout(driver: webdriver.Chrome) -> None:
    try:
        log("Attempting to log out...")
        time.sleep(1.5)
        driver.get(LOGOUT_URL)
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.NAME, "user")))
        log("Logout successful.")
    except Exception as exc:  # noqa: BLE001 - best effort logout
        log(f"WARNING: Could not confirm logout. REASON: {exc}")


def navigate_and_wait_for_unlock(driver: webdriver.Chrome) -> bool:
    """Navigate to event list and enter draw/queue as early as possible.

    Strategy:
    1. At 6:30pm (QUEUE_JOIN_TIME), click on the event to enter the draw
    2. Once in draw, DO NOT REFRESH - wait for countdown and auto-redirect
    3. Once in queue, DO NOT REFRESH - wait for position to reach zero
    4. Detect when tee sheet becomes available
    """
    log("Navigating to event list and attempting to enter draw/queue...")
    driver.get(EVENT_LIST_URL)

    draw_entry_attempted = False
    in_waiting_room = False  # True once we've entered draw or queue
    max_wait_seconds = 3600  # 60 minutes - must wait for 30min draw countdown + queue + booking
    deadline = time.time() + max_wait_seconds
    last_status_log = 0.0

    while time.time() < deadline:
        now = time.time()

        # If we're in the waiting room (draw or queue), check for tee sheet or keep waiting
        if in_waiting_room:
            # Check if tee sheet is now available (we've been redirected)
            if _has_tee_sheet(driver):
                log("SUCCESS! Tee sheet is now available after draw/queue.")
                return True

            # Check draw state
            in_draw, countdown_secs = _detect_draw_state(driver)
            if in_draw:
                if now - last_status_log > 10:
                    if countdown_secs is not None:
                        log(f"In draw, countdown: {countdown_secs}s. Waiting (NO refresh)...")
                    else:
                        log("In draw (waiting for countdown). Waiting (NO refresh)...")
                    last_status_log = now
                time.sleep(1)
                continue

            # Check queue state
            in_queue, position, approx_available = _detect_queue_position(driver)
            if in_queue:
                if now - last_status_log > 5:
                    pos_str = str(position) if position is not None else "unknown"
                    avail_str = f"~{approx_available}" if approx_available is not None else "unknown"
                    log(f"In queue, position: {pos_str}, available: {avail_str}. Waiting (NO refresh)...")
                    last_status_log = now
                time.sleep(0.5)
                continue

            # Not in draw/queue anymore but tee sheet not found - might be transitioning
            time.sleep(0.5)
            continue

        # Not yet in waiting room - try to enter
        try:
            event_div_xpath = (
                f"//div[contains(@class, 'full') and "
                f".//span[contains(., '{target_day_name}')] and "
                f".//span[contains(., '{target_date_combo}')]]"
            )
            target_event_div = WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.XPATH, event_div_xpath))
            )
            link = target_event_div.find_element(By.TAG_NAME, "a")
            event_url = link.get_attribute("href") or ""
            classes = link.get_attribute("class") or ""

            # If event is open, click it
            if "eventStatusOpen" in classes:
                log(f"Event {target_date_combo} is OPEN. Clicking to enter...")
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", link)
                try:
                    link.click()
                except ElementClickInterceptedException:
                    driver.execute_script("arguments[0].click();", link)

                time.sleep(1)

                # Check if we landed on tee sheet directly
                if _has_tee_sheet(driver):
                    log("SUCCESS! Tee sheet loaded directly (no queue).")
                    return True

                # Check if we entered draw or queue
                in_draw, _ = _detect_draw_state(driver)
                in_queue, position, _ = _detect_queue_position(driver)
                if in_draw or in_queue:
                    in_waiting_room = True
                    state = "draw" if in_draw else f"queue (position {position})"
                    log(f"Entered {state}. Will wait without refreshing.")
                    continue

                # Might still be loading
                log("Clicked event, waiting for page to settle...")
                time.sleep(2)
                continue

            # Event not yet open - check if we're past the draw entry time
            local_now = now_in_sydney()
            draw_open_time = local_now.replace(
                hour=QUEUE_JOIN_TIME[0],
                minute=QUEUE_JOIN_TIME[1],
                second=0,
                microsecond=0,
            )

            if local_now >= draw_open_time and not draw_entry_attempted:
                # Try to click the event even if it shows as locked - draw might be open
                log(f"Draw time reached ({QUEUE_JOIN_TIME[0]:02d}:{QUEUE_JOIN_TIME[1]:02d}). "
                    "Attempting to click event...")
                draw_entry_attempted = True
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", link)
                try:
                    link.click()
                except ElementClickInterceptedException:
                    driver.execute_script("arguments[0].click();", link)
                except Exception as click_err:
                    log(f"Click failed: {click_err}. Will retry via URL if available.")

                time.sleep(1)

                # Check if we entered draw/queue
                in_draw, _ = _detect_draw_state(driver)
                in_queue, position, _ = _detect_queue_position(driver)
                if in_draw or in_queue:
                    in_waiting_room = True
                    state = "draw" if in_draw else f"queue (position {position})"
                    log(f"Successfully entered {state}!")
                    continue

                # Try direct URL navigation if click didn't work
                if event_url and not event_url.lower().startswith("javascript"):
                    log(f"Trying direct navigation to: {event_url}")
                    driver.get(event_url)
                    time.sleep(1)
                    in_draw, _ = _detect_draw_state(driver)
                    in_queue, position, _ = _detect_queue_position(driver)
                    if in_draw or in_queue:
                        in_waiting_room = True
                        state = "draw" if in_draw else f"queue (position {position})"
                        log(f"Successfully entered {state} via direct URL!")
                        continue

                # Didn't enter draw - go back to event list and retry
                log("Did not enter draw yet. Returning to event list...")
                driver.get(EVENT_LIST_URL)
                draw_entry_attempted = False  # Allow retry
                time.sleep(2)
                continue

            # Before draw time - poll and wait
            if now - last_status_log > 30:
                log(f"Status: {target_date_combo} not yet open. Waiting for draw time...")
                last_status_log = now
            time.sleep(OPEN_POLL_INTERVAL_SEC)
            driver.refresh()

        except Exception as exc:  # noqa: BLE001 - wait/retry
            log(f"Page not ready: {exc}. Refreshing...")
            time.sleep(3)
            try:
                driver.refresh()
            except Exception:
                driver.get(EVENT_LIST_URL)

    log(f"TIMEOUT: Could not access tee sheet within {max_wait_seconds}s.")
    return False


def execute_group_booking(
    driver: webdriver.Chrome,
    booker_username: str,
    required_slots: int,
    max_attempts: int,
) -> bool:
    log(
        "\n--- Searching for a slot with at least "
        f"{required_slots} empty spaces for user {booker_username} ---"
    )
    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        try:
            tee_wait = TEE_SHEET_WAIT_FIRST if attempt == 1 else TEE_SHEET_WAIT_SUBSEQUENT
            log(f"\nAttempt #{attempt}/{max_attempts} (tee-sheet wait {tee_wait}s)...")

            if not _wait_teetime_table(driver, timeout=tee_wait):
                snap_png(driver, f"attempt{attempt}_no_table")
                snap_html(driver, f"attempt{attempt}_no_table")
                log("    -> Tee sheet not fully ready. Refreshing and retrying...")
                driver.refresh()
                time.sleep(5)
                continue

            log("Tee sheet page loaded and has rows.")
            snap_png(driver, f"attempt{attempt}_sheet_loaded")

            all_rows = driver.find_elements(By.XPATH, "//div[contains(@class, 'row-time')]")
            target_row = None
            for row in all_rows:
                try:
                    empties = row.find_elements(By.XPATH, ".//button[contains(@class, 'btn-book-me')]")
                    if len(empties) >= required_slots:
                        target_row = row
                        break
                except StaleElementReferenceException:
                    continue

            if not target_row:
                log("No suitable empty slots found. Refreshing and retrying...")
                snap_html(driver, f"attempt{attempt}_no_slot_html")
                driver.refresh()
                time.sleep(4)
                continue

            try:
                time_text = target_row.find_element(By.TAG_NAME, "h3").text
            except Exception:
                time_text = "(Unknown time)"
            log(f"Found suitable slot at {time_text}. Attempting to book group...")

            btn_group = target_row.find_element(By.XPATH, ".//button[contains(@class, 'btn-book-group')]")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn_group)
            try:
                btn_group.click()
            except ElementClickInterceptedException:
                driver.execute_script("arguments[0].click();", btn_group)
            snap_png(driver, f"attempt{attempt}_after_book_group_click")

            which, obj = _wait_confirm_or_alert(driver, timeout=YES_BUTTON_WAIT_SEC)
            if which == "alert":
                log("    -> Slot locked by another user. Refresh + retry...")
                snap_png(driver, f"attempt{attempt}_alert")
                driver.refresh()
                time.sleep(2)
                continue

            if which == "modal":
                try:
                    modal, modal_text, yes_button, cancel_button = _read_confirm_modal(
                        driver, timeout=YES_BUTTON_WAIT_SEC
                    )
                except Exception as exc:  # noqa: BLE001 - we will retry
                    log(f"    -> Modal read failed: {exc}. Refresh + retry...")
                    snap_png(driver, f"attempt{attempt}_modal_read_failed")
                    driver.refresh()
                    time.sleep(3)
                    continue

                expected = expected_group_for(booker_username)
                if expected:
                    ok, missing = _modal_contains_expected_names(modal_text, expected)
                    preview = modal_text[:200].replace("\n", " ")
                    log(
                        f"Modal text preview: {preview}" + ("..." if len(modal_text) > 200 else "")
                    )
                    if not ok:
                        log(
                            "    -> EXPECTED NAMES NOT FOUND for "
                            f"{booker_username}. Missing: {', '.join(missing)}"
                        )
                        if cancel_button:
                            try:
                                cancel_button.click()
                                log("    -> Cancelled modal due to mismatch. Trying next slot...")
                            except Exception:
                                log("    -> Could not click Cancel; refreshing...")
                        else:
                            log("    -> No cancel button detected; refreshing...")
                        driver.refresh()
                        time.sleep(2)
                        continue

                if yes_button is None:
                    log("    -> No Yes/Confirm button found in modal; refresh + retry...")
                    snap_png(driver, f"attempt{attempt}_no_yes_button")
                    driver.refresh()
                    time.sleep(3)
                    continue

                log("Confirmation modal appeared and names validated. Clicking to finalise booking.")
                try:
                    try:
                        yes_button.click()
                    except ElementClickInterceptedException:
                        driver.execute_script("arguments[0].click();", yes_button)
                except Exception as exc:
                    log(f"    -> Failed to click Yes/Confirm: {exc}")
                    snap_png(driver, f"attempt{attempt}_yes_click_failed")
                    driver.refresh()
                    time.sleep(2)
                    continue

                log(f"Booking command sent for {booker_username}'s group. Verifying...")
                snap_png(driver, f"attempt{attempt}_after_confirm_click")

                # Wait briefly then check for confirmation alert or page text
                time.sleep(0.5)
                booked_ok, alert_text = _safe_accept_alert(driver)
                if booked_ok:
                    log(f"SUCCESS: Booking finalised (alert said): {alert_text}")
                    snap_png(driver, f"attempt{attempt}_booked_alert")
                    return True

                # Check page text for success indicators
                try:
                    body_text = driver.find_element(By.TAG_NAME, "body").text
                except Exception:
                    body_text = ""

                success_indicators = [
                    "Your booking has been made",
                    "Booking successful",
                    "successfully booked",
                    "Booking confirmed",
                ]
                if any(indicator.lower() in body_text.lower() for indicator in success_indicators):
                    log("SUCCESS: Booking finalised (page text indicates success).")
                    snap_png(driver, f"attempt{attempt}_booked_page")
                    return True

                # Check if tee sheet shows our expected players (names in the modal)
                if expected and _has_tee_sheet(driver):
                    try:
                        table = driver.find_element(By.CLASS_NAME, "teetime-day-table")
                        sheet_text = table.text
                        # Check if at least some expected names appear on the sheet
                        names_found = sum(1 for name in expected if name in sheet_text)
                        if names_found >= len(expected) // 2:  # At least half the names found
                            log(f"SUCCESS: Found {names_found}/{len(expected)} expected names on tee sheet.")
                            snap_png(driver, f"attempt{attempt}_names_on_sheet")
                            return True
                    except Exception:
                        pass

                # Booking may have gone through but unclear - log warning and return success
                # (better to assume success and verify later than retry and double-book)
                log("WARNING: Booking status unclear after clicking confirm. "
                    "Assuming success - will verify in final check.")
                snap_png(driver, f"attempt{attempt}_status_unclear")
                snap_html(driver, f"attempt{attempt}_status_unclear")
                return True

            log("    -> TIMEOUT waiting for confirm modal/alert. Refresh + retry...")
            snap_png(driver, f"attempt{attempt}_timeout_waiting_modal")
            snap_html(driver, f"attempt{attempt}_timeout_waiting_modal")
            driver.refresh()
            time.sleep(4)
            continue

        except UnexpectedAlertPresentException:
            _safe_accept_alert(driver)
            snap_png(driver, f"attempt{attempt}_unexpected_alert")
            driver.refresh()
            time.sleep(2)
            continue
        except TimeoutException:
            log("    -> TIMEOUT (generic): Refresh + retry...")
            snap_png(driver, f"attempt{attempt}_generic_timeout")
            driver.refresh()
            time.sleep(5)
            continue
        except Exception as exc:  # noqa: BLE001 - capture evidence then retry
            log(f"    -> Unexpected error: {exc}. Refresh + retry in 5s...")
            snap_png(driver, f"attempt{attempt}_unexpected_error")
            snap_html(driver, f"attempt{attempt}_unexpected_error")
            driver.refresh()
            time.sleep(5)
            continue

    log(f"ERROR: Failed to book a slot after {max_attempts} attempts.")
    return False


def _read_row_players(row: WebElement) -> List[str]:
    """Best-effort scrape of visible player names on a tee-time row."""

    names: List[str] = []
    try:
        links = row.find_elements(By.XPATH, ".//a[contains(@href,'member')]")
        for link in links:
            text = (link.text or "").strip()
            if text:
                names.append(text)
    except Exception:
        pass
    return names


def verify_all_bookings(driver: webdriver.Chrome, all_players: List[str]) -> None:
    log("\n===== STARTING FINAL BOOKING VERIFICATION =====")
    try:
        if not navigate_and_wait_for_unlock(driver):
            return
        if not _wait_teetime_table(driver, timeout=45):
            log("WARNING: Tee sheet did not load for verification.")
            snap_png(driver, "verify_no_table")
            return

        snap_png(driver, "verify_sheet")
        table = driver.find_element(By.CLASS_NAME, "teetime-day-table")
        rows = table.find_elements(By.XPATH, ".//div[contains(@class, 'row-time')]")

        log("\n--- TEE SHEET (time → players) ---")
        for row in rows:
            try:
                time_text = row.find_element(By.TAG_NAME, "h3").text
            except Exception:
                time_text = "(Unknown time)"
            players = _read_row_players(row)
            log(f"{time_text}: {', '.join(players) if players else '(empty)'}")

        sheet_text = table.text
        confirmed = [player for player in all_players if player in sheet_text]
        missing = [player for player in all_players if player not in sheet_text]
        log("\n--- SUMMARY CHECK ---")
        log(
            f"Found {len(confirmed)} out of {len(all_players)} players "
            "(string match on page)."
        )
        if confirmed:
            log(f"Confirmed: {', '.join(confirmed)}")
        if missing:
            log(f"Missing: {', '.join(missing)}")
        else:
            log("All players present by string match.")

    except Exception as exc:  # noqa: BLE001 - verification is best effort
        log(f"An error occurred during final verification. REASON: {exc}")


def main() -> None:
    log("--- Golf Booking Bot Initialized [v55 extended timeout for draw+queue] ---")
    driver = make_driver()

    try:
        try:
            start = time.time()
            driver.get("https://example.com")
            log(f"Pre-flight OK: title='{driver.title}', took {time.time() - start:.2f}s")
        except Exception as exc:  # noqa: BLE001 - fail early
            log(f"Pre-flight navigation failed: {exc}")
            raise

        group1_success = False
        group2_success = False
        group3_success = False

        try:
            log("\n===== STARTING BOOKING FOR GROUP 1 =====")
            if login(driver, BOOKER_1_USERNAME, BOOKER_1_PASSWORD):
                hold_until_queue_poll_window(driver)
                if navigate_and_wait_for_unlock(driver):
                    hold_until_booking_release()
                    group1_success = execute_group_booking(
                        driver,
                        BOOKER_1_USERNAME,
                        GROUP_1_SIZE,
                        GROUP_1_MAX_ATTEMPTS,
                    )
                    logout(driver)

            if group1_success:
                log(
                    "\n[INFO] Skipping Group 2 booking within this script "
                    "(handled by dedicated runner)."
                )
            else:
                log(
                    "\n[INFO] Group 1 did not succeed; Group 2 booking remains with the "
                    "dedicated runner."
                )

            if not group1_success:
                log("\n===== STARTING BOOKING FOR GROUP 3 (BACKUP because G1 failed) =====")
                time.sleep(5)
                if login(driver, BOOKER_3_USERNAME, BOOKER_3_PASSWORD):
                    hold_until_queue_poll_window(driver)
                    if navigate_and_wait_for_unlock(driver):
                        hold_until_booking_release(
                            "Booking release gate (7:00pm Sydney) prior to backup run"
                        )
                        group3_success = execute_group_booking(
                            driver,
                            BOOKER_3_USERNAME,
                            GROUP_3_SIZE,
                            GROUP_3_MAX_ATTEMPTS,
                        )
                        logout(driver)
            else:
                log("\n[INFO] Skipping Group 3 (backup not needed because G1 succeeded).")

            if group1_success or group2_success or group3_success:
                if login(driver, BOOKER_1_USERNAME, BOOKER_1_PASSWORD):
                    verify_all_bookings(driver, PLAYERS_TO_VERIFY)
                    logout(driver)
        finally:
            driver.quit()
            log("\n--- All booking tasks complete. Browser closed. ---")
            zip_run_folder()
            log(
                f"Summary: G1={group1_success}, G2={group2_success}, "
                f"G3={group3_success}"
            )
            log(f"Log file: {LOG_FILE}")
            log(f"Evidence ZIP: {ZIP_PATH}")

    except Exception as exc:  # noqa: BLE001 - surface unexpected issues
        log(f"CRITICAL: booking run failed unexpectedly. REASON: {exc}")
        raise


if __name__ == "__main__":
    main()
