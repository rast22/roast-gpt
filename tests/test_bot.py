import datetime
from dataclasses import replace
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import Chat, Message, MessageEntity, Update, User
from telegram.ext import Application

import config
import main
from history import append_message, build_context, json_bytes
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

    def test_new_defaults_and_environment_overrides(self):
        settings = make_settings("/tmp/unused")
        self.assertEqual(settings.roast_model, "gpt-5.4")
        self.assertEqual(settings.roast_reasoning_effort, "low")
        self.assertTrue(settings.mention_only)
        self.assertTrue(settings.humanize_roasts)
        self.assertEqual(settings.chat_history_length, 500)
        self.assertEqual(settings.context_max_bytes, 60000)
        changed = make_settings("/tmp/unused", ROAST_MODEL="gpt-4.1", MENTION_ONLY="false", HUMANIZE_ROASTS="false", CHAT_HISTORY_LENGTH="1000", CONTEXT_MAX_BYTES="100000")
        self.assertEqual(changed.roast_model, "gpt-4.1")
        self.assertFalse(changed.mention_only)
        self.assertFalse(changed.humanize_roasts)
        self.assertEqual(changed.chat_history_length, 1000)
        self.assertEqual(changed.context_max_bytes, 100000)

    def test_invalid_context_settings_fail_at_startup(self):
        for name, values in {
            "CHAT_HISTORY_LENGTH": ("0", "-1", "5001", "1.5", ""),
            "CONTEXT_MAX_BYTES": ("0", "200001", "NaN"),
            "MENTION_ONLY": ("sometimes",),
            "HUMANIZE_ROASTS": ("maybe",),
            "ROAST_REASONING_EFFORT": ("very smart", ""),
        }.items():
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, name):
                    make_settings("/tmp/unused", **{name: value})


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
        settings = make_settings(self.path, TARGET_CHAT_ID="-100123,-100456", CHAT_HISTORY_LENGTH="50")
        history = main.load_history(settings)
        for i in range(settings.chat_history_length + 2):
            append_message(history, settings, -100123, str(i))
        append_message(history, settings, -100456, "another chat")
        loaded = main.load_history(settings)
        self.assertEqual(loaded["-100123"], [str(i) for i in range(2, 52)])
        self.assertEqual(loaded["-100456"], ["another chat"])
        self.assertNotIn("another chat", "\n".join(loaded["-100123"]))

    def test_mixed_history_survives_restart_with_author_metadata(self):
        settings = make_settings(self.path)
        history = main.load_history(settings)
        record = {"role": "user", "author": {"name": "Макс", "username": "max", "user_id": 42}, "timestamp": "now", "text": "слова Макса"}
        append_message(history, settings, -100123, "Legacy first-name-only message")
        append_message(history, settings, -100123, record)
        self.assertEqual(main.load_history(settings)["-100123"], ["Legacy first-name-only message", record])

    def test_invalid_structured_history_is_not_overwritten(self):
        self.path.parent.mkdir()
        contents = '{"-100123": [{"text": "missing author"}]}'
        self.path.write_text(contents)
        with self.assertRaisesRegex(ValueError, "lists of messages"):
            main.load_history(make_settings(self.path))
        self.assertEqual(self.path.read_text(), contents)

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


class ContextTests(unittest.TestCase):
    def test_all_500_short_messages_can_reach_context(self):
        messages = [f"@max: message {i}" for i in range(500)]
        self.assertEqual(build_context(messages, config.CONTEXT_MAX_BYTES), messages)

    def test_budget_keeps_newest_contiguous_messages_in_order(self):
        messages = ["очень старое " * 100, "вчера", "сегодня", "сейчас"]
        budget = json_bytes(messages[-2:])
        self.assertEqual(build_context(messages, budget), ["сегодня", "сейчас"])
        self.assertEqual(build_context(messages, budget - 1), ["сейчас"])

    def test_large_russian_message_and_reply_fit_without_mutating_storage(self):
        record = {"role": "user", "author": {"name": "Макс", "username": "max", "user_id": 42}, "timestamp": "now", "text": 'я💩"\\\n' * 10000}
        record["reply_to"] = {**record, "text": "очень длинная цитата " * 1000}
        before = json.dumps(record, ensure_ascii=False)
        context = build_context(["older", record], 4096)
        self.assertLessEqual(json_bytes(context), 4096)
        self.assertEqual(context[0]["author"]["user_id"], 42)
        self.assertTrue(context[0]["text_truncated"])
        self.assertIn("[truncated]", context[0]["text"])
        self.assertEqual(json.loads(json.dumps(context, ensure_ascii=False)), context)
        self.assertEqual(json.dumps(record, ensure_ascii=False), before)

    def test_oversized_legacy_entry_and_empty_history(self):
        context = build_context(["ё💩" * 10000], 4096)
        self.assertEqual(len(context), 1)
        self.assertLessEqual(json_bytes(context), 4096)
        self.assertIn("[truncated]", context[0])
        self.assertEqual(build_context([], 4096), [])


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "history.json"
        self.settings = make_settings(self.path, HUMANIZE_ROASTS="false")
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
        saved = self.context.bot_data["chat_history"]["-100123"][-1]
        self.assertEqual(saved["text"], "Your ego < your bullshit.")
        self.assertEqual(saved["role"], "assistant")
        self.assertEqual(saved["author"]["user_id"], 99)
        self.assertEqual(saved["reply_to"]["author"]["user_id"], 42)

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
        self.context.bot_data["settings"] = replace(self.settings, mention_only=False)
        for score in ("8", " 10\n"):
            with patch.object(main, "openai_request", new=AsyncMock(side_effect=[score, "The comeback."])) as request:
                await main.handle_message(self.update(), self.context)
            self.assertEqual(request.await_count, 2)
            self.assertEqual(request.call_args_list[0].kwargs["max_tokens"], 3)
            self.assertEqual(request.call_args_list[0].kwargs["model"], config.CHECK_MODEL)

    async def test_failed_malformed_and_low_scores_do_not_send(self):
        self.context.bot_data["settings"] = replace(self.settings, mention_only=False)
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

    async def test_mention_only_ignores_whitelist_fuzzy_and_legacy_but_remembers_chat(self):
        with patch.object(config, "FUZZY_USER_FILTER", True), patch.object(main.random, "randint", return_value=1), patch.object(main, "openai_request", new=AsyncMock()) as request:
            await main.handle_message(self.update(), self.context)
            await main.handle_message(self.update(text="wordle bot you are terrible"), self.context)
        request.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(len(self.context.bot_data["chat_history"]["-100123"]), 2)

    async def test_request_target_and_old_context_are_passed_with_author_identity(self):
        self.context.bot_data["chat_history"] = {
            "-100123": [f"@max: old message {i}" for i in range(120)],
            "-100999": ["private other chat"],
        }
        text = "@my_roaster_bot что думаешь о @max?"
        with patch.object(main, "openai_request", new=AsyncMock(return_value="@max обещал.")) as request:
            await main.handle_message(self.update(text=text), self.context)
        prompt = json.loads(request.call_args.args[1])
        self.assertEqual(prompt["request"], text)
        self.assertEqual(prompt["requester"], {"name": "Alice", "username": "Alice", "user_id": 42})
        self.assertEqual(prompt["chat_history"][0], "@max: old message 0")
        self.assertEqual(prompt["chat_history"][-1]["text"], text)
        self.assertEqual(len(prompt["chat_history"]), 121)
        self.assertNotIn("private other chat", request.call_args.args[1])
        self.assertEqual(request.call_args.kwargs["model"], "gpt-5.4")

    async def test_humanizer_receives_draft_and_same_context_and_only_final_is_sent(self):
        self.context.bot_data["settings"] = replace(self.settings, humanize_roasts=True)
        with patch.object(main, "openai_request", new=AsyncMock(side_effect=["New Bot Name: Черновик.", "Панч без воды."])) as request:
            await main.handle_message(self.update(replying_to=99), self.context)
        self.assertEqual(request.await_count, 2)
        original = json.loads(request.call_args_list[0].args[1])
        editor = json.loads(request.call_args_list[1].args[1])
        self.assertEqual(request.call_args_list[1].args[0], config.SYSTEM_ROLE_HUMANIZER)
        self.assertEqual(editor["original_request_and_context"], original)
        self.assertEqual(editor["draft"], "Черновик.")
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.call_args.kwargs["text"], "Панч без воды.")
        self.assertEqual(self.context.bot_data["chat_history"]["-100123"][-1]["text"], "Панч без воды.")

    async def test_failed_or_empty_editor_falls_back_to_draft(self):
        self.context.bot_data["settings"] = replace(self.settings, humanize_roasts=True)
        for edited in (None, "", "New Bot Name: "):
            with patch.object(main, "openai_request", new=AsyncMock(side_effect=["Хорошо, прекращаю.", edited])):
                await main.handle_message(self.update(text="@my_roaster_bot прекрати"), self.context)
            self.assertEqual(self.bot.send_message.call_args.kwargs["text"], "Хорошо, прекращаю.")

    def test_spoiler_redaction_preserves_unicode_offsets_and_plain_text(self):
        # The leading emoji occupies two UTF-16 units, one Python character.
        message = Message(1, datetime.datetime.now(datetime.timezone.utc), Chat(-100123, "group"), from_user=User(42, "Alice", False), text="😀 secret < & public", entities=[MessageEntity("spoiler", 3, 6)])
        self.assertEqual(main.message_record(message)["text"], "😀 [SPOILER REDACTED] < & public")


class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_errors_retry_then_return_content(self):
        completion = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="roast"), finish_reason="stop")])
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

    async def test_model_parameters_work_for_stronger_and_legacy_models(self):
        completion = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="roast"), finish_reason="stop")])
        for model in ("gpt-5.4", "gpt-5.4-mini", "gpt-4.1", "gpt-4o-mini"):
            with patch.object(main.openai.ChatCompletion, "acreate", new=AsyncMock(return_value=completion)) as request:
                await main.openai_request("system", "prompt", temperature=0.2, max_tokens=50, model=model)
            params = request.call_args.kwargs
            self.assertEqual(params["model"], model)
            self.assertEqual(params["max_completion_tokens"], 50)
            self.assertNotIn("max_tokens", params)
            if model.startswith("gpt-5.4"):
                self.assertEqual(params["reasoning_effort"], "low")
                self.assertNotIn("temperature", params)
            else:
                self.assertEqual(params["temperature"], 0.2)
                self.assertNotIn("reasoning_effort", params)

    def test_reasoning_budget_can_be_disabled(self):
        self.assertEqual(main.completion_parameters("gpt-5.4")["max_completion_tokens"], 1600)
        params = main.completion_parameters("gpt-5.4", reasoning_effort="none")
        self.assertEqual(params["max_completion_tokens"], 320)
        self.assertEqual(params["reasoning_effort"], "none")
        self.assertEqual(main.completion_parameters("gpt-4o-mini")["max_completion_tokens"], 320)

    async def test_incomplete_output_is_not_sent_as_a_broken_joke(self):
        for reason in ("length", "content_filter"):
            completion = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="half a sentence"), finish_reason=reason)])
            with patch.object(main.openai.ChatCompletion, "acreate", new=AsyncMock(return_value=completion)):
                self.assertIsNone(await main.openai_request("system", "prompt"))


if __name__ == "__main__":
    unittest.main()
