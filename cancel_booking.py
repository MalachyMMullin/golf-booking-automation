#!/usr/bin/env python3
"""Cancel ALL players from golf booking for Fri 20 Mar at Macquarie Links MiClub."""

import time
import traceback
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

MEMBER_ID = "2009"
PASSWORD = "Golf123#"
LOGIN_URL = "https://macquarielinks.miclub.com.au/security/login.msp"
EVENT_URL = "https://macquarielinks.miclub.com.au/members/bookings/open/event.msp?booking_event_id=287&booking_resource_id=3000000"
SCREENSHOT_DIR = "/tmp"

def screenshot(driver, name):
    path = f"{SCREENSHOT_DIR}/cancel_{name}.png"
    driver.save_screenshot(path)
    print(f"Screenshot saved: {path}")

def setup_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_argument("--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    opts.add_argument("--window-size=1920,1080")
    d = webdriver.Chrome(options=opts)
    d.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    return d

def main():
    driver = setup_driver()
    wait = WebDriverWait(driver, 15)

    try:
        # Step 1: Login
        print("Step 1: Logging in...")
        driver.get(LOGIN_URL)
        time.sleep(2)
        driver.find_element(By.NAME, "user").send_keys(MEMBER_ID)
        driver.find_element(By.NAME, "password").send_keys(PASSWORD)
        driver.find_element(By.XPATH, "//input[@value='Login']").click()
        time.sleep(3)
        print(f"Logged in. URL: {driver.current_url}")

        # Step 2: Go to the Fri 20 Mar tee sheet
        print("\nStep 2: Navigating to Fri 20 Mar tee sheet...")
        driver.get(EVENT_URL)
        time.sleep(3)
        screenshot(driver, "01_tee_sheet_before")

        # Step 3: Find ALL delete buttons for row 27185 (06:36am)
        print("\nStep 3: Finding all delete buttons for row 27185...")

        # Get all cell-remove links for this row
        delete_calls = driver.execute_script("""
            var results = [];
            var links = document.querySelectorAll('a.cell-remove');
            for (var i = 0; i < links.length; i++) {
                var onclick = links[i].getAttribute('onclick') || '';
                if (onclick.indexOf('27185') >= 0) {
                    results.push(onclick);
                }
            }
            // Return unique values only
            return [...new Set(results)];
        """)
        print(f"Delete calls for row 27185: {delete_calls}")

        # Also check current state - who's still booked
        booked_names = driver.execute_script("""
            var names = [];
            var row = document.getElementById('heading-27185');
            if (row) {
                var parent = row.parentElement;
                var spans = parent.querySelectorAll('.booking-name');
                for (var i = 0; i < spans.length; i++) {
                    names.push(spans[i].textContent.trim());
                }
            }
            return names;
        """)
        print(f"Currently booked: {booked_names}")

        # Execute each delete call one at a time, confirming each
        for i, call in enumerate(delete_calls):
            print(f"\nDeleting booking {i+1}/{len(delete_calls)}: {call}")
            driver.execute_script(call)
            time.sleep(1)

            # Handle confirmation dialog
            try:
                alert = driver.switch_to.alert
                print(f"  Alert: {alert.text}")
                alert.accept()
                time.sleep(1)
            except:
                pass

            # Also check for on-page confirmation buttons
            try:
                confirm_btns = driver.find_elements(By.XPATH,
                    "//button[contains(text(),'Yes')] | //button[contains(text(),'Confirm')] | "
                    "//button[contains(text(),'OK')] | //button[contains(text(),'Delete')]")
                for btn in confirm_btns:
                    if btn.is_displayed():
                        print(f"  Clicking confirm: {btn.text}")
                        btn.click()
                        time.sleep(1)
                        break
            except:
                pass

            # Wait for page to settle
            time.sleep(2)
            screenshot(driver, f"02_after_delete_{i+1}")

            # Check if page reloaded or if we need to re-fetch
            # After deletion, the page might ajax-update, so re-check remaining deletes
            remaining = driver.execute_script("""
                var results = [];
                var links = document.querySelectorAll('a.cell-remove');
                for (var i = 0; i < links.length; i++) {
                    var onclick = links[i].getAttribute('onclick') || '';
                    if (onclick.indexOf('27185') >= 0) {
                        results.push(onclick);
                    }
                }
                return [...new Set(results)];
            """)
            print(f"  Remaining delete buttons for row 27185: {remaining}")

            # Check who's still booked
            still_booked = driver.execute_script("""
                var names = [];
                var spans = document.querySelectorAll('.booking-name');
                for (var i = 0; i < spans.length; i++) {
                    var parent = spans[i].closest('[class*="row-"]') || spans[i].parentElement.parentElement.parentElement;
                    var text = parent ? parent.textContent : '';
                    if (text.indexOf('06:36') >= 0 || text.indexOf('6:36') >= 0) {
                        names.push(spans[i].textContent.trim());
                    }
                }
                // Broader approach - check the whole 27185 container
                var all_names = [];
                var all_spans = document.querySelectorAll('.booking-name');
                all_spans.forEach(function(s) {
                    all_names.push(s.textContent.trim());
                });
                return {row_names: names, all_names: all_names};
            """)
            print(f"  Still booked in 06:36: {still_booked}")

        # Final check
        print("\nStep 4: Final verification...")
        driver.get(EVENT_URL)
        time.sleep(3)
        screenshot(driver, "03_final_tee_sheet")

        # Check 06:36 row
        final_check = driver.execute_script("""
            var el = document.getElementById('heading-27185');
            if (!el) return '06:36 row heading not found';
            var row = el.parentElement;
            return row.textContent.substring(0, 300);
        """)
        print(f"06:36 row final state: {final_check}")

        # Check if any names remain in 06:36
        names_left = driver.execute_script("""
            var row = document.getElementById('heading-27185');
            if (!row) return [];
            var parent = row.parentElement;
            var spans = parent.querySelectorAll('.booking-name');
            var names = [];
            spans.forEach(function(s) { names.push(s.textContent.trim()); });
            return names;
        """)
        print(f"Names remaining at 06:36: {names_left}")

        if len(names_left) == 0:
            print("\nSUCCESS: All bookings at 06:36 have been cancelled!")
        else:
            print(f"\nPARTIAL: {len(names_left)} bookings still remain: {names_left}")

    except Exception as e:
        print(f"ERROR: {e}")
        traceback.print_exc()
        screenshot(driver, "error")
    finally:
        driver.quit()

if __name__ == "__main__":
    main()
