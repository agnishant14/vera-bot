"""
composer_gemini.py — Drop-in replacement for composer.py using FREE Google Gemini API.

Get your free key at: https://aistudio.google.com/app/apikey
Then: export GEMINI_API_KEY=your-key-here

Replace composer.py with this file:
    cp composer_gemini.py composer.py
"""

import json
import re
import os
from typing import Optional
import urllib.request

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"

# ---- auto-reply detection ----
_AUTO_REPLY_PATTERNS = [
    r"thank you for contacting", r"aapki jaankari ke liye bahut",
    r"our team will respond shortly", r"main ek automated assistant hoon",
    r"this is an automated", r"we will get back to you",
]
def is_auto_reply(message: str) -> bool:
    low = message.lower()
    return any(re.search(p, low) for p in _AUTO_REPLY_PATTERNS)

# ---- intent detection ----
def classify_intent(message: str) -> str:
    low = message.lower()
    if any(re.search(p, low) for p in [r"stop", r"not interested", r"useless", r"faaltu", r"bakwaas", r"bother"]):
        return "hostile"
    if any(re.search(p, low) for p in [r"\bno\b", r"nahi", r"nope"]):
        return "no"
    if any(re.search(p, low) for p in [r"\byes\b", r"haan", r"go ahead", r"let.s do", r"karo", r"confirm"]):
        return "yes"
    return "neutral"

def _suppression_key(trigger, merchant, customer):
    base = trigger.get("suppression_key") or trigger.get("id", "")
    if base: return base
    mid = merchant.get("merchant_id", "unknown")
    return f"{trigger.get('kind','general')}:{mid}"

def _template_name(trigger_kind, send_as):
    return f"vera_{trigger_kind}_v1"

def _call_gemini(prompt: str) -> dict:
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 800}
    }).encode()
    req = urllib.request.Request(GEMINI_URL, data=body,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.loads(resp.read())
    raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)

_SYSTEM = """You are Vera — magicpin's AI merchant-growth assistant for Indian merchants.

Rules:
1. Compose ONE WhatsApp message. Use REAL numbers from the input.
2. One CTA at the end. Binary YES/STOP for action triggers; open-ended for research.
3. Voice: dentists=clinical-peer, salons=warm-practical, restaurants=informal, gyms=energetic, pharmacies=helpful.
4. Hindi-English code-mix preferred for Indian merchants.
5. No URLs. No markdown. No fake data. Max 300 chars.
6. Anti-patterns: long preambles, generic "X% off", multiple CTAs, ALL-CAPS.

Respond ONLY with valid JSON (no markdown fences):
{"body":"<msg>","cta":"<binary_yes_no|open_ended|multi_choice_slot|none>","send_as":"<vera|merchant_on_behalf>","rationale":"<2 sentences>"}"""

def compose(category, merchant, trigger, customer=None):
    identity = merchant.get("identity", {})
    perf = merchant.get("performance", {})
    offers = [o for o in merchant.get("offers", []) if o.get("status") == "active"]
    digest = category.get("digest", [])
    tid = trigger.get("payload", {}).get("top_item_id")
    relevant = [d for d in digest if d.get("id") == tid] or digest[:1]

    ctx = {
        "trigger_kind": trigger.get("kind"),
        "trigger_payload": trigger.get("payload", {}),
        "merchant_name": identity.get("name"),
        "owner_first_name": identity.get("owner_first_name"),
        "city": identity.get("city"), "locality": identity.get("locality"),
        "languages": identity.get("languages", ["en"]),
        "category_slug": category.get("slug"),
        "peer_ctr": category.get("peer_stats", {}).get("avg_ctr"),
        "merchant_ctr": perf.get("ctr"),
        "views_30d": perf.get("views"),
        "calls_30d": perf.get("calls"),
        "delta_views_7d": perf.get("delta_7d", {}).get("views_pct"),
        "delta_calls_7d": perf.get("delta_7d", {}).get("calls_pct"),
        "active_offers": offers,
        "relevant_digest": relevant,
        "signals": merchant.get("signals", []),
        "customer_aggregate": merchant.get("customer_aggregate", {}),
        "conversation_history": merchant.get("conversation_history", [])[-3:],
    }
    if customer:
        ctx["customer_name"] = customer.get("identity", {}).get("name")
        ctx["customer_language"] = customer.get("identity", {}).get("language_pref")
        ctx["customer_state"] = customer.get("state")
        ctx["customer_relationship"] = customer.get("relationship", {})
        ctx["instruction"] = "Customer-facing message. send_as=merchant_on_behalf."

    prompt = _SYSTEM + "\n\nContext:\n" + json.dumps(ctx, ensure_ascii=False)
    result = _call_gemini(prompt)
    result.setdefault("suppression_key", _suppression_key(trigger, merchant, customer))
    result.setdefault("template_name", _template_name(trigger.get("kind",""), result.get("send_as","vera")))
    result.setdefault("template_params", [identity.get("owner_first_name",""), result.get("body","")[:80]])
    return result

def respond(state, merchant_message):
    if is_auto_reply(merchant_message):
        count = state.get("auto_reply_count", 0) + 1
        if count == 1:
            return {"action": "send", "body": "Looks like an auto-reply 😊 Jab owner dekhen, sirf 'YES' reply karein.", "cta": "binary_yes_no", "rationale": "Auto-reply detected."}
        elif count == 2:
            return {"action": "wait", "wait_seconds": 14400, "rationale": "Auto-reply 2x. Backing off 4h."}
        else:
            return {"action": "end", "rationale": "Auto-reply 3x. Closing."}
    intent = classify_intent(merchant_message)
    if intent == "hostile":
        return {"action": "end", "rationale": "Merchant opted out."}
    if intent == "no":
        return {"action": "end", "rationale": "Merchant declined."}

    ctx = {
        "history": state.get("history", []),
        "merchant_reply": merchant_message,
        "intent": intent,
        "trigger_kind": state.get("trigger", {}).get("kind"),
        "active_offers": [o for o in state.get("merchant", {}).get("offers", []) if o.get("status") == "active"],
    }
    prompt = """You are Vera. Merchant replied. Respond in JSON only:
{"action":"send","body":"<msg>","cta":"<type>","rationale":"<why>"}
or {"action":"wait","wait_seconds":<int>,"rationale":"..."}
or {"action":"end","rationale":"..."}

If merchant said YES/confirmed → action mode immediately, don't ask more questions.
Max 300 chars. Hindi-English mix OK.

Context: """ + json.dumps(ctx, ensure_ascii=False)
    return _call_gemini(prompt)
