"""Parse deployment variables without evaluating Python or silently opening access."""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


def _list_value(value, name):
    value = (value or "").strip()
    # Accept quotes pasted around an entire Railway or Docker variable value.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        if value.startswith("[") or value.endswith("]"):
            if not (value.startswith("[") and value.endswith("]")):
                raise ValueError(f"{name} has mismatched list brackets.") from None
            value = value[1:-1].strip()
        if not value:
            return []
        parsed = []
        for item in re.split(r"[,\n]", value):
            item = item.strip()
            if len(item) >= 2 and item[0] == item[-1] and item[0] in "\"'":
                item = item[1:-1].strip()
            if not item:
                raise ValueError(f"{name} contains an empty list entry.")
            parsed.append(item)
    return parsed if isinstance(parsed, list) else [parsed]


def parse_usernames(value, name):
    usernames = set()
    for item in _list_value(value, name):
        if not isinstance(item, str):
            raise ValueError(f'{name} must contain usernames, e.g. ["alice", "bob"] or alice,bob.')
        username = item.strip().removeprefix("@").lower()
        if not re.fullmatch(r"[a-z0-9_]{1,32}", username):
            raise ValueError(f'{name} must contain usernames, e.g. ["alice", "bob"] or alice,bob.')
        usernames.add(username)
    return frozenset(usernames)


def parse_chat_ids(value):
    ids = []
    for item in _list_value(value, "TARGET_CHAT_ID"):
        if isinstance(item, bool) or not isinstance(item, (str, int)) or not re.fullmatch(r"-?\d+", str(item).strip()):
            raise ValueError("TARGET_CHAT_ID must contain integer chat IDs, e.g. -100987654321 or [-100987654321].")
        chat_id = int(item)
        if chat_id == 0:
            raise ValueError("TARGET_CHAT_ID cannot contain 0.")
        if chat_id not in ids:
            ids.append(chat_id)
    if not ids:
        raise ValueError("TARGET_CHAT_ID is required, e.g. -100987654321 or [-100987654321].")
    return tuple(ids)


def parse_bool(value, name="CLEAR_CHAT_HISTORY"):
    value = (value or "").strip().lower()
    if value in ("", "false", "0", "no", "off"):
        return False
    if value in ("true", "1", "yes", "on"):
        return True
    raise ValueError(f"{name} must be true or false (also accepts 1/0, yes/no, on/off).")


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    openai_token: str
    whitelist_usernames: frozenset[str]
    blacklist_usernames: frozenset[str]
    target_chat_ids: tuple[int, ...]
    clear_chat_history: bool
    history_path: Path

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        telegram_token = env.get("TELEGRAM_TOKEN", "").strip()
        openai_token = (env.get("OPENAI_TOKEN") or env.get("OPENAI_API_KEY") or "").strip()
        if not telegram_token:
            raise ValueError("TELEGRAM_TOKEN is required.")
        if not openai_token:
            raise ValueError("OPENAI_TOKEN (or OPENAI_API_KEY) is required.")
        return cls(
            telegram_token=telegram_token,
            openai_token=openai_token,
            whitelist_usernames=parse_usernames(env.get("WHITELIST_USERNAMES"), "WHITELIST_USERNAMES"),
            blacklist_usernames=parse_usernames(env.get("BLACKLIST_USERNAMES"), "BLACKLIST_USERNAMES"),
            target_chat_ids=parse_chat_ids(env.get("TARGET_CHAT_ID")),
            clear_chat_history=parse_bool(env.get("CLEAR_CHAT_HISTORY")),
            history_path=Path(env.get("CHAT_HISTORY_PATH") or "/data/chat_history.json"),
        )
