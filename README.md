# Vera Signal Engine

FastAPI service for the magicpin merchant-assistant challenge. It provides a deterministic `compose(...)` function and the HTTP API required for deployment.

## How it works (v2.0.0)

The engine is fully deterministic — no LLM calls, no API keys, no per-request cost, and no timeout risk. Every message is built from the four supplied contexts (category, merchant, trigger, customer) by a template composer that follows three rules:

1. **Only judge-visible facts reach a human.** Composition draws exclusively on the fields the evaluator can also see — the merchant's own views/calls/CTR, locality, active offers, and the trigger payload — so every claim is verifiable and nothing is fabricated. A `_JARGON` filter and a taboo-phrase stripper guarantee internal vocabulary (context, trigger, payload, rationale, and each category's `vocab_taboo`) never appears in merchant- or customer-facing text.
2. **Speak the merchant's language.** A `Voice` layer renders each message in the category's declared register — natural Hindi-English code-mix for dentists, pharmacies, restaurants and salons; lighter mix for gyms; English elsewhere — and downgrades to English when the merchant does not list Hindi. Customer messages honour the customer's own `language_pref`.
3. **Anchor every proactive message in a fact the evaluator can check.** Merchant-directed sends always cite a real performance number (or the listing locality as a fallback); customer-directed sends address the customer and merchant by name and never leak internal analytics like CTR.

Around the composer sit the operational safeguards the judge exercises: consent/scope gating (customer-scope triggers are suppressed without a customer, `supply_alert` never goes to a non-pharmacy, opted-out merchants are muted), per-tick fan-out limits and suppression-key dedup, per-merchant **and** per-thread auto-reply detection (the simulator sends the same canned text on four different conversation ids), an intent transition from qualifying to actioning, precise decline matching, and replay-context recovery for threads the bot never opened.

`test_bot.py` replays every one of these judge phases in-process (612 checks) and lints every composed message against the scoring rubric.

## Run locally

```bash
python3 -m pip install -r requirements.txt
python3 -m uvicorn bot:app --host 0.0.0.0 --port 8080
```

Check that it is running:

```bash
curl http://localhost:8080/v1/healthz
```

Interactive API documentation is available at `http://localhost:8080/docs`.

To load the expanded local dataset and run a sample tick:

```bash
python3 load_contexts.py --tick
```

## API

- `GET /v1/healthz`
- `GET /v1/metadata`
- `POST /v1/context`
- `POST /v1/tick`
- `POST /v1/reply`
- `POST /v1/teardown`

JSON `POST` requests must include `Content-Type: application/json`.

## Deploy on Render

The repository includes `render.yaml` and a Dockerfile.

1. Push the repository to GitHub.
2. In Render, choose **New -> Blueprint** and select the repository.
3. Set `TEAM_NAME`, `TEAM_MEMBERS`, `CONTACT_EMAIL`, and `SUBMITTED_AT`.
4. Deploy and wait for the service to become **Live**.
5. Verify the generated URL:

```bash
curl https://YOUR-SERVICE.onrender.com/v1/healthz
curl https://YOUR-SERVICE.onrender.com/v1/metadata
```

Submit the base URL, for example:

```text
https://YOUR-SERVICE.onrender.com
```

The evaluator loads its own contexts through the API. Do not pre-load the expanded dataset on the deployed service.

## Generate submission

```bash
python3 generate_submission.py --expanded-dir /path/to/magicpin-ai-challenge/expanded
```

This writes `submission.jsonl` (one row per canonical test pair) with `test_id`, `body`, `cta`, `send_as`, `suppression_key`, and `rationale`.

## Tests

Run the offline judge emulation and rubric lint (no server or network required):

```bash
python3 test_bot.py --expanded-dir /path/to/magicpin-ai-challenge/expanded
```

It covers the context lifecycle (accept / 409 on stale version / higher version / invalid scope), tick composition and dedup, the four-thread auto-reply trap, the qualifying→actioning intent transition, a hostile opt-out, decline precision, replay of unknown conversations, a full rubric lint over every canonical pair, and language coverage for the code-mixed categories.
