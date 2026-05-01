# Vera Bot — magicpin AI Challenge Submission

## TL;DR

A FastAPI server that implements the full 5-endpoint Vera contract.  
Core idea: **trigger-kind routing → Claude composer with merchant-anchored prompts → fast-path reply handling**.

---

## Architecture

```
judge → POST /v1/context   →  in-memory context store (category/merchant/customer/trigger)
judge → POST /v1/tick      →  trigger loop → compose() → actions[]
judge → POST /v1/reply     →  fast-path (auto-reply/intent/hostile) → respond() → LLM fallback
```

### `composer.py` — two functions

**`compose(category, merchant, trigger, customer?)`**  
Builds a dense JSON context packet from the 4 input layers — trimmed to the most decision-relevant facts (peer stats, active offers, digest items matching the trigger, conversation history last 4 turns, customer aggregate signals). Passes to Claude (temperature=0) with a tight system prompt encoding all rubric constraints: voice match, specificity requirements, anti-patterns, compulsion levers.  

Trigger-kind routing is implicit in the prompt — the trigger payload + category digest alignment guide Claude to the right lever. No separate routing table needed; the context packet is self-describing.

**`respond(state, merchant_message)`**  
Three fast-path detections before touching LLM:
1. **Auto-reply** — regex on 10 canned-greeting patterns; backs off progressively (send once → wait 4h → end)
2. **Intent YES** — regex ("yes", "haan", "let's do it", "go ahead"…) → switches to action mode immediately  
3. **Hostile/opt-out** — ends gracefully, no further engagement

Only neutral / curveball replies go to the LLM, with full conversation history for context.

---

## What I optimised for

1. **Specificity** — the prompt explicitly tells Claude to anchor on verifiable facts: peer stats, trial_n, page numbers, CTR deltas, active offer prices, customer aggregate counts. Generic "X% off" framings are listed as hard anti-patterns.

2. **Category voice** — tone rules, salutation patterns, and taboo vocabulary are all injected from the CategoryContext. A dentist message cannot accidentally sound like a restaurant message.

3. **Merchant fit** — active vs expired offers, review themes, signals (stale_posts, ctr_below_peer), conversation history, and the owner_first_name are all included. The model always has the merchant's real context.

4. **Trigger relevance** — the trigger payload (including top_item_id to retrieve the exact digest entry) goes in the prompt. The model knows *why* it's messaging, not just that it should.

5. **Engagement compulsion** — 8 named compulsion levers are explained in the system prompt. The model picks 1-2 based on what fits the trigger (curiosity + reciprocity for research digest; loss aversion + effort externalization for perf_dip; etc).

---

## Tradeoffs

- **In-memory state only** — fine for the 60-minute evaluation window; would need Redis for production
- **Single Claude call per compose** — clean + debuggable; could add retrieval-augmented digest search for deeper research items in longer-running scenarios
- **No multi-language detection per turn** — the composer looks at merchant.identity.languages but doesn't dynamically shift based on the merchant's reply language; the reply handler passes the full history so the model infers it
- **Temperature=0** — deterministic for judge reproducibility; real production might use temp=0.3 for variety

---

## Deployment

```bash
# Local
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
python app.py

# Docker
docker build -t vera-bot .
docker run -p 8080:8080 -e ANTHROPIC_API_KEY=sk-ant-... vera-bot

# Railway / Render / Fly
# Push this directory as the repo root; set ANTHROPIC_API_KEY as env var
```

## Local testing

```bash
# Health check
curl http://localhost:8080/v1/healthz

# Push a category context
curl -X POST http://localhost:8080/v1/context \
  -H "Content-Type: application/json" \
  -d @../magicpin/expanded/categories/dentists.json \
  # Note: wrap in {"scope":"category","context_id":"dentists","version":1,"payload":<content>}

# Tick
curl -X POST http://localhost:8080/v1/tick \
  -H "Content-Type: application/json" \
  -d '{"now":"2026-04-29T10:00:00Z","available_triggers":["trg_001_research_digest_dentists"]}'
```

---

## What additional context would have helped most

1. **Real conversation history** — seeing 10-20 real Vera conversations per category would sharpen voice calibration more than the 4 examples provided
2. **Merchant reply corpus** — knowing how merchants in each category actually phrase "yes"/"no"/"send me more" would improve the intent classifier (currently regex-based)
3. **Slot availability API** — for customer-facing recall messages, real slot data would allow actual booking confirmation instead of plausible-but-synthetic slots
4. **Suppression rule details** — knowing magicpin's production suppression window (e.g., "don't contact same merchant twice in 48h on the same topic") would help the bot be more conservative and realistic
