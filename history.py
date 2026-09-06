"""Per-chat history with attributed messages and a bounded model context."""

from copy import deepcopy
import json
from pathlib import Path


def save_history(history, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    temporary_path.replace(path)


def valid_record(record):
    if isinstance(record, str):
        return True  # Old histories have no reliable Telegram identity metadata.
    if not isinstance(record, dict):
        return False
    author = record.get("author")
    if not (
        record.get("role") in ("user", "assistant")
        and isinstance(record.get("text"), str)
        and isinstance(record.get("timestamp"), str)
        and isinstance(author, dict)
        and isinstance(author.get("name"), str)
        and (author.get("username") is None or isinstance(author["username"], str))
        and (author.get("user_id") is None or type(author["user_id"]) is int)
    ):
        return False
    reply = record.get("reply_to")
    return reply is None or (
        isinstance(reply, dict) and "reply_to" not in reply and valid_record(reply)
    )


def load_history(settings):
    path = settings.history_path
    history = {}
    if path.exists() and not settings.clear_chat_history:
        try:
            history = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Chat history is invalid JSON. Restore the file or set CLEAR_CHAT_HISTORY=true to reset it.") from exc
        if isinstance(history, list):
            if history and len(settings.target_chat_ids) != 1:
                raise ValueError("Legacy history has no chat IDs. Use one TARGET_CHAT_ID to migrate it, or CLEAR_CHAT_HISTORY=true to reset it.")
            history = {str(settings.target_chat_ids[0]): history}
        if not isinstance(history, dict) or any(
            not isinstance(messages, list) or not all(valid_record(message) for message in messages)
            for messages in history.values()
        ):
            raise ValueError("Chat history must map chat IDs to lists of messages. Restore the file or set CLEAR_CHAT_HISTORY=true to reset it.")
    history = {chat_id: messages[-settings.chat_history_length:] for chat_id, messages in history.items()}
    for chat_id in settings.target_chat_ids:
        history.setdefault(str(chat_id), [])
    save_history(history, path)
    return history


def append_message(history, settings, chat_id, record):
    chat_history = history.setdefault(str(chat_id), [])
    chat_history.append(record)
    del chat_history[:-settings.chat_history_length]
    save_history(history, settings.history_path)


def json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _fit_latest(record, max_bytes):
    """Keep a readable prefix of an oversized last message without invalid JSON."""
    record = deepcopy(record)
    marker = " … [truncated]"
    if isinstance(record, dict):
        # Replies can themselves contain a full Telegram message.
        if record.get("reply_to"):
            record["reply_to"]["text"] = "[omitted to fit context]"
        original = record["text"]
        record["text_truncated"] = True
    else:
        original = record
    low, high = 0, len(original)
    best = None
    while low <= high:
        length = (low + high) // 2
        shortened = original[:length] + marker
        if isinstance(record, dict):
            record["text"] = shortened
            candidate = record
        else:
            candidate = shortened
        if json_bytes([candidate]) <= max_bytes:
            best = deepcopy(candidate)
            low = length + 1
        else:
            high = length - 1
    return best


def build_context(messages, max_bytes):
    """Newest contiguous messages, chronological, measured as UTF-8 JSON bytes.

    The budget covers chat_history, not the system prompt or the actual request.
    Stored history is never shortened by this operation.
    """
    selected = []
    size = 2  # []
    for record in reversed(messages):
        added_size = json_bytes(record) + (2 if selected else 0)  # comma + space
        if size + added_size > max_bytes:
            if not selected:
                truncated = _fit_latest(record, max_bytes)
                if truncated is not None:
                    selected.append(truncated)
            break
        selected.append(record)
        size += added_size
    return list(reversed(selected))
