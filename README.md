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
```

Starting the server does not print the challenge lifecycle or sample responses in the terminal. It starts a JSON API. Open [http://localhost:8080/docs](http://localhost:8080/docs) for interactive API documentation, or call the endpoints below from another terminal.

To load the complete expanded dataset automatically, keep the server running and use a second terminal:

```bash
python3 load_contexts.py --tick
```

This discovers the standard sibling `magicpin-ai-challenge/expanded` folder and loads all categories, merchants, customers, and triggers. To provide the folder explicitly:

```bash
python3 load_contexts.py \
  --expanded-dir /path/to/magicpin-ai-challenge/expanded \
  --tick
```

The loader prints the loaded context counts, health response, and any actions returned by the tick. Use `--url https://your-deployed-host.example` when testing a deployed service.

## API quickstart

Every JSON `POST` must include `Content-Type: application/json`. A tick can create an action only after its category, merchant, and trigger contexts have been loaded.

### 1. Check the service

```bash
curl -sS http://localhost:8080/v1/healthz
curl -sS http://localhost:8080/v1/metadata
```

### 2. Push category context

```bash
curl -sS -X POST http://localhost:8080/v1/context \
  -H "Content-Type: application/json" \
  -d '{
    "scope": "category",
    "context_id": "dentists",
    "version": 1,
    "payload": {
      "slug": "dentists",
      "voice": {"tone": "peer_clinical"},
      "digest": [{
        "id": "digest_fluoride",
        "kind": "research",
        "title": "3-month fluoride recall outperforms 6-month recall",
        "source": "JIDA Oct 2026",
        "trial_n": 2100,
        "summary": "A supplied study reported lower recurrence for high-risk adults."
      }]
    },
    "delivered_at": "2026-04-29T10:00:00Z"
  }'
```

### 3. Push merchant context

```bash
curl -sS -X POST http://localhost:8080/v1/context \
  -H "Content-Type: application/json" \
  -d '{
    "scope": "merchant",
    "context_id": "m_001_drmeera",
    "version": 1,
    "payload": {
      "merchant_id": "m_001_drmeera",
      "category_slug": "dentists",
      "identity": {
        "name": "Dr. Meera Dental Clinic",
        "owner_first_name": "Meera",
        "locality": "South Delhi"
      },
      "performance": {"window_days": 30, "views": 2410, "calls": 18, "ctr": 0.021},
      "offers": [{"title": "Dental Cleaning @ ₹299", "status": "active"}],
      "customer_aggregate": {"high_risk_adult_count": 124}
    },
    "delivered_at": "2026-04-29T10:00:00Z"
  }'
```

Reposting the same scope, context ID, and version returns `409 stale_version`. A higher version atomically replaces the stored context.

### 4. Push a trigger

```bash
curl -sS -X POST http://localhost:8080/v1/context \
  -H "Content-Type: application/json" \
  -d '{
    "scope": "trigger",
    "context_id": "trg_research_digest_dentists",
    "version": 1,
    "payload": {
      "id": "trg_research_digest_dentists",
      "scope": "merchant",
      "kind": "research_digest",
      "source": "external",
      "merchant_id": "m_001_drmeera",
      "customer_id": null,
      "payload": {"top_item_id": "digest_fluoride"},
      "urgency": 2,
      "suppression_key": "research:dentists:2026-W17",
      "expires_at": "2026-05-03T00:00:00Z"
    },
    "delivered_at": "2026-04-29T10:05:00Z"
  }'
```

### 5. Run a periodic tick

```bash
curl -sS -X POST http://localhost:8080/v1/tick \
  -H "Content-Type: application/json" \
  -d '{
    "now": "2026-04-29T10:30:00Z",
    "available_triggers": ["trg_research_digest_dentists"]
  }'
```

The response contains up to 20 actions. Copy the returned `conversation_id` for the reply call. If the result is `{"actions":[]}`, check that all three contexts were loaded, the trigger is not expired or suppressed, and the merchant is not muted.

### 6. Send a merchant reply

```bash
curl -sS -X POST http://localhost:8080/v1/reply \
  -H "Content-Type: application/json" \
  -d '{
    "conversation_id": "PASTE_CONVERSATION_ID_FROM_TICK",
    "merchant_id": "m_001_drmeera",
    "customer_id": null,
    "from_role": "merchant",
    "message": "Yes, send me the abstract",
    "received_at": "2026-04-29T10:35:00Z",
    "turn_number": 2
  }'
```

Valid reply actions are `send`, `wait`, and `end`.

## Evaluation lifecycle

1. **Warmup:** health and metadata checks, followed by category, merchant, and customer context loading.
2. **Test window:** 60 simulated minutes with context updates and `/v1/tick` calls.
3. **Adaptive injection:** fresh digest items, metric shifts, triggers, and customer scopes arrive during the run.
4. **Replay test:** leading bots are tested on auto-replies, intent transitions, hostile replies, and off-topic requests.
5. **Score report:** message scores, transcripts, logs, timeline, and judge rationale.

Operational limits: 30-second response timeout, 10 judge requests per second, 500 KB per context payload, and 20 actions per tick.

## Deployment and submission generation

For Docker: `docker build -t vera-signal-engine .` then `docker run -p 8080:8080 vera-signal-engine`. `render.yaml` and `Procfile` are included for public deployment. Set `TEAM_NAME`, `TEAM_MEMBERS`, `CONTACT_EMAIL`, and `SUBMITTED_AT` in the deployment environment.

To regenerate the static challenge submission:

```bash
python3 generate_submission.py --expanded-dir /path/to/magicpin-ai-challenge/expanded
```

If the expanded challenge folder is available in the standard sibling-folder layout, the generator finds it automatically. Otherwise, pass its local path with `--expanded-dir` as shown above. The path is only used during local submission generation and is not part of the deployed service.

The most useful additional context would be real slot/inventory data for generated placeholder triggers and explicit per-trigger consent scopes; when absent, the engine avoids inventing those details.
