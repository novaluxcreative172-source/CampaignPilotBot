"""
CampaignPilotBot — craft a message from a topic (or write your own), then send
it to a single email, a pasted/uploaded list, or a saved reusable list, via
Telegram.

Commands:
  /start      Compose and send a message
  /broadcast  Same as /start (alias, for quick access to sending)
  /savelist   Save a named, reusable list of recipients (e.g. "customers")
  /lists      View and delete your saved lists
  /cancel     Abort whatever you're currently doing

Run locally:   python bot.py   (reads .env)
Deploy:        Railway (see README.md)
"""
import logging
import os

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from utils import ai_writer, mailer, lists_store

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Conversation states: main compose/send flow ---
(
    CHOOSING_MODE,
    AWAITING_TOPIC,
    AWAITING_SUBJECT,
    AWAITING_BODY,
    REVIEWING_DRAFT,
    AWAITING_FEEDBACK,
    CHOOSING_RECIPIENT_MODE,
    AWAITING_RECIPIENTS,
    AWAITING_LIST_CHOICE,
    CONFIRMING_SEND,
) = range(10)

# --- Conversation states: /savelist flow (separate handler, own state space) ---
AWAITING_NEW_LIST_NAME, AWAITING_NEW_LIST_EMAILS = range(100, 102)

ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()
}


def _authorized(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return update.effective_user and update.effective_user.id in ALLOWED_USER_IDS


def _draft_preview(subject: str, body: str) -> str:
    return f"📧 *Subject:* {subject}\n\n{body}"


# ============================================================
# Main compose & send flow
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _authorized(update):
        await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        return ConversationHandler.END

    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("🤖 AI Draft from a topic", callback_data="mode_ai")],
        [InlineKeyboardButton("✍️ Write my own", callback_data="mode_manual")],
    ]
    await update.message.reply_text(
        "Hi! I'm Campaign Pilot. I can draft a message for you and send it to "
        "one email, a pasted/uploaded list, or a saved list.\n\n"
        "How do you want to start?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CHOOSING_MODE


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Cancelled. Send /start to begin again.")
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*Commands*\n"
        "/start or /broadcast — compose and send a message\n"
        "/savelist — save a named, reusable recipient list\n"
        "/lists — view or delete your saved lists\n"
        "/cancel — abort the current action",
        parse_mode=ParseMode.MARKDOWN,
    )


# ---------- Mode selection ----------

async def choose_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "mode_ai":
        await query.edit_message_text("What do you want the email to talk about?")
        return AWAITING_TOPIC
    else:
        await query.edit_message_text("What should the subject line be?")
        return AWAITING_SUBJECT


# ---------- AI drafting path ----------

async def receive_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    topic = update.message.text.strip()
    context.user_data["topic"] = topic
    await update.message.reply_text("Drafting your email...")

    try:
        draft = ai_writer.draft_email(topic)
    except Exception as e:
        logger.exception("AI draft failed")
        await update.message.reply_text(
            f"Sorry, drafting failed ({e}). Try /start again, or write your own instead."
        )
        return ConversationHandler.END

    context.user_data["subject"] = draft["subject"]
    context.user_data["body"] = draft["body"]
    return await _show_draft_for_review(update.message, context)


async def _show_draft_for_review(message, context: ContextTypes.DEFAULT_TYPE) -> int:
    subject = context.user_data["subject"]
    body = context.user_data["body"]
    keyboard = [
        [InlineKeyboardButton("✅ Use this", callback_data="draft_use")],
        [InlineKeyboardButton("🔄 Regenerate", callback_data="draft_regenerate")],
        [InlineKeyboardButton("✏️ Edit manually", callback_data="draft_edit")],
    ]
    await message.reply_text(
        _draft_preview(subject, body),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return REVIEWING_DRAFT


async def review_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "draft_use":
        return await _ask_recipient_mode(query.message, context)

    if query.data == "draft_edit":
        await query.edit_message_text("Okay, what should the subject line be?")
        return AWAITING_SUBJECT

    if query.data == "draft_regenerate":
        await query.edit_message_text("Any feedback to steer the new version? (or send \"-\" for none)")
        return AWAITING_FEEDBACK


async def receive_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    feedback = update.message.text.strip()
    if feedback == "-":
        feedback = ""
    await update.message.reply_text("Regenerating...")
    try:
        draft = ai_writer.regenerate(
            context.user_data["topic"], context.user_data["body"], feedback
        )
    except Exception as e:
        logger.exception("Regeneration failed")
        await update.message.reply_text(f"Regeneration failed ({e}). Try /start again.")
        return ConversationHandler.END

    context.user_data["subject"] = draft["subject"]
    context.user_data["body"] = draft["body"]
    return await _show_draft_for_review(update.message, context)


# ---------- Manual writing path ----------

async def receive_subject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["subject"] = update.message.text.strip()
    await update.message.reply_text("Got it. Now send me the body text of the email.")
    return AWAITING_BODY


async def receive_body(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["body"] = update.message.text.strip()
    return await _show_draft_for_review(update.message, context)


# ---------- Recipients ----------

async def _ask_recipient_mode(message, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        [InlineKeyboardButton("👤 Single email", callback_data="rcpt_single")],
        [InlineKeyboardButton("📋 Paste a list", callback_data="rcpt_list")],
        [InlineKeyboardButton("📎 Upload a file", callback_data="rcpt_file")],
        [InlineKeyboardButton("📁 Use a saved list", callback_data="rcpt_saved")],
    ]
    await message.reply_text(
        "Who should receive this?", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CHOOSING_RECIPIENT_MODE


async def choose_recipient_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "rcpt_saved":
        user_id = update.effective_user.id
        saved = lists_store.get_lists(user_id)
        if not saved:
            await query.edit_message_text(
                "You don't have any saved lists yet. Use /savelist to create one "
                "(e.g. \"customers\" or \"leads\"), then /start again to use it."
            )
            return ConversationHandler.END

        keyboard = [
            [InlineKeyboardButton(f"📁 {name} ({len(emails)})", callback_data=f"uselist_{name}")]
            for name, emails in saved.items()
        ]
        await query.edit_message_text(
            "Which saved list?", reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return AWAITING_LIST_CHOICE

    prompts = {
        "rcpt_single": "Send me the recipient's email address.",
        "rcpt_list": "Paste the email addresses (comma, semicolon, or newline separated).",
        "rcpt_file": "Upload a .txt or .csv file with one email per line (or per row).",
    }
    await query.edit_message_text(prompts[query.data])
    return AWAITING_RECIPIENTS


async def choose_saved_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    name = query.data[len("uselist_"):]
    user_id = update.effective_user.id
    emails = lists_store.get_list(user_id, name)

    if not emails:
        await query.edit_message_text(f"That list ('{name}') is empty or was deleted.")
        return ConversationHandler.END

    context.user_data["recipients"] = emails
    return await _handle_recipients_parsed(query.message, context, emails, edit=True)


async def receive_recipients_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    emails = mailer.extract_emails(update.message.text)
    return await _handle_recipients_parsed(update.message, context, emails)


async def receive_recipients_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    if doc.file_size and doc.file_size > 2_000_000:
        await update.message.reply_text("That file's too large — please keep it under 2MB.")
        return AWAITING_RECIPIENTS

    tg_file = await doc.get_file()
    raw_bytes = await tg_file.download_as_bytearray()
    try:
        text = raw_bytes.decode("utf-8", errors="ignore")
    except Exception:
        await update.message.reply_text("Couldn't read that file as text. Try a .txt or .csv.")
        return AWAITING_RECIPIENTS

    emails = mailer.extract_emails(text)
    return await _handle_recipients_parsed(update.message, context, emails)


async def _handle_recipients_parsed(message, context: ContextTypes.DEFAULT_TYPE, emails: list, edit: bool = False) -> int:
    if not emails:
        text = "I couldn't find any valid email addresses in that. Please try again."
        if edit:
            await message.edit_text(text)
        else:
            await message.reply_text(text)
        return AWAITING_RECIPIENTS

    context.user_data["recipients"] = emails
    subject = context.user_data["subject"]
    body = context.user_data["body"]

    preview = body if len(body) < 300 else body[:300] + "..."
    keyboard = [
        [InlineKeyboardButton("🚀 Send now", callback_data="send_confirm")],
        [InlineKeyboardButton("❌ Cancel", callback_data="send_cancel")],
    ]
    text = (
        f"Ready to send to *{len(emails)}* recipient(s):\n"
        f"{', '.join(emails[:5])}{' ...' if len(emails) > 5 else ''}\n\n"
        f"*Subject:* {subject}\n*Preview:* {preview}"
    )
    if edit:
        await message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
    return CONFIRMING_SEND


async def confirm_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "send_cancel":
        await query.edit_message_text("Cancelled. Send /start to begin again.")
        context.user_data.clear()
        return ConversationHandler.END

    await query.edit_message_text("Sending...")
    recipients = context.user_data["recipients"]
    subject = context.user_data["subject"]
    body = context.user_data["body"]

    result = mailer.send_bulk(recipients, subject, body)
    sent, failed = result["sent"], result["failed"]

    report = f"✅ Sent: {len(sent)}\n❌ Failed: {len(failed)}"
    if failed:
        detail = "\n".join(f"- {email}: {err}" for email, err in failed[:10])
        report += f"\n\nFailures:\n{detail}"

    await query.message.reply_text(report)
    context.user_data.clear()
    return ConversationHandler.END


# ============================================================
# /savelist flow — save a named, reusable recipient list
# ============================================================

async def savelist_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _authorized(update):
        await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        return ConversationHandler.END

    context.user_data.clear()
    await update.message.reply_text(
        "What should this list be called? (e.g. \"customers\", \"leads\")"
    )
    return AWAITING_NEW_LIST_NAME


async def receive_new_list_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Please send a non-empty name.")
        return AWAITING_NEW_LIST_NAME
    context.user_data["new_list_name"] = name
    await update.message.reply_text(
        f"Now paste the email addresses for '{name}' (comma/semicolon/newline "
        "separated), or upload a .txt/.csv file."
    )
    return AWAITING_NEW_LIST_EMAILS


async def receive_new_list_emails_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    emails = mailer.extract_emails(update.message.text)
    return await _finish_savelist(update.message, context, emails)


async def receive_new_list_emails_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    if doc.file_size and doc.file_size > 2_000_000:
        await update.message.reply_text("That file's too large — please keep it under 2MB.")
        return AWAITING_NEW_LIST_EMAILS

    tg_file = await doc.get_file()
    raw_bytes = await tg_file.download_as_bytearray()
    text = raw_bytes.decode("utf-8", errors="ignore")
    emails = mailer.extract_emails(text)
    return await _finish_savelist(update.message, context, emails)


async def _finish_savelist(message, context: ContextTypes.DEFAULT_TYPE, emails: list) -> int:
    if not emails:
        await message.reply_text("I couldn't find any valid email addresses in that. Try again.")
        return AWAITING_NEW_LIST_EMAILS

    name = context.user_data["new_list_name"]
    user_id = message.chat_id
    count = lists_store.save_list(user_id, name, emails)
    await message.reply_text(
        f"✅ Saved list '{name}' with {count} email(s). Use it any time from "
        f"/start → \"Use a saved list\"."
    )
    context.user_data.clear()
    return ConversationHandler.END


# ============================================================
# /lists — view & delete saved lists
# ============================================================

async def show_lists(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        return

    user_id = update.effective_user.id
    saved = lists_store.get_lists(user_id)
    if not saved:
        await update.message.reply_text(
            "You don't have any saved lists yet. Use /savelist to create one."
        )
        return

    keyboard = [
        [InlineKeyboardButton(f"🗑 Delete '{name}' ({len(emails)})", callback_data=f"listdel_{name}")]
        for name, emails in saved.items()
    ]
    await update.message.reply_text(
        "Your saved lists:", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def list_delete_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    name = query.data[len("listdel_"):]
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes, delete", callback_data=f"listdelyes_{name}"),
            InlineKeyboardButton("❌ No", callback_data="listdelno"),
        ]
    ]
    await query.edit_message_text(
        f"Delete list '{name}'? This can't be undone.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def list_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "listdelno":
        await query.edit_message_text("Okay, kept the list as-is.")
        return

    name = query.data[len("listdelyes_"):]
    user_id = update.effective_user.id
    ok = lists_store.delete_list(user_id, name)
    if ok:
        await query.edit_message_text(f"🗑 Deleted list '{name}'.")
    else:
        await query.edit_message_text(f"Couldn't find a list called '{name}' (already deleted?).")


# ============================================================
# App wiring
# ============================================================

def build_app() -> Application:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    application = Application.builder().token(token).build()

    compose_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("broadcast", start),
        ],
        states={
            CHOOSING_MODE: [CallbackQueryHandler(choose_mode, pattern="^mode_")],
            AWAITING_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_topic)],
            AWAITING_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_subject)],
            AWAITING_BODY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_body)],
            REVIEWING_DRAFT: [CallbackQueryHandler(review_draft, pattern="^draft_")],
            AWAITING_FEEDBACK: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_feedback)],
            CHOOSING_RECIPIENT_MODE: [
                CallbackQueryHandler(choose_recipient_mode, pattern="^rcpt_")
            ],
            AWAITING_RECIPIENTS: [
                MessageHandler(filters.Document.ALL, receive_recipients_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_recipients_text),
            ],
            AWAITING_LIST_CHOICE: [CallbackQueryHandler(choose_saved_list, pattern="^uselist_")],
            CONFIRMING_SEND: [CallbackQueryHandler(confirm_send, pattern="^send_")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    savelist_conv = ConversationHandler(
        entry_points=[CommandHandler("savelist", savelist_start)],
        states={
            AWAITING_NEW_LIST_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_list_name)
            ],
            AWAITING_NEW_LIST_EMAILS: [
                MessageHandler(filters.Document.ALL, receive_new_list_emails_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_list_emails_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(compose_conv)
    application.add_handler(savelist_conv)
    application.add_handler(CommandHandler("lists", show_lists))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(list_delete_prompt, pattern="^listdel_"))
    application.add_handler(CallbackQueryHandler(list_delete_confirm, pattern="^listdelyes_|^listdelno"))

    return application


if __name__ == "__main__":
    app = build_app()
    logger.info("Starting CampaignPilotBot...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
