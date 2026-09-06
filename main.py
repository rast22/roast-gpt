import asyncio
import datetime
import random
import re

import openai
from telegram import constants, Update, Message
from telegram.ext import filters, MessageHandler, ApplicationBuilder, ContextTypes, Defaults

import config
from history import append_message, build_context, load_history
from settings import Settings


def author_info(user):
    if not user:
        return {"name": "Unknown author", "username": None, "user_id": None}
    return {"name": getattr(user, "full_name", user.first_name), "username": user.username, "user_id": user.id}


def visible_text(message: Message):
    text = message.text or message.caption
    if text:
        # Telegram entity offsets count UTF-16 code units, including emoji.
        entities = message.entities if message.text else message.caption_entities
        encoded = text.encode("utf-16-le")
        for entity in sorted(entities or (), key=lambda item: item.offset, reverse=True):
            if entity.type == "spoiler":
                encoded = encoded[:entity.offset * 2] + "[SPOILER REDACTED]".encode("utf-16-le") + encoded[(entity.offset + entity.length) * 2:]
        return encoded.decode("utf-16-le")
    return (message.sticker.emoji or "[sticker]") if message.sticker else "[non-text message]"


def message_record(message: Message, *, include_reply=True):
    record = {
        "role": "assistant" if message.from_user and message.from_user.is_bot else "user",
        "author": author_info(message.from_user),
        "message_id": message.message_id,
        "timestamp": message.date.strftime(config.DATE_FORMAT),
        "text": visible_text(message),
    }
    if include_reply and message.reply_to_message:
        record["reply_to"] = message_record(message.reply_to_message, include_reply=False)
    return record


def completion_parameters(model, temperature=1, max_tokens=None, reasoning_effort=config.ROAST_REASONING_EFFORT):
    reasoning = model.startswith("gpt-5.4")
    if max_tokens is None:
        # This API limit includes both hidden reasoning and the visible reply.
        max_tokens = 1600 if reasoning and reasoning_effort != "none" else 320
    parameters = {"model": model, "max_completion_tokens": max_tokens, "request_timeout": 30}
    if reasoning:
        parameters["reasoning_effort"] = reasoning_effort
    else:
        parameters["temperature"] = temperature
    return parameters


async def openai_request(system: str, prompt: str, temperature: float = 1, max_tokens=None, *, model=None, reasoning_effort=config.ROAST_REASONING_EFFORT):
    # The repository's lockfile uses OpenAI SDK 0.27.8; extra API fields pass through.
    parameters = completion_parameters(model or config.ROAST_MODEL, temperature, max_tokens, reasoning_effort)
    for attempt in range(config.MAX_RETRIES):
        try:
            completion = await openai.ChatCompletion.acreate(
                **parameters,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            )
            choice = completion.choices[0]
            if choice.finish_reason != "stop":
                print(f"OPENAI incomplete response ({choice.finish_reason}), skipping.")
                return None
            return choice.message.content
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


async def generate_roast(prompt, settings, bot_name):
    response = await openai_request(config.SYSTEM_ROLE_MAIN, prompt, model=settings.roast_model, reasoning_effort=settings.roast_reasoning_effort)
    if not response:
        return None
    draft = clean_response(response, bot_name)
    if not draft:
        return None
    if settings.humanize_roasts:
        edited = await openai_request(
            config.SYSTEM_ROLE_HUMANIZER,
            config.generate_humanizer_prompt(prompt, draft),
            model=settings.roast_model,
            reasoning_effort=settings.roast_reasoning_effort,
        )
        if edited:
            return clean_response(edited, bot_name) or draft
    return draft


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

    if not (message.text or message.sticker):
        return
    record = message_record(message)
    text = record["text"]
    reply = message.reply_to_message
    history = context.bot_data["chat_history"]
    append_message(history, settings, message.chat_id, record)

    direct_reply = bool(reply and reply.from_user and reply.from_user.id == context.bot.id)
    mentioned = bool(context.bot.username and re.search(rf"(?<!\w)@{re.escape(context.bot.username)}(?!\w)", text, re.IGNORECASE))
    if settings.mention_only and not (direct_reply or mentioned):
        return
    legacy_trigger = not settings.mention_only and "wordle bot" in text.lower() and len(text) > 15
    if not (direct_reply or mentioned or legacy_trigger):
        eligible = username in settings.whitelist_usernames or (
            config.FUZZY_USER_FILTER and random.randint(1, 100) <= config.FUZZY_PROBABILITY
        )
        if not eligible:
            return
        sentiment = await openai_request(config.SYSTEM_ROLE_CHECK, config.generate_check_prompt(text), temperature=0.2, max_tokens=3, model=config.CHECK_MODEL)
        # Reject malformed scores instead of turning e.g. '7/10' into 710.
        if not sentiment or not re.fullmatch(r"(?:10|[0-9])", sentiment.strip()):
            print("NO VALID ROAST SCORE, SKIPPING...")
            return
        if int(sentiment.strip()) < config.QUALIFICATION_THRESHOLD:
            return

    thread = build_context(history[str(message.chat_id)], settings.context_max_bytes)
    prompt = config.generate_main_prompt(record["author"], thread, record.get("reply_to"), request=text)
    await context.bot.send_chat_action(chat_id=message.chat_id, action=constants.ChatAction.TYPING)
    response = await generate_roast(prompt, settings, context.bot.first_name)
    if not response:
        print("FAILED TO GET RESPONSE, SKIPPING...")
        return
    # Model output may contain '<', '&', or malformed markup. Send it as plain text.
    await context.bot.send_message(chat_id=message.chat_id, reply_to_message_id=message.message_id, text=response, parse_mode=None)
    append_message(history, settings, message.chat_id, {
        "role": "assistant",
        "author": author_info(context.bot),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime(config.DATE_FORMAT),
        "text": response,
        "reply_to": message_record(message, include_reply=False),
    })


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
