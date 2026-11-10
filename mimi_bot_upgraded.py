"""Mimi Telegram AI bot - upgraded, async, persistent and configurable.

Required environment variables:
  TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, OWNER_ID
Optional:
  BOT_NAME=Mimi
  GEMINI_MODEL=gemini-2.5-flash
  USE_GOOGLE_SEARCH=true
  MAX_HISTORY_MESSAGES=20
  MAX_OUTPUT_TOKENS=2048
  REQUEST_TIMEOUT=45
  ENABLE_STICKERS=true

Install:
  pip install -U python-telegram-bot google-genai edge-tts

Note: API, Telegram and hosting quotas still apply. This bot handles failures
without pretending that a failed Gemini request was a successful AI answer.
"""

import asyncio
import json
import logging
import os
import random
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import edge_tts
from google import genai
from google.genai import types
from telegram import Update, ReactionTypeEmoji
from telegram.constants import ChatAction, ChatType
from telegram.error import Conflict, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()
BOT_NAME = os.getenv("BOT_NAME", "Mimi").strip() or "Mimi"
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
DATA_FILE = Path(os.getenv("DATA_FILE", "mimi_data.json"))
USE_GOOGLE_SEARCH = os.getenv("USE_GOOGLE_SEARCH", "true").lower() in {"1", "true", "yes", "on"}
MAX_HISTORY_MESSAGES = max(2, min(int(os.getenv("MAX_HISTORY_MESSAGES", "20")), 60))
MAX_OUTPUT_TOKENS = max(256, min(int(os.getenv("MAX_OUTPUT_TOKENS", "2048")), 8192))
REQUEST_TIMEOUT = max(10, min(int(os.getenv("REQUEST_TIMEOUT", "45")), 120))
ENABLE_STICKERS = os.getenv("ENABLE_STICKERS", "true").lower() in {"1", "true", "yes", "on"}
VOICE_MODEL = os.getenv("VOICE_MODEL", "en-US-AriaNeural")
VOICE_PITCH = os.getenv("VOICE_PITCH", "+5Hz")
VOICE_RATE = os.getenv("VOICE_RATE", "+0%")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN environment variable")
if not GEMINI_API_KEY:
    raise RuntimeError("Missing GEMINI_API_KEY environment variable")
try:
    OWNER_ID = int(OWNER_ID_RAW)
except ValueError as exc:
    raise RuntimeError("OWNER_ID must be your numeric Telegram user ID") from exc

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("mimi")

client = genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT * 1000))

DEFAULT_WAKE_WORDS = [BOT_NAME.lower(), "baby", "babyy", "babe", "jaan", "jaanu", "cutie", "wifey", "honey"]
DEFAULT_DATA: dict[str, Any] = {
    "sudo_users": [],
    "cute_stickers": [],
    "roast_stickers": [],
    "wake_words": DEFAULT_WAKE_WORDS,
    "histories": {},
    "roast_count": 0,
    "message_count": 0,
}

data_lock = asyncio.Lock()

def load_data() -> dict[str, Any]:
    data = json.loads(json.dumps(DEFAULT_DATA))
    if DATA_FILE.exists():
        try:
            saved = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                data.update(saved)
        except Exception:
            log.exception("Could not read %s; starting with defaults", DATA_FILE)
    # Migrate the old single-user memory list to the new history structure.
    if not isinstance(data.get("histories"), dict):
        data["histories"] = {}
    old_memory = data.pop("memory", None)
    if old_memory and not data["histories"]:
        data["histories"][f"private:{OWNER_ID}"] = [
            {"role": "user" if str(x).startswith("Boss:") else "model", "text": str(x).split(":", 1)[-1].strip()}
            for x in old_memory[-MAX_HISTORY_MESSAGES:]
        ]
    for key, value in DEFAULT_DATA.items():
        data.setdefault(key, json.loads(json.dumps(value)))
    return data


db = load_data()

def save_data() -> None:
    """Atomic JSON save so a restart does not leave a half-written file."""
    try:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = DATA_FILE.with_suffix(DATA_FILE.suffix + ".tmp")
        temp.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(DATA_FILE)
    except Exception:
        log.exception("Failed to save bot data")


def is_authorized(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in db["sudo_users"]


def history_key(update: Update) -> str:
    chat = update.effective_chat
    user = update.effective_user
    return f"{chat.id if chat else 'unknown'}:{user.id if user else 'unknown'}"


def clean_text(value: str, limit: int = 3900) -> str:
    value = (value or "").strip()
    return value[:limit] if value else "I couldn't produce a text response. Please try again."


def wants_web_search(prompt: str) -> bool:
    terms = ("search the web", "search online", "look up", "latest", "today", "current", "news", "recent", "right now", "this week")
    lower = prompt.lower()
    return any(term in lower for term in terms)


async def ask_gemini(prompt: str, history: list[dict[str, str]] | None = None) -> str:
    """Async Gemini request with bounded retries and optional Google Search grounding."""
    history = history or []
    contents: list[types.Content] = []
    for item in history[-MAX_HISTORY_MESSAGES:]:
        role = item.get("role", "user")
        text = item.get("text", "")
        if text:
            contents.append(types.Content(role="model" if role == "model" else "user", parts=[types.Part.from_text(text=text)]))
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=prompt)]))

    config_args: dict[str, Any] = {
        "system_instruction": (
            f"You are {BOT_NAME}, a friendly, playful and helpful Telegram AI companion. "
            "Be natural, kind, respectful and useful. Answer the actual question rather than repeating greetings. "
            "You can help with general knowledge, explanations, writing, brainstorming and programming. "
            "Do not claim to have performed actions you cannot perform. If you are unsure, say so. "
            "Keep replies clear and reasonably concise unless the user asks for detail."
        ),
        "temperature": 0.8,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }
    if USE_GOOGLE_SEARCH and wants_web_search(prompt):
        config_args["tools"] = [types.Tool(google_search=types.GoogleSearch())]

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = await client.aio.models.generate_content(
                model=MODEL_NAME,
                contents=contents,
                config=types.GenerateContentConfig(**config_args),
            )
            answer = (response.text or "").strip()
            if not answer:
                raise RuntimeError("Gemini returned an empty response (possibly blocked by safety filters).")
            return answer
        except Exception as exc:
            last_error = exc
            log.exception("Gemini request failed (attempt %s/3)", attempt + 1)
            if attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Gemini request failed: {type(last_error).__name__}: {last_error}") from last_error


async def send_long_message(update: Update, text: str) -> None:
    """Telegram text messages have a 4096-character limit."""
    text = text or "(empty response)"
    while text:
        chunk = text[:4000]
        if len(text) > 4000:
            split_at = chunk.rfind("\n")
            if split_at > 1000:
                chunk = text[:split_at]
        await update.effective_message.reply_text(chunk, disable_web_page_preview=True)
        text = text[len(chunk):].lstrip()


async def generate_voice_note(text: str, filename: str = "mimi_voice.mp3") -> str:
    safe = re.sub(r"[^\w\s.,?!'\-]", "", text, flags=re.UNICODE).strip()[:1200]
    if not safe:
        safe = "Hello! I'm ready to help."
    tts = edge_tts.Communicate(safe, VOICE_MODEL, pitch=VOICE_PITCH, rate=VOICE_RATE)
    await tts.save(filename)
    return filename


async def ai_reply(update: Update, prompt: str, *, use_history: bool = True) -> str:
    key = history_key(update)
    history = db["histories"].get(key, []) if use_history else []
    try:
        reply = await ask_gemini(prompt, history)
    except Exception as exc:
        log.error("AI reply failed: %s", exc)
        return (
            "I couldn't reach Gemini just now. Please try again in a moment.\n"
            f"Diagnostic: {type(exc).__name__}: {str(exc)[:500]}"
        )
    if use_history:
        history = list(history)
        history.extend([{"role": "user", "text": prompt[-5000:]}, {"role": "model", "text": reply[-5000:]}])
        db["histories"][key] = history[-MAX_HISTORY_MESSAGES:]
        db["message_count"] += 1
        save_data()
    return reply


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_message:
        return
    if not is_authorized(update.effective_user.id):
        await update.effective_message.reply_text("Hi! I'm a private assistant bot. This chat isn't enabled for your account.")
        return
    await update.effective_message.reply_text(
        f"Hi! I'm {BOT_NAME}, your AI assistant. Send me a message here, or use /help to see what I can do."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        f"{BOT_NAME} commands:\n"
        "/ask <question> — ask AI directly\n"
        "/testai — test Gemini connection\n"
        "/status — show bot status\n"
        "/clear — clear your conversation history\n"
        "/voice <text> — create a voice note\n"
        "/hug — friendly virtual hug\n"
        "/kiss — friendly greeting (non-romantic)\n"
        "/roast — reply to someone's message with a light roast\n"
        "/addwake <word> and /rmwake <word> — owner only\n"
        "/addcute and /addroast — save a replied-to sticker, owner only\n"
        "/addsudo and /rmsudo — manage VIP users, owner only\n"
        "In private chat, authorized users can talk normally. In groups, call me by a wake word, tag me, or reply to me."
    )
    await update.effective_message.reply_text(msg)


async def testai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_authorized(update.effective_user.id):
        return
    await update.effective_message.reply_text("Testing Gemini connection…")
    try:
        answer = await ask_gemini("Reply with exactly: Gemini connection is working.")
        await update.effective_message.reply_text(f"✅ Gemini is responding.\nModel: {MODEL_NAME}\nReply: {answer[:500]}")
    except Exception as exc:
        await update.effective_message.reply_text(
            f"❌ Gemini test failed.\nError: {type(exc).__name__}: {str(exc)[:1200]}\n"
            "Check GEMINI_API_KEY, model access, API quota and Render logs."
        )


async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_authorized(update.effective_user.id):
        return
    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.effective_message.reply_text("Usage: /ask explain how Python decorators work")
        return
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    answer = await ai_reply(update, prompt)
    await send_long_message(update, answer)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_authorized(update.effective_user.id):
        return
    key = history_key(update)
    history_count = len(db["histories"].get(key, []))
    status = (
        f"{BOT_NAME} status\n"
        f"Model: {MODEL_NAME}\n"
        f"Google Search grounding: {'enabled' if USE_GOOGLE_SEARCH else 'disabled'}\n"
        f"Your saved history messages: {history_count}\n"
        f"Authorized VIP users: {len(db['sudo_users'])}\n"
        f"Saved stickers: {len(db['cute_stickers'])} cute / {len(db['roast_stickers'])} roast\n"
        f"AI replies completed: {db['message_count']}\n"
        f"Roast count: {db['roast_count']}"
    )
    await update.effective_message.reply_text(status)


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_authorized(update.effective_user.id):
        return
    db["histories"].pop(history_key(update), None)
    save_data()
    await update.effective_message.reply_text("Your conversation history has been cleared.")


async def hug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and is_authorized(update.effective_user.id):
        await update.effective_message.reply_text("Sending a friendly virtual hug! 🫂✨")
    else:
        await update.effective_message.reply_text("Friendly virtual high-five! ✋")


async def kiss_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("A friendly hello and a cheerful wave! 👋✨")


async def voice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_authorized(update.effective_user.id):
        return
    text = " ".join(context.args).strip() or "Hello! I'm ready to chat and help you." 
    try:
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.RECORD_VOICE)
        path = await generate_voice_note(text)
        ogg_path = "mimi_voice.ogg"
        try:
            # Telegram voice messages require OGG/Opus. If ffmpeg is present,
            # convert the Edge TTS MP3; otherwise send a normal audio attachment.
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg:
                subprocess.run(
                    [ffmpeg, "-y", "-i", path, "-c:a", "libopus", "-b:a", "32k", ogg_path],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                with open(ogg_path, "rb") as audio:
                    await update.effective_message.reply_voice(voice=audio, caption="Voice note 🎧")
            else:
                with open(path, "rb") as audio:
                    await update.effective_message.reply_audio(audio=audio, caption="Audio message 🎧")
        finally:
            for temp_path in (path, ogg_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
    except Exception as exc:
        log.exception("Voice generation failed")
        await update.effective_message.reply_text(f"Voice generation failed: {type(exc).__name__}: {str(exc)[:500]}")


async def roast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not is_authorized(user.id):
        return
    if not msg.reply_to_message:
        await msg.reply_text("Reply to someone's message with /roast.")
        return
    target = msg.reply_to_message.from_user
    if target and target.id == OWNER_ID:
        await msg.reply_text("I won't roast the bot owner. 🙂")
        return
    target_name = target.first_name if target else "there"
    quoted = msg.reply_to_message.text or msg.reply_to_message.caption or "(no text)"
    prompt = (
        f"Write a playful, non-hateful, group-chat-friendly roast about {target_name}. "
        f"Their message was: {quoted[:700]!r}. Keep it light, no slurs, threats or personal sensitive traits."
    )
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    answer = await ai_reply(update, prompt, use_history=False)
    db["roast_count"] += 1
    save_data()
    await msg.reply_to_message.reply_text(answer[:3900])


async def add_wake_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /addwake <word>")
        return
    word = context.args[0].lower().strip()
    if word not in db["wake_words"]:
        db["wake_words"].append(word)
        save_data()
    await update.effective_message.reply_text(f"Wake word added: {word}")


async def rm_wake_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /rmwake <word>")
        return
    word = context.args[0].lower().strip()
    if word in db["wake_words"]:
        db["wake_words"].remove(word)
        save_data()
    await update.effective_message.reply_text(f"Wake word removed if it existed: {word}")


async def save_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str) -> None:
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    replied = update.effective_message.reply_to_message
    if not replied or not replied.sticker:
        await update.effective_message.reply_text(f"Reply to a sticker with /add{kind}.")
        return
    key = "cute_stickers" if kind == "cute" else "roast_stickers"
    file_id = replied.sticker.file_id
    if file_id not in db[key]:
        db[key].append(file_id)
        save_data()
    await update.effective_message.reply_text(f"Saved {kind} sticker.")


async def add_cute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_sticker(update, context, "cute")


async def add_roast_sticker_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_sticker(update, context, "roast")


async def add_sudo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    replied = update.effective_message.reply_to_message
    if not replied or not replied.from_user:
        await update.effective_message.reply_text("Reply to a user's message with /addsudo.")
        return
    uid = replied.from_user.id
    if uid not in db["sudo_users"] and uid != OWNER_ID:
        db["sudo_users"].append(uid)
        save_data()
    await update.effective_message.reply_text(f"VIP access granted to {replied.from_user.first_name}.")


async def rm_sudo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    replied = update.effective_message.reply_to_message
    if not replied or not replied.from_user:
        await update.effective_message.reply_text("Reply to a user's message with /rmsudo.")
        return
    uid = replied.from_user.id
    if uid in db["sudo_users"]:
        db["sudo_users"].remove(uid)
        save_data()
    await update.effective_message.reply_text(f"VIP access removed from {replied.from_user.first_name} if they had it.")


# ---------------------------------------------------------------------------
# Main message handler
# ---------------------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not msg.text or not user or not chat:
        return
    text = msg.text.strip()
    if not text:
        return

    # In private chats, authorized users can chat without a wake word.
    is_private = chat.type == ChatType.PRIVATE
    is_reply_to_bot = bool(msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == context.bot.id)
    bot_username = (context.bot.username or "").lower()
    is_tagged = bool(bot_username and f"@{bot_username}" in text.lower())
    words = set(re.findall(r"\b\w+\b", text.lower()))

    if user.id == OWNER_ID:
        wake = is_private or is_reply_to_bot or is_tagged or bool(words.intersection(set(db["wake_words"])))
    elif user.id in db["sudo_users"]:
        wake = is_private or is_reply_to_bot or is_tagged or BOT_NAME.lower() in words
    else:
        # Non-authorized users only get a response when explicitly calling the bot in a group.
        wake = (not is_private) and (is_reply_to_bot or is_tagged or BOT_NAME.lower() in words)
        if wake:
            await msg.reply_text("I'm currently configured for the owner and approved VIP users only.")
            return

    if not wake or not is_authorized(user.id):
        return

    if user.id == OWNER_ID:
        try:
            await msg.set_reaction(reaction=[ReactionTypeEmoji(random.choice(["❤️", "✨", "👍"]))])
        except Exception:
            pass

    await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
    prompt = text
    if chat.type != ChatType.PRIVATE and bot_username:
        prompt = re.sub(rf"@{re.escape(bot_username)}\b", "", prompt, flags=re.IGNORECASE).strip() or text

    reply = await ai_reply(update, prompt)
    await send_long_message(update, reply)

    if ENABLE_STICKERS:
        pool = db["cute_stickers"] if user.id == OWNER_ID or user.id in db["sudo_users"] else db["roast_stickers"]
        if pool and random.random() < 0.20:
            try:
                await msg.reply_sticker(random.choice(pool))
            except Exception:
                log.exception("Could not send saved sticker")


# ---------------------------------------------------------------------------
# Health check and error handling
# ---------------------------------------------------------------------------
class HealthCheck(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = f"{BOT_NAME} is running".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def start_health_server() -> None:
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthCheck)
    log.info("Health server listening on port %s", port)
    server.serve_forever()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, Conflict):
        log.error("Telegram polling conflict: another bot instance is polling. Stop duplicate deployments/processes.")
    elif isinstance(err, RetryAfter):
        log.warning("Telegram rate limit; retry after %s seconds", err.retry_after)
    elif isinstance(err, (NetworkError, TimedOut)):
        log.warning("Temporary Telegram network issue: %s", err)
    else:
        if isinstance(err, BaseException):
            log.error("Unhandled Telegram error", exc_info=(type(err), err, err.__traceback__))
        else:
            log.error("Unhandled Telegram error: %r", err)


def main() -> None:
    threading.Thread(target=start_health_server, daemon=True, name="health-server").start()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(8).build()
    app.add_error_handler(error_handler)

    commands = [
        ("start", start_cmd), ("help", help_cmd), ("testai", testai_cmd),
        ("ask", ask_cmd), ("status", status_cmd), ("clear", clear_cmd),
        ("hug", hug_cmd), ("kiss", kiss_cmd), ("voice", voice_cmd),
        ("roast", roast_cmd), ("addwake", add_wake_cmd), ("rmwake", rm_wake_cmd),
        ("addcute", add_cute_cmd), ("addroast", add_roast_sticker_cmd),
        ("addsudo", add_sudo_cmd), ("rmsudo", rm_sudo_cmd),
    ]
    for name, callback in commands:
        app.add_handler(CommandHandler(name, callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("%s starting with model %s", BOT_NAME, MODEL_NAME)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
