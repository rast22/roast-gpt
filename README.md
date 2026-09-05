# RoastGPT

RoastGPT is a Telegram bot that writes short, savage comebacks for a group chat. It looks for bragging, bad takes, excuses, and insults, then uses the recent conversation to write a specific punchline with sarcasm and profanity. It uses OpenAI's `gpt-4o-mini`; the sharper personality is configured in `config.py`.

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

## Reply behavior

- Direct replies to this bot and `@mentions` of its actual Telegram username trigger a comeback. The old `wordle bot` phrase remains supported.
- Whitelisted users and a random 25% of other messages are scored for roast material. A score of 8-10 triggers a reply. Change `FUZZY_USER_FILTER`, `FUZZY_PROBABILITY`, or `QUALIFICATION_THRESHOLD` in `config.py` to adjust frequency.
- Blacklisted usernames are excluded from every trigger. Bot messages are ignored.
- The roast prompt asks for 1-2 sharp sentences with natural profanity and specific callbacks, and instructs the model to drop the roast for genuine distress or a request to stop. It no longer forces Singaporean slang onto every conversation.
- Each configured chat keeps its own last 40 messages. Original single-chat history files migrate automatically; ambiguous legacy history with multiple chat IDs requires choosing one chat to migrate or explicitly resetting it.

## Tests

```bash
pipenv run python -m unittest discover -s tests -v
```

The tests use mocked OpenAI and Telegram calls; they do not send messages or spend API credits.

## Contributing

Pull requests are welcome. For major changes, please open an issue first
to discuss what you would like to change.

Particularly, any prompt improvements are welcome.
