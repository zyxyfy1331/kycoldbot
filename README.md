# Single Person Lead Bot

Simple private Telegram lead caller bot for one user.

## Flow

1. Upload `.txt` or `.csv`
2. Enter a FILE NAME
3. Use `/lead`
4. Choose:
   - PICKED
   - NO ANSWER
   - RAN
   - INVALID

### NO ANSWER
Returns to the retry pool and is sent again after the untouched leads are exhausted.

### RAN / INVALID
Permanently closed. They never recirculate.

### PICKED
The bot asks you to type notes.

After notes:
- READY FOR WA
- RAN

### READY FOR WA
The bot sends a neat message containing:

- full lead
- notes underneath

Then shows NEW LEAD.

## File detection

Supports both automatically:

### Normal files
Every non-empty line = one lead.

### Personal Information files
If `Personal Information` occurs in the file, every occurrence starts a new lead.
Everything until the next occurrence belongs to that lead.

## Commands

- `/start`
- `/lead`
- `/stats`
- `/reset`

## Railway

Variables:

`TELEGRAM_BOT_TOKEN=...`

Recommended:

`OWNER_TELEGRAM_ID=your Telegram numeric user ID`

`LEADS_DB_PATH=/data/single_person_leads.db`

Mount a Railway persistent volume at `/data`.

Start command:

`python bot.py`


## Compact READY FOR WA

READY FOR WA messages are intentionally compact:

`📲 READY FOR WA`
`<lead>`
`📝 <notes>`

No extra blank lines, timestamps, caller name, file name, or extra labels.
