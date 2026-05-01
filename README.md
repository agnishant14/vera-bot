# Vera Bot

magicpin AI challenge submission. FastAPI server that handles all 5 judge endpoints.

---

## How it works

Judges push context (merchants, categories, triggers) → bot composes a WhatsApp message using Gemini → judge plays the merchant and replies → bot handles the conversation.

Three files that matter:

- `app.py` — the server, all endpoints
- `composer.py` — builds the message using Gemini API (temperature=0 for consistency)
- `rule_composer.py` — deterministic fallback, no API needed

---

## Run locally

```bash
pip install fastapi uvicorn pydantic httpx

export GEMINI_API_KEY=your-key-here

python app.py
```

Test it:
```bash
curl http://localhost:8080/v1/healthz
```

---

## Deploy

Push to Railway, set `GEMINI_API_KEY` as an environment variable, submit the public URL.

---

## Approach

Gave Gemini the full merchant context — real CTR numbers, active offers, peer benchmarks, conversation history — and told it to write one specific message per trigger. Auto-reply detection and intent transitions are handled with regex before touching the API, so common cases are fast and don't waste calls.

Biggest tradeoff: everything is in-memory, which is fine for a 60-minute eval window.

---

## What would've helped

- Real merchant reply examples per category (the intent classifier is regex-based right now)
- Actual slot availability data for booking flows
- Magicpin's production suppression windows
