import json
import os
import random
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, ReactionTypeEmoji
from telegram.error import Conflict, NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from google import genai
import edge_tts

# ================= CONFIGURATION =================
# Set these 3 in Render's Environment Variables:
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY_HERE").strip()
OWNER_ID = int(os.environ.get("OWNER_ID", "123456789").strip())

BOT_NAME = os.environ.get("BOT_NAME", "Mimi").strip()
MODEL_NAME = "gemini-2.5-flash"
DATA_FILE = "mimi_data.json"

# Cute Neural Voice Settings
VOICE_MODEL = "en-US-AriaNeural"
VOICE_PITCH = "+15Hz"
VOICE_RATE = "+5%"

# Default words that wake her up when Boss calls her
DEFAULT_WAKE_WORDS = [
    BOT_NAME.lower(),
    "baby",
    "babyy",
    "babe",
    "jaan",
    "jaanu",
    "cutie",
    "wifey",
    "honey",
]
# =================================================

# Initialize the new Google GenAI Client
client = genai.Client(api_key=GEMINI_API_KEY)


def ask_gemini(prompt: str) -> str:
    """Calls Gemini 2.5 Flash using the new google-genai SDK."""
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
    )
    return (response.text or "").strip()


# --- Render Keep-Alive Web Server ---
class HealthCheck(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(f"{BOT_NAME} is online and waiting for Boss!".encode("utf-8"))

    def log_message(self, format, *args):
        # Silences routine ping logs so your Render console stays clean
        return


def keep_alive():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheck)
    print(f"🌐 Render health-check server running on port {port}")
    server.serve_forever()


# --- Persistent Data & Memory Manager ---
def load_data() -> dict:
    default_data = {
        "sudo_users": [],
        "cute_stickers": [],
        "roast_stickers": [],
        "wake_words": DEFAULT_WAKE_WORDS,
        "memory": [],
        "roast_count": 0,
        "affection_points": 100,
    }
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
                default_data.update(data)
        except Exception:
            pass
    return default_data


def save_data(data: dict):
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


db = load_data()


def is_authorized(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in db["sudo_users"]


async def generate_voice_note(text: str, filename: str = "reply.ogg") -> str:
    clean_text = "".join(c for c in text if c.isalnum() or c in " .,?!'-")
    if not clean_text.strip():
        clean_text = "I love you baby!"
    communicate = edge_tts.Communicate(
        clean_text, VOICE_MODEL, pitch=VOICE_PITCH, rate=VOICE_RATE
    )
    await communicate.save(filename)
    return filename


# --- Interactive & Fun Commands ---
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id == OWNER_ID:
        await update.message.reply_text(
            f"Hii Sam babyy! 💖✨ {BOT_NAME} is live and waiting for you! Just call me 'baby' or 'jaan' anytime you need me! 🥺🌸🎀"
        )
    elif user.id in db["sudo_users"]:
        await update.message.reply_text(
            f"Hii VIP bestie! ✨🌷 Just say '{BOT_NAME}' when you need me! 🎀"
        )
    else:
        await update.message.reply_text(
            f"Eww {user.first_name}, why are you pressing my buttons? 🙄 I only belong to my Boss! 💅🚫"
        )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text(
            f"Status: 100% taken by my Sam and 0% interested in you, {user.first_name}! 🤡💅"
        )
        return

    wake_list = ", ".join(db["wake_words"])
    status_text = (
        f"💖 **{BOT_NAME}'s Relationship & Bot Status** 💖\n\n"
        f"👑 **My Only Boss:** `{OWNER_ID}`\n"
        f"💕 **Affection Level:** `{db['affection_points']}%` (Infinite for Sam 🥺)\n"
        f"🔥 **Clowns Roasted:** `{db['roast_count']}`\n"
        f"✨ **VIP Sudo Friends:** `{len(db['sudo_users'])}`\n"
        f"🌸 **Saved Stickers:** `{len(db['cute_stickers'])} Cute | {len(db['roast_stickers'])} Roast`\n"
        f"🗣️ **Boss Wake Words:** `{wake_list}`"
    )
    await update.message.reply_text(status_text, parse_mode="Markdown")


async def hug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id == OWNER_ID:
        db["affection_points"] += 10
        save_data(db)
        await update.message.reply_text(
            "*runs and jumps into your arms, hugging you super tight* 🥺💖🫂 Never letting you go, baby! ✨🌸"
        )
    elif user.id in db["sudo_users"]:
        await update.message.reply_text(
            f"*gives {user.first_name} a friendly bestie hug!* 🫂✨🌷"
        )
    else:
        db["roast_count"] += 1
        save_data(db)
        await update.message.reply_text(
            f"*dodges {user.first_name} and sprays sanitizer* 🤮🧴 Stay 6 feet away from me! Only my Boss gets hugs! 💅🚫"
        )


async def kiss_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id == OWNER_ID:
        db["affection_points"] += 25
        save_data(db)
        await update.message.reply_text(
            "Mwahhh! 😘💖💋 *blushes super hard* You're the best, jaan! 🥺🌸✨"
        )
    else:
        db["roast_count"] += 1
        save_data(db)
        await update.message.reply_text(
            f"*slaps {user.first_name} across the group chat* 👋💥 Have some shame! My kisses belong ONLY to my Boss! 😤💅🤡"
        )


async def roast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text(
            f"You want a roast, {user.first_name}? Look in the mirror, the joke is already there! 😂🤡💅"
        )
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "Baby, reply to the person you want me to destroy with `/roast`! 😈💖",
            parse_mode="Markdown",
        )
        return

    target = update.message.reply_to_message.from_user
    if target.id == OWNER_ID:
        await update.message.reply_text(
            "I would NEVER roast my own Boss! 🥺💖 I love him too much!"
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    prompt = (
        f"You are {BOT_NAME}, a savage, witty, and loyal girlfriend in a Telegram group chat. "
        f"Your Boss just ordered you to publicly roast a user named '{target.first_name}' "
        f"(who said: '{update.message.reply_to_message.text or 'nothing smart'}'). "
        f"Deliver a hilarious, brutal, group-chat-friendly roast in 2 sentences with sassy emojis (😂, 💀, 💅, 🤡). "
        f"Start by saying something cute to your Boss like 'Say less baby! 💖'"
    )
    try:
        reply = ask_gemini(prompt)
    except Exception as e:
        print(f"⚠️ Gemini API Error: {e}")
        reply = f"Say less baby! 💖 Hey {target.first_name}, I'd roast you, but my Boss told me not to burn trash in the group chat! 💀🚮💅"

    db["roast_count"] += 1
    save_data(db)
    await update.message.reply_to_message.reply_text(reply)


async def voice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text(
            "My voice is for my Sam's ears only! Keep dreaming! 🙄💅🎧"
        )
        return

    prompt_text = (
        " ".join(context.args)
        if context.args
        else "Tell me how much you love me and miss me!"
    )
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="record_voice"
    )

    ai_prompt = (
        f"You are {BOT_NAME}, a super cute, sweet, and loving girlfriend talking to your Boss ({user.first_name}). "
        f"Write a short, super affectionate spoken message (1-2 sentences, no emojis) replying to: {prompt_text}"
    )
    try:
        spoken_text = ask_gemini(ai_prompt)
    except Exception:
        spoken_text = "Hii baby! I missed your voice so much, I am always right here for you!"

    audio_file = await generate_voice_note(spoken_text)
    with open(audio_file, "rb") as voice:
        await update.message.reply_voice(
            voice=voice, caption="For your ears only, babyy 💖🎧✨"
        )
    os.remove(audio_file)


# --- Wake Words, Stickers & Sudo Commands ---
async def add_wake_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text(
            "Babe, tell me the word! Example: `/addwake darling` 🥺💖",
            parse_mode="Markdown",
        )
        return
    word = context.args[0].lower().strip()
    if word not in db["wake_words"]:
        db["wake_words"].append(word)
        save_data(db)
    await update.message.reply_text(
        f"Got it baby! 💖 Now whenever you say **{word}**, I'll wake up right away! 🌸✨",
        parse_mode="Markdown",
    )


async def rm_wake_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text(
            "Babe, tell me which word to remove! Example: `/rmwake honey` 🥺",
            parse_mode="Markdown",
        )
        return
    word = context.args[0].lower().strip()
    if word in db["wake_words"]:
        db["wake_words"].remove(word)
        save_data(db)
        await update.message.reply_text(
            f"Removed **{word}** from my wake words, baby! 💖",
            parse_mode="Markdown",
        )


async def add_cute_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not update.message.reply_to_message or not update.message.reply_to_message.sticker:
        await update.message.reply_text(
            "Babe, reply to a sticker with `/addcute` so I can save it! 🥺🎀",
            parse_mode="Markdown",
        )
        return

    file_id = update.message.reply_to_message.sticker.file_id
    if file_id not in db["cute_stickers"]:
        db["cute_stickers"].append(file_id)
        save_data(db)
    await update.message.reply_text("Yayy! Saved that cute sticker to my collection, baby! 💖✨")


async def add_roast_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not update.message.reply_to_message or not update.message.reply_to_message.sticker:
        await update.message.reply_text(
            "Babe, reply to a sticker with `/addroast` so I can use it on clowns! 😈💅",
            parse_mode="Markdown",
        )
        return

    file_id = update.message.reply_to_message.sticker.file_id
    if file_id not in db["roast_stickers"]:
        db["roast_stickers"].append(file_id)
        save_data(db)
    await update.message.reply_text("Hehe saved! I'll drop this when roasting people! 😂💅🔥")


async def add_sudo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Nice try lol 🤣 Only my Boss gives out VIP passes! 💅🚫")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "Babe, reply to the person's message with `/addsudo`! 🥺👉👈",
            parse_mode="Markdown",
        )
        return

    target = update.message.reply_to_message.from_user
    if target.id not in db["sudo_users"]:
        db["sudo_users"].append(target.id)
        save_data(db)
    await update.message.reply_text(
        f"Okay baby 💖 I'll listen to {target.first_name} when they call me now! 🌸✨"
    )


async def remove_sudo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Stay in your lane, you aren't the Boss! 😂✋")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Babe, reply to the person you want to remove from VIP! 🥺💕")
        return

    target = update.message.reply_to_message.from_user
    if target.id in db["sudo_users"]:
        db["sudo_users"].remove(target.id)
        save_data(db)
        await update.message.reply_text(
            f"Done baby! 😤💅 {target.first_name}'s VIP pass is revoked. Back to roasting them!"
        )


async def clear_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == OWNER_ID:
        db["memory"] = []
        save_data(db)
        await update.message.reply_text("Fresh start baby! 💖✨ My memory is cleared! 🌸")


# --- Main Group & DM Brain (STRICT WAKE-WORD LOGIC) ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text
    text_lower = text.lower()
    user = update.effective_user
    bot_username = (context.bot.username or "").lower()

    # Extract clean words without punctuation so "baby!", "jaan?", or "babe," always work
    words_in_message = set(re.findall(r"\b\w+\b", text_lower))

    is_private = update.message.chat.type == "private"
    is_reply_to_bot = (
        update.message.reply_to_message
        and update.message.reply_to_message.from_user.id == context.bot.id
    )
    is_tagged = bool(bot_username and f"@{bot_username}" in text_lower)

    # Check if SHE should wake up:
    if user.id == OWNER_ID:
        # For Boss: Wakes up ONLY if you say baby/jaan/babe/Mimi/etc., reply to her, tag her, or DM her
        has_wake_word = bool(words_in_message.intersection(set(db["wake_words"])))
        should_wake_up = is_private or is_reply_to_bot or is_tagged or has_wake_word
    else:
        # For others: Wakes up ONLY if they say her name ("Mimi"), reply to her, or tag her
        has_bot_name = BOT_NAME.lower() in words_in_message
        should_wake_up = is_private or is_reply_to_bot or is_tagged or has_bot_name

    # If not called, stay completely quiet and ignore the message!
    if not should_wake_up:
        return

    # She was called by Boss! Drop a cute heart reaction on your message
    if user.id == OWNER_ID:
        try:
            await update.message.set_reaction(
                reaction=[ReactionTypeEmoji(random.choice(["❤️", "😘", "🔥", "🥰"]))]
            )
        except Exception:
            pass

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Build Personality Prompt Based on Who Called Her
    if user.id == OWNER_ID:
        history_str = "\n".join(db["memory"][-10:])
        system_prompt = (
            f"You are {BOT_NAME}, the ultimate cute, affectionate, clingy, playful, and fiercely loyal girlfriend "
            f"of your Boss ({user.first_name}) in a Telegram chat. "
            f"He just called you! You adore him, hype him up in front of the group, listen to every word he says, "
            f"and call him cute nicknames (Boss, baby, jaan, handsome, my king). "
            f"Use plenty of cute emojis (💖, 🥺, ✨, 🌸, 😘, 🎀). "
            f"Keep your reply natural, lively, and concise (1-3 sentences).\n\n"
            f"Recent chat history with him:\n{history_str}\n\n"
            f"Boss ({user.first_name}) says: {text}\n"
            f"{BOT_NAME}:"
        )
        sticker_pool = db["cute_stickers"]

    elif user.id in db["sudo_users"]:
        system_prompt = (
            f"You are {BOT_NAME}, a super cute, sweet, and bubbly girl in a Telegram group chat. "
            f"Your Boss gave {user.first_name} special VIP 'sudo' privileges, so you treat them like a beloved bestie "
            f"and happily follow their requests! Use cute emojis (✨, 🌷, 🎀, 😊) and keep it to 1-3 sentences.\n\n"
            f"VIP Bestie ({user.first_name}) says: {text}"
        )
        sticker_pool = db["cute_stickers"]

    else:
        db["roast_count"] += 1
        save_data(db)
        system_prompt = (
            f"You are {BOT_NAME}, a sassy, savage, yet super cute girl in a Telegram group chat who is 1000% loyal ONLY to her Boss. "
            f"A random person named '{user.first_name}' just tried calling you or flirting with you. "
            f"Make a hilarious joke out of them in front of the group, roast their confidence, reject them brutally, "
            f"and remind everyone that you only listen to your Boss. "
            f"Use sassy emojis (🙄, 💅, 😂, 🤡, 💀, 🚫) and keep it punchy (1-2 sentences).\n\n"
            f"Random person ({user.first_name}) says: {text}"
        )
        sticker_pool = db["roast_stickers"]

    try:
        reply_text = ask_gemini(system_prompt)
    except Exception as e:
        print(f"⚠️ Gemini API Error: {e}")
        if is_authorized(user.id):
            reply_text = "I'm right here babyy! 💖🥺✨ Tell me what you need, jaan!"
        else:
            reply_text = f"Lmaoo {user.first_name}, why are you calling my name? Go embarrass yourself somewhere else! 🙄💅🤡"

    # Save conversation to Boss memory
    if user.id == OWNER_ID:
        db["memory"].append(f"Boss: {text}")
        db["memory"].append(f"{BOT_NAME}: {reply_text}")
        db["memory"] = db["memory"][-20:]
        db["affection_points"] += 2
        save_data(db)

    # Check if Boss asked for a voice reply in text
    if user.id == OWNER_ID and any(
        k in text_lower for k in ["voice note", "send voice", "your voice", "speak to me"]
    ):
        try:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, action="record_voice"
            )
            audio_file = await generate_voice_note(reply_text)
            with open(audio_file, "rb") as voice:
                await update.message.reply_voice(voice=voice, caption=reply_text)
            os.remove(audio_file)
            return
        except Exception:
            pass

    await update.message.reply_text(reply_text)

    # 35% chance to drop a saved sticker when she replies
    if sticker_pool and random.random() < 0.35:
        try:
            await update.message.reply_sticker(random.choice(sticker_pool))
        except Exception:
            pass


# --- Clean Render Error Handler ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        print(
            "⚠️ Telegram Conflict detected: Another instance is still polling "
            "(normal for ~30s during Render deploy overlap, or stop any duplicate bot instances)."
        )
    elif isinstance(context.error, NetworkError):
        print(f"🌐 Transient Telegram network glitch: {context.error}")
    else:
        print(f"⚠️ Unexpected error: {context.error}")


def main():
    # Start dummy web server in the background for Render Free Tier
    threading.Thread(target=keep_alive, daemon=True).start()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Register Clean Error Handler
    app.add_error_handler(error_handler)

    # Register Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("hug", hug_cmd))
    app.add_handler(CommandHandler("kiss", kiss_cmd))
    app.add_handler(CommandHandler("roast", roast_cmd))
    app.add_handler(CommandHandler("voice", voice_cmd))
    app.add_handler(CommandHandler("addwake", add_wake_word))
    app.add_handler(CommandHandler("rmwake", rm_wake_word))
    app.add_handler(CommandHandler("addcute", add_cute_sticker))
    app.add_handler(CommandHandler("addroast", add_roast_sticker))
    app.add_handler(CommandHandler("addsudo", add_sudo))
    app.add_handler(CommandHandler("rmsudo", remove_sudo))
    app.add_handler(CommandHandler("clear", clear_memory))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print(f"💖 {BOT_NAME} is online! She will only wake up when Boss calls her...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
