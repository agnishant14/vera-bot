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
    for candidate in (ROOT / "expanded", ROOT.parent / "magicpin-ai-challenge" / "expanded", ROOT.parent / "magicpin" / "expanded", Path.home() / "Downloads" / "magicpin-ai-challenge" / "expanded"):
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
    check(result["action"] == "wait", "timing: a request for later must defer instead of ending or sending immediately")

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


def test_useful_replies(fx):
    """Exercise real outputs, not just words such as 'done' or 'draft'."""
    import copy

    def open_thread(kind):
        warmup(fx)
        trigger = next(t for t in fx["triggers"].values() if t["kind"] == kind and not t.get("payload", {}).get("placeholder"))
        action = do_tick([trigger["id"]])["actions"][0]
        return action, trigger

    action, trigger = open_thread("regulation_change")
    mid, cid = action["merchant_id"], action["conversation_id"]
    answer = do_reply(cid, "Yes", 1, mid)
    check("1)" in answer.get("body", "") and "2)" in answer["body"], "delivery: acceptance must return an actual checklist")
    check("E-speed" in answer["body"] and "RVG" in answer["body"], "delivery: checklist must include the requested notice's equipment details")
    approved = do_reply(cid, "CONFIRM", 2, mid)
    check(approved.get("body") != answer["body"], "delivery: confirmation must advance without repeating the draft")
    check(bot.STORE.conversations[cid].get("approved"), "delivery: record approval on the thread")
    check(approved.get("cta") != "binary_confirm_edit", "delivery: do not demand confirmation of a confirmation")

    action, trigger = open_thread("perf_dip")
    mid, cid = action["merchant_id"], action["conversation_id"]
    answer = do_reply(cid, "Send me the draft", 1, mid)
    check(bot._merchant_name(fx["merchants"][mid]) in answer["body"], "delivery: post contains merchant name")
    check("Message" in answer["body"] or "message" in answer["body"], "delivery: post contains actual customer call to action")
    price = do_reply(cid, "No, what is the price?", 2, mid)
    check(price["action"] == "send" and "DRAFT" not in price["body"], "question: price question gets an answer, not an invitation to ask again")
    waiting = do_reply(cid, "Yes, but give me 30 minutes", 3, mid)
    check(waiting["action"] == "wait" and waiting.get("wait_seconds") == 1800, "timing: specific delay wins over acceptance")
    tax = do_reply(cid, "Please send me help to file my GST", 4, mid)
    check(tax["action"] == "send" and ("tax professional" in tax["body"]), "scope: action phrasing must not bypass the tax-advice boundary")
    stop_loss = do_reply("loss_question", "How can I stop losing customers?", 1, mid)
    check(stop_loss["action"] == "send" and mid not in bot.STORE.muted_merchants, "intent: 'stop losing customers' is not an opt-out")

    action, trigger = open_thread("active_planning_intent")
    mid, cid = action["merchant_id"], action["conversation_id"]
    join = do_reply(cid, "I want to join magicpin", 1, mid)
    check("registration" in join["body"] and "current numbers" not in join["body"], "onboarding: joining intent gets registration steps")
    voice = do_reply("english_request", "Please send the draft in English", 1, mid)
    check(not re.search(r"\b(bhejiye|kijiye|hai|aapki)\b", voice["body"]), "language: explicit per-turn English preference wins")

    action, trigger = open_thread("research_digest")
    mid, cid = action["merchant_id"], action["conversation_id"]
    slug = fx["merchants"][mid]["category_slug"]
    category = copy.deepcopy(fx["categories"][slug])
    item_id = trigger["payload"]["top_item_id"]
    for item in category["digest"]:
        if item["id"] == item_id:
            item["summary"] = "Updated controlled study enrolled 3,217 adults; no effect was found in the comparison group."
    push("category", slug, category, version=2)
    result = do_reply(cid, "Send me the abstract", 1, mid)
    check("3,217" in result["body"] and "38%" not in result["body"], "adaptation: an open thread uses updated category data")
    merchant = copy.deepcopy(fx["merchants"][mid])
    merchant["performance"]["views"] = 8765
    push("merchant", mid, merchant, version=2)
    do_reply(cid, "How does this apply?", 2, mid)
    check(bot.STORE.conversations[cid]["merchant"]["performance"]["views"] == 8765, "adaptation: an open thread refreshes merchant data")

    updated_trigger = copy.deepcopy(trigger)
    updated_trigger["payload"]["top_item_id"] = "missing_source"
    push("trigger", trigger["id"], updated_trigger, version=2)
    missing = do_reply(cid, "Please share the source?", 3, mid)
    check("missing" in missing["body"] or "nahi hai" in missing["body"], "adaptation: a missing referenced source must not silently select a different study")
    check("3,217" not in missing["body"], "adaptation: missing source must not borrow another finding")


def test_customer_reply_isolation(fx):
    import copy
    warmup(fx)
    trigger = next(t for t in fx["triggers"].values() if t["kind"] == "recall_due" and t.get("payload", {}).get("available_slots"))
    action = do_tick([trigger["id"]])["actions"][0]
    mid, cid, customer_id = action["merchant_id"], action["conversation_id"], action["customer_id"]

    def customer_reply(message, turn):
        return bot.reply(SimpleNamespace(conversation_id=cid, merchant_id=mid, customer_id=customer_id,
            from_role="customer", message=message, received_at="2026-04-26T10:00:00Z", turn_number=turn))

    selected = customer_reply("1", 1)
    check("5 Nov" in selected["body"], "customer: numeric slot selection must resolve to the real time")
    check("CTR" not in selected["body"] and "views" not in selected["body"], "customer: replies must not leak merchant analytics")
    check(bot.STORE.conversations[cid].get("requested_slot"), "customer: retain slot choice for subsequent turns")
    check("not confirmed" in selected["body"] or "confirm nahi" in selected["body"], "customer: request must not be misrepresented as completed booking")
    customer_reply("STOP", 2)
    check(customer_id in bot.STORE.muted_customers, "customer: opt-out is stored for this customer")
    check(mid not in bot.STORE.muted_merchants, "customer: customer opt-out does not mute merchant")
    fresh = copy.deepcopy(trigger)
    fresh["id"], fresh["suppression_key"] = "new_customer_reminder", "new_customer_reminder_key"
    push("trigger", fresh["id"], fresh)
    check(not do_tick([fresh["id"]])["actions"], "customer: opt-out blocks later proactive reminders")
    merchant_trigger = next(t for t in fx["triggers"].values() if t.get("merchant_id") == mid and t["scope"] == "merchant")
    check(bool(do_tick([merchant_trigger["id"]])["actions"]), "customer: merchant remains reachable after customer opt-out")


def test_grounding_and_dedup(fx):
    import copy
    warmup(fx)
    # Two different recipients can be queued with the same suppression key.
    eligible = [t for t in fx["triggers"].values() if t["scope"] == "merchant" and t["kind"] == "perf_dip"][:2]
    ids = []
    for index, original in enumerate(eligible):
        t = copy.deepcopy(original)
        t["id"], t["suppression_key"] = f"duplicate_{index}", "shared_once"
        push("trigger", t["id"], t)
        ids.append(t["id"])
    check(len(do_tick(ids)["actions"]) == 1, "dedup: same suppression key cannot be sent twice within one tick")

    match = next(t for t in fx["triggers"].values() if t["kind"] == "ipl_match_today" and not t["payload"].get("placeholder"))
    merchant = fx["merchants"][match["merchant_id"]]
    category = fx["categories"][merchant["category_slug"]]
    result = bot.compose(category, merchant, match)
    check("Tue-Thu" not in result["body"], "grounding: Sunday match must not promote a Tuesday-Thursday offer")
    planning = next(t for t in fx["triggers"].values() if t["kind"] == "active_planning_intent" and "kids" in str(t["payload"]))
    merchant = fx["merchants"][planning["merchant_id"]]
    result = bot.compose(fx["categories"][merchant["category_slug"]], merchant, planning)
    check("₹499" not in result["body"], "grounding: existing membership price must not become the kids camp fee")
    check("draft" in result["body"].lower(), "planning: first message must deliver usable copy")

    expired = {"kind": "perf_dip", "scope": "merchant", "expires_at": "2026-04-26"}
    check(not bot._trigger_quality(expired, category, None, "2026-04-26T09:00:00Z"), "expiry: date-only expiry is handled without a timezone exception")
    body = bot._finalize("Useful source detail. " * 100 + "Reply YES, or STOP.")
    check(len(body) <= 900 and body.endswith("Reply YES, or STOP."), "length: large injected text must preserve the final CTA")
    category = {"offer_catalog": [{"title": "Flat 30% OFF"}, {"title": "Lunch Thali @ ₹149"}]}
    check(bot._catalog_offer(category) == "Lunch Thali @ ₹149", "offers: prefer service-plus-price over flat discounts")


def test_healthz_and_metadata():
    health = bot.healthz()
    check(health.get("status") == "ok", "healthz: must report ok")
    check("contexts_loaded" in health, "healthz: must report loaded contexts")
    meta = bot.metadata()
    for field in ("team_name", "team_members", "approach", "version"):
        check(bool(meta.get(field)), f"metadata: {field} must be populated")


def test_followup_regressions(fx):
    warmup(fx)
    planning = next(t for t in fx["triggers"].values() if t["kind"] == "active_planning_intent" and not t.get("payload", {}).get("placeholder"))
    action = do_tick([planning["id"]])["actions"][0]
    cid, mid = action["conversation_id"], action["merchant_id"]
    original = bot.STORE.conversations[cid].get("delivered")
    approved = do_reply(cid, "APPROVE", 1, mid)
    check(original == action["body"], "approval: retain the draft delivered in the opening message")
    check(bot.STORE.conversations[cid].get("approved"), "approval: advertised APPROVE command must approve the opening draft")
    check(approved.get("body") != original, "approval: do not repeat the planning pitch after approval")

    warmup(fx)
    match = next(t for t in fx["triggers"].values() if t["kind"] == "ipl_match_today" and not t.get("payload", {}).get("placeholder"))
    action = do_tick([match["id"]])["actions"][0]
    cid, mid = action["conversation_id"], action["merchant_id"]
    draft = do_reply(cid, "Cutoff is 10:30pm", 1, mid)
    check("10:30pm" in draft.get("body", "") and "DC vs MI" in draft["body"], "match: supplied cutoff produces a usable match-specific draft")
    check("Tue-Thu" not in draft["body"], "match: cutoff follow-up must not reintroduce an invalid weekday offer")
    check(bot.STORE.conversations[cid].get("delivered") == draft["body"], "match: retain the draft for approval")
    do_reply(cid, "APPROVE", 2, mid)
    check(bot.STORE.conversations[cid].get("approved"), "match: approve the delivered cutoff draft")
    price = do_reply(cid, "Can you share the price?", 3, mid)
    check("Tue-Thu" not in price["body"] and "subscription" not in price["body"], "match: price question must not quote a weekday deal or Vera subscription")

    warmup(fx)
    action = do_tick([match["id"]])["actions"][0]
    draft = do_reply(action["conversation_id"], "Yes", 1, mid)
    check("DC vs MI" in draft["body"] and "Tue-Thu" not in draft["body"], "match: simple acceptance must keep event details and offer restrictions")

    warmup(fx)
    performance = {**match, "kind": "perf_dip"}
    push("trigger", performance["id"], performance, version=2)
    action = do_tick([performance["id"]])["actions"][0]
    price = do_reply(action["conversation_id"], "Can you share the price?", 1, action["merchant_id"])
    offer = bot._best_offer(fx["merchants"][action["merchant_id"]])
    check(offer in price["body"], "price: conversational 'you' must not turn a service-price question into a subscription question")


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
        lambda: test_useful_replies(fx),
        lambda: test_customer_reply_isolation(fx),
        lambda: test_grounding_and_dedup(fx),
        lambda: test_followup_regressions(fx),
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
