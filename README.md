# Vera Signal Engine

A deterministic, stateful solution to the magicpin AI Challenge. `bot.py` exposes the required `/v1/healthz`, `/v1/metadata`, `/v1/context`, `/v1/tick`, and `/v1/reply` endpoints plus optional `/v1/teardown`. It also exports the standalone `compose(category, merchant, trigger, customer=None)` contract. `submission.jsonl` contains all 30 canonical compositions.

## Approach

The engine ranks triggers by urgency and decision value, sends at most one action per merchant/customer entity in a tick, and composes from only the latest received category, merchant, trigger, and customer records. Category strategies cover research, regulation, performance, planning, review, competition, events, retention, appointments, recalls, refill, and win-back flows. Unknown trigger kinds use a conservative fact-only fallback.

Operational safeguards include atomic context versioning, 500 KB context limits, suppression-key deduplication, customer-consent checks, category/trigger compatibility checks, first-touch template metadata, no-URL validation, 20-action caps, opt-out muting, and teardown wiping. Reply routing detects canned auto-replies across conversations (send once → wait → end), switches commitments directly to action, stops on hostility/declines, and keeps off-topic requests within Vera's scope.

## Tradeoffs

The solution uses no runtime LLM. This gives sub-second, reproducible responses and removes API cost, availability, and hallucination risk. The tradeoff is less linguistic variety than a frontier model; the strategy library and grounded adaptive fallback are designed to preserve decision quality on fresh judge injections. State is in memory, which matches the challenge contract but requires a single long-lived process during evaluation.

## Run and verify

```bash
python3 -m pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
python3 -m unittest discover -s tests -v
python3 generate_submission.py --expanded-dir /Users/nishantagarwal/Desktop/magicpin-ai-challenge/expanded
```

For Docker: `docker build -t vera-signal-engine .` then `docker run -p 8080:8080 vera-signal-engine`. `render.yaml` and `Procfile` are included for public deployment. Set `TEAM_NAME`, `TEAM_MEMBERS`, and `CONTACT_EMAIL` before submission.

If you keep the expanded challenge folder elsewhere, pass that path to `--expanded-dir`; the generator also auto-discovers the common sibling-folder layout.

The most useful additional context would be real slot/inventory data for generated placeholder triggers and explicit per-trigger consent scopes; when absent, the engine avoids inventing those details.
