import datetime
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import Chat, Message, Update, User
from telegram.ext import Application

import config
import main
from settings import Settings, parse_bool, parse_chat_ids, parse_usernames


def make_settings(path, **overrides):
    env = {
        "TELEGRAM_TOKEN": "12345:test-token",
        "OPENAI_TOKEN": "test-key",
        "TARGET_CHAT_ID": "-100123",
        "WHITELIST_USERNAMES": "alice",
        "CHAT_HISTORY_PATH": str(path),
    }
    env.update(overrides)
    return Settings.from_env(env)


class SettingsTests(unittest.TestCase):
    def test_common_username_formats(self):
        for raw in ('["Alice", "@BOB"]', "['Alice', '@BOB']", "[Alice, @BOB]", "Alice, @BOB", "Alice\n@BOB", "'[\"Alice\", \"@BOB\"]'"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_usernames(raw, "WHITELIST_USERNAMES"), {"alice", "bob"})

    def test_empty_optional_lists(self):
        for raw in (None, "", " ", "[]", "'[]'"):
            self.assertEqual(parse_usernames(raw, "BLACKLIST_USERNAMES"), set())

    def test_single_username(self):
        for raw in ("@Alice", '"@Alice"', "'@Alice'"):
            self.assertEqual(parse_usernames(raw, "WHITELIST_USERNAMES"), {"alice"})

    def test_invalid_usernames_fail_with_variable_name(self):
        for raw in ('[true]', '[123]', 'null', '{}', '[alice', 'alice]', 'alice,,bob', '["alice", null]', 'alice bob', "['alice',]", '[[]]'):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "BLACKLIST_USERNAMES"):
                parse_usernames(raw, "BLACKLIST_USERNAMES")

    def test_chat_id_formats(self):
        for raw in ("-100123", "[-100123]", '["-100123"]', '"-100123"', "[-100123,-100123]"):
            self.assertEqual(parse_chat_ids(raw), (-100123,))
        self.assertEqual(parse_chat_ids("-100123, -100456"), (-100123, -100456))

    def test_invalid_chat_ids_fail_closed(self):
        for raw in (None, "", "[]", "null", "true", "[false]", "[1.5]", "0", "group", "[{}]", "-100123,"):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "TARGET_CHAT_ID"):
                parse_chat_ids(raw)

    def test_booleans_are_not_evaluated(self):
        for raw in ("true", "True", "1", "YES", "on"):
            self.assertTrue(parse_bool(raw))
        for raw in (None, "", "false", "False", "0", "NO", "off"):
            self.assertFalse(parse_bool(raw))
        with self.assertRaisesRegex(ValueError, "CLEAR_CHAT_HISTORY"):
            parse_bool("__import__('os').getcwd()")

    def test_required_settings_and_key_alias(self):
        with self.assertRaisesRegex(ValueError, "TELEGRAM_TOKEN"):
            Settings.from_env({})
        with self.assertRaisesRegex(ValueError, "OPENAI_TOKEN"):
            Settings.from_env({"TELEGRAM_TOKEN": "test"})
        settings = make_settings("/tmp/unused", OPENAI_TOKEN="", OPENAI_API_KEY="alias-key")
        self.assertEqual(settings.openai_token, "alias-key")


class HistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "data" / "chat_history.json"

    def test_first_start_creates_directory_and_history(self):
        self.assertEqual(main.load_history(make_settings(self.path)), {"-100123": []})
        self.assertEqual(json.loads(self.path.read_text()), {"-100123": []})

    def test_legacy_history_migrates_without_loss(self):
        self.path.parent.mkdir()
        self.path.write_text('["old message"]')
        self.assertEqual(main.load_history(make_settings(self.path)), {"-100123": ["old message"]})

    def test_ambiguous_legacy_history_is_preserved(self):
        self.path.parent.mkdir()
        self.path.write_text('["old message"]')
        with self.assertRaisesRegex(ValueError, "Legacy history"):
            main.load_history(make_settings(self.path, TARGET_CHAT_ID="-100123,-100456"))
        self.assertEqual(json.loads(self.path.read_text()), ["old message"])

    def test_corrupt_history_requires_explicit_reset(self):
        self.path.parent.mkdir()
        self.path.write_text("broken")
        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            main.load_history(make_settings(self.path))
        self.assertEqual(self.path.read_text(), "broken")
        self.assertEqual(main.load_history(make_settings(self.path, CLEAR_CHAT_HISTORY="true")), {"-100123": []})

    def test_histories_stay_separate_and_bounded(self):
        settings = make_settings(self.path, TARGET_CHAT_ID="-100123,-100456")
        history = main.load_history(settings)
        for i in range(config.CHAT_HISTORY_LENGTH + 2):
            main.log_message(history, self.path, -100123, "Alice", "now", str(i))
        main.log_message(history, self.path, -100456, "Bob", "now", "another chat")
        loaded = main.load_history(settings)
        self.assertEqual(len(loaded["-100123"]), config.CHAT_HISTORY_LENGTH)
        self.assertEqual(loaded["-100456"], ["Bob, [now]\nanother chat"])
        self.assertNotIn("another chat", "\n".join(loaded["-100123"]))

    def test_startup_builds_application_with_parsed_settings(self):
        settings = make_settings(self.path)
        with patch.object(main.Settings, "from_env", return_value=settings), patch.object(Application, "run_polling", autospec=True) as polling:
            main.main()
        application = polling.call_args.args[0]
        self.assertEqual(application.bot_data["settings"], settings)
        self.assertEqual(application.bot_data["chat_history"], {"-100123": []})

    def test_removing_chat_from_configuration_preserves_saved_history(self):
        self.path.parent.mkdir()
        self.path.write_text('{"-100123": [], "-100456": ["saved"]}')
        main.load_history(make_settings(self.path))
        self.assertEqual(json.loads(self.path.read_text())["-100456"], ["saved"])


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "history.json"
        self.settings = make_settings(self.path)
        self.bot = SimpleNamespace(id=99, username="my_roaster_bot", first_name="New Bot Name", send_chat_action=AsyncMock(), send_message=AsyncMock())
        self.context = SimpleNamespace(bot=self.bot, bot_data={"settings": self.settings, "chat_history": {}})
        fuzzy = patch.object(config, "FUZZY_USER_FILTER", False)
        fuzzy.start()
        self.addCleanup(fuzzy.stop)

    def update(self, text="I am obviously a genius", username="Alice", replying_to=None, chat_id=-100123, is_bot=False):
        user = User(42, "Alice", is_bot, username=username)
        reply = None
        if replying_to is not None:
            reply = Message(1, datetime.datetime.now(datetime.timezone.utc), Chat(chat_id, "group"), from_user=User(replying_to, "New Bot Name", True), text="previous reply")
        return Update(1, Message(2, datetime.datetime.now(datetime.timezone.utc), Chat(chat_id, "group"), from_user=user, text=text, reply_to_message=reply))

    async def test_direct_reply_uses_identity_and_sends_clean_plain_text(self):
        with patch.object(main, "openai_request", new=AsyncMock(return_value='"New Bot Name: Your ego < your bullshit."')) as request:
            await main.handle_message(self.update(replying_to=99, username=None), self.context)
        self.assertEqual(request.await_count, 1)
        self.assertEqual(request.call_args.args[0], config.SYSTEM_ROLE_MAIN)
        self.bot.send_message.assert_awaited_once_with(chat_id=-100123, reply_to_message_id=2, text="Your ego < your bullshit.", parse_mode=None)
        self.assertIn("Your ego < your bullshit.", self.context.bot_data["chat_history"]["-100123"][-1])

    async def test_blacklist_overrides_whitelist_fuzzy_and_direct_triggers(self):
        self.context.bot_data["settings"] = make_settings(self.path, BLACKLIST_USERNAMES="@ALICE")
        with patch.object(config, "FUZZY_USER_FILTER", True), patch.object(main.random, "randint", return_value=1), patch.object(main, "openai_request", new=AsyncMock()) as request:
            for update in (self.update(), self.update(replying_to=99), self.update(text="@my_roaster_bot hey"), self.update(text="wordle bot you are terrible")):
                await main.handle_message(update, self.context)
        request.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    async def test_actual_username_mention_bypasses_scoring(self):
        with patch.object(main, "openai_request", new=AsyncMock(return_value="A cutting reply.")) as request:
            await main.handle_message(self.update(text="@MY_ROASTER_BOT do your worst", username="unlisted"), self.context)
        self.assertEqual(request.await_count, 1)
        self.bot.send_message.assert_awaited_once()

    async def test_similar_username_and_other_bot_reply_do_not_trigger(self):
        with patch.object(main, "openai_request", new=AsyncMock()) as request:
            await main.handle_message(self.update(text="@my_roaster_bot_extra hi", username="unlisted"), self.context)
            await main.handle_message(self.update(replying_to=98, username="unlisted"), self.context)
        request.assert_not_awaited()

    async def test_bot_messages_and_unconfigured_chats_are_ignored(self):
        with patch.object(main, "openai_request", new=AsyncMock()) as request:
            await main.handle_message(self.update(is_bot=True), self.context)
            await main.handle_message(self.update(chat_id=-100999), self.context)
        request.assert_not_awaited()
        self.assertEqual(self.context.bot_data["chat_history"], {})

    async def test_qualified_message_generates_roast(self):
        for score in ("8", " 10\n"):
            with patch.object(main, "openai_request", new=AsyncMock(side_effect=[score, "The comeback."])) as request:
                await main.handle_message(self.update(), self.context)
            self.assertEqual(request.await_count, 2)
            self.assertEqual(request.call_args_list[0].kwargs["max_tokens"], 3)

    async def test_failed_malformed_and_low_scores_do_not_send(self):
        for score in (None, "", "7", "7/10", "score: 10", "11", "-1"):
            with patch.object(main, "openai_request", new=AsyncMock(return_value=score)) as request:
                await main.handle_message(self.update(), self.context)
            self.assertEqual(request.await_count, 1)
        self.bot.send_message.assert_not_awaited()

    async def test_failed_roast_does_not_log_a_bot_reply(self):
        with patch.object(main, "openai_request", new=AsyncMock(return_value=None)):
            await main.handle_message(self.update(replying_to=99), self.context)
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(len(self.context.bot_data["chat_history"]["-100123"]), 1)


class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_errors_retry_then_return_content(self):
        completion = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="roast"))])
        with patch.object(main.openai.ChatCompletion, "acreate", new=AsyncMock(side_effect=[main.openai.error.ServiceUnavailableError("busy"), completion])) as request, patch.object(main.asyncio, "sleep", new=AsyncMock()) as sleep:
            self.assertEqual(await main.openai_request("system", "prompt"), "roast")
        self.assertEqual(request.await_count, 2)
        sleep.assert_awaited_once_with(config.RETRY_DELAY)

    async def test_exhausted_retries_return_none_without_final_sleep(self):
        with patch.object(main.openai.ChatCompletion, "acreate", new=AsyncMock(side_effect=main.openai.error.Timeout("timeout"))) as request, patch.object(main.asyncio, "sleep", new=AsyncMock()) as sleep:
            self.assertIsNone(await main.openai_request("system", "prompt"))
        self.assertEqual(request.await_count, config.MAX_RETRIES)
        self.assertEqual(sleep.await_count, config.MAX_RETRIES - 1)

    async def test_authentication_error_is_not_retried(self):
        with patch.object(main.openai.ChatCompletion, "acreate", new=AsyncMock(side_effect=main.openai.error.AuthenticationError("bad key"))) as request:
            self.assertIsNone(await main.openai_request("system", "prompt"))
        self.assertEqual(request.await_count, 1)


if __name__ == "__main__":
    unittest.main()
