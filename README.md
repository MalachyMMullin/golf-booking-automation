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
