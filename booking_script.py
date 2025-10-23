"""Golf booking automation script for PythonAnywhere.

This module provisions a Selenium-based job that monitors the Macquarie Links
MiClub portal and attempts to reserve tee-times for pre-defined player groups.
The code is intentionally verbose and defensive so that it can operate in a
headless, scheduled environment such as PythonAnywhere.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Tuple

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

# Note: name validation removed; no expected group mapping required.


OPEN_POLL_INTERVAL_SEC = 2
QUEUE_POLL_INTERVAL_SEC = 3
QUEUE_TIMEOUT_EXTENSION_SEC = 120
YES_BUTTON_WAIT_BASE_SEC = 20  # modal wait starts at 20s and expands each retry
YES_BUTTON_WAIT_STEP_SEC = 5
YES_BUTTON_WAIT_MAX_SEC = 40
TEE_SHEET_WAIT_INITIAL_SEC = 30  # tee-sheet wait grows by 15s per attempt up to 90s
TEE_SHEET_WAIT_STEP_SEC = 15
TEE_SHEET_WAIT_MAX_SEC = 90
BOOKING_VERIFY_TIMEOUT_SEC = 60
BOOKING_VERIFY_POLL_INTERVAL_SEC = 1.5
JOB_MAX_RUNTIME_SEC = 2700  # hard cap on total job runtime (45 minutes)
UNLOCK_WAIT_BOOKING_SEC = 1800  # max wait for bookings to open during booking flows (30 min)
UNLOCK_WAIT_VERIFY_SEC = 60  # max wait during verification (1 min)

# Waitlist/queue probing (allows entering the MiClub waiting room before the sheet opens)
def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


WAITLIST_PROBE_ENABLED = _env_flag("WAITLIST_PROBE_ENABLED", True)
WAITLIST_PROBE_INTERVAL_SEC = max(3, _env_int("WAITLIST_PROBE_INTERVAL_SEC", 3))

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
    now_utc = datetime.now(timezone.utc)
    try:
        import zoneinfo

        syd = zoneinfo.ZoneInfo("Australia/Sydney")
        local_now = now_utc.astimezone(syd)
    except Exception:  # fallback if zoneinfo unavailable
        local_now = now_utc

    days_until_thu = (3 - local_now.weekday() + 7) % 7
    this_thu = local_now + timedelta(days=days_until_thu)
    target = this_thu + timedelta(days=9)
    dayname = target.strftime("%a")
    try:
        combo = target.strftime("%-d %b")
    except Exception:
        combo = target.strftime("%d %b").lstrip("0")
    return local_now, this_thu, target, dayname, combo


local_now, upcoming_thursday, target_date, target_day_name, target_date_combo = compute_target_date()
log("--- RUNNING IN LIVE AUTOMATIC MODE ---")
try:
    log(f"Local time at script start: {local_now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
except Exception:  # strftime may fail on some platforms; fallback to ISO format
    log(f"Local time at script start: {local_now.isoformat()}")
log(f"The next booking Thursday is: {upcoming_thursday.strftime('%Y-%m-%d')}")
log(
    "Therefore, the script is targeting Saturday: "
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
            drv.set_page_load_timeout(120)
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


def _detect_queue_status(driver: webdriver.Chrome) -> Tuple[bool, str, int | None]:
    """Detect the MiClub queue banner if present."""

    try:
        banner = driver.find_element(
            By.XPATH,
            "//div[contains(., 'Current Position') and contains(., 'queue')]",
        )
        text = banner.text.strip()
        match = re.search(r"Current Position\s*:\s*(\d+)", text)
        position = int(match.group(1)) if match else None
        return True, text, position
    except Exception:
        pass

    try:
        # Some variants use a dedicated class or alternative phrasing
        banner = driver.find_element(
            By.XPATH,
            "//div[contains(@class,'queue') or contains(., 'placed in a queue')]",
        )
        text = banner.text.strip()
        match = re.search(r"Current Position\s*:\s*(\d+)", text)
        position = int(match.group(1)) if match else None
        return True, text, position
    except Exception:
        return False, "", None


def _has_tee_sheet(driver: webdriver.Chrome) -> bool:
    """Return True if the tee-sheet table (with rows) is present."""

    try:
        table = driver.find_element(By.CLASS_NAME, "teetime-day-table")
        rows = table.find_elements(By.XPATH, ".//div[contains(@class, 'row-time')]")
        return bool(rows)
    except Exception:
        return False


def _attempt_waitlist_probe(driver: webdriver.Chrome, event_url: str) -> str:
    """
    Navigate directly to the event page to engage the MiClub waitlist/queue.

    Returns:
        "queue" if the waitlist banner is detected and we should remain on the page.
        "open" if the tee sheet is already accessible.
        "locked" if bookings are still closed with no queue.
        "error" if navigation failed.
    """

    try:
        log("Waitlist probe: attempting to reach event page ahead of unlock.")
        driver.get(event_url)
        _wait_ready_state_complete(driver, timeout=8)
        queue_active, queue_text, queue_position = _detect_queue_status(driver)
        if queue_active:
            pos_msg = queue_position if queue_position is not None else "unknown"
            log(f"Waitlist probe: queue banner detected (position={pos_msg}). Holding position.")
            if queue_text:
                log(f"    Queue banner: {queue_text.replace(os.linesep, ' | ')}")
            return "queue"
        if _has_tee_sheet(driver):
            log("Waitlist probe: tee sheet already available; proceeding immediately.")
            return "open"
        log("Waitlist probe: event still locked and no queue yet.")
        return "locked"
    except Exception as exc:  # noqa: BLE001 - best-effort probing
        log(f"Waitlist probe encountered an error: {exc}")
        return "error"


def _wait_teetime_table(driver: webdriver.Chrome, timeout: int) -> bool:
    _wait_ready_state_complete(driver, timeout=min(10, timeout))
    deadline = time.time() + timeout
    last_err: Exception | None = None
    last_queue_log = 0.0
    while time.time() < deadline:
        try:
            table = driver.find_element(By.CLASS_NAME, "teetime-day-table")
            rows = table.find_elements(By.XPATH, ".//div[contains(@class, 'row-time')]")
            if rows:
                return True
        except Exception as exc:  # noqa: BLE001 - we retry until timeout
            last_err = exc

        queue_active, queue_text, queue_position = _detect_queue_status(driver)
        if queue_active:
            now = time.time()
            if now - last_queue_log > 5:
                pos_msg = f"position={queue_position}" if queue_position is not None else "position unknown"
                log(f" -> Queue detected ({pos_msg}). Waiting before re-checking.")
                if queue_text:
                    log(f"    Queue banner: {queue_text.replace(os.linesep, ' | ')}")
                last_queue_log = now
            # Extend deadline so queue waits do not prematurely timeout.
            deadline = max(deadline, time.time() + QUEUE_TIMEOUT_EXTENSION_SEC)
            time.sleep(QUEUE_POLL_INTERVAL_SEC)
            continue

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
    driver: webdriver.Chrome, timeout: int = 15
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


# Modal parsing helpers


def _parse_modal_names(modal_text: str) -> List[str]:
    """Extract player names from the booking confirmation modal."""

    names: List[str] = []
    for line in modal_text.splitlines():
        candidate = line.strip()
        if not candidate or "," not in candidate:
            continue
        # Remove trailing helper tokens such as [H]
        names.append(candidate.split("[")[0].strip())
    return names


def _wait_row_booked(
    driver: webdriver.Chrome, row_id: str, expected_names: List[str]
) -> bool:
    """Wait until the row shows the expected player surnames."""

    if not row_id:
        return False

    expected_surnames = {name.split(",")[0].strip().lower() for name in expected_names if "," in name}
    expected_surnames.update(name.lower() for name in expected_names if "," not in name)

    deadline = time.time() + BOOKING_VERIFY_TIMEOUT_SEC
    while time.time() < deadline:
        try:
            row = driver.find_element(By.ID, row_id)
            row_text = row.text.lower()
            if expected_surnames and all(surname in row_text for surname in expected_surnames):
                return True
            if not expected_surnames and row.find_elements(By.CLASS_NAME, "my-booking"):
                return True
        except StaleElementReferenceException:
            pass
        except Exception:
            pass
        time.sleep(BOOKING_VERIFY_POLL_INTERVAL_SEC)
    return False


def _sheet_contains_expected_names(
    driver: webdriver.Chrome, expected_names: List[str]
) -> bool:
    """Check the entire tee sheet text for expected player surnames."""

    try:
        table = driver.find_element(By.CLASS_NAME, "teetime-day-table")
    except Exception:
        return False

    expected_surnames = {
        name.split(",")[0].strip().lower() for name in expected_names if "," in name
    }
    expected_surnames.update(name.lower() for name in expected_names if "," not in name)

    sheet_text = table.text.lower()
    if expected_surnames:
        return all(surname in sheet_text for surname in expected_surnames)

    # Fallback for scenarios where expected names are unknown: look for any booking marker.
    try:
        return bool(table.find_elements(By.CLASS_NAME, "my-booking"))
    except Exception:
        return False


def _verify_booking_via_refresh(
    driver: webdriver.Chrome, expected_names: List[str], wait_timeout: int = 120
) -> bool:
    """Wait for the tee sheet to reappear and re-check for expected players."""

    try:
        log("Attempting fallback verification by waiting for tee sheet to stabilise.")
        queue_active, queue_text, queue_position = _detect_queue_status(driver)
        if queue_active:
            pos_msg = queue_position if queue_position is not None else "unknown"
            log(
                "Fallback verification: queue detected (position="
                f"{pos_msg}). Holding position without refreshing."
            )
            if queue_text:
                log(f"    Queue banner: {queue_text.replace(os.linesep, ' | ')}")

        if not _wait_teetime_table(driver, timeout=wait_timeout):
            log("Fallback verification: tee sheet did not become available in time.")
            return False

        if _sheet_contains_expected_names(driver, expected_names):
            log("Fallback verification succeeded; tee sheet shows expected playing partners.")
            return True

        log("Fallback verification: expected names still not visible after waiting.")
    except Exception as exc:  # noqa: BLE001 - capture but do not raise
        log(f"Fallback verification encountered an error: {exc}")
    return False


# ------------------------------------------------------------------------------------
# CORE ACTIONS
# ------------------------------------------------------------------------------------
def login(driver: webdriver.Chrome, username: str, password: str) -> bool:
    log(f"\nAttempting to log in as user: {username}")
    start = time.time()
    driver.get(LOGIN_URL)
    user_field = WebDriverWait(driver, 45).until(
        EC.presence_of_element_located((By.NAME, "user"))
    )
    user_field.clear()
    user_field.send_keys(username)
    pw_field = driver.find_element(By.NAME, "password")
    pw_field.clear()
    pw_field.send_keys(password)
    driver.find_element(By.XPATH, "//input[@value='Login']").click()
    WebDriverWait(driver, 45).until(
        EC.presence_of_element_located((By.XPATH, "//a[contains(@href, 'logout')]"))
    )
    log(f"Login successful for user: {username} (took {time.time() - start:.2f}s)")
    return True


def logout(driver: webdriver.Chrome) -> None:
    try:
        log("Attempting to log out...")
        time.sleep(1.5)
        driver.get(LOGOUT_URL)
        WebDriverWait(driver, 40).until(EC.presence_of_element_located((By.NAME, "user")))
        log("Logout successful.")
    except Exception as exc:  # noqa: BLE001 - best effort logout
        log(f"WARNING: Could not confirm logout. REASON: {exc}")


def navigate_and_wait_for_unlock(
    driver: webdriver.Chrome, max_wait_seconds: int = UNLOCK_WAIT_BOOKING_SEC
) -> bool:
    log("Navigating to event list and waiting for bookings to open...")
    driver.get(EVENT_LIST_URL)
    deadline = time.time() + max_wait_seconds
    last_waitlist_probe = 0.0
    while time.time() < deadline:
        try:
            event_div_xpath = (
                f"//div[contains(@class, 'full') and "
                f".//span[contains(., '{target_day_name}')] and "
                f".//span[contains(., '{target_date_combo}')]]"
            )
            target_event_div = WebDriverWait(driver, 40).until(
                EC.presence_of_element_located((By.XPATH, event_div_xpath))
            )
            link = target_event_div.find_element(By.TAG_NAME, "a")
            event_url = link.get_attribute("href") or ""
            if event_url.lower().startswith("javascript"):
                event_url = ""
            classes = link.get_attribute("class") or ""
            if "eventStatusOpen" in classes:
                log(f"SUCCESS! {target_date_combo} is now OPEN. Clicking it!")
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", link)
                try:
                    link.click()
                except ElementClickInterceptedException:
                    driver.execute_script("arguments[0].click();", link)
                return True

            now = time.time()
            if (
                WAITLIST_PROBE_ENABLED
                and event_url
                and now - last_waitlist_probe >= WAITLIST_PROBE_INTERVAL_SEC
            ):
                last_waitlist_probe = now
                outcome = _attempt_waitlist_probe(driver, event_url)
                if outcome == "queue":
                    log("Waitlist probe: staying on queue page until tee sheet releases.")
                    return True
                if outcome == "open":
                    log("Waitlist probe: tee sheet already accessible; proceeding.")
                    return True
                log("Waitlist probe did not enter queue; returning to event list.")
                driver.get(EVENT_LIST_URL)
                continue
            log(
                f"Status check: {target_date_combo} is still LOCKED. "
                f"Refreshing in {OPEN_POLL_INTERVAL_SEC} seconds..."
            )
            time.sleep(OPEN_POLL_INTERVAL_SEC)
            driver.refresh()
        except Exception as exc:  # noqa: BLE001 - wait/retry
            log(f"Page not ready, refreshing... REASON: {exc}")
            time.sleep(3)
            driver.refresh()
    log(
        f"TIMEOUT: Booking did not open within {max_wait_seconds}s for {target_date_combo}."
    )
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
            tee_wait = min(
                TEE_SHEET_WAIT_INITIAL_SEC
                + TEE_SHEET_WAIT_STEP_SEC * (attempt - 1),
                TEE_SHEET_WAIT_MAX_SEC,
            )
            confirm_wait = min(
                YES_BUTTON_WAIT_BASE_SEC
                + YES_BUTTON_WAIT_STEP_SEC * (attempt - 1),
                YES_BUTTON_WAIT_MAX_SEC,
            )
            log(
                f"\nAttempt #{attempt}/{max_attempts} "
                f"(tee-sheet wait {tee_wait}s, confirm wait {confirm_wait}s)..."
            )

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
            target_row: WebElement | None = None
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

            row_dom_id = target_row.get_attribute("id") or ""
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

            which, obj = _wait_confirm_or_alert(driver, timeout=confirm_wait)
            if which == "alert":
                log("    -> Slot locked by another user. Refresh + retry...")
                snap_png(driver, f"attempt{attempt}_alert")
                driver.refresh()
                time.sleep(2)
                continue

            if which == "modal":
                expected_names: List[str] = []
                try:
                    modal, modal_text, yes_button, cancel_button = _read_confirm_modal(
                        driver, timeout=confirm_wait
                    )
                    expected_names = _parse_modal_names(modal_text)
                except Exception as exc:  # noqa: BLE001 - we will retry
                    log(f"    -> Modal read failed: {exc}. Refresh + retry...")
                    snap_png(driver, f"attempt{attempt}_modal_read_failed")
                    driver.refresh()
                    time.sleep(3)
                    continue

                if yes_button is None:
                    log("    -> No Yes/Confirm button found in modal; refresh + retry...")
                    snap_png(driver, f"attempt{attempt}_no_yes_button")
                    driver.refresh()
                    time.sleep(3)
                    continue

                log("Confirmation modal appeared. Clicking to finalise booking.")
                try:
                    yes_button.click()
                except ElementClickInterceptedException:
                    driver.execute_script("arguments[0].click();", yes_button)
                log(f"SUCCESS: Booking command sent for {booker_username}'s group.")
                snap_png(driver, f"attempt{attempt}_after_confirm_click")
                verified = False
                if expected_names:
                    verified = _wait_row_booked(driver, row_dom_id, expected_names)
                    if verified:
                        log("Confirmed tee sheet now shows expected playing partners.")
                    else:
                        if _sheet_contains_expected_names(driver, expected_names):
                            log(
                                "Initial row check failed, but tee sheet text already shows expected playing partners."
                            )
                            verified = True
                        elif _verify_booking_via_refresh(driver, expected_names):
                            verified = True
                else:
                    verified = _wait_row_booked(driver, row_dom_id, [])
                    if verified:
                        log("Confirmed tee sheet updated with booking for current user.")
                    else:
                        if _verify_booking_via_refresh(driver, []):
                            verified = True
                            log("Fallback verification detected booking for current user.")

                if not verified:
                    log(
                        "WARNING: Could not confirm tee sheet contained expected names; treating as failed."
                    )
                    snap_png(driver, f"attempt{attempt}_no_confirmation")
                    snap_html(driver, f"attempt{attempt}_no_confirmation")
                    return False

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
        # Keep verification bounded; do not wait indefinitely for unlock
        if not navigate_and_wait_for_unlock(driver, max_wait_seconds=UNLOCK_WAIT_VERIFY_SEC):
            return
        if not _wait_teetime_table(driver, timeout=10):
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
    log("--- Golf Booking Bot Initialized [v51 modal-validated + verify rows] ---")
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

        job_start_time = time.time()

        try:
            log("\n===== STARTING BOOKING FOR GROUP 1 =====")
            if login(driver, BOOKER_1_USERNAME, BOOKER_1_PASSWORD):
                if navigate_and_wait_for_unlock(driver, max_wait_seconds=UNLOCK_WAIT_BOOKING_SEC):
                    group1_success = execute_group_booking(
                        driver,
                        BOOKER_1_USERNAME,
                        GROUP_1_SIZE,
                        GROUP_1_MAX_ATTEMPTS,
                    )
                    logout(driver)

            # Job-wide timeout check before proceeding to Group 2
            if time.time() - job_start_time > JOB_MAX_RUNTIME_SEC:
                log("JOB TIMEOUT REACHED before Group 2. Aborting remaining flows.")
            else:
                log("\n===== STARTING BOOKING FOR GROUP 2 =====")
                time.sleep(5)
                if login(driver, BOOKER_2_USERNAME, BOOKER_2_PASSWORD):
                    if navigate_and_wait_for_unlock(driver, max_wait_seconds=UNLOCK_WAIT_BOOKING_SEC):
                        group2_success = execute_group_booking(
                            driver,
                            BOOKER_2_USERNAME,
                            GROUP_2_SIZE,
                            GROUP_2_MAX_ATTEMPTS,
                        )
                        logout(driver)

            # Job-wide timeout check before proceeding to Group 3
            if time.time() - job_start_time > JOB_MAX_RUNTIME_SEC:
                log("JOB TIMEOUT REACHED before Group 3. Aborting remaining flows.")
            elif not (group1_success and group2_success):
                log("\n===== STARTING BOOKING FOR GROUP 3 (BACKUP because a primary group failed) =====")
                time.sleep(5)
                if login(driver, BOOKER_3_USERNAME, BOOKER_3_PASSWORD):
                    if navigate_and_wait_for_unlock(driver, max_wait_seconds=UNLOCK_WAIT_BOOKING_SEC):
                        group3_success = execute_group_booking(
                            driver,
                            BOOKER_3_USERNAME,
                            GROUP_3_SIZE,
                            GROUP_3_MAX_ATTEMPTS,
                        )
                        logout(driver)
            else:
                log("\n[INFO] Skipping Group 3 (backup not needed because Groups 1 and 2 succeeded).")

            # Job-wide timeout check before verification
            if time.time() - job_start_time > JOB_MAX_RUNTIME_SEC:
                log("JOB TIMEOUT REACHED before verification. Skipping verification.")
            elif group1_success or group2_success or group3_success:
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
