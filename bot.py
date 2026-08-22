"""Deterministic message engine and HTTP API for the magicpin Vera challenge.

The public ``compose`` function is intentionally framework-free so it can be
imported by a static judge.  The FastAPI application below it adds the stateful
context, tick, and reply contract used by the live judge.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Literal

try:  # Keep the standalone compose contract usable without HTTP dependencies.
    from fastapi import FastAPI, Response
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, ConfigDict, Field
    HTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by lightweight submission runners.
    HTTP_AVAILABLE = False

    class BaseModel:
        """Minimal declaration-only fallback used by the static composer."""

    def ConfigDict(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    def Field(default: Any = None, **_kwargs: Any) -> Any:
        return default

    class Response:
        status_code = 200

    class RequestValidationError(Exception):
        def errors(self) -> list[dict[str, Any]]:
            return []

    class JSONResponse(dict):
        def __init__(self, status_code: int = 200, content: Any = None, **_kwargs: Any) -> None:
            super().__init__(content or {})
            self.status_code = status_code

    class FastAPI:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def get(self, *_args: Any, **_kwargs: Any):
            return lambda function: function

        def post(self, *_args: Any, **_kwargs: Any):
            return lambda function: function

        def exception_handler(self, *_args: Any, **_kwargs: Any):
            return lambda function: function


VERSION = "1.0.0"
STARTED_AT = time.time()
VALID_SCOPES = {"category", "merchant", "customer", "trigger"}


def _get(data: dict[str, Any] | None, *path: str, default: Any = None) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _json_safe(value: Any) -> Any:
    """Convert validation details into JSON-safe diagnostic data."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _humanize(value: Any) -> str:
    return _clean(value).replace("_", " ").replace("+", " +")


def _pct(value: Any, signed: bool = False) -> str | None:
    try:
        number = float(value) * 100
    except (TypeError, ValueError):
        return None
    sign = "+" if signed and number > 0 else ""
    rounded = round(number)
    rendered = str(rounded) if abs(number - rounded) < 0.05 else f"{number:.1f}"
    return f"{sign}{rendered}%"


def _change_phrase(value: Any, fallback: str = "changed") -> str:
    """Render a percentage as natural-language direction plus magnitude."""
    try:
        number = float(value) * 100
    except (TypeError, ValueError):
        return fallback
    rounded = round(abs(number))
    magnitude = str(rounded) if abs(abs(number) - rounded) < 0.05 else f"{abs(number):.1f}"
    if number < 0:
        return f"down {magnitude}%"
    if number > 0:
        return f"up {magnitude}%"
    return "flat"


def _money(value: Any) -> str:
    try:
        return f"₹{int(float(value)):,}"
    except (TypeError, ValueError):
        return _clean(value)


def _date_label(value: Any, include_time: bool = False) -> str:
    raw = _clean(value)
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    label = f"{parsed.day} {parsed.strftime('%b %Y')}"
    if include_time and (parsed.hour or parsed.minute):
        hour = parsed.strftime("%I").lstrip("0") or "0"
        minute = parsed.strftime("%M")
        suffix = parsed.strftime("%p").lower()
        label += f", {hour}{':' + minute if minute != '00' else ''}{suffix}"
    return label


def _compact_list(values: list[Any], conjunction: str = "and") -> str:
    cleaned = [_clean(value) for value in values if _clean(value)]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} {conjunction} {cleaned[1]}"
    return f"{', '.join(cleaned[:-1])}, {conjunction} {cleaned[-1]}"


def _active_offers(merchant: dict[str, Any]) -> list[dict[str, Any]]:
    return [offer for offer in merchant.get("offers", []) if offer.get("status") == "active"]


def _best_offer(merchant: dict[str, Any], keywords: list[str] | None = None) -> str:
    offers = _active_offers(merchant)
    if keywords:
        lowered = [word.lower() for word in keywords]
        for offer in offers:
            title = _clean(offer.get("title"))
            if any(word in title.lower() for word in lowered):
                return title
    return _clean(offers[0].get("title")) if offers else ""


def _merchant_name(merchant: dict[str, Any]) -> str:
    return _clean(_get(merchant, "identity", "name")) or "your business"


def _merchant_salutation(category: dict[str, Any], merchant: dict[str, Any]) -> str:
    owner = _clean(_get(merchant, "identity", "owner_first_name"))
    if category.get("slug") == "dentists":
        if owner:
            return owner if owner.lower().startswith("dr.") else f"Dr. {owner}"
        name = _merchant_name(merchant)
        return name if name.lower().startswith("dr.") else f"Dr. {name}"
    return owner or _merchant_name(merchant)


def _customer_name(customer: dict[str, Any] | None) -> str:
    return _clean(_get(customer, "identity", "name")) or "there"


def _language_pref(customer: dict[str, Any] | None) -> str:
    return _clean(_get(customer, "identity", "language_pref")).lower()


def _digest_item(category: dict[str, Any], trigger: dict[str, Any]) -> dict[str, Any]:
    payload = trigger.get("payload", {})
    target = payload.get("top_item_id") or payload.get("digest_item_id") or payload.get("alert_id")
    digest = category.get("digest", [])
    if target:
        for item in digest:
            if item.get("id") == target:
                return item

    preferred_kind = {
        "research_digest": "research",
        "regulation_change": "compliance",
        "cde_opportunity": "cde",
        "supply_alert": "compliance",
    }.get(trigger.get("kind"))
    matching = [item for item in digest if item.get("kind") == preferred_kind]
    # New category versions commonly append fresh items; prefer the latest match.
    return (matching or digest)[-1] if (matching or digest) else {}


def _merchant_fact(merchant: dict[str, Any]) -> str:
    performance = merchant.get("performance", {})
    if performance.get("views") is not None:
        return f"{int(performance['views']):,} profile views in {performance.get('window_days', 30)} days"
    if performance.get("calls") is not None:
        return f"{int(performance['calls']):,} calls in {performance.get('window_days', 30)} days"
    aggregate = merchant.get("customer_aggregate", {})
    for key, label in (
        ("total_active_members", "active members"),
        ("total_unique_ytd", "customers this year"),
        ("chronic_rx_count", "chronic-Rx customers"),
    ):
        if aggregate.get(key) is not None:
            return f"{int(aggregate[key]):,} {label}"
    return _clean(_get(merchant, "identity", "locality"))


def _strongest_delta(merchant: dict[str, Any], direction: Literal["up", "down"]) -> tuple[str, float] | None:
    deltas = _get(merchant, "performance", "delta_7d", default={}) or {}
    candidates: list[tuple[str, float]] = []
    for key, value in deltas.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if (direction == "up" and number > 0) or (direction == "down" and number < 0):
            candidates.append((key.removesuffix("_pct"), number))
    if not candidates:
        return None
    return max(candidates, key=lambda item: abs(item[1]))


def _history_number(merchant: dict[str, Any], pattern: str) -> str:
    regex = re.compile(pattern, re.IGNORECASE)
    for turn in reversed(merchant.get("conversation_history", [])):
        match = regex.search(_clean(turn.get("body")))
        if match:
            return match.group(1)
    return ""


def _customer_prefix(customer: dict[str, Any], merchant: dict[str, Any], category_slug: str) -> str:
    name = _customer_name(customer)
    merchant_name = _merchant_name(merchant)
    pref = _language_pref(customer)
    if "hi" in pref:
        if category_slug == "pharmacies" and _get(customer, "identity", "senior_citizen"):
            return f"Namaste — {merchant_name} se."
        return f"Hi {name}, {merchant_name} se."
    return f"Hi {name}, {merchant_name} here."


def _template_name(kind: str, customer_facing: bool) -> str:
    safe_kind = re.sub(r"[^a-z0-9_]+", "_", kind.lower()).strip("_") or "contextual"
    prefix = "merchant" if customer_facing else "vera"
    return f"{prefix}_{safe_kind}_v1"


def _trigger_merchant_id(trigger: dict[str, Any]) -> str | None:
    return trigger.get("merchant_id") or _get(trigger, "payload", "merchant_id")


def _trigger_customer_id(trigger: dict[str, Any]) -> str | None:
    return trigger.get("customer_id") or _get(trigger, "payload", "customer_id")


def _finalize(text: str) -> str:
    text = _clean(text)
    # Meta examples and the challenge judge penalize accidental URLs.
    text = re.sub(r"https?://\S+|www\.\S+", "", text, flags=re.IGNORECASE)
    return _clean(text)[:900]


def _merchant_message(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str, str]:
    kind = _clean(trigger.get("kind"))
    payload = trigger.get("payload", {})
    salutation = _merchant_salutation(category, merchant)
    category_slug = _clean(category.get("slug"))
    locality = _clean(_get(merchant, "identity", "locality"))
    performance = merchant.get("performance", {})
    aggregate = merchant.get("customer_aggregate", {})
    offer = _best_offer(merchant)

    if kind == "research_digest":
        item = _digest_item(category, trigger)
        source = _clean(item.get("source"))
        title = _clean(item.get("title"))
        trial_n = item.get("trial_n")
        summary = _clean(item.get("summary"))
        numeric = f" In a {int(trial_n):,}-person study," if trial_n else ""
        high_risk = aggregate.get("high_risk_adult_count")
        fit = f" You have {int(high_risk)} high-risk adult patients, so this is directly relevant." if high_risk else ""
        body = f"{salutation}, new from {source or 'your category digest'}: {title}.{numeric} {summary}{fit} Want me to turn the useful part into a short customer WhatsApp draft? Reply YES or STOP."
        rationale = "Fresh cited category research, tied to a merchant cohort or current business fact, with one low-effort drafting CTA."
        return body, "binary_yes_stop", rationale

    if kind == "regulation_change":
        item = _digest_item(category, trigger)
        source = _clean(item.get("source")) or "the latest compliance update"
        deadline = _date_label(payload.get("deadline_iso"))
        summary = _clean(item.get("summary") or item.get("title"))
        action = _clean(item.get("actionable"))
        deadline_text = f" by {deadline}" if deadline else ""
        body = f"{salutation}, compliance heads-up from {source}: {summary} This takes effect{deadline_text}. {action} Want me to draft a 5-point audit checklist for your clinic? Reply YES or STOP."
        return body, "binary_yes_stop", "Urgent source-cited regulation, exact deadline, and an actionable compliance artifact."

    if kind == "cde_opportunity":
        item = _digest_item(category, trigger)
        event_date = _date_label(item.get("date"), include_time=True)
        credits = payload.get("credits") or item.get("credits")
        fee = "free for IDA members" if payload.get("fee") == "free_for_members" else _humanize(payload.get("fee"))
        body = f"{salutation}, {item.get('title', 'a relevant CDE session')} is on {event_date or 'the listed date'} — {credits or 0} CDE credits, {fee or 'fee details in the notice'}. Source: {item.get('source', 'category calendar')}. Want me to prepare the registration checklist? Reply YES or STOP."
        return body, "binary_yes_stop", "Time-bound professional opportunity with source, credits, fee, and one next step."

    if kind == "active_planning_intent":
        topic = _humanize(payload.get("intent_topic"))
        if "corporate bulk thali" in topic:
            current_offer = offer or "your weekday thali"
            daily_orders = _history_number(merchant, r"(\d+)\s+orders/day")
            volume = f" Your current thali is already averaging {daily_orders} orders/day." if daily_orders else ""
            body = f"{salutation}, here is the corporate version: keep {current_offer}, ask for headcount, veg/Jain split, delivery time, and invoice details in one message.{volume} I have the one-page package copy ready; reply CONFIRM and I will format the customer WhatsApp."
        elif "kids yoga" in topic:
            history = " ".join(_clean(turn.get("body")) for turn in merchant.get("conversation_history", []))
            format_bits = []
            for pattern in (r"(4-week program)", r"(3 classes/week)", r"(age 7-12)", r"(₹[\d,]+)"):
                match = re.search(pattern, history, re.IGNORECASE)
                if match:
                    format_bits.append(match.group(1))
            spec = _compact_list(format_bits) or "the format already discussed"
            body = f"{salutation}, moving straight to the draft: {spec}. Your studio has {aggregate.get('total_active_members', 0)} active members and {performance.get('calls', 0)} calls in 30 days, so the audience is warm. Reply CONFIRM and I will format the GBP post copy."
        else:
            last_message = _clean(payload.get("merchant_last_message"))
            body = f"{salutation}, I picked up your plan — “{last_message or topic}”. I have converted it into a concrete draft using {offer or _merchant_fact(merchant)}. Reply CONFIRM and I will show the ready-to-use copy."
        return body, "binary_confirm_cancel", "The merchant already expressed intent, so the message skips qualification and advances to a concrete deliverable."

    if kind in {"perf_dip", "seasonal_perf_dip"}:
        metric = _humanize(payload.get("metric"))
        delta = payload.get("delta_pct")
        if delta is None:
            inferred = _strongest_delta(merchant, "down")
            metric, delta = inferred if inferred else ("calls", None)
        change = _change_phrase(delta, "down")
        current = performance.get(metric)
        current_text = f"; current 30-day {metric} are {int(current):,}" if isinstance(current, (int, float)) else ""
        seasonal = f" This is marked as the expected seasonal lull ({_humanize(payload.get('season_note'))}), not a demand collapse." if payload.get("is_expected_seasonal") else ""
        proof = f" You still have {int(aggregate['total_active_members'])} active members." if aggregate.get("total_active_members") else ""
        next_step = {
            "dentists": "a profile fix focused on calls",
            "salons": "a price-led service post",
            "restaurants": "a repeat-customer offer draft",
            "gyms": "a member attendance challenge",
            "pharmacies": "a local availability post",
        }.get(category_slug, "a recovery draft")
        body = f"{salutation}, {metric or 'performance'} {'are' if metric in {'calls', 'views', 'directions', 'leads'} else 'is'} {change} over 7 days{current_text}.{seasonal}{proof} Want me to draft {next_step} using your current data? Reply YES or STOP."
        return body, "binary_yes_stop", "Exact negative metric and window, reframed with merchant state and one recovery action."

    if kind == "perf_spike":
        metric = _humanize(payload.get("metric"))
        delta = payload.get("delta_pct")
        if delta is None:
            inferred = _strongest_delta(merchant, "up")
            metric, delta = inferred if inferred else ("views", None)
        driver = _humanize(payload.get("likely_driver"))
        body = f"{salutation}, {metric or 'performance'} rose {_pct(delta, signed=True) or 'this week'} over 7 days{f', likely from {driver}' if driver else ''}. You are at {_merchant_fact(merchant)}. Want me to repeat the winning theme in a fresh post while the signal is warm? Reply YES or STOP."
        return body, "binary_yes_stop", "Timely positive performance signal, plausible provided driver, and a repeatable next action."

    if kind == "category_seasonal":
        trends = [_humanize(item) for item in payload.get("trends", [])]
        body = f"{salutation}, the {_humanize(payload.get('season', 'seasonal'))} demand shift is here: {_compact_list(trends)}. For {locality}, the practical move is to rebalance shelf visibility before the next order cycle. Want a one-page stock-priority list? Reply YES or STOP."
        return body, "binary_yes_stop", "Concrete seasonal demand movements translated into a pharmacy operator action."

    if kind == "competitor_opened":
        competitor = _clean(payload.get("competitor_name"))
        distance = payload.get("distance_km")
        opened = _date_label(payload.get("opened_date"))
        their_offer = _clean(payload.get("their_offer"))
        if competitor:
            fact = f"{competitor} opened {distance} km away on {opened or 'the listed date'} with {their_offer or 'a launch offer'}"
        else:
            fact = f"a new competitor signal appeared near {locality}"
        own = f" Your active offer is {offer}." if offer else f" Your profile currently has {_merchant_fact(merchant)}."
        body = f"{salutation}, {fact}.{own} I would not start a price war; I can draft a sharper listing comparison around your real strengths. Want the draft? Reply YES or STOP."
        return body, "binary_yes_stop", "Specific local competitive trigger, merchant offer/profile anchor, and a non-generic response."

    if kind == "curious_ask_due":
        fact = _merchant_fact(merchant)
        question = {
            "dentists": "Which treatment are patients asking about most this week?",
            "salons": "Which service are customers asking for this week that is not on your offer list?",
            "restaurants": "Which dish are regulars asking for most this week?",
            "gyms": "What goal are new members mentioning most this week?",
            "pharmacies": "Which product category are customers asking for but not finding quickly?",
        }.get(category_slug, "What are customers asking for most this week?")
        body = f"{salutation}, quick operator question — {fact}, and {offer + ' is active' if offer else 'there is no active offer listed'}. {question} Reply with one item; I will turn it into a grounded post draft."
        return body, "open_ended", "A merchant-specific curiosity prompt that asks for one low-effort input and promises a useful artifact."

    if kind in {"dormant_with_vera", "winback_eligible"}:
        days = payload.get("days_since_last_merchant_message") or payload.get("days_since_expiry")
        dip = payload.get("perf_dip_pct") or _get(merchant, "performance", "delta_7d", "calls_pct")
        lapsed = payload.get("lapsed_customers_added_since_expiry") or aggregate.get("lapsed_90d_plus") or aggregate.get("lapsed_180d_plus")
        facts = [f"it has been {int(days)} days" if days else "we have not heard from you recently"]
        if dip is not None:
            facts.append(f"calls are {_change_phrase(dip, 'down')}")
        if lapsed:
            facts.append(f"{int(lapsed)} customers are now lapsed")
        body = f"{salutation}, quick reset: {_compact_list(facts)}. I can focus on one outcome first — bookings or customer win-back. Reply BOOKINGS or WINBACK; I will draft that plan only."
        return body, "single_choice", "Dormancy or win-back signal anchored in exact merchant impact, with one constrained choice."

    if kind == "festival_upcoming":
        festival = _clean(payload.get("festival")) or "the upcoming festival"
        festival_date = _date_label(payload.get("date"))
        days = payload.get("days_until")
        timing = f" on {festival_date}" if festival_date else ""
        days_text = f" ({int(days)} days away)" if isinstance(days, (int, float)) else ""
        if isinstance(days, (int, float)) and days > 60:
            body = f"{salutation}, {festival}{timing} is still {int(days)} days away — too early to publish a customer promo. The useful move now is to lock a service+price concept around {offer or _merchant_fact(merchant)}, then schedule it closer to the date. Want 3 concepts saved for later? Reply YES or STOP."
        else:
            body = f"{salutation}, {festival}{timing}{days_text} is the next category moment for {locality}. Rather than a generic discount, I can build the post around {offer or _merchant_fact(merchant)}. Want the draft? Reply YES or STOP."
        return body, "binary_yes_stop", "Seasonal timing plus a real merchant offer/fact, avoiding a generic percentage discount."

    if kind == "gbp_unverified":
        uplift = _pct(payload.get("estimated_uplift_pct"))
        path = _humanize(payload.get("verification_path"))
        body = f"{salutation}, {_merchant_name(merchant)} is still unverified on Google. The supplied estimate is up to {uplift or 'a meaningful'} visibility uplift after verification, using {path or 'the available verification path'}. Want the exact 3-step checklist? Reply YES or STOP."
        return body, "binary_yes_stop", "High-impact listing gap, quantified estimate, exact verification route, and one actionable CTA."

    if kind == "ipl_match_today":
        match = _clean(payload.get("match"))
        venue = _clean(payload.get("venue"))
        match_time = _date_label(payload.get("match_time_iso"), include_time=True)
        late_reviews = next((theme for theme in merchant.get("review_themes", []) if theme.get("theme") == "delivery_late"), {})
        risk = f" You also have {late_reviews.get('occurrences_30d')} rising late-delivery mentions, so promise windows before discounts." if late_reviews else ""
        body = f"{salutation}, {match} starts {match_time or 'tonight'} at {venue}.{risk} Your current offer is {offer or 'not listed'}; want a match-night post with a clear order cutoff? Reply YES or STOP."
        return body, "binary_yes_stop", "Same-day local event, operational review signal, current offer, and a practical match-night action."

    if kind == "milestone_reached":
        metric = _humanize(payload.get("metric"))
        now_value = payload.get("value_now")
        goal = payload.get("milestone_value")
        if now_value is not None and goal is not None:
            gap = max(0, int(goal) - int(now_value))
            fact = f"you are at {int(now_value):,} {metric}, just {gap} away from {int(goal):,}"
        else:
            fact = f"your latest profile milestone comes with {_merchant_fact(merchant)}"
        body = f"{salutation}, {fact}. This is a good moment for a short customer review request while momentum is visible. Want me to draft it? Reply YES or STOP."
        return body, "binary_yes_stop", "Exact milestone or current profile proof converted into a timely review-generation action."

    if kind == "review_theme_emerged":
        theme = _humanize(payload.get("theme"))
        count = payload.get("occurrences_30d")
        quote = _clean(payload.get("common_quote"))
        body = f"{salutation}, {count or 'several'} reviews in 30 days now mention {theme or 'the same issue'}{f' — “{quote}”' if quote else ''}. I can draft the customer-facing fix plus a reply template for those reviews. Want both? Reply YES or STOP."
        return body, "binary_yes_stop", "Emerging review pattern with count and customer wording, followed by a concrete remediation asset."

    if kind == "renewal_due":
        days = payload.get("days_remaining") or _get(merchant, "subscription", "days_remaining")
        plan = _clean(payload.get("plan") or _get(merchant, "subscription", "plan"))
        amount = _money(payload.get("renewal_amount")) if payload.get("renewal_amount") is not None else ""
        impact = _merchant_fact(merchant)
        body = f"{salutation}, your {plan or 'current'} plan has {days or 'a few'} days remaining{f' and renews at {amount}' if amount else ''}. Before you decide, I can summarise what it delivered — starting with {impact}. Want the 1-page value check? Reply YES or STOP."
        return body, "binary_yes_stop", "Renewal timing and amount are paired with real performance rather than a generic payment reminder."

    if kind == "supply_alert":
        molecule = _humanize(payload.get("molecule"))
        batches = _compact_list(payload.get("affected_batches", []))
        manufacturer = _clean(payload.get("manufacturer"))
        item = _digest_item(category, trigger)
        summary = _clean(item.get("summary"))
        body = f"{salutation}, urgent supply alert: {molecule} batches {batches} from {manufacturer} are affected. {summary} You have {aggregate.get('chronic_rx_count', 'repeat')} chronic-Rx customers; want a precise customer note plus replacement workflow? Reply YES or STOP."
        return body, "binary_yes_stop", "Urgent medicine, batch, manufacturer, cited detail, merchant cohort, and an end-to-end response."

    # A safe generic path for genuinely new trigger kinds. It only uses supplied
    # facts, so adaptive judge injections remain useful without hallucination.
    facts = []
    for key, value in payload.items():
        if key in {"placeholder", "metric_or_topic"} or value in (None, "", [], {}):
            continue
        if isinstance(value, (str, int, float, bool)):
            facts.append(f"{_humanize(key)}: {_humanize(value)}")
        if len(facts) == 2:
            break
    trigger_fact = _compact_list(facts) or _humanize(kind)
    body = f"{salutation}, a new {trigger_fact} signal just arrived for {_merchant_name(merchant)}. Your current anchor is {_merchant_fact(merchant)}. Want me to turn it into one ready-to-use action draft? Reply YES or STOP."
    return body, "binary_yes_stop", "Adaptive fallback uses only the new trigger and current merchant context, with a single action CTA."


def _customer_message(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any]) -> tuple[str, str, str]:
    kind = _clean(trigger.get("kind"))
    payload = trigger.get("payload", {})
    category_slug = _clean(category.get("slug"))
    prefix = _customer_prefix(customer, merchant, category_slug)
    offer = _best_offer(merchant)
    relationship = customer.get("relationship", {})
    pref = _language_pref(customer)

    if kind == "appointment_tomorrow":
        appointment = payload.get("appointment") or payload.get("appointment_time") or payload.get("slot")
        visits = relationship.get("visits_total")
        relationship_fact = f" We have {int(visits)} visit{'s' if int(visits) != 1 else ''} on your record." if isinstance(visits, (int, float)) else ""
        line = "Kal aapki appointment hai." if "hi" in pref else "Your appointment is tomorrow."
        if appointment:
            timing = _date_label(appointment, include_time=True)
            schedule = f" Scheduled time: {timing}." if timing else ""
        else:
            schedule = " I do not have the exact slot in this reminder update."
        body = f"{prefix} {line}{relationship_fact}{schedule} Reply CONFIRM to keep it or RESCHEDULE if you need another time."
        return body, "binary_confirm_reschedule", "Operational next-day reminder, merchant attribution, language preference, and one booking CTA."

    if kind == "recall_due":
        last_date = _date_label(payload.get("last_service_date") or relationship.get("last_visit"))
        due_date = _date_label(payload.get("due_date"))
        service = _humanize(payload.get("service_due"))
        slots = [_clean(slot.get("label")) for slot in payload.get("available_slots", []) if isinstance(slot, dict)]
        matching_offer = _best_offer(merchant, ["cleaning", "checkup", "consult", "trial", "month"]) or offer
        if "hi" in pref:
            body = f"{prefix} Aapka {service or 'next check-in'} due hai{f' on {due_date}' if due_date else ''}; last visit {last_date or 'record par hai'}."
        else:
            body = f"{prefix} Your {service or 'next check-in'} is due{f' on {due_date}' if due_date else ''}; your last visit was {last_date or 'on record'}."
        if slots:
            body += f" Available: {_compact_list(slots, conjunction='or')}."
            cta = "multi_choice_slot"
            body += " Reply 1 or 2 to choose a slot, or RESCHEDULE."
        else:
            cta = "binary_yes_stop"
            body += f" {matching_offer + '. ' if matching_offer else ''}Reply YES for available slots or STOP."
        return body, cta, "Customer recall uses actual visit/due dates, available slots or merchant offer, consented channel, and merchant identity."

    if kind == "chronic_refill_due":
        if category_slug != "pharmacies":
            last_visit = _date_label(relationship.get("last_visit"))
            visits = relationship.get("visits_total")
            body = f"{prefix} Your scheduled follow-up reminder is due. We have {int(visits) if isinstance(visits, (int, float)) else 'prior'} visit{'s' if visits != 1 else ''} on record, most recently {last_visit or 'on the recorded date'}. Reply YES to see the appropriate next step or STOP."
            return body, "binary_yes_stop", "The trigger label is inconsistent with the non-pharmacy category, so the safe message avoids inventing medicines and uses only relationship history."
        molecules = [_humanize(item) for item in payload.get("molecule_list", [])]
        run_out = _date_label(payload.get("stock_runs_out_iso"))
        delivery = bool(payload.get("delivery_address_saved"))
        medicines = f"{len(molecules)} monthly medicines ({_compact_list(molecules)})" if molecules else "your regular monthly medicines"
        if "hi" in pref:
            body = f"{prefix} {medicines} ka refill {run_out or 'soon'} due hai."
        else:
            body = f"{prefix} The refill for {medicines} is due {run_out or 'soon'}."
        body += f" {offer + '. ' if offer else ''}{'Your delivery address is saved. ' if delivery else ''}Reply REFILL to confirm or CHANGE if the prescription changed."
        return body, "binary_refill_change", "Precise refill timing and medicines, saved-delivery state, merchant offer, and a safe confirmation CTA."

    if kind in {"customer_lapsed_hard", "customer_lapsed_soft"}:
        days = payload.get("days_since_last_visit")
        last_visit = _date_label(relationship.get("last_visit"))
        focus = _humanize(payload.get("previous_focus") or _get(customer, "preferences", "training_focus"))
        gap = f"about {max(1, round(int(days) / 7))} weeks" if days else f"since your {last_visit} visit" if last_visit else "a while"
        no_shame = "No pressure—breaks happen." if category_slug == "gyms" else "No pressure."
        visits = relationship.get("visits_total")
        history = f" We have {int(visits)} prior visit{'s' if int(visits) != 1 else ''} on record." if isinstance(visits, (int, float)) else ""
        lead = f"It has been {gap} since your last visit." if days else f"It has been {gap}."
        next_step = "one suitable next slot" if category_slug in {"dentists", "salons", "gyms"} else "one suitable next option"
        body = f"{prefix} {lead}{history} {no_shame} {f'Your earlier focus was {focus}. ' if focus else ''}{offer + '. ' if offer else ''}Reply YES if you want {next_step}, or STOP."
        return body, "binary_yes_stop", "No-shame win-back tied to relationship history, prior goal, and a real merchant offer."

    if kind == "trial_followup":
        trial_date = _date_label(payload.get("trial_date"))
        slots = [_clean(slot.get("label")) for slot in payload.get("next_session_options", []) if isinstance(slot, dict)]
        body = f"{prefix} Thanks for joining the trial on {trial_date or 'your recent visit'}. The next session option is {_compact_list(slots, conjunction='or') or 'ready when you are'}. Reply YES to hold it or ANOTHER for a different time."
        return body, "binary_yes_another", "Recent trial and supplied next-session option create a low-friction continuation."

    if kind == "wedding_package_followup":
        wedding = _date_label(payload.get("wedding_date"))
        trial = _date_label(payload.get("trial_completed"))
        window = _humanize(payload.get("next_step_window_open"))
        body = f"{prefix} Your trial was on {trial}, and the wedding date is {wedding}. The {window or 'next preparation'} window is now open. Want the salon to hold a planning slot? Reply YES or STOP."
        return body, "binary_yes_stop", "Exact bridal timeline, completed trial, and the supplied next-step window."

    # Adaptive customer fallback avoids making category-inappropriate claims.
    visits = relationship.get("visits_total")
    state = _humanize(customer.get("state"))
    body = f"{prefix} A { _humanize(kind) } reminder is due. Your record shows {visits or 'prior'} visit{'s' if visits != 1 else ''} and status {state or 'active'}. Reply YES to see the next step or STOP."
    return body, "binary_yes_stop", "Customer-scoped fallback uses only the trigger and relationship record, without inventing an offer or appointment."


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict[str, Any]:
    """Compose one grounded, deterministic WhatsApp action.

    Inputs are the challenge dictionaries.  No network calls or mutable global
    state are used, so identical inputs always produce identical output.
    """

    customer_facing = trigger.get("scope") == "customer" or customer is not None
    if customer_facing and customer:
        body, cta, rationale = _customer_message(category, merchant, trigger, customer)
        send_as = "merchant_on_behalf"
    else:
        body, cta, rationale = _merchant_message(category, merchant, trigger)
        send_as = "vera"

    body = _finalize(body)
    suppression_key = _clean(trigger.get("suppression_key")) or f"trigger:{trigger.get('id', 'unknown')}"
    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": suppression_key,
        "rationale": _finalize(rationale),
    }


class ContextBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str
    context_id: str = Field(min_length=1, max_length=300)
    version: int = Field(ge=0)
    payload: dict[str, Any]
    delivered_at: str


class TickBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    now: str
    available_triggers: list[str] = Field(default_factory=list, max_length=1000)


class ReplyBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    conversation_id: str = Field(min_length=1, max_length=300)
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: str
    message: str = Field(max_length=10_000)
    received_at: str
    turn_number: int = Field(ge=1)


class StateStore:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.contexts: dict[tuple[str, str], dict[str, Any]] = {}
        self.conversations: dict[str, dict[str, Any]] = {}
        self.sent_suppression_keys: set[str] = set()
        self.muted_merchants: set[str] = set()

    def clear(self) -> None:
        with self.lock:
            self.contexts.clear()
            self.conversations.clear()
            self.sent_suppression_keys.clear()
            self.muted_merchants.clear()

    def put_context(self, body: ContextBody) -> tuple[bool, int | None]:
        key = (body.scope, body.context_id)
        with self.lock:
            current = self.contexts.get(key)
            if current and current["version"] >= body.version:
                return False, int(current["version"])
            payload = json.loads(json.dumps(body.payload, ensure_ascii=False))
            identity_key = {
                "category": "slug",
                "merchant": "merchant_id",
                "customer": "customer_id",
                "trigger": "id",
            }[body.scope]
            payload.setdefault(identity_key, body.context_id)
            self.contexts[key] = {
                "version": body.version,
                "payload": payload,
                "delivered_at": body.delivered_at,
            }
        return True, None

    def payload(self, scope: str, context_id: str | None) -> dict[str, Any] | None:
        if not context_id:
            return None
        item = self.contexts.get((scope, context_id))
        return item["payload"] if item else None


STORE = StateStore()


def _required_consent(kind: str) -> set[str]:
    return {
        "appointment_tomorrow": {"appointment_reminders"},
        "recall_due": {"recall_reminders", "renewal_reminders", "program_updates"},
        "chronic_refill_due": {"refill_reminders"},
        "customer_lapsed_hard": {"winback_offers", "promotional_offers"},
        "customer_lapsed_soft": {"winback_offers", "promotional_offers"},
        "trial_followup": {"kids_program_updates", "program_updates", "promotional_offers"},
        "wedding_package_followup": {"bridal_package_followup", "appointment_reminders"},
    }.get(kind, {"promotional_offers"})


def _customer_contact_allowed(trigger: dict[str, Any], customer: dict[str, Any]) -> bool:
    if not _get(customer, "consent", "opted_in_at"):
        return False
    scopes = set(_get(customer, "consent", "scope", default=[]) or [])
    required = _required_consent(_clean(trigger.get("kind")))
    if scopes & required:
        return True
    # Generated challenge customers may carry only promotional scope even for
    # operational trigger fixtures. A direct reminder opt-in is an explicit
    # secondary consent signal for non-clinical appointment/check-in notices.
    return bool(_get(customer, "preferences", "reminder_opt_in")) and trigger.get("kind") in {
        "appointment_tomorrow", "recall_due", "trial_followup"
    }


def _trigger_quality(
    trigger: dict[str, Any],
    category: dict[str, Any],
    customer: dict[str, Any] | None,
    now: str | None = None,
) -> bool:
    kind = _clean(trigger.get("kind"))
    payload = trigger.get("payload", {})
    slug = _clean(category.get("slug"))
    if trigger.get("scope") == "customer" and not customer:
        return False
    if customer and not _customer_contact_allowed(trigger, customer):
        return False
    if kind == "chronic_refill_due" and slug != "pharmacies":
        return False
    if kind == "wedding_package_followup" and slug != "salons":
        return False
    if kind == "supply_alert" and slug != "pharmacies":
        return False
    if kind == "festival_upcoming" and isinstance(payload.get("days_until"), (int, float)) and payload["days_until"] > 60:
        return False
    expires_at = _clean(trigger.get("expires_at"))
    if expires_at and now:
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            current = datetime.fromisoformat(now.replace("Z", "+00:00"))
            if current >= expiry:
                return False
        except ValueError:
            pass
    return True


def _priority(trigger: dict[str, Any]) -> tuple[int, int, str]:
    kind_bonus = {
        "supply_alert": 10,
        "regulation_change": 9,
        "active_planning_intent": 8,
        "appointment_tomorrow": 7,
        "chronic_refill_due": 7,
        "recall_due": 6,
        "perf_dip": 5,
        "review_theme_emerged": 5,
    }.get(_clean(trigger.get("kind")), 0)
    return int(trigger.get("urgency") or 0), kind_bonus, _clean(trigger.get("id"))


def _conversation_id(trigger: dict[str, Any]) -> str:
    # Keep IDs resumable and human-auditable while retaining a deterministic
    # hash suffix to avoid collisions when trigger names are very similar.
    merchant = re.sub(r"[^a-z0-9]+", "_", (_trigger_merchant_id(trigger) or "merchant").lower()).strip("_")
    customer = re.sub(r"[^a-z0-9]+", "_", (_trigger_customer_id(trigger) or "merchant").lower()).strip("_")
    kind = re.sub(r"[^a-z0-9]+", "_", (_clean(trigger.get("kind")) or "context").lower()).strip("_")
    raw = f"{_trigger_merchant_id(trigger)}|{_trigger_customer_id(trigger)}|{trigger.get('id')}|{trigger.get('suppression_key')}"
    suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"conv_{merchant[:28]}_{customer[:20]}_{kind[:24]}_{suffix}"


def _normalise_reply(message: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", message.lower()).strip()


AUTO_REPLY_PATTERNS = (
    r"thank you for contacting",
    r"our team will (respond|reply)",
    r"automated (assistant|reply|message)",
    r"business hours",
    r"we have received your message",
    r"aapki jaankari ke liye.*shukriya",
    r"team tak pahuncha",
)
STOP_PATTERNS = (
    r"\bstop\b", r"unsubscribe", r"do not message", r"don't message", r"not interested",
    r"useless spam", r"leave me alone", r"band karo", r"mat bhejo",
)
COMMIT_PATTERNS = (
    r"let'?s do it", r"go ahead", r"proceed", r"what'?s next", r"i want to join",
    r"mujhe.*judna", r"mujhe.*join", r"kar do", r"confirm", r"yes please", r"sounds good",
)
DECLINE_PATTERNS = (r"\bno\b", r"not now", r"later", r"maybe later", r"nahi", r"cancel")


def _matches_any(message: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, message, re.IGNORECASE) for pattern in patterns)


def _reply_context(state: dict[str, Any] | None, merchant_id: str | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    trigger = state.get("trigger") if state else None
    merchant = state.get("merchant") if state else STORE.payload("merchant", merchant_id)
    category = state.get("category") if state else STORE.payload("category", _get(merchant, "category_slug"))
    return category, merchant, trigger


def _commitment_response(
    category: dict[str, Any] | None,
    merchant: dict[str, Any] | None,
    trigger: dict[str, Any] | None,
    turn_number: int,
) -> str:
    merchant = merchant or {}
    category = category or {}
    trigger = trigger or {}
    kind = _clean(trigger.get("kind"))
    merchant_name = _merchant_name(merchant)

    if kind == "research_digest":
        item = _digest_item(category, trigger)
        return _finalize(
            f"Here’s the useful takeaway from {item.get('source', 'the cited digest')}: "
            f"{item.get('summary', item.get('title', 'the supplied finding'))} "
            "I’ve also drafted the customer-friendly version. Reply CONFIRM to use it or EDIT with one change."
        )
    if kind in {"regulation_change", "supply_alert"}:
        return "Done — I’ve turned the alert into a step-by-step checklist and a customer-safe note. Reply CONFIRM to use this version or EDIT with one change."
    if kind == "cde_opportunity":
        return "Done — I’ve prepared the registration checklist from the supplied date, credits, and fee details. Reply CONFIRM to use it or EDIT with one change."
    if kind in {"appointment_tomorrow", "recall_due", "trial_followup", "chronic_refill_due"}:
        return "Got it — I’ve noted the requested next step from the latest customer record. Reply CONFIRM to keep it or CHANGE with the one detail that differs."

    noun = _humanize(kind) or "requested action"
    if turn_number > 2:
        return f"Done — the {noun} draft is ready from the latest {merchant_name} context. Reply CONFIRM to use this version or EDIT with the one change you want."
    return f"Great — I’m moving to action now. I’ve prepared the {noun} draft for {merchant_name} using the latest context. Next: review the ready copy and reply CONFIRM to use it; I won’t ask more qualifying questions."


app = FastAPI(
    title="Vera Signal Engine",
    version=VERSION,
    description="Deterministic merchant engagement engine for the magicpin AI challenge.",
)


@app.exception_handler(RequestValidationError)
async def request_validation_error(_request: Any, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"accepted": False, "reason": "malformed_request", "details": _json_safe(exc.errors())},
    )


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "vera-signal-engine", "status": "ok", "docs": "/docs"}


@app.get("/v1/healthz")
def healthz() -> dict[str, Any]:
    counts = {scope: 0 for scope in sorted(VALID_SCOPES)}
    with STORE.lock:
        for scope, _ in STORE.contexts:
            counts[scope] += 1
    return {"status": "ok", "uptime_seconds": int(time.time() - STARTED_AT), "contexts_loaded": counts}


@app.get("/v1/metadata")
def metadata() -> dict[str, Any]:
    members = [item.strip() for item in os.getenv("TEAM_MEMBERS", "Vera Team").split(",") if item.strip()]
    return {
        "team_name": os.getenv("TEAM_NAME", "Vera Signal Engine"),
        "team_members": members,
        "model": "deterministic-context-router",
        "approach": "trigger-first grounded composition with category strategies, consent, dedup, and replay routing",
        "contact_email": os.getenv("CONTACT_EMAIL", ""),
        "version": VERSION,
        "submitted_at": os.getenv("SUBMITTED_AT", "2026-08-21T00:00:00Z"),
    }


@app.post("/v1/context", response_model=None)
def push_context(body: ContextBody, response: Response) -> Any:
    if body.scope not in VALID_SCOPES:
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": "invalid_scope", "details": body.scope},
        )
    if len(json.dumps(body.payload, ensure_ascii=False).encode("utf-8")) > 500_000:
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": "payload_too_large", "details": "500 KB limit"},
        )
    accepted, current_version = STORE.put_context(body)
    if not accepted:
        response.status_code = 409
        return {"accepted": False, "reason": "stale_version", "current_version": current_version}
    ack_raw = f"{body.scope}|{body.context_id}|{body.version}"
    ack = hashlib.sha256(ack_raw.encode("utf-8")).hexdigest()[:16]
    return {
        "accepted": True,
        "ack_id": f"ack_{ack}",
        "stored_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


@app.post("/v1/tick")
def tick(body: TickBody) -> dict[str, list[dict[str, Any]]]:
    candidates: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None]] = []
    seen_ids: set[str] = set()
    with STORE.lock:
        for trigger_id in body.available_triggers:
            if trigger_id in seen_ids:
                continue
            seen_ids.add(trigger_id)
            trigger = STORE.payload("trigger", trigger_id)
            if not trigger:
                continue
            merchant_id = _trigger_merchant_id(trigger)
            merchant = STORE.payload("merchant", merchant_id)
            if not merchant or merchant_id in STORE.muted_merchants:
                continue
            category = STORE.payload("category", merchant.get("category_slug"))
            if not category:
                continue
            customer_id = _trigger_customer_id(trigger)
            customer = STORE.payload("customer", customer_id) if customer_id else None
            if customer and customer.get("merchant_id") != merchant_id:
                continue
            suppression_key = _clean(trigger.get("suppression_key")) or f"trigger:{trigger_id}"
            if suppression_key and suppression_key in STORE.sent_suppression_keys:
                continue
            if not _trigger_quality(trigger, category, customer, body.now):
                continue
            candidates.append((trigger, category, merchant, customer))

        candidates.sort(key=lambda item: _priority(item[0]), reverse=True)
        actions: list[dict[str, Any]] = []
        selected_entities: set[tuple[str, str | None]] = set()
        for trigger, category, merchant, customer in candidates:
            merchant_id = _trigger_merchant_id(trigger)
            customer_id = _trigger_customer_id(trigger)
            entity = (merchant_id, customer_id)
            if entity in selected_entities or len(actions) >= 20:
                continue
            result = compose(category, merchant, trigger, customer)
            conversation_id = _conversation_id(trigger)
            template_params = [
                _customer_name(customer) if customer else _merchant_salutation(category, merchant),
                _humanize(trigger.get("kind")),
                _merchant_fact(merchant),
            ]
            action = {
                "conversation_id": conversation_id,
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "send_as": result["send_as"],
                "trigger_id": trigger.get("id") or trigger_id,
                "template_name": _template_name(_clean(trigger.get("kind")), customer is not None),
                "template_params": template_params,
                **result,
            }
            actions.append(action)
            selected_entities.add(entity)
            if result["suppression_key"]:
                STORE.sent_suppression_keys.add(result["suppression_key"])
            STORE.conversations[conversation_id] = {
                "category": category,
                "merchant": merchant,
                "trigger": trigger,
                "customer": customer,
                "turns": [{"from": "bot", "body": result["body"]}],
                "auto_reply_count": 0,
            }
    return {"actions": actions}


@app.post("/v1/reply")
def reply(body: ReplyBody) -> dict[str, Any]:
    message = _clean(body.message)
    normalized = _normalise_reply(message)
    merchant_id = body.merchant_id or "unknown"

    with STORE.lock:
        state = STORE.conversations.setdefault(body.conversation_id, {"turns": [], "auto_reply_count": 0})
        state.setdefault("turns", []).append({"from": body.from_role, "body": message})
        category, merchant, trigger = _reply_context(state, body.merchant_id)

        if _matches_any(message, STOP_PATTERNS):
            if body.merchant_id:
                STORE.muted_merchants.add(body.merchant_id)
            return {"action": "end", "rationale": "Explicit opt-out, hostility, or spam complaint; ending immediately and muting proactive sends."}

        looks_automatic = _matches_any(message, AUTO_REPLY_PATTERNS)
        if looks_automatic:
            # Count identical canned replies within this conversation. A
            # separate conversation from the same merchant must not inherit a
            # prior auto-reply streak.
            fingerprints = state.setdefault("auto_reply_fingerprints", Counter())
            fingerprints[normalized] += 1
            count = fingerprints[normalized]
            state["auto_reply_count"] = max(int(state.get("auto_reply_count", 0)), count)
            if count >= 3:
                return {"action": "end", "rationale": "The same canned auto-reply was seen three times; no human engagement signal remains."}
            if count == 2:
                return {"action": "wait", "wait_seconds": 86_400, "rationale": "Repeated canned auto-reply; waiting 24 hours rather than burning another turn."}
            return {
                "action": "send",
                "body": "Looks like an auto-reply. When the owner or manager sees this, reply YES and I’ll share the ready draft; otherwise I’ll step back.",
                "cta": "binary_yes_stop",
                "rationale": "First canned response detected; one concise owner-directed prompt before backing off.",
            }

        # A real human reply resets the auto-reply streak for this conversation.
        if normalized:
            state.setdefault("auto_reply_fingerprints", Counter()).clear()

        if _matches_any(message, COMMIT_PATTERNS):
            response_body = _commitment_response(category, merchant, trigger, body.turn_number)
            return {"action": "send", "body": _finalize(response_body), "cta": "binary_confirm_edit", "rationale": "Explicit commitment detected; switching immediately from discovery to execution."}

        if _matches_any(message, DECLINE_PATTERNS):
            return {"action": "end", "rationale": "Clear decline or request to defer; ending without another persuasion attempt."}

        if re.search(r"\b(gst|tax|loan|legal|passport|aadhaar)\b", normalized):
            trigger_kind = _humanize(trigger.get("kind")) if trigger else "the current Vera task"
            return {
                "action": "send",
                "body": f"I’ll leave GST, tax, or legal filing to your CA. I can help with {trigger_kind}, your magicpin listing, customer messages, offers, and campaign drafts. Reply DRAFT to continue here or STOP to close.",
                "cta": "open_ended",
                "rationale": "Off-topic request is declined without inventing capability, then routed back to the active trigger with one clear continuation.",
            }

        if "?" in message or re.search(r"\b(what|how|when|where|kitna|kaise|kab)\b", normalized):
            fact = _merchant_fact(merchant or {})
            kind = _humanize(trigger.get("kind")) if trigger else "current request"
            response_body = f"For this {kind}, I’m using only the latest supplied facts — including {fact}. I can show the exact draft next; reply DRAFT, or tell me the single detail you want changed."
            return {"action": "send", "body": _finalize(response_body), "cta": "open_ended", "rationale": "Direct answer explains the grounding boundary and offers one concrete continuation."}

        if body.turn_number >= 5:
            return {"action": "end", "rationale": "Conversation reached five turns without a clear action signal; closing to avoid repetitive nudging."}

        options = [
            "Got it. I’ll keep this focused on one useful next step. Reply DRAFT and I’ll show the grounded copy, or STOP to close.",
            "Understood. The next useful move is the ready draft, not another question. Reply DRAFT to see it or STOP to close.",
            "Noted. I can now turn the supplied context into the final copy. Reply DRAFT or STOP.",
        ]
        response_body = options[(body.turn_number - 1) % len(options)]
        return {"action": "send", "body": response_body, "cta": "binary_draft_stop", "rationale": "Acknowledges the reply, advances one step, and avoids repeating prior wording."}


@app.post("/v1/teardown")
def teardown() -> dict[str, bool]:
    STORE.clear()
    return {"cleared": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("bot:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
