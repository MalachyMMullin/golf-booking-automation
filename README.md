# Golf Booking Automation

PythonAnywhere-ready Selenium bot that monitors the Macquarie Links MiClub booking
portal and automatically books up to three player groups in sequence (Group 1,
fallback Group 2, backup Group 3). The script captures detailed logs, HTML
snapshots, and screenshots for post-run evidence.

## Requirements
- Python 3.10+ (PythonAnywhere default works)
- Google Chrome or Chromium with a matching `chromedriver` on the PATH
- Dependencies listed in `requirements.txt`

Install the Python dependencies:
```bash
pip install -r requirements.txt
```

## Configuration
Set the credentials through environment variables before running the script.
Defaults in the code are placeholders and only exist to keep the script usable
during testing.
```bash
export MIGOLF_USER_1="2007"
export MIGOLF_PASS_1="Golf123#"
export MIGOLF_USER_2="1107"
export MIGOLF_PASS_2="Golf123#"
export MIGOLF_USER_3="2008"
export MIGOLF_PASS_3="Golf123#"
```

The bot stores run artifacts under `~/golfbot_logs/run_YYYY-MM-DD_HH-MM-SS/` and
produces a zipped evidence bundle per run.

## Running on PythonAnywhere
1. Upload the project files and ensure Chrome/Chromedriver paths suit your account.
2. Create a virtualenv and install requirements.
3. Add the environment variables (or use the PythonAnywhere `vars.py` helper).
4. Schedule the script (e.g., daily at 18:55 Sydney time) via the PythonAnywhere task
   scheduler using `python3 /path/to/booking_script.py`.

The script emits verbose stdout logging and writes the same content to
`run.log` for later inspection.

## Running via GitHub Actions
You can also exercise the automation in a GitHub-hosted runner using the
workflows provided in [`.github/workflows`](.github/workflows).

1. In your repository, open **Settings ▸ Secrets and variables ▸ Actions** and
   add the required credentials as *Actions secrets*:
   - `MIGOLF_USER_1`, `MIGOLF_PASS_1`
   - `MIGOLF_USER_2`, `MIGOLF_PASS_2`
   - `MIGOLF_USER_3`, `MIGOLF_PASS_3`
   - Optional: `PREFERRED_DAY`, `PREFERRED_TIME` if you have downstream logic
     that reads them.
2. Navigate to the **Actions** tab, select the workflow you want to exercise,
   and click **“Run workflow.”** You can trigger any workflow on the default
   branch or specify a different ref for testing changes.
   - **Weekly golf booking (Sydney 7 pm Thu):** production workflow that runs
     `booking_script.py` on the scheduled Thursday evening cron as well as on
     demand via the **Run workflow** button.
   - **Friday golf booking (Sydney 7 pm):** companion workflow that targets the
     Sunday tee sheet by running `booking_script_sunday.py` from the Friday
     evening cron (07:40/08:40 UTC) or via manual dispatch.
   - **Manual Thursday booking test:** ad-hoc workflow that runs only when
     manually triggered and executes `booking_script_thursday.py`. It accepts a
     `really_book` toggle (defaults to `false`) so you can run dry tests, and a
     `headless` toggle if you need to see the browser UI during debugging.
3. GitHub Actions provisions Chrome/Chromedriver, installs the Python
   dependencies, and executes the selected script with live logs streamed to the
   workflow run page.
4. When the run finishes, download the `run-artifacts` workflow artifact for any
   evidence bundles, HTML captures, or screenshots produced during the run.

Use workflow_dispatch runs for safe testing, and leave the scheduled triggers in
place for production booking times.
