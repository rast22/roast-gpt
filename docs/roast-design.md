# Roast design and model choice

## Choice

The default is `gpt-5.4` with `ROAST_REASONING_EFFORT=low` and a second editing request using the same model. The choice prioritizes the quality of Russian chat replies. The configured OpenAI account successfully generated responses with all four candidates below using this repository's locked OpenAI 0.27.8 SDK.

This was a small qualitative comparison, not a statistical benchmark. The initial runs used four fictional cases per model. GPT-4.1 and GPT-5.4 also received an editing-pass comparison. The prompts then changed to address observed language, attribution and unsupported-inference problems. The raw [initial comparison](../evals/model-comparison.json) and [final GPT-5.4 evaluation](../evals/validated-gpt-5.4.json) are separate so their different conditions remain visible.

| Model | Observed behavior in the initial samples |
| --- | --- |
| gpt-4o-mini | Awkward Russian, an unnecessary English word, confused details and generic insults. |
| gpt-4.1 | Shorter and cleaner drafts, but usually mild. The editing pass sometimes introduced nonsensical comparisons. |
| gpt-5.4-mini | More usable wording, but still generic comparisons and an ambiguous second-person reply when another user was the target. |
| gpt-5.4 | Stronger callbacks and more natural profanity in several samples. Selected for the final prompt iteration. |

The final nine-case run used the current structured author records, prompts, context builder and request parameters. All draft and edit calls finished normally. Manual review confirmed Russian/English language matching, a neutral stop acknowledgement, a request for material about an unknown user, and a callback to an older message in the long-context case. One draw per case cannot establish consistent quality or perfect adherence to the rules. An editor can also weaken an otherwise good draft; `HUMANIZE_ROASTS=false` is available for comparison.

The long-context case starts with 500 fictional messages. The 60,000-byte budget retained 301 messages (59,983 UTF-8 JSON bytes), including the target's microphone purchase 200 messages before the latest message. Its final reply was:

> @max купил дорогущий микрофон для подкаста, а за два месяца записал только кота. У него даже контент чихнул и съебался.

The two calls in that long-context example took 3.90 seconds in this run. This is an observation, not a latency guarantee. All cases are fictional; evaluation does not read live chat history or connect to Telegram.

## Why these joke rules

- GoStandup's Russian explanation of Greg Dean's method describes a setup that creates an assumption and a connected punchline that changes the interpretation. We adapted this into: find one concrete hook, use the incoming message as the setup, add a connected surprise and put the strongest reveal last. [GoStandup, 8 December 2021](https://gostandup.ru/articles/kak_napisat_shutku_metod_grega_dina).
- Interviews with Russian roast writers emphasize sharpness while retaining an actual joke. Vera Kotelnikova describes looking for specific mistakes and salient traits; the discussion warns that exchanges can deteriorate into plain insults. We adapted that into callbacks to the target's actual messages and contradictions, rather than interchangeable name-calling. No sample jokes or private biographies were copied. [КиноРепортер, «Прожарка по-русски»](https://kinoreporter.ru/prozharka-po-russki/).
- The installed Humanizer skill (MIT, version 2.9.1) informed a compact runtime editing pass: preserve facts and voice, remove stock introductions and symmetrical templates, prefer a specific detail, cut explanation after the punchline and avoid forced informality. The Russian adaptation lives in [humanizer.md](../prompts/humanizer.md); deployment does not depend on a locally installed Codex skill.

The runtime prompts explicitly request a 9/10 tone, direct vulgar insults tied to the target's behavior, and abrasive examples. The editor is instructed to strengthen mild drafts and preserve direct insults while correcting unnatural phrasing. The 9/10 label is a style target, not a measured score. The instructions avoid fabricated incidents and personal facts. Genuine distress and requests to stop receive a short ordinary response. These are model instructions, not a claim that every generated reply is guaranteed to follow them.

The [revised intensity samples](../evals/toxic-gpt-5.4.json) use the same nine fictional cases with the more abrasive prompts. The earlier evaluation files above document the earlier wording and remain available for comparison. Drafts and edited outputs are both retained so an editor that softens a joke or preserves awkward phrasing can be spotted during review.

## Model API and cost

OpenAI documents Chat Completions and `none`, `low`, `medium`, `high`, and `xhigh` reasoning for GPT-5.4. The bot exposes the first three settings and reserves completion space for reasoning when enabled. It keeps the existing SDK rather than requiring a dependency migration. [GPT-5.4 documentation](https://developers.openai.com/api/docs/models/gpt-5.4).

As checked on 6 September 2026, the documented standard text prices are $2.50 per million input tokens, $0.25 per million cached input tokens and $15 per million output tokens. A completed roast normally uses two requests, each receiving the selected chat context. Both the stronger model and the larger context therefore cost more than the previous single-call `gpt-4o-mini` setup. Actual usage for the fictional examples is saved with the evaluation results. [GPT-5.4 pricing](https://developers.openai.com/api/docs/models/gpt-5.4).

## Validation

- 42 mocked tests cover deployment parsing, mention-only triggers, author attribution, history isolation/migration, UTF-8 context limits, spoiler redaction, model parameters, retry handling and editor fallback.
- All Python source files parse with Python 3.10 syntax. Tests ran on Python 3.11 with the exact packages from `Pipfile.lock`.
- The Dockerfile includes `history.py` and both runtime prompts. A full Docker build could not be run because the local Docker daemon was unavailable.
- No Telegram messages were sent and no Railway deployment was performed.
