"""Explicit, paid, synthetic evaluation. Never connects to Telegram."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openai
import config
from history import build_context, json_bytes
from main import completion_parameters


def long_context_case():
    def record(author, text):
        return {"role": "user", "author": author, "timestamp": "2026-09-06 12:00:00", "text": text}

    dima = {"name": "Дима", "username": "dima", "user_id": 43}
    max_user = {"name": "Макс", "username": "max", "user_id": 42}
    history = [record(dima, f"Встреча в четверг. Пункт повестки {i}.") for i in range(500)]
    # Within the byte budget, well before the old 40-message cutoff.
    history[300] = record(max_user, "Купил дорогущий микрофон. Теперь точно начну записывать подкаст.")
    history[499] = record(max_user, "Прошло два месяца. Пока только записал, как кот чихнул.")
    return {"id": "ru_long_context", "requester": dima, "request": "@roast_bot что думаешь о подкасте @max?", "history": history}


async def run(args):
    openai.api_key = os.environ.get("OPENAI_TOKEN") or os.environ.get("OPENAI_API_KEY")
    if not openai.api_key:
        raise SystemExit("Set OPENAI_TOKEN or run through pipenv to load .env.")
    cases = json.loads(Path(__file__).with_name("roast_cases.json").read_text())[:args.limit]
    if args.long_context:
        cases.append(long_context_case())
    semaphore = asyncio.Semaphore(3)

    async def evaluate(model, case):
        async with semaphore:
            history = build_context(case["history"][-config.CHAT_HISTORY_LENGTH:], config.CONTEXT_MAX_BYTES)
            prompt = config.generate_main_prompt(case["requester"], history, request=case["request"])
            started = time.monotonic()
            result = {"model": model, "case": case["id"], "request": case["request"], "history_messages": len(history), "history_bytes": json_bytes(history)}
            params = completion_parameters(model, reasoning_effort=args.reasoning_effort or config.ROAST_REASONING_EFFORT)
            result["reasoning_effort"] = params.get("reasoning_effort")
            try:
                completion = await openai.ChatCompletion.acreate(**params, messages=[{"role": "system", "content": config.SYSTEM_ROLE_MAIN}, {"role": "user", "content": prompt}])
                result.update(draft=completion.choices[0].message.content, usage=dict(completion.usage), finish_reason=completion.choices[0].finish_reason)
                if args.humanize and result["draft"]:
                    edited = await openai.ChatCompletion.acreate(**params, messages=[{"role": "system", "content": config.SYSTEM_ROLE_HUMANIZER}, {"role": "user", "content": config.generate_humanizer_prompt(prompt, result["draft"])}])
                    result.update(final=edited.choices[0].message.content, edit_usage=dict(edited.usage), edit_finish_reason=edited.choices[0].finish_reason)
            except openai.error.OpenAIError as exc:
                result["error"] = type(exc).__name__
                result["status"] = exc.http_status
                # Only expose the API's error code, never headers, tokens or bodies.
                result["code"] = exc.code
            result["seconds"] = round(time.monotonic() - started, 2)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return result

    results = await asyncio.gather(*(evaluate(model, case) for model in args.models for case in cases))
    Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    if any("error" in result or result.get("finish_reason") != "stop" or (args.humanize and result.get("edit_finish_reason") != "stop") for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", default=["gpt-4o-mini", "gpt-4.1", "gpt-5.4-mini", "gpt-5.4"])
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--humanize", action="store_true")
    parser.add_argument("--long-context", action="store_true", help="Also test a 500-message fictional conversation.")
    parser.add_argument("--reasoning-effort", choices=["none", "low", "medium"], help="Compare reasoning effort on GPT-5.4 models.")
    parser.add_argument("--output", default="/tmp/roast-model-comparison.json")
    asyncio.run(run(parser.parse_args()))
