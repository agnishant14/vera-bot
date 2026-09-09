"""Offline test suite for the Vera bot.

Replays the judge's phases in-process — no fastapi, uvicorn, or live server
needed, since the endpoints are plain callables without the HTTP deps.

    python3 test_bot.py --expanded-dir ../magicpin-ai-challenge/expanded
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import bot

ROOT = Path(__file__).resolve().parent

FAILURES: list[str] = []
CHECKS = 0

# Vocabulary the merchant must never see, mirroring the judge's
# "Exposing internal jargon to merchant: -1" penalty.
JARGON = re.compile(
    r"\b(context|payload|suppression|composer|composition|grounded|grounding|"
    r"fallback|adaptive|template_name|rationale|trigger)\b",
    re.IGNORECASE,
)
QUALIFYING = ("would you", "do you", "can you tell", "what if", "how about")
ACTIONING = ("done", "sending", "draft", "here", "confirm", "proceed", "next")


def check(condition: bool, label: str) -> bool:
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(label)
    return bool(condition)


def default_expanded_dir() -> Path:
    for candidate in (ROOT / "expanded", ROOT.parent / "magicpin-ai-challenge" / "expanded", ROOT.parent / "magicpin" / "expanded"):
        if (candidate / "test_pairs.json").is_file():
            return candidate
    raise FileNotFoundError("Expanded fixtures not found; pass --expanded-dir")


def load_fixtures(expanded: Path) -> dict[str, dict]:
    def load(name: str, key: str) -> dict[str, dict]:
        return {
            item[key]: item
            for path in sorted((expanded / name).glob("*.json"))
            for item in [json.loads(path.read_text(encoding="utf-8"))]
        }

    return {
        "categories": load("categories", "slug"),
        "merchants": load("merchants", "merchant_id"),
        "customers": load("customers", "customer_id"),
        "triggers": load("triggers", "id"),
        "pairs": json.loads((expanded / "test_pairs.json").read_text(encoding="utf-8"))["pairs"],
    }


def push(scope: str, context_id: str, payload: dict, version: int = 1):
    response = SimpleNamespace(status_code=200)
    body = SimpleNamespace(
        scope=scope, context_id=context_id, version=version,
        payload=payload, delivered_at="2026-04-26T09:00:00Z",
    )
    result = bot.push_context(body, response)
    return result, response.status_code


def do_tick(trigger_ids: list[str], now: str = "2026-04-26T09:00:00Z"):
    return bot.tick(SimpleNamespace(now=now, available_triggers=trigger_ids))


def do_reply(conversation_id: str, message: str, turn: int, merchant_id: str | None = None, customer_id: str | None = None):
    return bot.reply(SimpleNamespace(
        conversation_id=conversation_id, merchant_id=merchant_id, customer_id=customer_id,
        from_role="merchant", message=message, received_at="2026-04-26T10:00:00Z", turn_number=turn,
    ))


# ---------------------------------------------------------------------------

def test_context_lifecycle(fx):
    bot.STORE.clear()
    merchant = next(iter(fx["merchants"].values()))
    result, status = push("merchant", merchant["merchant_id"], merchant, version=1)
    check(status == 200 and result.get("accepted"), "context: first push must be accepted")
    check(str(result.get("ack_id", "")).startswith("ack_"), "context: ack_id must be returned")

    result, status = push("merchant", merchant["merchant_id"], merchant, version=1)
    check(status == 409 and not result.get("accepted"), "context: replayed version must return 409")
    check(result.get("current_version") == 1, "context: 409 must report the current version")

    result, status = push("merchant", merchant["merchant_id"], merchant, version=2)
    check(status == 200 and result.get("accepted"), "context: higher version must be accepted")

    result, status = push("nonsense", "x", {}, version=1)
    check(getattr(result, "status_code", 200) == 400, "context: unknown scope must be rejected")


def warmup(fx) -> list[str]:
    bot.STORE.clear()
    for slug, category in fx["categories"].items():
        push("category", slug, category)
    for merchant_id, merchant in fx["merchants"].items():
        push("merchant", merchant_id, merchant)
    for customer_id, customer in fx["customers"].items():
        push("customer", customer_id, customer)
    for trigger_id, trigger in fx["triggers"].items():
        push("trigger", trigger_id, trigger)
    return list(fx["triggers"])


def test_tick(fx):
    trigger_ids = warmup(fx)
    first = do_tick(trigger_ids)
    actions = first["actions"]
    check(len(actions) > 0, "tick: must return at least one action")
    check(len(actions) <= 20, "tick: must respect the fan-out cap")

    for action in actions:
        tid = action.get("trigger_id")
        check(tid in fx["triggers"], f"tick: trigger_id {tid!r} must be a real trigger id")
        check(bool(action.get("body")), "tick: body must be non-empty")
        check(len(action["body"]) <= 900, "tick: body must stay under the length cap")
        check(bool(action.get("cta")), "tick: cta must be set")
        check(action.get("send_as") in {"vera", "merchant_on_behalf"}, "tick: send_as must be valid")
        check(bool(action.get("suppression_key")), "tick: suppression_key must be set")
        check(bool(action.get("rationale")), "tick: rationale must be set")
        check(not JARGON.search(action["body"]), f"tick: internal jargon in body -> {action['body'][:80]!r}")

    entities = [(a.get("merchant_id"), a.get("customer_id")) for a in actions]
    check(len(entities) == len(set(entities)), "tick: must not target the same entity twice in one tick")

    # A second tick may serve entities the fan-out cap deferred, but it must
    # never re-send a suppression key that already went out.
    first_keys = {a["suppression_key"] for a in actions}
    second = do_tick(trigger_ids)
    repeat = first_keys & {a["suppression_key"] for a in second["actions"]}
    check(not repeat, f"tick: already-sent suppression keys must not fire again -> {sorted(repeat)[:3]}")

    # Once every trigger has been served, ticking again must be silent.
    for _ in range(12):
        if not do_tick(trigger_ids)["actions"]:
            break
    check(do_tick(trigger_ids)["actions"] == [], "tick: must go quiet once all triggers are exhausted")
    return actions


def test_auto_reply_trap(fx):
    """The simulator sends the same canned text on four different threads."""
    warmup(fx)
    actions = do_tick(list(fx["triggers"]))["actions"]
    merchant_id = actions[0]["merchant_id"]
    canned = "Thank you for contacting us. Our team will get back to you during business hours."

    outcomes = []
    for index in range(1, 5):
        result = do_reply(f"conv_auto_{index}", canned, turn=index, merchant_id=merchant_id)
        outcomes.append(result["action"])

    check(outcomes[0] == "send", "auto-reply: first canned message may get one short owner-directed reply")
    check("wait" in outcomes or "end" in outcomes[:2], "auto-reply: must back off by the second identical message")
    check(outcomes[-1] == "end", f"auto-reply: must end after repeats, got {outcomes}")
    check(outcomes.count("send") == 1, f"auto-reply: must not keep replying, got {outcomes}")

    # A genuine human message on a fresh thread must not inherit the streak.
    fresh = do_reply("conv_auto_human", "Haan, bhej do", turn=1, merchant_id=merchant_id)
    check(fresh["action"] == "send", "auto-reply: a real human reply must still be answered")


def test_intent_transition(fx):
    warmup(fx)
    actions = do_tick(list(fx["triggers"]))["actions"]
    merchant_id = actions[0]["merchant_id"]

    first = do_reply("conv_intent_1", "What is this about?", turn=1, merchant_id=merchant_id)
    check(first["action"] == "send", "intent: an opening question must be answered")

    second = do_reply("conv_intent_1", "Ok lets do it. Whats next?", turn=2, merchant_id=merchant_id)
    check(second["action"] == "send", "intent: a commitment must be answered, not ended")
    body = second.get("body", "").lower()
    check(any(word in body for word in ACTIONING), f"intent: reply must contain an actioning word -> {body[:100]!r}")
    check(not any(word in body for word in QUALIFYING), f"intent: reply must not re-qualify -> {body[:100]!r}")
    check(not JARGON.search(body), "intent: reply must not expose internal jargon")


def test_hostile(fx):
    warmup(fx)
    actions = do_tick(list(fx["triggers"]))["actions"]
    merchant_id = actions[0]["merchant_id"]

    result = do_reply("conv_hostile_1", "Stop messaging me. This is useless spam.", turn=1, merchant_id=merchant_id)
    check(result["action"] == "end", "hostile: an opt-out must end the conversation")
    check("body" not in result or not result.get("body"), "hostile: nothing further may be sent")
    check(merchant_id in bot.STORE.muted_merchants, "hostile: the merchant must be muted for proactive sends")

    remaining = [tid for tid, t in fx["triggers"].items() if bot._trigger_merchant_id(t) == merchant_id]
    bot.STORE.sent_suppression_keys.clear()
    after = do_tick(remaining)["actions"]
    check(after == [], "hostile: a muted merchant must not be targeted again")


def test_decline_precision(fx):
    warmup(fx)
    actions = do_tick(list(fx["triggers"]))["actions"]
    merchant_id = actions[0]["merchant_id"]

    # A question that merely contains "no" is not a refusal.
    result = do_reply("conv_q_1", "No, what is the price for this?", turn=2, merchant_id=merchant_id)
    check(result["action"] == "send", "decline: a question containing 'no' must still be answered")

    result = do_reply("conv_q_2", "Can you send me the draft later today?", turn=2, merchant_id=merchant_id)
    check(result["action"] == "send", "decline: 'later' inside a request must not read as a refusal")

    result = do_reply("conv_d_1", "Not interested", turn=2, merchant_id=merchant_id)
    check(result["action"] == "end", "decline: a clear refusal must end the conversation")


def test_replay_unknown_conversation(fx):
    """Judge replays open threads the bot never created; context must survive."""
    warmup(fx)
    merchant_id = next(
        mid for mid, m in fx["merchants"].items()
        if m.get("performance", {}).get("views")
    )
    merchant = fx["merchants"][merchant_id]

    result = do_reply("replay_thread_never_seen", "Ok lets do it. Whats next?", turn=2, merchant_id=merchant_id)
    check(result["action"] == "send", "replay: an unknown thread must still be answered")
    body = result.get("body", "")
    anchors = bot._anchor_tokens(merchant)
    check(
        any(token and token in body for token in anchors),
        f"replay: reply must stay grounded in the merchant's own facts -> {body[:120]!r}",
    )
    check(not JARGON.search(body), "replay: reply must not expose internal jargon")


def test_composition_rubric(fx):
    """Lint every canonical pair the way the scoring prompt reads it."""
    for pair in fx["pairs"]:
        merchant = fx["merchants"][pair["merchant_id"]]
        trigger = fx["triggers"][pair["trigger_id"]]
        category = fx["categories"][merchant["category_slug"]]
        customer = fx["customers"].get(pair.get("customer_id"))
        result = bot.compose(category, merchant, trigger, customer)
        body = result["body"]
        tid = pair["test_id"]

        check(bool(body), f"{tid}: body must be non-empty")
        check(len(body) <= 900, f"{tid}: body must stay under the length cap")
        check(not JARGON.search(body), f"{tid}: internal jargon leaked to a human")
        check(not re.search(r"\{|\}|\bNone\b|\bnan\b", body), f"{tid}: unrendered placeholder in body")
        check("  " not in body and " ." not in body, f"{tid}: spacing artefact in body")
        check(body.count("Reply") + body.count("bhejiye") <= 3, f"{tid}: too many competing calls to action")

        for taboo in category.get("voice", {}).get("vocab_taboo", []):
            phrase = taboo.split("(")[0].strip()
            if len(phrase) > 3:
                check(phrase.lower() not in body.lower(), f"{tid}: forbidden phrase {phrase!r} in body")

        if result["send_as"] == "vera":
            check(
                any(token and token in body for token in bot._anchor_tokens(merchant)),
                f"{tid}: merchant message must cite a fact the evaluator can verify",
            )
        else:
            check(bot._customer_name(customer) in body, f"{tid}: customer message must address the customer")
            check(bot._merchant_name(merchant) in body, f"{tid}: customer message must name the merchant")
            check(not re.search(r"\bCTR\b", body), f"{tid}: merchant analytics must not reach a customer")

        # Determinism: the static judge composes the same pair twice.
        check(bot.compose(category, merchant, trigger, customer) == result, f"{tid}: compose must be deterministic")


def test_language_coverage(fx):
    """Hindi-mixed categories must actually produce code-mixed merchant copy."""
    hindi_markers = re.compile(r"\b(aapki|aapke|aapka|main|hai|hain|bhejiye|kar|doon|nahi|se|par|ke liye)\b", re.I)
    seen: dict[str, bool] = {}
    for pair in fx["pairs"]:
        merchant = fx["merchants"][pair["merchant_id"]]
        category = fx["categories"][merchant["category_slug"]]
        trigger = fx["triggers"][pair["trigger_id"]]
        if trigger.get("scope") == "customer":
            continue
        mix = category.get("voice", {}).get("code_mix")
        if mix != "hindi_english_natural":
            continue
        body = bot.compose(category, merchant, trigger, None)["body"]
        seen[merchant["category_slug"]] = seen.get(merchant["category_slug"], False) or bool(hindi_markers.search(body))
    for slug, found in seen.items():
        check(found, f"language: {slug} declares natural code-mix but produced English-only copy")
    check(len(seen) >= 3, "language: expected several code-mixed categories in the canonical pairs")


def test_healthz_and_metadata():
    health = bot.healthz()
    check(health.get("status") == "ok", "healthz: must report ok")
    check("contexts_loaded" in health, "healthz: must report loaded contexts")
    meta = bot.metadata()
    for field in ("team_name", "team_members", "approach", "version"):
        check(bool(meta.get(field)), f"metadata: {field} must be populated")


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline judge emulation for the Vera bot.")
    parser.add_argument("--expanded-dir", type=Path, help="Path to magicpin-ai-challenge/expanded")
    args = parser.parse_args()
    fx = load_fixtures(args.expanded_dir or default_expanded_dir())

    for suite in (
        lambda: test_context_lifecycle(fx),
        lambda: test_tick(fx),
        lambda: test_auto_reply_trap(fx),
        lambda: test_intent_transition(fx),
        lambda: test_hostile(fx),
        lambda: test_decline_precision(fx),
        lambda: test_replay_unknown_conversation(fx),
        lambda: test_composition_rubric(fx),
        lambda: test_language_coverage(fx),
        test_healthz_and_metadata,
    ):
        suite()

    print(f"{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("\nFAILURES:")
        for failure in dict.fromkeys(FAILURES):
            print("  -", failure)
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
