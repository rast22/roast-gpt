# RoastGPT

RoastGPT is a Telegram bot for short, harsh group-chat roasts in Russian or the language of your request. It uses `gpt-5.4`, writes a draft, then edits it for a sharper punchline and more natural wording. The prompts focus on specific chat details, contradictions, callbacks and uncensored profanity.

By default it replies only when someone mentions its Telegram username or replies to one of its messages. Ask `@your_bot что думаешь о @max?` to roast Max using his messages in the available history.

This was built as a fun project for a group chat with friends.

## Sample

![image](https://github.com/pakshuang/roast-gpt/assets/81917538/fa865bcc-37a7-4f40-8e42-fb72f71516b6)

## CI/CD Deployment Pipeline

![AWS Pipeline](assets/aws.svg)

## Building

Copy `.env.example` to `.env` and fill in your tokens and chat IDs. Keep `.env` private. Example:

```Dotenv
TELEGRAM_TOKEN=your-telegram-bot-token
OPENAI_TOKEN=your-openai-api-key
WHITELIST_USERNAMES=username1,username2
BLACKLIST_USERNAMES=username3,username4
TARGET_CHAT_ID=-100987654321
CLEAR_CHAT_HISTORY=false
CHAT_HISTORY_PATH=./data/chat_history.json
ROAST_MODEL=gpt-5.4
ROAST_REASONING_EFFORT=low
HUMANIZE_ROASTS=true
MENTION_ONLY=true
CHAT_HISTORY_LENGTH=500
CONTEXT_MAX_BYTES=60000
```

Username lists accept `alice,bob`, `["alice", "bob"]`, `['alice', 'bob']`, or `[alice, bob]`. Names are case-insensitive and may include `@`. Blank username lists mean no entries. `TARGET_CHAT_ID` accepts one integer or a list/comma-separated set of IDs; missing or invalid chat IDs stop startup with a readable configuration error. `OPENAI_API_KEY` is also accepted if `OPENAI_TOKEN` is unset.

Ensure that you have Docker installed, then run this command to build the Docker image.

```bash
docker build -t roast-gpt .
```

## Usage

```bash
docker run --env-file .env -e CHAT_HISTORY_PATH=/data/chat_history.json -v roast-gpt-data:/data roast-gpt
```

### Without Docker

```bash
pipenv sync
pipenv run python main.py
```

`pipenv run` loads `.env`. For local use, set `CHAT_HISTORY_PATH=./data/chat_history.json` so the bot can create its history directory. Direct `python main.py` expects variables already exported by your environment.

Use the committed `Pipfile.lock` for installation. The bot uses the OpenAI 0.27.8 API and python-telegram-bot 20.4 locked there; regenerating the lockfile or installing the latest OpenAI SDK requires a separate SDK migration.

## Railway

Railway detects the repository's [Dockerfile](https://docs.railway.com/builds/dockerfiles). Add these service [variables](https://docs.railway.com/variables), entering the values without wrapping the whole value in extra quotes:

```dotenv
TELEGRAM_TOKEN=your-telegram-bot-token
OPENAI_TOKEN=your-openai-api-key
TARGET_CHAT_ID=-100987654321
WHITELIST_USERNAMES=username1,username2
BLACKLIST_USERNAMES=
CLEAR_CHAT_HISTORY=false
CHAT_HISTORY_PATH=/data/chat_history.json
```

The old `JSONDecodeError` at `WHITELIST_USERNAMES` means its value was not strict JSON. For the original code, the value must be `["username1","username2"]`. The updated parser also accepts the simpler `username1,username2` format above and validates every entry.

Mount a Railway [volume](https://docs.railway.com/volumes) at `/data` to keep conversation history across deployments. Without a volume, the bot can start but its history is ephemeral. Keep `CLEAR_CHAT_HISTORY=false`; setting it to `true` erases history on every startup until you change it back. The Docker start command is already `python -u ./main.py`.

Run one bot instance with this Telegram token. This is a polling worker, so it does not expose an HTTP port or healthcheck endpoint. For the bot to see ordinary group messages, disable its group privacy mode through BotFather or make it a group admin; see the [Telegram bot FAQ](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get).

The new model, editor, history limits and mention-only behavior apply by default even if your existing Railway variables omit them. You can add the optional variables from `.env.example` to override them. The image includes the runtime prompt files. Rebuild and redeploy to apply code or prompt changes.

## Reply behavior

- With `MENTION_ONLY=true`, only direct replies and mentions of this bot's actual username trigger an API request. Whitelisting does not make it speak spontaneously.
- Ordinary incoming messages still build context. Receiving a message and replying to it are separate decisions.
- With `MENTION_ONLY=false`, the original automatic behavior is available: whitelist/fuzzy eligibility followed by an 8-10 roast score, plus the legacy `wordle bot` trigger. The inexpensive `gpt-4o-mini` scores messages; `ROAST_MODEL` writes the jokes. Frequency settings remain in `config.py`.
- Blacklisted usernames are excluded from every trigger. Bot messages are ignored.
- The prompt requests a 9/10 roast tone: a direct vulgar insult tied to the person's behavior, a concrete callback and a harsh ending in 1-3 sentences. The model is instructed to respect genuine distress and requests to stop.
- Requests about another user name that user explicitly. The prompt asks for example messages if the target has no material in context.

## Jokes and the editing pass

Edit [prompts/roast.md](prompts/roast.md) to change the personality. Its examples demonstrate blunt, vulgar attacks on specific behavior. [prompts/humanizer.md](prompts/humanizer.md) contains the runtime editing rules inspired by the Humanizer skill: remove stock openings, vague insults, stale comparisons, translated phrasing, explanations after the punchline and invented facts. The editor must preserve direct insults and profanity, strengthen mild drafts and fix clumsy wording without replacing it with polite euphemisms. The 9/10 label describes the requested voice; it is not a measured output score.

`HUMANIZE_ROASTS=true` makes two sequential model requests per reply, sending the available context to both. If editing fails, the completed draft is sent. Set it to `false` to use one request. Larger histories and the stronger model increase API cost. `ROAST_MODEL=gpt-5.4-mini` or `gpt-4.1` also work, but gave weaker results in the small Russian comparison. See the [model and comedy research notes](docs/roast-design.md) for examples, sources and limitations.

`ROAST_REASONING_EFFORT=low` gives GPT-5.4 a small reasoning budget. It also accepts `none` and `medium`; this setting is ignored for the tested GPT-4 models. Requests allow 1600 completion tokens with reasoning, including hidden reasoning, or 320 with `none`. The prompt still asks for a short reply. Incomplete responses are discarded.

## Conversation history

- `CHAT_HISTORY_LENGTH=500` stores up to 500 messages per chat, including the bot's sent replies. It accepts 1-5000.
- New records contain the author's display name, username, Telegram user ID, timestamp, text and a snapshot of the replied-to message. This helps distinguish the requester from the roast target. Spoiler text is redacted.
- Each model request gets the newest contiguous messages that fit `CONTEXT_MAX_BYTES=60000`. This is a UTF-8 JSON byte budget for history, not a token count; system instructions and the current request are additional. It accepts 4096-200000. Short messages allow more history than long messages. An oversized newest entry is visibly truncated only in the model context; the stored entry remains intact.
- Histories are isolated by chat ID. Original string records remain readable, but their missing usernames and user IDs cannot be recovered. New messages gradually replace them. An ambiguous legacy global history requires selecting one chat to migrate or explicitly resetting it.
- The bot only knows messages Telegram delivered while it was running. It does not fetch a group's earlier message archive or look up private information about a tagged user.

## Tests

```bash
pipenv run python -m unittest discover -s tests -v
```

The tests use mocked OpenAI and Telegram calls; they do not send messages or spend API credits.

For an explicit paid comparison using fictional conversations only:

```bash
pipenv run python evals/compare_models.py --models gpt-4o-mini gpt-4.1 gpt-5.4-mini gpt-5.4 --limit 4 --humanize
pipenv run python evals/compare_models.py --models gpt-5.4 --limit 8 --humanize --long-context
```

The second command also exercises a 500-message conversation and applies the same history budget as the bot. Results include drafts, final replies, token usage and elapsed time. Neither command connects to Telegram. Generated text still needs human review; successful API calls do not prove a joke is good.

## Contributing

Pull requests are welcome. For major changes, please open an issue first
to discuss what you would like to change.

Particularly, any prompt improvements are welcome.
