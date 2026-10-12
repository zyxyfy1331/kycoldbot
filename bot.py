
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import Forbidden, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DB_PATH = os.getenv("LEADS_DB_PATH", "profile_leads.db").strip()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("profile-lead-manager")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def uk_time():
    return datetime.now(ZoneInfo("Europe/London")).strftime("%d/%m/%Y %H:%M")


def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS workers(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            full_name TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            requested_at TEXT NOT NULL,
            approved_at TEXT,
            approved_by INTEGER
        );

        CREATE TABLE IF NOT EXISTS lead_files(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_user_id INTEGER NOT NULL,
            display_name TEXT NOT NULL,
            source_filename TEXT,
            parser_mode TEXT,
            marker_count INTEGER NOT NULL DEFAULT 0,
            total_leads INTEGER NOT NULL DEFAULT 0,
            current_round INTEGER NOT NULL DEFAULT 1,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            created_by INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS leads(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            profile_user_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            value TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'available',
            assigned_to INTEGER,
            assigned_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_round INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT,
            notes TEXT,
            caller_username TEXT,
            caller_name TEXT,
            ready_at TEXT,
            ran_at TEXT,
            invalid_at TEXT,
            taken_by INTEGER,
            taken_at TEXT
        );

        CREATE TABLE IF NOT EXISTS call_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            file_id INTEGER NOT NULL,
            profile_user_id INTEGER NOT NULL,
            outcome TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_queue
        ON leads(profile_user_id,file_id,status,last_round,position);

        CREATE INDEX IF NOT EXISTS idx_assigned
        ON leads(profile_user_id,assigned_to,status);
        """)


def set_setting(key, value):
    with db() as c:
        c.execute("""
            INSERT INTO settings(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, str(value)))


def get_setting(key):
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def group_id():
    raw = get_setting("team_group_id")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


PERSONAL_INFO_RE = re.compile(r"(?i)\bpersonal[\s_\-:]*information\b")


def decode_file(raw):
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def parse_leads(raw):
    text = decode_file(raw).replace("\x00","").replace("\r\n","\n").replace("\r","\n")
    matches = list(PERSONAL_INFO_RE.finditer(text))
    if matches:
        leads = []
        for i, m in enumerate(matches):
            end = matches[i+1].start() if i+1 < len(matches) else len(text)
            lead = text[m.start():end].strip()
            if lead:
                leads.append(lead)
        return leads, "PERSONAL_INFORMATION", len(matches)
    return [x.strip() for x in text.split("\n") if x.strip()], "LINE_PER_LEAD", 0


def worker(uid):
    with db() as c:
        return c.execute("SELECT * FROM workers WHERE user_id=?", (uid,)).fetchone()


def active_file(uid):
    with db() as c:
        return c.execute("""
            SELECT * FROM lead_files
            WHERE profile_user_id=? AND is_active=1
            ORDER BY id DESC LIMIT 1
        """, (uid,)).fetchone()


async def is_admin(context, uid, chat_id=None):
    gid = chat_id or group_id()
    if not gid:
        return False
    try:
        m = await context.bot.get_chat_member(gid, uid)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except TelegramError:
        return False


async def require_admin(update, context):
    gid = group_id()
    if not gid:
        await update.effective_message.reply_text("No review group configured. Run /setgroup in your group.")
        return False
    if not await is_admin(context, update.effective_user.id, gid):
        await update.effective_message.reply_text("⛔ Admins only.")
        return False
    return True


async def require_worker(update):
    r = worker(update.effective_user.id)
    if r and r["status"] == "approved":
        return True
    await update.effective_message.reply_text("⏳ You need admin approval first. Send /start.")
    return False


def approval_kb(uid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve:{uid}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"decline:{uid}")
    ]])


def profiles_kb():
    with db() as c:
        rows = c.execute("""
            SELECT * FROM workers
            WHERE status='approved'
            ORDER BY LOWER(COALESCE(first_name,full_name,username,''))
        """).fetchall()
    buttons = []
    for r in rows:
        name = r["first_name"] or r["full_name"] or r["username"] or str(r["user_id"])
        buttons.append([InlineKeyboardButton(f"👤 {name}", callback_data=f"profile:{r['user_id']}")])
    buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="admin:profiles")])
    return InlineKeyboardMarkup(buttons)


def profile_kb(uid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Upload Leads", callback_data=f"profile_upload:{uid}")],
        [InlineKeyboardButton("📊 Caller Stats", callback_data=f"profile_stats:{uid}")],
        [InlineKeyboardButton("📁 Lead Files", callback_data=f"profile_files:{uid}")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin:profiles")],
    ])


def files_kb(uid):
    with db() as c:
        rows = c.execute("""
            SELECT * FROM lead_files WHERE profile_user_id=?
            ORDER BY id DESC
        """, (uid,)).fetchall()
    buttons = []
    for r in rows:
        prefix = "✅ " if r["is_active"] else ""
        buttons.append([InlineKeyboardButton(
            f"{prefix}{r['display_name']} ({r['total_leads']:,})",
            callback_data=f"activate:{r['id']}:{uid}"
        )])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data=f"profile:{uid}")])
    return InlineKeyboardMarkup(buttons)


def call_kb(lead_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("PICKED ✅", callback_data=f"call:pickup:{lead_id}"),
            InlineKeyboardButton("NO ANSWER 📵", callback_data=f"call:noanswer:{lead_id}")
        ],
        [
            InlineKeyboardButton("RAN ❌", callback_data=f"call:ran:{lead_id}"),
            InlineKeyboardButton("INVALID 🚫", callback_data=f"call:invalid:{lead_id}")
        ],
    ])


def new_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("NEW LEAD ➡️", callback_data="newlead")]])


def notes_kb(lead_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("READY FOR WA ✅", callback_data=f"notes:ready:{lead_id}"),
        InlineKeyboardButton("RAN ❌", callback_data=f"notes:ran:{lead_id}")
    ]])


def take_kb(lead_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("TAKE FURTHER ➡️", callback_data=f"take:{lead_id}")]])


async def setgroup(update, context):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("Run /setgroup inside your review group.")
        return
    if not await is_admin(context, update.effective_user.id, chat.id):
        await update.effective_message.reply_text("⛔ Group admins only.")
        return
    set_setting("team_group_id", chat.id)
    await update.effective_message.reply_text("✅ Review group configured.")


async def start(update, context):
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    u = update.effective_user
    gid = group_id()

    if gid and await is_admin(context, u.id, gid):
        await update.message.reply_text(
            "👑 <b>ADMIN PANEL</b>\n\nSelect a profile:",
            parse_mode="HTML",
            reply_markup=profiles_kb()
        )
        return

    with db() as c:
        r = c.execute("SELECT * FROM workers WHERE user_id=?", (u.id,)).fetchone()

        if r and r["status"] == "approved":
            c.execute("""
                UPDATE workers SET username=?,first_name=?,full_name=? WHERE user_id=?
            """, (u.username or "", u.first_name or "", u.full_name or "", u.id))
            await update.message.reply_text("✅ You're approved.\n\nSend /lead.")
            return

        if r and r["status"] == "pending":
            await update.message.reply_text("⏳ Approval pending.")
            return

        if r:
            c.execute("""
                UPDATE workers SET username=?,first_name=?,full_name=?,status='pending',requested_at=?
                WHERE user_id=?
            """, (u.username or "", u.first_name or "", u.full_name or "", now_iso(), u.id))
        else:
            c.execute("""
                INSERT INTO workers(user_id,username,first_name,full_name,status,requested_at)
                VALUES(?,?,?,?,?,?)
            """, (u.id, u.username or "", u.first_name or "", u.full_name or "", "pending", now_iso()))

    await update.message.reply_text("⏳ Approval request sent.")

    if gid:
        await context.bot.send_message(
            gid,
            f"👤 <b>NEW CALLER</b>\n\nName: {escape(u.full_name or '')}\nUsername: {escape('@'+u.username if u.username else 'None')}",
            parse_mode="HTML",
            reply_markup=approval_kb(u.id)
        )


async def approve_cb(update, context):
    q = update.callback_query
    await q.answer()

    gid = group_id()
    if not gid or q.message.chat_id != gid or not await is_admin(context, update.effective_user.id, gid):
        return

    action, uid_s = q.data.split(":")
    uid = int(uid_s)
    approved = action == "approve"

    with db() as c:
        r = c.execute("SELECT * FROM workers WHERE user_id=?", (uid,)).fetchone()
        if not r:
            return
        c.execute("""
            UPDATE workers SET status=?,approved_at=?,approved_by=? WHERE user_id=?
        """, ("approved" if approved else "declined",
              now_iso() if approved else None,
              update.effective_user.id if approved else None,
              uid))

    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    await q.message.reply_text("✅ Profile created." if approved else "❌ Caller declined.")

    try:
        await context.bot.send_message(
            uid,
            "✅ You've been approved. Your profile is ready.\n\nSend /lead."
            if approved else "❌ Your request was declined."
        )
    except TelegramError:
        pass


async def admin_menu_cb(update, context):
    q = update.callback_query
    await q.answer()
    if not await require_admin(update, context):
        return

    if q.data == "admin:profiles":
        await q.edit_message_text("👑 <b>ADMIN PANEL</b>\n\nSelect a profile:", parse_mode="HTML", reply_markup=profiles_kb())
        return

    if q.data.startswith("profile:"):
        uid = int(q.data.split(":")[1])
        r = worker(uid)
        if not r or r["status"] != "approved":
            return
        name = r["first_name"] or r["full_name"] or r["username"] or str(uid)
        await q.edit_message_text(f"👤 <b>{escape(name)}</b>", parse_mode="HTML", reply_markup=profile_kb(uid))
        return

    if q.data.startswith("profile_upload:"):
        uid = int(q.data.split(":")[1])
        context.user_data["upload_for_profile"] = uid
        context.user_data.pop("pending_upload", None)
        r = worker(uid)
        name = r["first_name"] or r["full_name"] or r["username"] or str(uid)
        await q.message.reply_text(f"📤 Send the lead file for <b>{escape(name)}</b>.", parse_mode="HTML")
        return

    if q.data.startswith("profile_files:"):
        uid = int(q.data.split(":")[1])
        await q.edit_message_text("📁 <b>Lead Files</b>\n\nTap a file to make it active.", parse_mode="HTML", reply_markup=files_kb(uid))
        return

    if q.data.startswith("profile_stats:"):
        uid = int(q.data.split(":")[1])
        await show_stats(q.message, uid)


async def show_stats(message, uid):
    r = worker(uid)
    if not r:
        return

    with db() as c:
        file_count = c.execute("SELECT COUNT(*) c FROM lead_files WHERE profile_user_id=?", (uid,)).fetchone()["c"]
        af = c.execute("SELECT * FROM lead_files WHERE profile_user_id=? AND is_active=1 ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        rows = c.execute("SELECT status,COUNT(*) c FROM leads WHERE profile_user_id=? GROUP BY status", (uid,)).fetchall()
        events = c.execute("SELECT outcome,COUNT(*) c FROM call_events WHERE profile_user_id=? GROUP BY outcome", (uid,)).fetchall()

    s = {x["status"]: x["c"] for x in rows}
    e = {x["outcome"]: x["c"] for x in events}
    name = r["first_name"] or r["full_name"] or r["username"] or str(uid)

    await message.reply_text(
        f"📊 <b>{escape(name)} — Caller Stats</b>\n\n"
        f"Active File: <b>{escape(af['display_name']) if af else 'None'}</b>\n"
        f"Files: <b>{file_count}</b>\n\n"
        f"Available: <b>{s.get('available',0):,}</b>\n"
        f"No Answer: <b>{s.get('no_answer',0):,}</b>\n"
        f"Ready WA: <b>{s.get('ready_wa',0):,}</b>\n"
        f"Ran: <b>{s.get('ran',0):,}</b>\n"
        f"Invalid: <b>{s.get('invalid',0):,}</b>\n"
        f"Taken: <b>{s.get('taken',0):,}</b>\n\n"
        f"Picked: <b>{e.get('picked_up',0):,}</b>",
        parse_mode="HTML"
    )


async def activate_cb(update, context):
    q = update.callback_query
    await q.answer()
    if not await require_admin(update, context):
        return

    _, file_s, uid_s = q.data.split(":")
    file_id, uid = int(file_s), int(uid_s)

    with db() as c:
        f = c.execute("SELECT * FROM lead_files WHERE id=? AND profile_user_id=?", (file_id, uid)).fetchone()
        if not f:
            return
        c.execute("UPDATE lead_files SET is_active=0 WHERE profile_user_id=?", (uid,))
        c.execute("UPDATE lead_files SET is_active=1 WHERE id=?", (file_id,))

    await q.edit_message_text(
        f"✅ Active file: <b>{escape(f['display_name'])}</b>",
        parse_mode="HTML",
        reply_markup=files_kb(uid)
    )


async def document_handler(update, context):
    if update.effective_chat.type != ChatType.PRIVATE or not await require_admin(update, context):
        return

    uid = context.user_data.get("upload_for_profile")
    if not uid:
        await update.message.reply_text("Select a caller profile and tap Upload Leads first.")
        return

    doc = update.message.document
    filename = doc.file_name or "leads.txt"
    if not filename.lower().endswith((".txt",".csv")):
        await update.message.reply_text("Upload a .txt or .csv file.")
        return

    f = await doc.get_file()
    raw = bytes(await f.download_as_bytearray())
    values, mode, markers = parse_leads(raw)

    if not values:
        await update.message.reply_text("No leads detected.")
        return

    context.user_data["pending_upload"] = {
        "uid": uid,
        "filename": filename,
        "values": values,
        "mode": mode,
        "markers": markers
    }

    if mode == "PERSONAL_INFORMATION":
        await update.message.reply_text(f"✅ Personal Information format\nMarkers: {markers:,}\nLeads: {len(values):,}\n\nFILE NAME")
    else:
        await update.message.reply_text(f"✅ Line-per-lead format\nLeads: {len(values):,}\n\nFILE NAME")


async def text_handler(update, context):
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return

    pending = context.user_data.get("pending_upload")
    if pending:
        if not await require_admin(update, context):
            return

        uid = pending["uid"]
        values = pending["values"]
        display_name = text[:80]

        with db() as c:
            c.execute("UPDATE lead_files SET is_active=0 WHERE profile_user_id=?", (uid,))
            cur = c.execute("""
                INSERT INTO lead_files(
                    profile_user_id,display_name,source_filename,parser_mode,
                    marker_count,total_leads,current_round,is_active,created_at,created_by
                ) VALUES(?,?,?,?,?,?,1,1,?,?)
            """, (
                uid, display_name, pending["filename"], pending["mode"],
                pending["markers"], len(values), now_iso(), update.effective_user.id
            ))
            file_id = cur.lastrowid
            c.executemany("""
                INSERT INTO leads(file_id,profile_user_id,position,value,status,last_round)
                VALUES(?,?,?,?,'available',0)
            """, [(file_id, uid, i, v) for i, v in enumerate(values, 1)])

        context.user_data.pop("pending_upload", None)

        r = worker(uid)
        name = r["first_name"] or r["full_name"] or r["username"] or str(uid)
        await update.message.reply_text(
            f"✅ <b>{escape(display_name)}</b>\n{len(values):,} leads uploaded to <b>{escape(name)}</b>.",
            parse_mode="HTML",
            reply_markup=profile_kb(uid)
        )
        return

    if not await require_worker(update):
        return

    uid = update.effective_user.id
    with db() as c:
        lead = c.execute("""
            SELECT * FROM leads
            WHERE profile_user_id=? AND assigned_to=? AND status='awaiting_notes'
            ORDER BY assigned_at DESC LIMIT 1
        """, (uid, uid)).fetchone()

        if not lead:
            return

        c.execute("UPDATE leads SET notes=? WHERE id=?", (text, lead["id"]))

    await update.message.reply_text("📝 Notes saved.", reply_markup=notes_kb(lead["id"]))


def assign_next(uid, username, full_name):
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        f = c.execute("""
            SELECT * FROM lead_files
            WHERE profile_user_id=? AND is_active=1
            ORDER BY id DESC LIMIT 1
        """, (uid,)).fetchone()

        if not f:
            c.rollback()
            return "no_file", None, None

        existing = c.execute("""
            SELECT * FROM leads
            WHERE profile_user_id=? AND assigned_to=? AND status IN ('assigned','awaiting_notes')
            ORDER BY assigned_at DESC LIMIT 1
        """, (uid,uid)).fetchone()

        if existing:
            c.commit()
            return "existing", existing, f

        round_no = f["current_round"]
        lead = c.execute("""
            SELECT * FROM leads
            WHERE file_id=? AND status IN ('available','no_answer')
              AND assigned_to IS NULL AND last_round < ?
            ORDER BY position LIMIT 1
        """, (f["id"], round_no)).fetchone()

        if not lead:
            retryable = c.execute("""
                SELECT COUNT(*) c FROM leads
                WHERE file_id=? AND status IN ('available','no_answer')
            """, (f["id"],)).fetchone()["c"]

            if retryable == 0:
                c.commit()
                return "finished", None, f

            round_no += 1
            c.execute("UPDATE lead_files SET current_round=? WHERE id=?", (round_no, f["id"]))
            lead = c.execute("""
                SELECT * FROM leads
                WHERE file_id=? AND status IN ('available','no_answer')
                  AND assigned_to IS NULL AND last_round < ?
                ORDER BY position LIMIT 1
            """, (f["id"], round_no)).fetchone()

        if not lead:
            c.commit()
            return "waiting", None, f

        c.execute("""
            UPDATE leads SET status='assigned',assigned_to=?,assigned_at=?,
            caller_username=?,caller_name=? WHERE id=?
        """, (uid, now_iso(), username, full_name, lead["id"]))

        fresh = c.execute("SELECT * FROM leads WHERE id=?", (lead["id"],)).fetchone()
        c.commit()
        return "assigned", fresh, f
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


async def lead_command(update, context):
    if update.effective_chat.type != ChatType.PRIVATE or not await require_worker(update):
        return

    u = update.effective_user
    state, lead, f = assign_next(u.id, u.username or "", u.full_name or "")

    if state == "no_file":
        await update.effective_message.reply_text("No active lead file assigned to your profile.")
        return
    if state == "finished":
        await update.effective_message.reply_text("🎉 No unresolved leads left.")
        return
    if state == "waiting":
        await update.effective_message.reply_text("⏳ Try again shortly.")
        return

    warning = "⚠️ Finish this lead first.\n\n" if state == "existing" else ""
    await update.effective_message.reply_text(
        f"{warning}📞 <b>YOUR LEAD</b>\n\n<code>{escape(lead['value'])}</code>",
        parse_mode="HTML",
        reply_markup=call_kb(lead["id"])
    )


async def newlead_cb(update, context):
    q = update.callback_query
    await q.answer()
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass
    await lead_command(update, context)


async def call_result(update, context):
    q = update.callback_query
    await q.answer()

    if not await require_worker(update):
        return

    _, action, lead_s = q.data.split(":")
    lead_id = int(lead_s)
    uid = update.effective_user.id

    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        lead = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not lead or lead["assigned_to"] != uid or lead["status"] != "assigned":
            c.rollback()
            return

        f = c.execute("SELECT * FROM lead_files WHERE id=?", (lead["file_id"],)).fetchone()
        t = now_iso()

        if action == "pickup":
            c.execute("""
                UPDATE leads SET status='awaiting_notes',attempts=attempts+1,
                last_round=?,last_attempt_at=? WHERE id=?
            """, (f["current_round"], t, lead_id))
            outcome = "picked_up"
        elif action == "noanswer":
            c.execute("""
                UPDATE leads SET status='no_answer',assigned_to=NULL,assigned_at=NULL,
                attempts=attempts+1,last_round=?,last_attempt_at=? WHERE id=?
            """, (f["current_round"], t, lead_id))
            outcome = "no_answer"
        elif action == "ran":
            c.execute("""
                UPDATE leads SET status='ran',assigned_to=NULL,assigned_at=NULL,
                attempts=attempts+1,last_round=?,last_attempt_at=?,ran_at=? WHERE id=?
            """, (f["current_round"], t, t, lead_id))
            outcome = "ran"
        else:
            c.execute("""
                UPDATE leads SET status='invalid',assigned_to=NULL,assigned_at=NULL,
                attempts=attempts+1,last_round=?,last_attempt_at=?,invalid_at=? WHERE id=?
            """, (f["current_round"], t, t, lead_id))
            outcome = "invalid"

        c.execute("""
            INSERT INTO call_events(lead_id,file_id,profile_user_id,outcome,created_at)
            VALUES(?,?,?,?,?)
        """, (lead_id, lead["file_id"], uid, outcome, t))
        c.commit()
    finally:
        c.close()

    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    if action == "noanswer":
        await q.message.reply_text("📵 NO ANSWER", reply_markup=new_kb())
        return
    if action == "ran":
        await q.message.reply_text("❌ RAN", reply_markup=new_kb())
        return
    if action == "invalid":
        await q.message.reply_text("🚫 INVALID", reply_markup=new_kb())
        return

    caller = f"@{update.effective_user.username}" if update.effective_user.username else update.effective_user.full_name
    gid = group_id()
    if gid:
        await context.bot.send_message(
            gid,
            f"✅ <b>PICKED</b>\n👤 {escape(caller)}\n🏦 <b>{escape(f['display_name'])}</b>\n⏰ {escape(uk_time())}",
            parse_mode="HTML"
        )

    await q.message.reply_text("Upload Notes 📝")


async def notes_result(update, context):
    q = update.callback_query
    await q.answer()

    if not await require_worker(update):
        return

    _, action, lead_s = q.data.split(":")
    lead_id = int(lead_s)
    uid = update.effective_user.id

    with db() as c:
        lead = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not lead or lead["assigned_to"] != uid or lead["status"] != "awaiting_notes":
            return

        if action == "ran":
            c.execute("""
                UPDATE leads SET status='ran',assigned_to=NULL,assigned_at=NULL,ran_at=?
                WHERE id=?
            """, (now_iso(), lead_id))
            await q.message.reply_text("❌ RAN", reply_markup=new_kb())
            return

        if not (lead["notes"] or "").strip():
            await q.answer("Upload notes first.", show_alert=True)
            return

        f = c.execute("SELECT * FROM lead_files WHERE id=?", (lead["file_id"],)).fetchone()
        c.execute("""
            UPDATE leads SET status='ready_wa',assigned_to=NULL,assigned_at=NULL,ready_at=?
            WHERE id=?
        """, (now_iso(), lead_id))

    gid = group_id()
    if not gid:
        return

    caller = f"@{lead['caller_username']}" if lead["caller_username"] else lead["caller_name"]
    safe_notes = re.sub(r"(?<!\w)(?:\+?\d[\d\s().\-]{8,20}\d)(?!\w)", "[HIDDEN]", lead["notes"])

    await context.bot.send_message(
        gid,
        f"📲 <b>READY FOR WA</b>\n🏦 <b>{escape(f['display_name'])}</b>\n"
        f"📝 <b>Notes:</b>\n{escape(safe_notes)}\n\n\n"
        f"⏰ {escape(uk_time())}\n👤 {escape(caller)}",
        parse_mode="HTML",
        reply_markup=take_kb(lead_id)
    )

    await q.message.reply_text("✅ READY FOR WA", reply_markup=new_kb())


async def take_further(update, context):
    q = update.callback_query
    await q.answer()
    gid = group_id()

    if not gid or q.message.chat_id != gid or not await is_admin(context, update.effective_user.id, gid):
        return

    lead_id = int(q.data.split(":")[1])

    with db() as c:
        lead = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not lead or lead["status"] != "ready_wa":
            return
        f = c.execute("SELECT * FROM lead_files WHERE id=?", (lead["file_id"],)).fetchone()
        c.execute("UPDATE leads SET status='taking',taken_by=? WHERE id=?", (update.effective_user.id, lead_id))

    try:
        await context.bot.send_message(
            update.effective_user.id,
            f"🔥 <b>TAKE FURTHER</b>\n\n🏦 <b>{escape(f['display_name'])}</b>\n\n"
            f"<b>Full Lead</b>\n<code>{escape(lead['value'])}</code>\n\n"
            f"<b>Notes</b>\n{escape(lead['notes'] or '')}\n\n"
            f"<b>Attempts</b>: {lead['attempts']}",
            parse_mode="HTML"
        )
    except (Forbidden, TelegramError):
        with db() as c:
            c.execute("UPDATE leads SET status='ready_wa',taken_by=NULL WHERE id=?", (lead_id,))
        return

    with db() as c:
        c.execute("UPDATE leads SET status='taken',taken_at=? WHERE id=?", (now_iso(), lead_id))

    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass


def main():
    if not TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN")

    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setgroup", setgroup))
    app.add_handler(CommandHandler("lead", lead_command))

    app.add_handler(CallbackQueryHandler(approve_cb, pattern=r"^(approve|decline):\d+$"))
    app.add_handler(CallbackQueryHandler(admin_menu_cb, pattern=r"^(admin:profiles|profile:\d+|profile_upload:\d+|profile_stats:\d+|profile_files:\d+)$"))
    app.add_handler(CallbackQueryHandler(activate_cb, pattern=r"^activate:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(call_result, pattern=r"^call:(pickup|noanswer|ran|invalid):\d+$"))
    app.add_handler(CallbackQueryHandler(notes_result, pattern=r"^notes:(ready|ran):\d+$"))
    app.add_handler(CallbackQueryHandler(newlead_cb, pattern=r"^newlead$"))
    app.add_handler(CallbackQueryHandler(take_further, pattern=r"^take:\d+$"))

    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    log.info("Profile Lead Manager running")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
