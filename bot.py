
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DB_PATH = os.getenv("LEADS_DB_PATH", "single_person_leads.db").strip()
OWNER_TELEGRAM_ID = os.getenv("OWNER_TELEGRAM_ID", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("single-person-lead-bot")

PERSONAL_INFO_RE = re.compile(r"(?i)\bpersonal[\s_\-:]*information\b")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS campaigns(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name TEXT NOT NULL,
                source_filename TEXT,
                parser_mode TEXT,
                marker_count INTEGER NOT NULL DEFAULT 0,
                total_leads INTEGER NOT NULL DEFAULT 0,
                current_round INTEGER NOT NULL DEFAULT 1,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS leads(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                value TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'available',
                assigned INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_round INTEGER NOT NULL DEFAULT 0,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(campaign_id) REFERENCES campaigns(id)
            );

            CREATE INDEX IF NOT EXISTS idx_leads_queue
            ON leads(campaign_id,status,assigned,last_round,position);
            """
        )


def owner_id():
    try:
        return int(OWNER_TELEGRAM_ID) if OWNER_TELEGRAM_ID else None
    except ValueError:
        return None


def is_owner(user_id):
    oid = owner_id()
    return oid is None or user_id == oid


async def require_owner(update):
    if update.effective_chat.type != ChatType.PRIVATE:
        return False

    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ This bot is private.")
        return False

    return True


def active_campaign():
    with db() as conn:
        return conn.execute(
            "SELECT * FROM campaigns WHERE active=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()


def decode_file(raw):
    if not raw:
        return ""

    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass

    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            pass

    sample = raw[:4096]
    null_ratio = sample.count(b"\x00") / max(1, len(sample))

    if null_ratio > 0.10:
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                decoded = raw.decode(enc)
                if "personal" in decoded.lower():
                    return decoded
            except UnicodeDecodeError:
                pass

    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def normalize_text(text):
    return (
        text.replace("\x00", "")
        .replace("\u00a0", " ")
        .replace("\u2007", " ")
        .replace("\u202f", " ")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )


def parse_leads(raw):
    text = normalize_text(decode_file(raw))
    matches = list(PERSONAL_INFO_RE.finditer(text))

    if matches:
        leads = []
        for i, match in enumerate(matches):
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            value = text[start:end].strip()
            if value:
                leads.append(value)
        return leads, "PERSONAL_INFORMATION", len(matches)

    leads = [line.strip() for line in text.split("\n") if line.strip()]
    return leads, "LINE_PER_LEAD", 0


def call_keyboard(lead_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("PICKED ✅", callback_data=f"call:pickup:{lead_id}"),
                InlineKeyboardButton("NO ANSWER 📵", callback_data=f"call:noanswer:{lead_id}"),
            ],
            [
                InlineKeyboardButton("RAN ❌", callback_data=f"call:ran:{lead_id}"),
                InlineKeyboardButton("INVALID 🚫", callback_data=f"call:invalid:{lead_id}"),
            ],
        ]
    )


def notes_keyboard(lead_id):
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("READY FOR WA ✅", callback_data=f"notes:ready:{lead_id}"),
            InlineKeyboardButton("RAN ❌", callback_data=f"notes:ran:{lead_id}"),
        ]]
    )


def next_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("NEW LEAD ➡️", callback_data="newlead")]]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update):
        return

    await update.message.reply_text(
        "👋 <b>Single Lead Bot</b>\n\n"
        "Upload a .txt or .csv file, then use /lead.\n\n"
        "Commands:\n"
        "/lead — next lead\n"
        "/stats — campaign stats\n"
        "/reset — archive current campaign",
        parse_mode="HTML",
    )


async def upload_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update):
        return

    doc = update.message.document
    filename = doc.file_name or "leads.txt"

    if not filename.lower().endswith((".txt", ".csv")):
        await update.message.reply_text("Upload a .txt or .csv file.")
        return

    tg_file = await doc.get_file()
    raw = bytes(await tg_file.download_as_bytearray())
    leads, mode, markers = parse_leads(raw)

    if not leads:
        await update.message.reply_text("No leads found.")
        return

    context.user_data["pending_upload"] = {
        "filename": filename,
        "leads": leads,
        "mode": mode,
        "markers": markers,
    }

    if mode == "PERSONAL_INFORMATION":
        await update.message.reply_text(
            f"✅ Personal Information format detected\n"
            f"Markers: {markers:,}\n"
            f"Leads: {len(leads):,}\n\n"
            "FILE NAME"
        )
    else:
        await update.message.reply_text(
            f"✅ Line-per-lead format detected\n"
            f"Leads: {len(leads):,}\n\n"
            "FILE NAME"
        )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update):
        return

    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return

    pending = context.user_data.get("pending_upload")

    if pending:
        display_name = text[:80]

        with db() as conn:
            conn.execute("UPDATE campaigns SET active=0 WHERE active=1")

            cur = conn.execute(
                """
                INSERT INTO campaigns(
                    display_name,source_filename,parser_mode,marker_count,
                    total_leads,current_round,active,created_at
                ) VALUES(?,?,?,?,?,1,1,?)
                """,
                (
                    display_name,
                    pending["filename"],
                    pending["mode"],
                    pending["markers"],
                    len(pending["leads"]),
                    now_iso(),
                ),
            )
            campaign_id = cur.lastrowid

            conn.executemany(
                """
                INSERT INTO leads(
                    campaign_id,position,value,status,assigned,
                    attempts,last_round,created_at,updated_at
                ) VALUES(?,?,?,'available',0,0,0,?,?)
                """,
                [
                    (campaign_id, i, value, now_iso(), now_iso())
                    for i, value in enumerate(pending["leads"], start=1)
                ],
            )

        context.user_data.pop("pending_upload", None)

        await update.message.reply_text(
            f"✅ <b>{escape(display_name)}</b>\n"
            f"{len(pending['leads']):,} leads loaded.\n\n"
            "Use /lead.",
            parse_mode="HTML",
        )
        return

    camp = active_campaign()
    if not camp:
        return

    with db() as conn:
        lead = conn.execute(
            """
            SELECT * FROM leads
            WHERE campaign_id=?
              AND status='awaiting_notes'
              AND assigned=1
            ORDER BY id DESC
            LIMIT 1
            """,
            (camp["id"],),
        ).fetchone()

        if not lead:
            return

        conn.execute(
            "UPDATE leads SET notes=?, updated_at=? WHERE id=?",
            (text, now_iso(), lead["id"]),
        )

    await update.message.reply_text(
        "📝 Notes saved.",
        reply_markup=notes_keyboard(lead["id"]),
    )


def assign_next_lead():
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")

        camp = conn.execute(
            "SELECT * FROM campaigns WHERE active=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()

        if not camp:
            conn.rollback()
            return "no_campaign", None, None

        existing = conn.execute(
            """
            SELECT * FROM leads
            WHERE campaign_id=?
              AND assigned=1
              AND status IN ('assigned','awaiting_notes')
            ORDER BY id DESC
            LIMIT 1
            """,
            (camp["id"],),
        ).fetchone()

        if existing:
            conn.commit()
            return "existing", existing, camp

        round_no = camp["current_round"]

        lead = conn.execute(
            """
            SELECT * FROM leads
            WHERE campaign_id=?
              AND status IN ('available','no_answer')
              AND assigned=0
              AND last_round < ?
            ORDER BY position ASC
            LIMIT 1
            """,
            (camp["id"], round_no),
        ).fetchone()

        if not lead:
            retryable = conn.execute(
                """
                SELECT COUNT(*) c
                FROM leads
                WHERE campaign_id=?
                  AND status IN ('available','no_answer')
                """,
                (camp["id"],),
            ).fetchone()["c"]

            if retryable == 0:
                conn.commit()
                return "finished", None, camp

            round_no += 1
            conn.execute(
                "UPDATE campaigns SET current_round=? WHERE id=?",
                (round_no, camp["id"]),
            )

            lead = conn.execute(
                """
                SELECT * FROM leads
                WHERE campaign_id=?
                  AND status IN ('available','no_answer')
                  AND assigned=0
                  AND last_round < ?
                ORDER BY position ASC
                LIMIT 1
                """,
                (camp["id"], round_no),
            ).fetchone()

        if not lead:
            conn.commit()
            return "waiting", None, camp

        conn.execute(
            """
            UPDATE leads
            SET status='assigned',
                assigned=1,
                updated_at=?
            WHERE id=?
            """,
            (now_iso(), lead["id"]),
        )

        fresh = conn.execute(
            "SELECT * FROM leads WHERE id=?",
            (lead["id"],),
        ).fetchone()

        conn.commit()
        return "assigned", fresh, camp

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def lead_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update):
        return

    state, lead, camp = assign_next_lead()

    if state == "no_campaign":
        await update.effective_message.reply_text("Upload a lead file first.")
        return

    if state == "finished":
        await update.effective_message.reply_text("🎉 No unresolved leads left.")
        return

    if state == "waiting":
        await update.effective_message.reply_text("⏳ No lead available right now.")
        return

    warning = "⚠️ Finish this lead first.\n\n" if state == "existing" else ""

    await update.effective_message.reply_text(
        f"{warning}"
        f"📞 <b>LEAD</b>\n\n"
        f"<code>{escape(lead['value'])}</code>",
        parse_mode="HTML",
        reply_markup=call_keyboard(lead["id"]),
    )


async def newlead_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    await lead_command(update, context)


async def call_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not await require_owner(update):
        return

    _, action, lead_id_raw = q.data.split(":")
    lead_id = int(lead_id_raw)

    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")

        camp = conn.execute(
            "SELECT * FROM campaigns WHERE active=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()

        lead = conn.execute(
            "SELECT * FROM leads WHERE id=?",
            (lead_id,),
        ).fetchone()

        if not camp or not lead or lead["assigned"] != 1 or lead["status"] != "assigned":
            conn.rollback()
            await q.answer("Lead no longer active.", show_alert=True)
            return

        if action == "pickup":
            conn.execute(
                """
                UPDATE leads
                SET status='awaiting_notes',
                    attempts=attempts+1,
                    last_round=?,
                    updated_at=?
                WHERE id=?
                """,
                (camp["current_round"], now_iso(), lead_id),
            )

        elif action == "noanswer":
            conn.execute(
                """
                UPDATE leads
                SET status='no_answer',
                    assigned=0,
                    attempts=attempts+1,
                    last_round=?,
                    updated_at=?
                WHERE id=?
                """,
                (camp["current_round"], now_iso(), lead_id),
            )

        elif action == "ran":
            conn.execute(
                """
                UPDATE leads
                SET status='ran',
                    assigned=0,
                    attempts=attempts+1,
                    last_round=?,
                    updated_at=?
                WHERE id=?
                """,
                (camp["current_round"], now_iso(), lead_id),
            )

        elif action == "invalid":
            conn.execute(
                """
                UPDATE leads
                SET status='invalid',
                    assigned=0,
                    attempts=attempts+1,
                    last_round=?,
                    updated_at=?
                WHERE id=?
                """,
                (camp["current_round"], now_iso(), lead_id),
            )

        conn.commit()

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    if action == "pickup":
        await q.message.reply_text("Upload Notes 📝")
    elif action == "noanswer":
        await q.message.reply_text("📵 NO ANSWER", reply_markup=next_keyboard())
    elif action == "ran":
        await q.message.reply_text("❌ RAN", reply_markup=next_keyboard())
    elif action == "invalid":
        await q.message.reply_text("🚫 INVALID", reply_markup=next_keyboard())


async def notes_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not await require_owner(update):
        return

    _, action, lead_id_raw = q.data.split(":")
    lead_id = int(lead_id_raw)

    with db() as conn:
        lead = conn.execute(
            "SELECT * FROM leads WHERE id=?",
            (lead_id,),
        ).fetchone()

        if not lead or lead["assigned"] != 1 or lead["status"] != "awaiting_notes":
            await q.answer("Lead no longer active.", show_alert=True)
            return

        if action == "ran":
            conn.execute(
                """
                UPDATE leads
                SET status='ran',
                    assigned=0,
                    updated_at=?
                WHERE id=?
                """,
                (now_iso(), lead_id),
            )

            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except TelegramError:
                pass

            await q.message.reply_text("❌ RAN", reply_markup=next_keyboard())
            return

        notes = (lead["notes"] or "").strip()
        if not notes:
            await q.answer("Write notes first.", show_alert=True)
            return

        conn.execute(
            """
            UPDATE leads
            SET status='ready_wa',
                assigned=0,
                updated_at=?
            WHERE id=?
            """,
            (now_iso(), lead_id),
        )

    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    await q.message.reply_text(
        "📲 <b>READY FOR WA</b>\n"
        f"<code>{escape(lead['value'])}</code>\n"
        f"📝 {escape(notes)}",
        parse_mode="HTML",
        reply_markup=next_keyboard(),
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update):
        return

    camp = active_campaign()

    if not camp:
        await update.effective_message.reply_text("No active campaign.")
        return

    with db() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) c
            FROM leads
            WHERE campaign_id=?
            GROUP BY status
            """,
            (camp["id"],),
        ).fetchall()

    counts = {r["status"]: r["c"] for r in rows}

    await update.effective_message.reply_text(
        f"📊 <b>{escape(camp['display_name'])}</b>\n\n"
        f"Total: <b>{camp['total_leads']:,}</b>\n"
        f"Available: <b>{counts.get('available',0):,}</b>\n"
        f"No Answer: <b>{counts.get('no_answer',0):,}</b>\n"
        f"Picked / Waiting Notes: <b>{counts.get('awaiting_notes',0):,}</b>\n"
        f"Ready for WA: <b>{counts.get('ready_wa',0):,}</b>\n"
        f"Ran: <b>{counts.get('ran',0):,}</b>\n"
        f"Invalid: <b>{counts.get('invalid',0):,}</b>",
        parse_mode="HTML",
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update):
        return

    with db() as conn:
        conn.execute("UPDATE campaigns SET active=0 WHERE active=1")

    await update.effective_message.reply_text("✅ Current campaign archived.")


async def error_handler(update, context):
    log.exception("Unhandled bot error", exc_info=context.error)


def main():
    if not TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN.")

    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("lead", lead_command))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("reset", reset))

    app.add_handler(
        CallbackQueryHandler(
            call_result,
            pattern=r"^call:(pickup|noanswer|ran|invalid):\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            notes_result,
            pattern=r"^notes:(ready|ran):\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            newlead_callback,
            pattern=r"^newlead$",
        )
    )

    app.add_handler(MessageHandler(filters.Document.ALL, upload_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    app.add_error_handler(error_handler)

    log.info("Single Person Lead Bot running")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
