# Task: Rewrite golf booking script for 6 parallel users

## Background
This is a Selenium-based bot that books tee times on Macquarie Links MiClub booking portal.
The existing scripts (booking_script_thursday.py, booking_script_thursday_group2.py) work but are SEQUENTIAL.
We need a new parallel approach.

## Current flow (what we know works)
1. Login at LOGIN_URL with member number + password
2. Navigate to EVENT_LIST_URL
3. Wait for draw at 6:30pm, click event link to enter draw (DO NOT REFRESH once in draw)
4. Wait for queue position (DO NOT REFRESH while in queue)  
5. Once tee sheet appears: find row with available slots
6. Click BOOK GROUP button → modal appears saying "Would You Like To Book Your Playing Partners?" with Yes/No
7. Click NO → lands on makeBooking.xhtml?booking_row_id=XXXXX
8. THIS IS THE NEW PART: On makeBooking page:
   - Player 1 is auto-filled as logged-in user
   - Player 2/3/4 have "Find Player" search boxes
   - Also has "Select Partners" checkboxes at bottom for pre-configured partners
   - Type member number (e.g. "2008") → dropdown shows "Hillard, Gareth [2008]" → click to select
   - Repeat for each partner
   - Click "Confirm Booking" button
   - IMPORTANT: There's a reservation timer (~154 seconds) - must complete within this time!

## New Design: 6 parallel workers with multiprocessing

### Config
```python
# Fixed group composition (to be confirmed - using placeholders for unknown numbers)
FOUR_BALL_GROUP = ["2007", "2008", "2009", "2010"]  # member numbers (Mullin, Hillard, Rutherford, Rudge)
TWO_BALL_GROUP = ["1101", "1107"]  # member numbers (Lalor, Cheney)

ALL_USERS = [
    {"username": "2007", "password": "Golf123#"},
    {"username": "2008", "password": "Golf123#"},
    {"username": "2009", "password": "Golf123#"},
    {"username": "2010", "password": "Golf123#"},
    {"username": "1101", "password": "Golf123#"},
    {"username": "1107", "password": "Golf123#"},
]

# For each user, which partners to add if they win the 4-ball race
# (everyone in FOUR_BALL_GROUP except themselves)
def get_fourball_partners(username):
    return [u for u in FOUR_BALL_GROUP if u != username]

# For each user, which partner to add if they win the 2-ball race  
def get_twoball_partner(username):
    return [u for u in TWO_BALL_GROUP if u != username]
```

### Coordination mechanism
Use multiprocessing.Value and multiprocessing.Event:
- `fourball_booked` = multiprocessing.Event() - set when 4-ball is complete
- `twoball_booked` = multiprocessing.Event() - set when 2-ball is complete
- `fourball_winner` = multiprocessing.Value('u', '') - which username booked the 4-ball

### Worker logic
Each worker:
1. Logs in
2. Enters draw at 6:30pm (copy existing draw/queue logic from booking_script_thursday.py)
3. Waits for tee sheet
4. Tries to book:
   a. If fourball_booked not yet set AND user is in FOUR_BALL_GROUP (or all users try):
      - Find first row with 4+ empty btn-book-me slots
      - Click BOOK GROUP, click NO on modal
      - On makeBooking page: add 3 partners by search (their member numbers)
      - Click Confirm Booking
      - If success: set fourball_booked event, log success
   b. If fourball_booked is set OR user failed 4-ball:
      - Find first row with 2+ empty btn-book-me slots  
      - Click BOOK GROUP, click NO on modal
      - On makeBooking page: add 1 partner by search
      - Click Confirm Booking
      - If success: set twoball_booked event, log success
5. If both events are set: log and exit cleanly

### IMPORTANT DESIGN DECISIONS:
- All 6 users enter the draw independently (this is the KEY advantage - more chances of good queue position)
- Each user has their own Chrome driver instance (headless)
- ALL users try 4-ball first; first to succeed wins; others fall back to 2-ball
- Second to succeed on 2-ball wins; others exit
- The makeBooking.xhtml search flow (not pre-configured group) is REQUIRED because:
  - Pre-configured groups don't include all players we need
  - We need to add by member number search for reliability

### New function: execute_search_booking(driver, partners_to_add, size)
```
def execute_search_booking(driver, username, partners_to_add, required_slots, max_attempts):
    """
    partners_to_add: list of member numbers to add (e.g. ["2008", "2009", "2010"])
    required_slots: 4 for 4-ball, 2 for 2-ball
    
    Flow:
    1. Find first row with >= required_slots empty btn-book-me slots
    2. Click btn-book-group on that row
    3. Wait for "Would You Like To Book Your Playing Partners?" modal
    4. Click the "No" button
    5. Wait for makeBooking.xhtml to load
    6. For each member_number in partners_to_add:
       a. Find next empty "Find Player" input field
       b. Click it and type the member_number
       c. Wait for dropdown to appear (autocomplete list)
       d. Click the first result in dropdown
       e. Verify player was added (field should show name)
    7. Click "Confirm Booking" button
    8. Handle success/error
    """
```

### Selectors to use (based on screenshots and existing code):
- Tee sheet rows: `//div[contains(@class, 'row-time')]`
- Empty book buttons: `.//button[contains(@class, 'btn-book-me')]`
- Book group button: `.//button[contains(@class, 'btn-book-group')]`
- Yes button in modal: `//button[normalize-space()='Yes']`
- No button in modal: `//button[normalize-space()='No']`
- makeBooking page - Find Player inputs: `//input[contains(@placeholder, 'Find Player')]` or similar
- Autocomplete dropdown result: look for list items appearing after typing
- Confirm Booking button: `//button[normalize-space()='Confirm Booking']` or `//a[normalize-space()='Confirm Booking']`
- Reservation timer: look for "Seconds remaining until reservation terminates" text

For the Find Player search, the URL is makeBooking.xhtml and the inputs are likely:
- Input fields with placeholder "Find Player" 
- After typing, a dropdown/autocomplete appears with "Lastname, Firstname [membernumber]"
- Click the dropdown item to select

### GitHub Actions workflow: parallel-booking.yml
Create a NEW workflow file `.github/workflows/parallel-booking.yml`:
- Triggers: schedule (Thursday 6:00 AM UTC = 6:00 PM Sydney time on Thursday), workflow_dispatch
- Single job (not 6 separate jobs - runs all workers in one job via Python multiprocessing)
- Ubuntu latest, Python 3.11, Chrome + Chromedriver
- Env vars: all 6 user credentials from secrets
- Timeout: 150 minutes
- Uploads artifacts (logs, screenshots)

### What to build:
1. `booking_script_parallel.py` - the new parallel script
   - Copy/adapt the existing draw/queue detection logic from booking_script_thursday.py  
   - Add the new execute_search_booking() function
   - Add multiprocessing coordination
   - Worker function that handles the full flow for one user
   - Main function that spawns 6 workers
   
2. `.github/workflows/parallel-booking.yml` - new workflow
   - Run at Thu 06:00 UTC (= Thu 17:00 AEST / 16:00 AEDT)
   - Single job, all 6 users via multiprocessing
   - Upload artifacts

Keep existing files intact - don't modify booking_script.py, booking_script_thursday.py etc.

### Notes:
- The password for all accounts is "Golf123#" (this will come from env vars in production)
- Member numbers 2009, 2010, 1101 name mapping is TBD - use the numbers as-is for search (they work as search terms too)  
- The FOUR_BALL_GROUP and TWO_BALL_GROUP constants should be easy to reconfigure at the top of the file
- Be defensive with the makeBooking search - the page has a timer, so be fast but reliable
- Copy the excellent draw/queue detection from booking_script_thursday.py (it handles "don't refresh" correctly)
- Use logging extensively (same pattern as existing scripts)
- Handle "slot already taken" gracefully - fall back to next slot

When completely finished, run: openclaw system event --text "Done: Golf booking parallel script built - booking_script_parallel.py and parallel-booking.yml workflow ready for review" --mode now
