
import asyncio
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(message)s",
    level=logging.INFO
)

logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

# Gemini API setup
client = OpenAI(
    api_key=os.getenv("GEMINI_API_KEY"),
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
)

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

SYSTEM_PROMPT = (
    "You are a friendly study buddy for software students. "
    "Keep answers short."
)

HISTORY_FILE = Path("telegram_histories.json")
MAX_MESSAGES = 20

# Typing indicator settings
TYPING_INTERVAL = 4.0
MIN_TYPING_SECONDS = 3.0

# Telegram message limit
TG_LIMIT = 4096

# Estimated API prices (USD per 1M tokens)
PRICE_IN_PER_M = 0.75
PRICE_OUT_PER_M = 3.75


# Load saved chat histories
def load_all():
    if HISTORY_FILE.exists():
        try:
            return json.loads(
                HISTORY_FILE.read_text(encoding="utf-8")
            )
        except json.JSONDecodeError:
            log.warning(
                "History file is corrupt. Starting fresh."
            )
    return {}


# Save chat histories
def save_all(histories):
    HISTORY_FILE.write_text(
        json.dumps(
            histories,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )


histories = load_all()


# Send user message to Gemini and get response
def ask_llm(chat_id, user_text):
    key = str(chat_id)

    history = list(
        histories.get(
            key,
            [{"role": "system", "content": SYSTEM_PROMPT}]
        )
    )

    # 1. Append user message
    history.append({
        "role": "user",
        "content": user_text
    })

    # 2. Keep system prompt and last N messages
    history = [history[0]] + history[1:][-MAX_MESSAGES:]

    # 3. Call Gemini API
    response = client.chat.completions.create(
        model=MODEL,
        messages=history
    )

    reply = response.choices[0].message.content or (
        "Sorry, I couldn't generate a response."
    )

    # 4. Save assistant reply and history
    history.append({
        "role": "assistant",
        "content": reply
    })

    histories[key] = history
    save_all(histories)

    # 5. Log token usage and estimated cost
    t_in = response.usage.prompt_tokens
    t_out = response.usage.completion_tokens

    cost = (
        t_in * PRICE_IN_PER_M +
        t_out * PRICE_OUT_PER_M
    ) / 1_000_000

    log.info(
        "chat_id=%s | tokens_in=%d | tokens_out=%d | "
        "total_tokens=%d | estimated_cost=$%.8f",
        key,
        t_in,
        t_out,
        t_in + t_out,
        cost
    )

    return reply


# /start command
async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        "Hi! I'm your study buddy. "
        "Ask me anything. Send /reset to start over."
    )


# /reset command
async def reset(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    key = str(update.effective_chat.id)

    histories.pop(key, None)
    save_all(histories)

    await update.message.reply_text(
        "History cleared. Fresh start!"
    )


# Continuously show typing indicator
async def keep_typing(chat, stop: asyncio.Event):
    log.info("Typing task started")

    while not stop.is_set():
        try:
            await chat.send_action(ChatAction.TYPING)
            log.info("Typing action sent successfully")

        except RetryAfter as e:
            log.warning("Typing rate limited: %s", e)
            await asyncio.sleep(float(e.retry_after))

        except (TimedOut, NetworkError) as e:
            log.warning("Typing network error: %s", e)

        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=TYPING_INTERVAL
            )
        except asyncio.TimeoutError:
            continue

    log.info("Typing task stopped")


# Handle every text message
async def on_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    chat = update.effective_chat

    loop = asyncio.get_running_loop()
    started = loop.time()

    stop = asyncio.Event()

    # Start typing indicator
    typing_task = asyncio.create_task(
        keep_typing(chat, stop)
    )

    await asyncio.sleep(0.5)

    try:
        # Run blocking Gemini request in background thread
        reply = await asyncio.to_thread(
            ask_llm,
            chat.id,
            update.message.text
        )

    except Exception as e:
        log.error("LLM error: %s", e)
        reply = (
            "Sorry, something went wrong. Please try again."
        )

    # Keep typing visible for minimum duration
    elapsed = loop.time() - started

    if elapsed < MIN_TYPING_SECONDS:
        await asyncio.sleep(
            MIN_TYPING_SECONDS - elapsed
        )

    # Stop typing indicator
    stop.set()
    await typing_task

    # Split long messages into Telegram-sized messages
    for i in range(0, len(reply), TG_LIMIT):
        await update.message.reply_text(
            reply[i:i + TG_LIMIT]
        )


# Handle errors
async def on_error(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):
    if isinstance(
        context.error,
        (TimedOut, NetworkError)
    ):
        log.warning(
            "Telegram network issue: %s",
            context.error
        )
    else:
        log.error(
            "Unexpected error: %s",
            context.error,
            exc_info=context.error
        )


# Main function
def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not token:
        raise SystemExit(
            "Missing TELEGRAM_BOT_TOKEN in .env"
        )

    if not os.getenv("GEMINI_API_KEY"):
        raise SystemExit(
            "Missing GEMINI_API_KEY in .env"
        )

    app = (
        Application.builder()
        .token(token)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(30)
        .get_updates_write_timeout(30)
        .get_updates_pool_timeout(30)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("reset", reset)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            on_message
        )
    )

    app.add_error_handler(on_error)

    print("Bot is running. Press Ctrl+C to stop.")

    app.run_polling()


if __name__ == "__main__":
    main()
