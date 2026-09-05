import asyncio
import datetime
import json
import random
import re
from pathlib import Path

import openai
from telegram import constants, Update, Message
from telegram.ext import filters, MessageHandler, ApplicationBuilder, ContextTypes, Defaults

import config
from settings import Settings


def save_history(history, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    temporary_path.replace(path)


def load_history(settings):
    path = settings.history_path
    history = {}
    if path.exists() and not settings.clear_chat_history:
        try:
            history = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Chat history is invalid JSON. Restore the file or set CLEAR_CHAT_HISTORY=true to reset it.") from exc
        # Preserve the original fork's history when upgrading a single-chat bot.
        if isinstance(history, list):
            if history and len(settings.target_chat_ids) != 1:
                raise ValueError("Legacy history has no chat IDs. Use one TARGET_CHAT_ID to migrate it, or CLEAR_CHAT_HISTORY=true to reset it.")
            history = {str(settings.target_chat_ids[0]): history}
        if not isinstance(history, dict) or any(
            not isinstance(messages, list) or any(not isinstance(message, str) for message in messages)
            for messages in history.values()
        ):
            raise ValueError("Chat history must map chat IDs to lists of messages. Set CLEAR_CHAT_HISTORY=true to reset it.")
    history = {chat_id: messages[-config.CHAT_HISTORY_LENGTH:] for chat_id, messages in history.items()}
    for chat_id in settings.target_chat_ids:
        history.setdefault(str(chat_id), [])
    save_history(history, path)
    return history


def log_message(history, path, chat_id, message_sender, message_timestamp, message_text, reply: Message = None):
    reply_to = ""
    if reply and reply.from_user:
        reply_to = f"[Replying to {reply.from_user.first_name}'s message sent at {reply.date.strftime(config.DATE_FORMAT)}]"
    message = f"{message_sender}, [{message_timestamp}]{reply_to}\n{message_text}"
    chat_history = history.setdefault(str(chat_id), [])
    chat_history.append(message)
    del chat_history[:-config.CHAT_HISTORY_LENGTH]
    save_history(history, path)


async def openai_request(system: str, prompt: str, temperature: float = 1, max_tokens: int = 200):
    # The repository's lockfile uses OpenAI SDK 0.27.8.
    for attempt in range(config.MAX_RETRIES):
        try:
            completion = await openai.ChatCompletion.acreate(
                model="gpt-4o-mini",
                temperature=temperature,
                max_tokens=max_tokens,
                request_timeout=30,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            )
            return completion.choices[0].message.content
        except (openai.error.ServiceUnavailableError, openai.error.APIError,
                openai.error.APIConnectionError, openai.error.Timeout,
                openai.error.RateLimitError) as exc:
            print(f"OPENAI {type(exc).__name__}: attempt {attempt + 1}/{config.MAX_RETRIES}")
            if attempt + 1 < config.MAX_RETRIES:
                await asyncio.sleep(config.RETRY_DELAY)
        except openai.error.OpenAIError as exc:
            print(f"OPENAI {type(exc).__name__}: check the API token, model access, and account quota.")
            return None
    return None


def clean_response(response, bot_name):
    response = response.strip()
    if len(response) >= 2 and response[0] == response[-1] and response[0] in "\"'":
        response = response[1:-1].strip()
    return re.sub(rf"^(?:You|RoastGPT|NUS Wordle Bot|{re.escape(bot_name)}):\s*", "", response, flags=re.IGNORECASE).strip()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.from_user or message.from_user.is_bot:
        return
    settings = context.bot_data["settings"]
    if message.chat_id not in settings.target_chat_ids:
        return
    username = (message.from_user.username or "").lower()
    # Apply the blacklist before every trigger, including the fuzzy filter.
    if username in settings.blacklist_usernames:
        return

    sender = message.from_user.first_name
    timestamp = message.date.strftime(config.DATE_FORMAT)
    text = message.text_html if message.text else (message.sticker.emoji or "[sticker]") if message.sticker else ""
    if not text:
        return
    text = re.sub(r'(<span class="tg-spoiler">|<tg-spoiler>).*?(</span>|</tg-spoiler>)', '[SPOILER REDACTED]', text, flags=re.DOTALL)
    reply = message.reply_to_message
    reply_sender = reply.from_user.first_name if reply and reply.from_user else None
    history = context.bot_data["chat_history"]
    log_message(history, settings.history_path, message.chat_id, sender, timestamp, text, reply)

    direct_reply = bool(reply and reply.from_user and reply.from_user.id == context.bot.id)
    mentioned = bool(context.bot.username and re.search(rf"(?<!\w)@{re.escape(context.bot.username)}(?!\w)", text, re.IGNORECASE))
    legacy_trigger = "wordle bot" in text.lower() and len(text) > 15
    if not (direct_reply or mentioned or legacy_trigger):
        eligible = username in settings.whitelist_usernames or (
            config.FUZZY_USER_FILTER and random.randint(1, 100) <= config.FUZZY_PROBABILITY
        )
        if not eligible:
            return
        sentiment = await openai_request(config.SYSTEM_ROLE_CHECK, config.generate_check_prompt(text), temperature=0.2, max_tokens=3)
        # Reject malformed scores instead of turning e.g. '7/10' into 710.
        if not sentiment or not re.fullmatch(r"(?:10|[0-9])", sentiment.strip()):
            print("NO VALID ROAST SCORE, SKIPPING...")
            return
        if int(sentiment.strip()) < config.QUALIFICATION_THRESHOLD:
            return

    thread = "\n\n".join(history[str(message.chat_id)])
    prompt = config.generate_main_prompt(sender, thread, reply_sender)
    await context.bot.send_chat_action(chat_id=message.chat_id, action=constants.ChatAction.TYPING)
    response = await openai_request(config.SYSTEM_ROLE_MAIN, prompt)
    if not response:
        print("FAILED TO GET RESPONSE, SKIPPING...")
        return
    response = clean_response(response, context.bot.first_name)
    if not response:
        return
    # Model output may contain '<', '&', or malformed markup. Send it as plain text.
    await context.bot.send_message(chat_id=message.chat_id, reply_to_message_id=message.message_id, text=response, parse_mode=None)
    log_message(history, settings.history_path, message.chat_id, "You", datetime.datetime.now(datetime.timezone.utc).strftime(config.DATE_FORMAT), response, message)


def main():
    try:
        settings = Settings.from_env()
        history = load_history(settings)
    except (ValueError, OSError) as exc:
        raise SystemExit(f"Configuration error: {exc}") from None
    openai.api_key = settings.openai_token
    defaults = Defaults(tzinfo=datetime.timezone.utc)
    application = ApplicationBuilder().token(settings.telegram_token).defaults(defaults).build()
    application.bot_data["settings"] = settings
    application.bot_data["chat_history"] = history
    message_handler = MessageHandler(
        filters.Chat(chat_id=settings.target_chat_ids)
        & (filters.TEXT | filters.Sticker.ALL)
        & (~filters.COMMAND)
        & filters.UpdateType.MESSAGE,
        handle_message,
    )
    application.add_handler(message_handler)
    print("BOT STARTED, WAITING FOR NEW MESSAGES...")
    application.run_polling()


if __name__ == "__main__":
    main()
