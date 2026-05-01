"""
app.py — Vera Bot HTTP server.

Exposes the 5 endpoints required by the magicpin judge harness:
  GET  /v1/healthz
  GET  /v1/metadata
  POST /v1/context
  POST /v1/tick
  POST /v1/reply
"""

import os
import time
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn

from composer import compose, respond, is_auto_reply

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vera-bot")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Vera Bot", version="1.0.0")
START_TIME = time.time()

# ---------------------------------------------------------------------------
# In-memory state store
# ---------------------------------------------------------------------------
# contexts[scope][context_id] = {"version": int, "payload": dict, "stored_at": str}
contexts: Dict[str, Dict[str, Any]] = {
    "category": {},
    "merchant": {},
    "customer": {},
    "trigger": {},
}

# conversations[conversation_id] = {merchant_id, customer_id, trigger_id, history, auto_reply_count, closed}
conversations: Dict[str, Dict[str, Any]] = {}

# suppression_keys seen — prevents resending same trigger
fired_suppressions: set = set()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ack_id(context_id: str, version: int) -> str:
    return f"ack_{context_id}_v{version}"


def _get_context(scope: str, context_id: str) -> Optional[dict]:
    return contexts.get(scope, {}).get(context_id, {}).get("payload")


def _resolve_merchant_category(merchant: dict) -> Optional[dict]:
    cat_slug = merchant.get("category_slug")
    if not cat_slug:
        # try from identity
        cat_slug = merchant.get("identity", {}).get("category")
    if cat_slug:
        return _get_context("category", cat_slug)
    return None


def _context_counts() -> dict:
    return {scope: len(items) for scope, items in contexts.items()}


# ---------------------------------------------------------------------------
# Endpoint: GET /v1/healthz
# ---------------------------------------------------------------------------
@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": _context_counts(),
    }


# ---------------------------------------------------------------------------
# Endpoint: GET /v1/metadata
# ---------------------------------------------------------------------------
@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Team Vera",
        "team_members": ["Contestant"],
        "model": "claude-sonnet-4-20250514",
        "approach": (
            "4-context composer: trigger-kind routing → Claude prompt with merchant-specific "
            "fact anchoring → post-LLM validation of CTA shape + language. "
            "Auto-reply detection + intent-transition fast-paths for multi-turn."
        ),
        "contact_email": "contestant@example.com",
        "version": "1.0.0",
        "submitted_at": "2026-04-29T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# Endpoint: POST /v1/context
# ---------------------------------------------------------------------------
class ContextRequest(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict
    delivered_at: Optional[str] = None


@app.post("/v1/context")
async def receive_context(req: ContextRequest):
    if req.scope not in contexts:
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": "invalid_scope", "details": f"Unknown scope: {req.scope}"},
        )

    existing = contexts[req.scope].get(req.context_id)
    if existing and existing["version"] >= req.version:
        return JSONResponse(
            status_code=409,
            content={
                "accepted": False,
                "reason": "stale_version",
                "current_version": existing["version"],
            },
        )

    stored_at = _now_iso()
    contexts[req.scope][req.context_id] = {
        "version": req.version,
        "payload": req.payload,
        "stored_at": stored_at,
    }

    log.info(f"Stored {req.scope}/{req.context_id} v{req.version}")
    return {
        "accepted": True,
        "ack_id": _ack_id(req.context_id, req.version),
        "stored_at": stored_at,
    }


# ---------------------------------------------------------------------------
# Endpoint: POST /v1/tick
# ---------------------------------------------------------------------------
class TickRequest(BaseModel):
    now: str
    available_triggers: List[str] = []


@app.post("/v1/tick")
async def tick(req: TickRequest):
    actions = []

    for trigger_id in req.available_triggers:
        trigger = _get_context("trigger", trigger_id)
        if not trigger:
            log.warning(f"Trigger not found: {trigger_id}")
            continue

        # Suppression check
        sup_key = trigger.get("suppression_key", trigger_id)
        if sup_key in fired_suppressions:
            log.info(f"Suppressed trigger: {trigger_id} (key={sup_key})")
            continue

        # Expiry check
        expires = trigger.get("expires_at")
        if expires:
            try:
                exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                now_dt = datetime.fromisoformat(req.now.replace("Z", "+00:00"))
                if now_dt > exp_dt:
                    log.info(f"Expired trigger: {trigger_id}")
                    continue
            except Exception:
                pass

        merchant_id = trigger.get("merchant_id")
        customer_id = trigger.get("customer_id")

        if not merchant_id:
            log.warning(f"Trigger {trigger_id} has no merchant_id")
            continue

        merchant = _get_context("merchant", merchant_id)
        if not merchant:
            log.warning(f"Merchant not found: {merchant_id} for trigger {trigger_id}")
            continue

        category = _resolve_merchant_category(merchant)
        if not category:
            log.warning(f"Category not found for merchant {merchant_id}")
            continue

        customer = None
        if customer_id:
            customer = _get_context("customer", customer_id)

        try:
            result = compose(category, merchant, trigger, customer)
        except Exception as e:
            log.error(f"compose() failed for trigger {trigger_id}: {e}", exc_info=True)
            continue

        # Mark suppression key as fired
        fired_suppressions.add(sup_key)

        # Build conversation_id
        conv_id = f"conv_{trigger_id}_{uuid.uuid4().hex[:8]}"

        # Store conversation state
        conversations[conv_id] = {
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "trigger_id": trigger_id,
            "category": category,
            "merchant": merchant,
            "trigger": trigger,
            "customer": customer,
            "history": [{"role": "vera", "body": result["body"]}],
            "auto_reply_count": 0,
            "closed": False,
            "turn_number": 1,
        }

        action = {
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": result.get("send_as", "vera"),
            "trigger_id": trigger_id,
            "template_name": result.get("template_name", "vera_general_v1"),
            "template_params": result.get("template_params", []),
            "body": result["body"],
            "cta": result.get("cta", "open_ended"),
            "suppression_key": result.get("suppression_key", sup_key),
            "rationale": result.get("rationale", ""),
        }
        actions.append(action)
        log.info(f"Action queued: {conv_id} trigger={trigger_id}")

    return {"actions": actions}


# ---------------------------------------------------------------------------
# Endpoint: POST /v1/reply
# ---------------------------------------------------------------------------
class ReplyRequest(BaseModel):
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str] = None
    from_role: str = "merchant"
    message: str
    received_at: Optional[str] = None
    turn_number: Optional[int] = None


@app.post("/v1/reply")
async def reply(req: ReplyRequest):
    conv = conversations.get(req.conversation_id)

    if not conv:
        # Unknown conversation — try to respond generically
        log.warning(f"Unknown conversation_id: {req.conversation_id}")
        return {
            "action": "end",
            "rationale": "Unknown conversation_id; cannot continue.",
        }

    if conv.get("closed"):
        return {
            "action": "end",
            "rationale": "Conversation already closed.",
        }

    # Update auto-reply count
    if is_auto_reply(req.message):
        conv["auto_reply_count"] = conv.get("auto_reply_count", 0) + 1
    else:
        conv["auto_reply_count"] = 0  # reset on real reply

    # Append to history
    conv["history"].append({"role": req.from_role, "body": req.message})
    conv["turn_number"] = (req.turn_number or conv.get("turn_number", 1)) + 1

    # Build state for responder
    state = {
        "conversation_id": req.conversation_id,
        "merchant_id": req.merchant_id,
        "customer_id": req.customer_id,
        "category": conv.get("category", {}),
        "merchant": conv.get("merchant", {}),
        "trigger": conv.get("trigger", {}),
        "customer": conv.get("customer"),
        "history": conv["history"],
        "auto_reply_count": conv["auto_reply_count"],
        "turn_number": conv["turn_number"],
    }

    try:
        result = respond(state, req.message)
    except Exception as e:
        log.error(f"respond() failed for conv {req.conversation_id}: {e}", exc_info=True)
        return {
            "action": "end",
            "rationale": f"Internal error in response generation: {str(e)}",
        }

    # Update conversation state
    if result.get("action") == "send":
        conv["history"].append({"role": "vera", "body": result.get("body", "")})
    elif result.get("action") in ("end",):
        conv["closed"] = True

    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")
