# Profile Lead Manager

## Admin
- Run `/setgroup` in the review group.
- Callers send `/start`.
- Approve them in the group; approval creates their profile.
- Admin `/start` shows all approved profiles.
- Select a profile to:
  - Upload Leads
  - View Caller Stats
  - Manage Lead Files
- Every caller has separate files and a separate lead pool.
- Uploading a new file makes it that caller's active file.
- Lead Files lets you switch the active file.

## Caller
- `/start`
- `/lead`
- PICKED / NO ANSWER / RAN / INVALID
- PICKED -> upload notes -> READY FOR WA / RAN
- READY FOR WA -> review group -> TAKE FURTHER

## File formats
The bot auto-detects:
1. `Personal Information` files: every occurrence starts a new lead.
2. Otherwise: every non-empty line is one lead.

## Railway
Use:
- `TELEGRAM_BOT_TOKEN`
- `LEADS_DB_PATH=/data/profile_leads.db`

Mount a persistent Railway volume at `/data`.

Start command:
`python bot.py`
