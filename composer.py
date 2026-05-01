"""
composer.py — Vera message composer using Google Gemini API.
Fixes: SSL via requests, strict intent patterns, rule-based fallback, NO urllib.
"""

import json
import re
import os
import requests as _req
from typing import Optional

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"

# ---------------------------------------------------------------------------
# Auto-reply detection
# ---------------------------------------------------------------------------
_AUTO_REPLY_PATTERNS = [
    r"thank you for contacting",
    r"aapki jaankari ke liye shukriya",
    r"our team will respond shortly",
    r"yeh ek automated sandesh hai",
    r"this is an automated",
    r"we will get back to you",
    r"main abhi available nahi hoon",
    r"i am currently unavailable",
]

def is_auto_reply(message: str) -> bool:
    low = message.lower()
    return any(re.search(p, low) for p in _AUTO_REPLY_PATTERNS)


# ---------------------------------------------------------------------------
# Intent classification — STRICT patterns, no false positives
# ---------------------------------------------------------------------------
_HOSTILE_PATTERNS = [
    r"stop (messaging|msg|contacting|sending|texting|calling|it|this|these)",
    r"don'?t (message|contact|text|call|msg|bother|whatsapp) me",
    r"\bremove me\b",
    r"\bopt.?out\b",
    r"\bunsubscribe\b",
    r"leave me alone",
    r"\bspam\b",
    r"\bmat bhejo\b",
    r"\bband karo\b",
    r"\bpareshan mat karo\b",
]

_NO_PATTERNS = [
    r"^no$",
    r"^nahi$",
    r"^nope$",
    r"^nah$",
    r"\bnot now\b",
    r"\bmaybe later\b",
    r"\bno thanks\b",
    r"\bno thank you\b",
    r"\babhi nahi\b",
    r"\bnot interested\b",
]

_YES_PATTERNS = [
    r"\byes\b",
    r"\byes please\b",
    r"\bhaan\b",
    r"\bha\b",
    r"go ahead",
    r"let'?s do it",
    r"let'?s go",
    r"sounds good",
    r"sure",
    r"\bkaro\b",
    r"\bkar do\b",
    r"\bconfirm\b",
    r"\bproceed\b",
    r"please (send|do|book|proceed)",
    r"send (it|them|the)",
    r"book me",
    r"i'?ll take",
    r"\bready\b",
    r"please proceed",
    r"do it",
    r"chaliye",
    r"theek hai",
    r"bilkul",
]

def classify_intent(message: str) -> str:
    low = message.lower().strip()
    if any(re.search(p, low) for p in _HOSTILE_PATTERNS):
        return "hostile"
    if any(re.search(p, low) for p in _NO_PATTERNS):
        return "no"
    if any(re.search(p, low) for p in _YES_PATTERNS):
        return "yes"
    return "neutral"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _suppression_key(trigger, merchant, customer):
    base = trigger.get("suppression_key") or trigger.get("id", "")
    if base:
        return base
    mid = merchant.get("merchant_id", "unknown")
    return f"{trigger.get('kind', 'general')}:{mid}"

def _template_name(trigger_kind, send_as):
    return f"vera_{trigger_kind}_v1"


# ---------------------------------------------------------------------------
# Gemini API call — uses requests (no SSL issues)
# ---------------------------------------------------------------------------
def _call_gemini(prompt: str, max_tokens: int = 800) -> dict:
    resp = _req.post(
        GEMINI_URL,
        params={"key": GEMINI_API_KEY},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens}
        },
        timeout=25
    )
    resp.raise_for_status()
    raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------
_COMPOSE_SYSTEM = """You are Vera — magicpin's AI merchant-growth assistant for Indian merchants.

RULES:
1. Compose ONE WhatsApp message. Anchor on REAL numbers from input (CTR, views, calls, prices, dates).
2. One CTA at end. YES/STOP for action triggers. Open-ended for research/info.
3. Voice by category: dentists=clinical-peer (Dr. salutation, cite source+trial_n), salons=warm-practical, restaurants=informal-punchy (food-first), gyms=energetic-metric, pharmacies=helpful-precise.
4. Hindi-English code-mix natural for Indian merchants.
5. No URLs. No markdown. Max 280 chars. Never exceed 450.
6. ANTI-PATTERNS: long preambles, generic "X% off", multiple CTAs, ALL-CAPS, re-introductions.
7. Use 1-2 compulsion levers: specificity (real #s), loss-aversion, social proof, effort externalization ("I've drafted X, just say go"), curiosity, binary commitment.

For customer-facing triggers (send_as=merchant_on_behalf): write FROM the merchant, USE customer's name and language preference.

Output ONLY valid JSON, no markdown fences:
{"body":"<msg>","cta":"<binary_yes_no|open_ended|multi_choice_slot|none>","send_as":"<vera|merchant_on_behalf>","rationale":"<2 sentences>"}"""

_REPLY_SYSTEM = """You are Vera — magicpin's AI merchant-growth assistant handling a WhatsApp reply.

CRITICAL RULES:
1. YES/confirmed/asked-for-help → action mode IMMEDIATELY. Draft the next concrete step. DO NOT ask more questions.
2. They gave specific details (date, time, D-speed film, X-ray unit, etc.) → acknowledge it and move to action.
3. NO or "not now" → send ONE warm closing message (action=send), not action=end.
4. Match their language (Hindi/English mix if they wrote mixed).
5. Never re-introduce yourself. No preamble. Max 280 chars.
6. ONE CTA maximum.

IMPORTANT: NEVER return action=end for YES/neutral/engaged replies. Only return action=end for hostile/opt-out.

Output ONLY valid JSON, no markdown fences. One of:
{"action":"send","body":"<message>","cta":"<binary_yes_no|open_ended|none>","rationale":"<why>"}
{"action":"wait","wait_seconds":<int>,"rationale":"<why>"}
{"action":"end","rationale":"<why>"}"""


# ---------------------------------------------------------------------------
# Rule-based fallback replies (when Gemini unavailable)
# ---------------------------------------------------------------------------
def _rule_reply(message: str, state: dict, intent: str) -> dict:
    trigger = state.get("trigger", {})
    trigger_kind = trigger.get("kind", "")
    merchant = state.get("merchant", {})
    customer = state.get("customer")
    is_customer = customer is not None
    offers = [o for o in merchant.get("offers", []) if o.get("status") == "active"]
    offer_text = offers[0].get("title", "the offer") if offers else "this"
    msg_low = message.lower()

    if intent == "yes":
        if is_customer:
            if any(x in msg_low for x in ["wed", "thu", "fri", "sat", "sun", "mon", "tue",
                                           "pm", "am", "morning", "evening", "6", "7", "8",
                                           "nov", "dec", "jan", "feb", ":"]):
                return {"action": "send", "body": "Perfect, slot confirmed! You'll get a reminder 1 hour before. See you then 🙏", "cta": "none", "rationale": "Customer gave time — booking confirmed."}
            return {"action": "send", "body": "Great! Is morning or evening better for you? I'll lock in your slot right away.", "cta": "open_ended", "rationale": "Customer confirmed — asking for time preference."}
        else:
            replies = {
                "research_digest": "Pulling the abstract now — and drafting a patient WhatsApp you can forward. Give me 2 min ⚡",
                "regulation_change": "Drafting your SOP update based on the new guidelines now. I'll send the draft for review shortly.",
                "perf_dip": "On it! Drafting 3 specific listing fixes now. I'll send the first change to review in 5 min.",
                "perf_spike": "Locking in your momentum now! Drafting the offer post + banner. Ready in 2 min 🚀",
                "stale_posts": "Drafting 3 fresh posts now — you just review and approve. Coming up in 2 min.",
                "competitor_opened": "On it — pulling 3 listing strengtheners now. I'll send them to review in 5 min.",
                "festival_upcoming": f"Launching the campaign! Google post + WhatsApp broadcast with '{offer_text}' going live. I'll confirm once done.",
                "renewal_due": "Sending you the renewal link now. Takes 2 minutes and your listing stays uninterrupted 👍",
                "active_planning_intent": "Drafting the full plan now — pricing, package structure, and promo copy. 5 min mein ready hoga.",
            }
            body = replies.get(trigger_kind, "Got it! Working on it now — I'll send you the draft to review shortly 👍")
            return {"action": "send", "body": body, "cta": "none", "rationale": f"Merchant confirmed for {trigger_kind}. Moving to action."}

    elif intent == "no":
        return {"action": "send", "body": "No problem at all! Jab bhi ready hon, bas ping kar dena 😊", "cta": "none", "rationale": "Merchant declined — warm close, keeping door open."}

    elif intent == "neutral":
        # They gave context or asked a follow-up
        if is_customer:
            if any(x in msg_low for x in ["wed", "thu", "fri", "sat", "sun", "pm", "am", "morning", "evening"]):
                return {"action": "send", "body": "Noted! Your slot is confirmed. We'll see you then — reply if you need to reschedule 🙏", "cta": "none", "rationale": "Customer gave time — confirmed."}
            return {"action": "send", "body": "Got it! Which date and time works best? I'll confirm the slot right away.", "cta": "open_ended", "rationale": "Customer engaged, asking for time."}
        else:
            if any(x in msg_low for x in ["audit", "help", "how", "what", "check", "setup", "unit", "film", "speed", "kaise", "kya"]):
                return {"action": "send", "body": "Understood! Factoring this in. I'll put together a specific action plan — ready in 5 min. Theek hai?", "cta": "binary_yes_no", "rationale": "Merchant gave context — acknowledging and proceeding."}
            return {"action": "send", "body": "Got it, factoring that in! Want me to go ahead with the plan based on this? Reply YES.", "cta": "binary_yes_no", "rationale": "Merchant gave neutral context — confirming to proceed."}

    return {"action": "send", "body": "Samajh gaya! Kuch aur chahiye toh batayein 😊", "cta": "none", "rationale": "Generic acknowledgement."}


# ---------------------------------------------------------------------------
# Main compose function
# ---------------------------------------------------------------------------
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
        "city": identity.get("city"),
        "locality": identity.get("locality"),
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
        "review_themes": merchant.get("review_themes", [])[:3],
        "conversation_history": merchant.get("conversation_history", [])[-3:],
    }

    if customer:
        cust_id = customer.get("identity", {})
        ctx["customer_name"] = cust_id.get("name")
        ctx["customer_language"] = cust_id.get("language_pref")
        ctx["customer_state"] = customer.get("state")
        ctx["customer_relationship"] = customer.get("relationship", {})
        ctx["instruction"] = "CUSTOMER-FACING. send_as=merchant_on_behalf. Write FROM merchant perspective. Use customer's name + language."

    prompt = _COMPOSE_SYSTEM + "\n\nContext:\n" + json.dumps(ctx, ensure_ascii=False)

    try:
        result = _call_gemini(prompt)
    except Exception as e:
        # Fallback to rule-based composer
        try:
            from rule_composer import compose_rule_based
            result = compose_rule_based(category, merchant, trigger, customer)
        except Exception:
            result = {
                "body": f"Hi {identity.get('owner_first_name', identity.get('name', 'there'))}, quick update for your listing — want me to help improve visibility? Reply YES.",
                "cta": "binary_yes_no",
                "send_as": "vera",
                "rationale": "Fallback: API unavailable.",
            }

    result.setdefault("suppression_key", _suppression_key(trigger, merchant, customer))
    result.setdefault("template_name", _template_name(trigger.get("kind", ""), result.get("send_as", "vera")))
    result.setdefault("template_params", [identity.get("owner_first_name", ""), result.get("body", "")[:80]])
    return result


# ---------------------------------------------------------------------------
# Reply handler
# ---------------------------------------------------------------------------
def respond(state: dict, merchant_message: str) -> dict:
    # Auto-reply fast path
    if is_auto_reply(merchant_message):
        count = state.get("auto_reply_count", 0) + 1
        if count == 1:
            return {"action": "send", "body": "Looks like an auto-reply 😊 Jab owner dekhen, sirf 'YES' reply karein aage badhne ke liye.", "cta": "binary_yes_no", "rationale": "Auto-reply detected."}
        elif count == 2:
            return {"action": "wait", "wait_seconds": 14400, "rationale": "Auto-reply 2x. Backing off 4h."}
        else:
            return {"action": "end", "rationale": "Auto-reply 3x. No live engagement. Closing."}

    intent = classify_intent(merchant_message)

    # ONLY end on hostile opt-out
    if intent == "hostile":
        return {"action": "end", "rationale": "Merchant/customer explicitly opted out or expressed frustration."}

    # Try Gemini for richer, context-aware replies
    try:
        merchant = state.get("merchant", {})
        trigger = state.get("trigger", {})
        customer = state.get("customer")
        identity = merchant.get("identity", {})

        ctx = {
            "conversation_history": state.get("history", []),
            "latest_reply": merchant_message,
            "intent_signal": intent,
            "from_role": "customer" if customer else "merchant",
            "trigger_kind": trigger.get("kind"),
            "trigger_payload": trigger.get("payload", {}),
            "merchant_name": identity.get("name"),
            "owner_first_name": identity.get("owner_first_name"),
            "languages": identity.get("languages", ["en"]),
            "active_offers": [o for o in merchant.get("offers", []) if o.get("status") == "active"],
            "customer_name": customer.get("identity", {}).get("name") if customer else None,
            "customer_language": customer.get("identity", {}).get("language_pref") if customer else None,
        }

        prompt = _REPLY_SYSTEM + "\n\nContext:\n" + json.dumps(ctx, ensure_ascii=False)
        result = _call_gemini(prompt, max_tokens=400)

        # Validate — never return empty body on send
        if result.get("action") == "send" and not result.get("body", "").strip():
            raise ValueError("LLM returned send with empty body")

        # Prevent LLM from wrongly ending on YES/neutral
        if result.get("action") == "end" and intent in ("yes", "neutral"):
            raise ValueError(f"LLM wrongly ended on intent={intent}")

        return result

    except Exception:
        # Rule-based fallback — always returns a meaningful response
        return _rule_reply(merchant_message, state, intent)
