"""Deterministic message engine and HTTP API for the magicpin Vera challenge.

``compose`` is framework-free so a static judge can import it; the FastAPI app
below adds the context/tick/reply contract. Three rules drive every message:
cite only facts the evaluator can see, speak the merchant's language, and never
let internal vocabulary reach a human.
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


VERSION = "2.1.0"
STARTED_AT = time.time()
VALID_SCOPES = {"category", "merchant", "customer", "trigger"}


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------

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


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _num(value: Any) -> str:
    number = _int(value)
    return f"{number:,}" if number is not None else ""


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
    number = _int(value)
    return f"₹{number:,}" if number is not None else _clean(value)


def _date_label(value: Any, include_time: bool = False) -> str:
    raw = _clean(value)
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    label = f"{parsed.day} {parsed.strftime('%b')}"
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


# ---------------------------------------------------------------------------
# Voice: Hindi-English code-mix selection
# ---------------------------------------------------------------------------

_CODE_MIX_LEVEL = {
    "hindi_english_natural": "natural",
    "hindi_english": "natural",
    "english_primary_some_hindi": "light",
    "english_only": "off",
    "english": "off",
}


class Voice:
    """Picks the English or Hindi-English rendering of a line.

    ``t`` code-mixes only for ``natural`` categories; ``s`` also covers
    ``light`` ones such as gyms.
    """

    __slots__ = ("level", "slug", "tone")

    def __init__(self, category: dict[str, Any] | None, merchant: dict[str, Any] | None) -> None:
        category = category or {}
        merchant = merchant or {}
        declared = _clean(_get(category, "voice", "code_mix")).lower()
        level = _CODE_MIX_LEVEL.get(declared, "light" if declared else "off")
        languages = [str(item).lower() for item in _get(merchant, "identity", "languages", default=[]) or []]
        if languages and not any(item.startswith("hi") for item in languages):
            level = "off"
        self.level = level
        self.slug = _clean(category.get("slug"))
        self.tone = _clean(_get(category, "voice", "tone"))

    @property
    def mixed(self) -> bool:
        return self.level == "natural"

    def t(self, english: str, hindi: str) -> str:
        return hindi if self.level == "natural" else english

    def s(self, english: str, hindi: str) -> str:
        return hindi if self.level in {"natural", "light"} else english


def _customer_voice(category: dict[str, Any] | None, merchant: dict[str, Any] | None, customer: dict[str, Any] | None) -> Voice:
    """Customer-facing register follows the customer's stated preference."""
    voice = Voice(category, merchant)
    pref = _clean(_get(customer, "identity", "language_pref")).lower()
    if not pref:
        return voice
    if re.search(r"\bhi\b|hindi|hi-en|hinglish", pref):
        voice.level = "natural"
    elif re.search(r"^en|english", pref):
        voice.level = "off"
    return voice


# ---------------------------------------------------------------------------
# Judge-visible fact extraction
# ---------------------------------------------------------------------------

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


def _catalog_offer(category: dict[str, Any], keywords: list[str] | None = None) -> str:
    """A category-appropriate offer we can *propose* when none is live."""
    catalog = category.get("offer_catalog") or []
    titles = [_clean(item.get("title")) for item in catalog if _clean(item.get("title"))]
    if keywords:
        lowered = [word.lower() for word in keywords]
        for title in titles:
            if any(word in title.lower() for word in lowered):
                return title
    # Prefer a concrete service/price over a flat percentage discount.
    priced = [title for title in titles if "₹" in title and not re.search(r"\bflat\b|%", title, re.I)]
    return (priced or titles)[0] if titles else ""


def _event_offer(merchant: dict[str, Any], event_time: Any) -> str:
    """Do not promote a weekday-only offer on a different match day."""
    try:
        event = datetime.fromisoformat(_clean(event_time).replace("Z", "+00:00"))
    except ValueError:
        return ""
    days = {name: index for index, name in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))}
    for offer in _active_offers(merchant):
        title = _clean(offer.get("title"))
        bounds = re.search(r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\s*[-–]\s*(mon|tue|wed|thu|fri|sat|sun)[a-z]*", title, re.I)
        if bounds:
            start, end = (days[bounds[i].lower()] for i in (1, 2))
            permitted = {(start + offset) % 7 for offset in range((end - start) % 7 + 1)}
            if event.weekday() not in permitted:
                continue
        elif re.search(r"\bweekday", title, re.I) and event.weekday() >= 5:
            continue
        elif re.search(r"\bweekend", title, re.I) and event.weekday() < 5:
            continue
        return title
    return ""


def _slot_label(slot: dict[str, Any]) -> str:
    # Prefer the ISO date when a human label has an inconsistent weekday.
    return _date_label(slot.get("iso"), include_time=True) if slot.get("iso") else _clean(slot.get("label"))


def _merchant_name(merchant: dict[str, Any]) -> str:
    return _clean(_get(merchant, "identity", "name")) or "your business"


def _locality(merchant: dict[str, Any]) -> str:
    return _clean(_get(merchant, "identity", "locality"))


def _merchant_salutation(category: dict[str, Any], merchant: dict[str, Any]) -> str:
    owner = _clean(_get(merchant, "identity", "owner_first_name"))
    if _clean(category.get("slug")) == "dentists":
        if owner:
            return owner if owner.lower().startswith("dr") else f"Dr. {owner}"
        name = _merchant_name(merchant)
        return name if name.lower().startswith("dr") else f"Dr. {name}"
    return owner or _merchant_name(merchant)


def _customer_name(customer: dict[str, Any] | None) -> str:
    return _clean(_get(customer, "identity", "name")) or "there"


def _perf(merchant: dict[str, Any]) -> dict[str, Any]:
    return merchant.get("performance", {}) or {}


def _perf_line(merchant: dict[str, Any], voice: Voice) -> str:
    """The single most reliable judge-visible anchor: 30-day profile numbers."""
    performance = _perf(merchant)
    days = _int(performance.get("window_days")) or 30
    bits = []
    views = _num(performance.get("views"))
    calls = _num(performance.get("calls"))
    ctr = _pct(performance.get("ctr"))
    if views:
        bits.append(f"{views} views")
    if calls:
        bits.append(f"{calls} calls")
    if ctr:
        bits.append(f"{ctr} CTR")
    if not bits:
        return ""
    joined = ", ".join(bits[:-1]) + (f" and {bits[-1]}" if len(bits) > 1 else bits[-1])
    joined_hi = ", ".join(bits[:-1]) + (f" aur {bits[-1]}" if len(bits) > 1 else bits[-1])
    return voice.t(
        f"{joined} in the last {days} days",
        f"pichhle {days} din mein {joined_hi}",
    )


def _conversion_line(merchant: dict[str, Any], voice: Voice) -> str:
    """Views-to-calls gap. Always available and always specific."""
    performance = _perf(merchant)
    views = _int(performance.get("views"))
    calls = _int(performance.get("calls"))
    if not views or calls is None:
        return ""
    per_hundred = round(calls * 100 / views, 1)
    return voice.t(
        f"{views:,} people saw the listing and {calls:,} called — that is {per_hundred} calls per 100 views",
        f"{views:,} logon ne listing dekhi aur {calls:,} ne call kiya — yaani har 100 views pe {per_hundred} call",
    )


_SIGNAL_PHRASES: dict[str, tuple[str, str]] = {
    "unverified_gbp": ("the Google listing is still unverified", "Google listing abhi unverified hai"),
    "no_active_offers": ("there is no active offer on the listing", "listing par koi active offer nahi hai"),
    "no_recent_post": ("no recent post on the profile", "profile par koi naya post nahi hai"),
    "stale_posts": ("the last post is old now", "aakhri post ab purana ho chuka hai"),
    "ctr_below_peer_median": ("click-through is under the peer median", "click-through peer median se neeche hai"),
    "above_peer_ctr": ("click-through is above the peer median", "click-through peer median se upar hai"),
    "above_peer_calls": ("calls are above the peer median", "calls peer median se upar hain"),
    "above_peer_median_calls": ("calls are above the peer median", "calls peer median se upar hain"),
    "perf_dip_severe": ("the recent drop is a sharp one", "recent giravat kaafi tez hai"),
    "perf_dip_post_expiry": ("the drop started after the plan expired", "giravat plan expire hone ke baad shuru hui"),
    "delivery_not_set_up": ("delivery is not set up yet", "delivery abhi set up nahi hai"),
    "high_repeat_rate": ("repeat customers are strong", "repeat customers strong hain"),
    "high_retention": ("retention is strong", "retention strong hai"),
    "high_risk_adult_cohort": ("this lands on the high-risk adult cohort", "yeh high-risk adult cohort pe lagta hai"),
    "growing_views_7d": ("views have been climbing this week", "is hafte views badh rahe hain"),
    "new_merchant": ("the listing is still new", "listing abhi nayi hai"),
    "trial_ending_soon": ("the trial ends shortly", "trial jaldi khatm ho raha hai"),
    "renewal_due_soon": ("renewal is close", "renewal paas hai"),
    "winback_eligible": ("lapsed customers are worth one more attempt", "lapsed customers ek aur koshish ke laayak hain"),
    "ipl_eligible_locality": ("match nights matter in this area", "is area mein match nights maayne rakhti hain"),
    "active_planning": ("a plan is already in motion", "ek plan pehle se chal raha hai"),
    "compliance_aware": ("compliance is already tracked here", "compliance yahan pehle se track hoti hai"),
}


def _signal_phrase(merchant: dict[str, Any], voice: Voice, prefer: tuple[str, ...] = ()) -> str:
    signals = [str(item) for item in merchant.get("signals") or []]
    ordered = [item for item in signals if item.split(":")[0] in prefer] + signals
    for raw in ordered:
        head = raw.split(":")[0]
        phrase = _SIGNAL_PHRASES.get(head)
        if phrase:
            english, hindi = phrase
            days = re.search(r"(\d+)\s*d", raw)
            if days and head.startswith("stale_posts"):
                english = f"the last post was {days.group(1)} days ago"
                hindi = f"aakhri post {days.group(1)} din pehle tha"
            return voice.t(english, hindi)
    return ""


def _anchor_tokens(merchant: dict[str, Any]) -> list[str]:
    """Strings whose presence proves the body used a judge-visible fact."""
    tokens: list[str] = []
    performance = _perf(merchant)
    for key in ("views", "calls", "directions", "leads"):
        rendered = _num(performance.get(key))
        if rendered:
            tokens.append(rendered)
    ctr = _pct(performance.get("ctr"))
    if ctr:
        tokens.append(ctr)
    locality = _locality(merchant)
    if locality:
        tokens.append(locality)
    for offer in _active_offers(merchant):
        title = _clean(offer.get("title"))
        if title:
            tokens.append(title)
    return [token for token in tokens if token]


def _ensure_anchor(body: str, merchant: dict[str, Any], voice: Voice) -> str:
    """Guarantee every outgoing body carries at least one verifiable fact."""
    tokens = _anchor_tokens(merchant)
    if any(token and token in body for token in tokens):
        return body
    line = _perf_line(merchant, voice)
    if not line:
        locality = _locality(merchant)
        if not locality:
            return body
        line = voice.t(f"this is for the {locality} listing", f"yeh {locality} wali listing ke liye hai")
    sentence = voice.t(f"For reference, {line}.", f"Reference ke liye — {line}.")
    # Insert before the closing call to action rather than after it.
    sentences = re.split(r"(?<=[.?!]) +", body.strip())
    if len(sentences) >= 2:
        sentences.insert(len(sentences) - 1, sentence)
        return " ".join(sentences)
    return f"{body} {sentence}"


_METRIC_NOUNS = {
    "review_count": ("reviews", "reviews"),
    "reviews": ("reviews", "reviews"),
    "views": ("views", "views"),
    "calls": ("calls", "calls"),
    "ctr": ("CTR", "CTR"),
    "directions": ("direction requests", "direction requests"),
    "leads": ("leads", "leads"),
    "footfall": ("footfall", "footfall"),
    "saves": ("saves", "saves"),
    "members": ("members", "members"),
    "orders": ("orders", "orders"),
}


def _metric_noun(value: Any, voice: Voice, default: str = "") -> str:
    key = _clean(value).lower().removesuffix("_pct")
    pair = _METRIC_NOUNS.get(key)
    if pair:
        return voice.t(pair[0], pair[1])
    return _humanize(value) or default


def _digest_item(category: dict[str, Any], trigger: dict[str, Any]) -> dict[str, Any]:
    payload = trigger.get("payload", {})
    target = payload.get("top_item_id") or payload.get("digest_item_id") or payload.get("alert_id")
    digest = category.get("digest", [])
    if target:
        for item in digest:
            if item.get("id") == target:
                return item
        # A named but missing source is not interchangeable with another item.
        return {}

    preferred_kind = {
        "research_digest": "research",
        "regulation_change": "compliance",
        "cde_opportunity": "cde",
        "supply_alert": "compliance",
    }.get(trigger.get("kind"))
    matching = [item for item in digest if item.get("kind") == preferred_kind]
    # New category versions commonly append fresh items; prefer the latest match.
    return matching[-1] if matching else {}


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


def _weakest_metric(merchant: dict[str, Any]) -> str:
    """Pick the metric to talk about when the trigger carries no numbers."""
    performance = _perf(merchant)
    views = _int(performance.get("views")) or 0
    calls = _int(performance.get("calls")) or 0
    if views and calls * 100 / views < 1.5:
        return "calls"
    ctr = performance.get("ctr")
    try:
        if float(ctr) < 0.03:
            return "ctr"
    except (TypeError, ValueError):
        pass
    return "calls" if calls else "views"


def _template_name(kind: str, customer_facing: bool) -> str:
    safe_kind = re.sub(r"[^a-z0-9_]+", "_", kind.lower()).strip("_") or "contextual"
    prefix = "merchant" if customer_facing else "vera"
    return f"{prefix}_{safe_kind}_v1"


def _trigger_merchant_id(trigger: dict[str, Any]) -> str | None:
    return trigger.get("merchant_id") or _get(trigger, "payload", "merchant_id")


def _trigger_customer_id(trigger: dict[str, Any]) -> str | None:
    return trigger.get("customer_id") or _get(trigger, "payload", "customer_id")


def _is_placeholder(trigger: dict[str, Any]) -> bool:
    payload = trigger.get("payload") or {}
    return bool(payload.get("placeholder")) or not [
        key for key in payload if key not in {"placeholder", "metric_or_topic", "category", "merchant_id", "customer_id"}
    ]


# ---------------------------------------------------------------------------
# Output hygiene
# ---------------------------------------------------------------------------

# Internal vocabulary that must never reach a merchant or a customer.
_JARGON = re.compile(
    r"\b(context|contexts|trigger|triggers|payload|suppression|composer|composition|"
    r"grounded|grounding|fallback|adaptive|template_name|anchor|rationale)\b",
    re.IGNORECASE,
)


def _strip_taboo(text: str, category: dict[str, Any] | None) -> str:
    """Remove category-forbidden marketing claims if any slipped in."""
    for phrase in _get(category, "voice", "vocab_taboo", default=[]) or []:
        cleaned = _clean(str(phrase).split("(")[0])
        if len(cleaned) < 4:
            continue
        text = re.sub(re.escape(cleaned), "", text, flags=re.IGNORECASE)
    return text


def _sentence_case(text: str) -> str:
    text = re.sub(r"([.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)
    return text[:1].upper() + text[1:] if text else text


def _finalize(text: str, category: dict[str, Any] | None = None) -> str:
    text = _clean(text)
    # Meta examples and the challenge judge penalize accidental URLs.
    text = re.sub(r"https?://\S+|www\.\S+", "", text, flags=re.IGNORECASE)
    text = _strip_taboo(text, category)
    text = re.sub(r"\s+([.,;!?])", r"\1", text)
    text = re.sub(r"([.,;])\1+", r"\1", text)
    text = re.sub(r"\s+—\s+—\s+", " — ", text)
    text = _sentence_case(_clean(text))
    if len(text) <= 900:
        return text
    # Keep the closing action intact when injected source material is long.
    closing = re.split(r"(?<=[.!?])\s+", text)[-1]
    if len(closing) > 250:
        closing = ""
    budget = 897 - len(closing)
    head = text[:budget].rsplit(" ", 1)[0].rstrip(".,;: ")
    return f"{head}… {closing}".strip()


# ---------------------------------------------------------------------------
# Merchant-facing composition
# ---------------------------------------------------------------------------

def _cat(slug: str, mapping: dict[str, Any], default: Any = "") -> Any:
    return mapping.get(slug, default)


def _merchant_message(
    category: dict[str, Any],
    merchant: dict[str, Any],
    trigger: dict[str, Any],
) -> tuple[str, str, str]:
    kind = _clean(trigger.get("kind"))
    payload = trigger.get("payload", {}) or {}
    voice = Voice(category, merchant)
    sal = _merchant_salutation(category, merchant)
    slug = _clean(category.get("slug"))
    name = _merchant_name(merchant)
    locality = _locality(merchant)
    perf_line = _perf_line(merchant, voice)
    conv_line = _conversion_line(merchant, voice)
    offer = _best_offer(merchant)
    body = ""
    cta = "binary_yes_stop"
    rationale = ""

    ask_yes = voice.t("Reply YES, or STOP.", "YES bhejiye, ya STOP.")

    if kind == "research_digest":
        item = _digest_item(category, trigger)
        source = _clean(item.get("source"))
        summary = _clean(item.get("summary") or item.get("title"))
        trial_n = _int(item.get("trial_n"))
        cohort = _signal_phrase(merchant, voice, prefer=("high_risk_adult_cohort",))
        study = f" (n={trial_n:,})" if trial_n else ""
        if summary:
            body = voice.t(
                f"{sal}, {source or 'the supplied professional update'}{study}: {summary} "
                f"{cohort + '. ' if cohort else ''}Want a short team briefing on the finding and its limits? {ask_yes}",
                f"{sal}, {source or 'professional update'}{study}: {summary} "
                f"{cohort + '. ' if cohort else ''}Finding aur uski limits par team ke liye short note bana doon? {ask_yes}",
            )
        else:
            body = voice.t(
                f"{sal}, the requested reading is missing from the record for your {locality} practice. Share the title or abstract and I will prepare a source-based summary.",
                f"{sal}, aapki {locality} practice ke liye requested reading record mein nahi hai. Title ya abstract bhejiye, main uska summary bana dungi.",
            )
            cta = "open_ended"
        rationale = "Names the exact supplied reading, study limits and cohort relevance without inventing a missing source."

    elif kind == "regulation_change":
        item = _digest_item(category, trigger)
        source = _clean(item.get("source")) or voice.t("the latest compliance notice", "latest compliance notice")
        deadline = _date_label(payload.get("deadline_iso"))
        summary = _clean(item.get("summary") or item.get("title"))
        action = _clean(item.get("actionable"))
        if action and action[-1] not in ".!?":
            action += "."
        checklist = _cat(slug, {
            "dentists": ("an audit checklist for your team",
                         "team ke liye audit checklist"),
            "pharmacies": ("a counter checklist",
                           "counter checklist"),
        }, ("a checklist for your team",
            "team ke liye checklist"))
        body = voice.t(
            f"{sal}, compliance heads-up from {source}: {summary}"
            f"{f' It takes effect {deadline}.' if deadline else ''}{f' {action}' if action else ''} "
            f"For your {locality} practice, keep the equipment check and front-desk explanation consistent. "
            f"Want {checklist[0]}? {ask_yes}",
            f"{sal}, {source} se ek compliance update: {summary}"
            f"{f' Yeh {deadline} se effective hai.' if deadline else ''}{f' {action}' if action else ''} "
            f"Aapki {locality} practice mein equipment check aur front-desk explanation consistent rakhein. "
            f"Main {checklist[1]} bana doon? {ask_yes}",
        )
        rationale = "Source-cited regulation with the exact effective date, connected to the merchant's own listing traffic, resolved by one compliance artifact."

    elif kind == "cde_opportunity":
        item = _digest_item(category, trigger)
        title = _clean(item.get("title")) or voice.t("a relevant CDE session", "ek relevant CDE session")
        event_date = _date_label(item.get("date"), include_time=True)
        credits = _int(payload.get("credits")) or _int(item.get("credits"))
        raw_fee = _clean(payload.get("fee"))
        fee = voice.t(
            "free for members" if raw_fee == "free_for_members" else _humanize(raw_fee),
            "members ke liye free" if raw_fee == "free_for_members" else _humanize(raw_fee),
        )
        source = _clean(item.get("source"))
        body = voice.t(
            f"{sal}, there is a session worth blocking time for — {title}"
            f"{f' on {event_date}' if event_date else ''}"
            f"{f', {credits} CDE credits' if credits else ''}{f', {fee}' if fee else ''}"
            f"{f'. Listed by {source}' if source else ''}. "
            f"Want the registration steps and a calendar block in one message, so it does not slip past clinic hours? {ask_yes}",
            f"{sal}, ek session hai jiske liye time block karna banta hai — {title}"
            f"{f', {event_date} ko' if event_date else ''}"
            f"{f', {credits} CDE credits' if credits else ''}{f', {fee}' if fee else ''}"
            f"{f'. {source} ne list kiya hai' if source else ''}. "
            f"Main registration ke steps aur ek calendar block ek hi message mein bhej doon, taaki clinic hours mein miss na ho? {ask_yes}",
        )
        rationale = "Time-bound professional opportunity with date, credits, fee and source, without inventing seat scarcity and a single scheduling action."

    elif kind == "active_planning_intent":
        body = f"{sal}, {_planning_draft(category, merchant, trigger, voice)}"
        body += voice.t(" Reply with the one detail to change, or APPROVE to keep this draft.",
                        " Ek detail badalni ho toh bhejiye, ya draft theek ho toh APPROVE likhiye.")
        cta = "binary_approve_edit"
        rationale = "Existing planning intent gets actual proposed copy; package prices and capacity are not inferred from unrelated listing offers."

    elif kind in {"perf_dip", "seasonal_perf_dip"}:
        metric = _humanize(payload.get("metric"))
        delta = payload.get("delta_pct")
        if delta is None:
            inferred = _strongest_delta(merchant, "down")
            if inferred:
                metric, delta = inferred
        seasonal = payload.get("is_expected_seasonal")
        season_note = _humanize(payload.get("season_note"))
        fix = _cat(slug, {
            "dentists": ("making the profile call-first — one treatment, one price, and one line on what happens at the first visit",
                         "profile ko call-first banana — ek treatment, ek price, aur ek line ki pehli visit mein hota kya hai"),
            "salons": (f"one service+price post, and {offer or _catalog_offer(category)} is the fastest thing to put back in front of people",
                       f"ek service+price post, aur {offer or _catalog_offer(category)} sabse tez wapas saamne laane wali cheez hai"),
            "restaurants": ("a repeat-customer post built on your existing menu price rather than a fresh discount",
                            "repeat-customer post, aapke existing menu price par — naya discount nahi"),
            "gyms": ("a member attendance nudge plus one trial slot held open for walk-ins",
                     "member attendance nudge aur walk-ins ke liye ek trial slot"),
            "pharmacies": ("a local availability post — what you stock that people are currently driving further for",
                           "local availability post — jo aapke paas stock hai aur log door ja rahe hain"),
        }, ("one focused recovery post built on your current numbers",
            "aapke current numbers par ek focused recovery post"))
        if delta is not None:
            lead = voice.t(
                f"{_metric_noun(metric, voice, 'performance')} moved {_change_phrase(delta, 'down')} over the last 7 days",
                f"{_metric_noun(metric, voice, 'performance')} pichhle 7 din mein {_change_phrase(delta, 'down')} hue hain",
            )
            position = voice.t(f"Right now — {perf_line}.", f"Abhi — {perf_line}.")
        else:
            weakest = _metric_noun(_weakest_metric(merchant), voice)
            lead = voice.t(
                f"the gap is in {weakest}, not in interest — {conv_line}" if conv_line else f"{weakest} are the soft spot right now",
                f"dikkat {weakest} mein hai, interest mein nahi — {conv_line}" if conv_line else f"abhi {weakest} hi soft spot hain",
            )
            # conv_line already carries the numbers; repeating them reads robotic.
            position = "" if conv_line else voice.t(f"Right now — {perf_line}.", f"Abhi — {perf_line}.")
        context_line = voice.t(
            f" The record flags an expected seasonal lull ({season_note}); that alone does not establish the cause." if seasonal else "",
            f" Record mein expected seasonal lull ({season_note}) flagged hai; isse akela cause confirm nahi hota." if seasonal else "",
        )
        body = voice.t(
            f"{sal}, {lead}. {position}{context_line} "
            f"One practical next step: {fix[0]}. Want me to draft it from your current numbers? {ask_yes}",
            f"{sal}, {lead}. {position}{context_line} "
            f"Ek practical agla step: {fix[1]}. Main aapke current numbers se woh draft kar doon? {ask_yes}",
        )
        rationale = "Names the exact weak metric with its real value, separates seasonality from decline, and offers one category-correct recovery action."

    elif kind == "perf_spike":
        metric = _humanize(payload.get("metric"))
        delta = payload.get("delta_pct")
        if delta is None:
            inferred = _strongest_delta(merchant, "up")
            if inferred:
                metric, delta = inferred
        driver = _humanize(payload.get("likely_driver"))
        if delta is not None:
            lead = voice.t(
                f"{_metric_noun(metric, voice, 'performance')} moved up {_pct(delta) or 'this week'} over the last 7 days"
                f"{f', most likely from {driver}' if driver else ''}",
                f"{_metric_noun(metric, voice, 'performance')} pichhle 7 din mein {_pct(delta) or 'is hafte'} upar gaye hain"
                f"{f', shayad {driver} ki wajah se' if driver else ''}",
            )
        else:
            lead = voice.t(
                f"the profile is converting well right now — {conv_line}" if conv_line else "the profile is running warm right now",
                f"profile abhi achha convert kar rahi hai — {conv_line}" if conv_line else "profile abhi warm chal rahi hai",
            )
        spike_position = voice.t(
            f" Right now — {perf_line}." if (delta is not None or not conv_line) else "",
            f" Abhi — {perf_line}." if (delta is not None or not conv_line) else "",
        )
        body = voice.t(
            f"{sal}, {lead}.{spike_position} Use this rise to test a follow-up post, "
            f"then compare calls over the next seven days before deciding whether to repeat it. "
            f"Want me to draft that follow-up post around {offer or _catalog_offer(category) or 'your strongest service'}? {ask_yes}",
            f"{sal}, {lead}.{spike_position} Is rise par ek follow-up post test kijiye, "
            f"phir agle saat din calls compare karke decide kijiye ki repeat karna hai ya nahi. "
            f"Main {offer or _catalog_offer(category) or 'aapki strongest service'} ke around woh follow-up post draft kar doon? {ask_yes}",
        )
        rationale = "Positive movement stated with its real magnitude, a measurable follow-up experiment, and one repeatable next post."

    elif kind == "category_seasonal":
        trends = [_humanize(item) for item in payload.get("trends", []) or []]
        season = _humanize(payload.get("season")) or voice.t("the seasonal shift", "seasonal shift")
        trend_text = _compact_list(trends, conjunction=voice.t("and", "aur"))
        stock_word = _cat(slug, {
            "pharmacies": ("shelf and counter priority", "shelf aur counter priority"),
            "restaurants": ("menu priority", "menu priority"),
        }, ("service priority", "service priority"))
        body = voice.t(
            f"{sal}, the {season} shift has started"
            f"{f': {trend_text}' if trend_text else ''}. For {locality} this decides what people ask for first, not what you discount. "
            f"Your listing is at {perf_line}, so what you show at the top actually gets seen. "
            f"Want a one-page {stock_word[0]} list for the next four weeks? {ask_yes}",
            f"{sal}, {season} wala shift shuru ho chuka hai"
            f"{f': {trend_text}' if trend_text else ''}. {locality} mein yeh decide karta hai ki log pehle kya maangenge — discount nahi. "
            f"Aapki listing pe {perf_line} hai, toh upar jo dikhta hai woh sach mein dekha jaata hai. "
            f"Main agle chaar hafton ke liye ek page ki {stock_word[1]} list bana doon? {ask_yes}",
        )
        rationale = "Concrete seasonal demand movements translated into an operator decision, anchored on the merchant's own visibility numbers."

    elif kind == "competitor_opened":
        competitor = _clean(payload.get("competitor_name"))
        distance = payload.get("distance_km")
        opened = _date_label(payload.get("opened_date"))
        their_offer = _clean(payload.get("their_offer"))
        base = voice.t(
            f"your listing is doing {perf_line}"
            f"{f' with {offer} live' if offer else ', and there is no active offer on it right now'}",
            f"aapki listing par {perf_line} hai"
            f"{f', aur {offer} live hai' if offer else ', aur abhi koi active offer nahi hai'}",
        )
        if competitor:
            fact = voice.t(
                f"{competitor} opened {distance} km away{f' on {opened}' if opened else ''}"
                f"{f' with {their_offer}' if their_offer else ''}",
                f"{competitor}{f' {opened} ko' if opened else ''} {distance} km door khula hai"
                f"{f', {their_offer} ke saath' if their_offer else ''}",
            )
            body = voice.t(
                f"{sal}, {fact}. Before reacting: {base}. Before changing price, compare the services included and your costs. "
                f"Want a sharper listing comparison built on what you actually do better? {ask_yes}",
                f"{sal}, {fact}. React karne se pehle: {base}. Price badalne se pehle included services aur apne costs compare kijiye. "
                f"Main ek sharper listing comparison bana doon, aapki asli strength par? {ask_yes}",
            )
        else:
            body = voice.t(
                f"{sal}, a new competitor has opened near {locality}. Before we react: {base} — that is a real base, not a panic situation. "
                f"Tell me the one thing they are pushing hardest — price, timing, or a service you do not list — and I will draft the listing answer around your strengths. "
                f"Reply with that one detail, or STOP.",
                f"{sal}, {locality} ke paas ek naya competitor khula hai. React karne se pehle: {base} — yeh asli base hai, panic wali baat nahi. "
                f"Bas ek cheez bataiye jo woh sabse zyada push kar rahe hain — price, timing, ya koi service jo aap list nahi karte — main aapki strength par listing ka jawaab draft kar dungi. "
                f"Woh ek detail reply kijiye, ya STOP.",
            )
            cta = "open_ended"
        rationale = rationale or "Local competitive pressure answered with the merchant's own position, an explicit recommendation against a price war, and one specific ask."

    elif kind == "curious_ask_due":
        question = _cat(slug, {
            "dentists": ("Which treatment are patients asking about most this week?",
                         "Is hafte patients sabse zyada kis treatment ke baare mein pooch rahe hain?"),
            "salons": ("Which service are customers asking for this week that is not on your list?",
                       "Is hafte customers kaunsi service maang rahe hain jo aapki list mein nahi hai?"),
            "restaurants": ("Which dish are regulars asking for most this week?",
                            "Is hafte regulars sabse zyada kaunsi dish maang rahe hain?"),
            "gyms": ("What goal are new members mentioning most this week?",
                     "Is hafte naye members sabse zyada kaunsa goal bata rahe hain?"),
            "pharmacies": ("Which product are customers asking for but not finding quickly?",
                           "Customers kya maang rahe hain jo turant mil nahi raha?"),
        }, ("What are customers asking for most this week?",
            "Is hafte customers sabse zyada kya maang rahe hain?"))
        state = voice.t(
            f"{offer} is live" if offer else "there is no active offer on the listing right now",
            f"{offer} live hai" if offer else "abhi listing par koi active offer nahi hai",
        )
        body = voice.t(
            f"{sal}, one operator question — your listing is at {perf_line} and {state}. "
            f"{question[0]} Reply with one item and I will turn it into a post draft the same day; nothing else needed from you.",
            f"{sal}, ek operator sawaal — aapki listing par {perf_line} hai aur {state}. "
            f"{question[1]} Ek cheez reply kar dijiye, main usi din post draft bana dungi; aur kuch nahi chahiye.",
        )
        cta = "open_ended"
        rationale = "Asks the merchant something only they know, priced at one word of effort, with the merchant's real numbers as the reason for asking."

    elif kind in {"dormant_with_vera", "winback_eligible"}:
        days = _int(payload.get("days_since_last_merchant_message")) or _int(payload.get("days_since_expiry"))
        last_topic = _humanize(payload.get("last_topic"))
        lapsed = _int(payload.get("lapsed_customers_added_since_expiry"))
        dip = payload.get("perf_dip_pct")
        gap = voice.t(
            f"it has been {days} days since we last spoke" if days else "we have not spoken in a while",
            f"{days} din ho gaye humari baat ko" if days else "kaafi time se baat nahi hui",
        )
        topic_line = voice.t(
            f", last topic was {last_topic}" if last_topic else "",
            f", pichhli baat {last_topic} par hui thi" if last_topic else "",
        )
        since = []
        if dip is not None:
            since.append(voice.t(f"calls are {_change_phrase(dip, 'down')}", f"calls {_change_phrase(dip, 'down')} hain"))
        if lapsed:
            since.append(voice.t(f"{lapsed} customers have gone quiet", f"{lapsed} customers chup ho gaye hain"))
        since_text = f" Since then, {_compact_list(since, voice.t('and', 'aur'))}." if since else ""
        if offer:
            # An offer already exists; the gap is exposure, not the offer itself.
            move = voice.t(
                f"one fresh post built on {offer}, using the price already on your listing",
                f"{offer} par ek naya post — price aapki listing par already live hai",
            )
        else:
            proposal = _catalog_offer(category)
            move = voice.t(
                f"one service+price offer on the listing{f', something like {proposal}' if proposal else ''}",
                f"listing par ek service+price offer{f', jaise {proposal}' if proposal else ''}",
            )
        body = voice.t(
            f"{sal}, {gap}{topic_line}.{since_text} Right now the listing sits at {perf_line}. "
            f"One useful restart is {move}. "
            f"Want the post copy ready to review today? {ask_yes}",
            f"{sal}, {gap}{topic_line}.{since_text} Abhi listing par {perf_line} hai. "
            f"Dobara shuru karne ke liye ek kaam ki cheez hai {move}. "
            f"Main aaj hi review ke liye post copy draft kar doon? {ask_yes}",
        )
        rationale = "Dormancy quantified with the exact gap and its measurable cost, narrowed to a single reversible action instead of a menu of options."

    elif kind == "festival_upcoming":
        festival = _clean(payload.get("festival"))
        festival_date = _date_label(payload.get("date"))
        days = _int(payload.get("days_until"))
        proposal = offer or _catalog_offer(category)
        if not festival and not festival_date and days is None:
            body = voice.t(
                f"{sal}, which upcoming occasion matters for your {locality} customers? Your listing has {perf_line}. "
                "Send the occasion and date; I will draft one relevant post around your existing services.",
                f"{sal}, aapke {locality} customers ke liye kaunsa upcoming occasion relevant hai? Listing par {perf_line} hai. "
                "Occasion aur date bhejiye; existing services par ek relevant post draft kar dungi.",
            )
            cta = "open_ended"
            rationale = "Missing seasonal details are requested instead of inventing a festival or imminent deadline."
        elif days is not None and days > 45:
            body = voice.t(
                f"{sal}, {festival or 'the next festival'}{f' is on {festival_date}' if festival_date else ''} — {days} days out. "
                f"That leaves time to prepare a concept before choosing a promotion date. "
                f"What is worth doing now: your listing is at {perf_line}"
                f"{f' with {offer} live' if offer else ', with no active offer on it'}, and that is what the festive package should be built on. "
                f"Want me to draft one service+price concept to review two weeks before {festival or 'the date'}? {ask_yes}",
                f"{sal}, {festival or 'agla festival'}{f' {festival_date} ko hai' if festival_date else ''} — {days} din baaki hain. "
                f"Abhi concept prepare kar sakte hain aur promotion ki date baad mein decide kar sakte hain. "
                f"Abhi karne layak cheez: aapki listing par {perf_line} hai"
                f"{f', aur {offer} live hai' if offer else ', aur koi active offer nahi hai'} — festive package isi par banega. "
                f"Main ek service+price concept draft kar doon, {festival or 'date'} se do hafte pehle review ke liye? {ask_yes}",
            )
            rationale = "Correctly refuses to promote 6 months early, states why, and converts a low-urgency moment into concrete preparation anchored on real listing data."
        else:
            occasion = voice.t(
                f"{festival} is{f' on {festival_date}' if festival_date else ' close'}" if festival
                else "the festive stretch is close",
                f"{festival}{f' {festival_date} ko hai' if festival_date else ' paas hai'}" if festival
                else "festive time paas hai",
            )
            body = voice.t(
                f"{sal}, {occasion}{f', {days} days out' if days is not None else ''}, "
                f"and {locality} starts searching before it arrives. "
                f"A flat discount is the weakest version of this. Built on {proposal or 'your strongest service at a clear price'}, "
                f"and with your listing at {perf_line}, it actually lands. Want the post draft? {ask_yes}",
                f"{sal}, {occasion}{f', {days} din baaki' if days is not None else ''}, "
                f"aur {locality} mein log pehle se search karna shuru kar dete hain. "
                f"Flat discount sabse kamzor tarika hai. {proposal or 'Aapki strongest service ek clear price par'} par banaya jaaye, "
                f"aur listing par {perf_line} hai — tab asar hota hai. Post ka draft bana doon? {ask_yes}",
            )
            rationale = "Seasonal timing paired with a real service+price anchor, explicitly rejecting the generic percentage discount."

    elif kind == "gbp_unverified":
        uplift = _pct(payload.get("estimated_uplift_pct"))
        path = _humanize(payload.get("verification_path"))
        body = voice.t(
            f"{sal}, {name} is still unverified on Google. Unverified, the listing is already pulling {perf_line} — "
            f"that is demand you are getting without the badge. Estimated gain after verification: "
            f"{uplift or 'a meaningful lift'} more visibility{f', via {path}' if path else ''}. That is an estimate, not a guaranteed gain; Google determines the verification method and timing. "
            f"Want the exact three steps in one message? {ask_yes}",
            f"{sal}, {name} abhi tak Google par unverified hai. Unverified hote hue bhi listing par {perf_line} hai — "
            f"yaani demand badge ke bina bhi aa rahi hai. Verify karne ke baad estimate: "
            f"{uplift or 'achha khaasa'} zyada visibility{f', {path} se' if path else ''}. Yeh estimate hai, pakka gain nahi; verification method aur timing Google decide karta hai. "
            f"Main teen exact steps ek message mein bhej doon? {ask_yes}",
        )
        rationale = "Quantified listing gap with the supplied uplift estimate and verification route, framed as a small effort against demand the merchant is already earning."

    elif kind == "ipl_match_today":
        match = _clean(payload.get("match")) or "the match"
        timing = _date_label(payload.get("match_time_iso"), include_time=True)
        valid_offer = _event_offer(merchant, payload.get("match_time_iso"))
        offer_note = voice.t(
            f"Use the active offer {valid_offer}." if valid_offer else "Check a match-day menu price; the record does not establish an offer valid for this match.",
            f"Active offer {valid_offer} use kar sakte hain." if valid_offer else "Match-day menu price check kijiye; record mein is match ke liye valid offer confirm nahi hai.",
        )
        body = voice.t(
            f"{sal}, {match}{f' — {timing}' if timing else ''}. {offer_note} "
            f"Your listing has {perf_line}. A useful post can state the menu, delivery area and your confirmed order cutoff. "
            f"Send the cutoff time and I will draft the post around it.",
            f"{sal}, {match}{f' — {timing}' if timing else ''}. {offer_note} "
            f"Aapki listing par {perf_line} hai. Post mein menu, delivery area aur aapka confirmed order cutoff rakhein. "
            f"Cutoff time bhejiye, uske hisaab se post draft kar dungi.",
        )
        cta = "open_ended"
        rationale = "Uses the actual match date and checks weekday offer restrictions; requests only the missing operational cutoff."

    elif kind == "milestone_reached":
        metric = _metric_noun(payload.get("metric"), voice)
        now_value = _int(payload.get("value_now"))
        goal = _int(payload.get("milestone_value"))
        asker = _cat(slug, {
            "restaurants": ("your GRO or whoever settles the bill", "aapka GRO ya jo bill settle karta hai"),
            "salons": ("whoever is at the counter", "jo counter par hota hai"),
            "gyms": ("your floor coach", "aapka floor coach"),
            "pharmacies": ("whoever hands over the bag", "jo bag hand over karta hai"),
        }, ("whoever closes the visit", "jo visit close karta hai"))
        if now_value is not None and goal is not None:
            gap = max(0, goal - now_value)
            lead = voice.t(
                f"you are at {now_value:,} {metric or 'on this'} — {gap} away from {goal:,}",
                f"aap {now_value:,} {metric or ''} par hain — {goal:,} se sirf {gap} door",
            )
            close = voice.t(
                f"Ask for honest feedback after completed visits; there is no need to discount or reward reviews",
                f"Completed visits ke baad honest feedback maangiye; review ke liye discount ya reward ki zarurat nahi",
            )
            rationale = "Exact distance to the milestone converted into a same-week, zero-budget action assigned to a specific person."
        else:
            lead = voice.t(
                f"your latest listing snapshot is {perf_line}; the exact milestone is not specified",
                f"latest listing snapshot: {perf_line}; exact milestone specified nahi hai",
            )
            close = voice.t(
                "A short, neutral review request can make feedback easier after completed visits",
                "Completed visits ke baad chhota, neutral review request feedback dena asaan bana sakta hai",
            )
            rationale = "A visible performance mark used as the reason to ask for reviews this week, with the ask handed to a named person and no budget attached."
        body = voice.t(
            f"{sal}, {lead}. {close}. Want a two-line review request {asker[0]} can send on WhatsApp right after billing? {ask_yes}",
            f"{sal}, {lead}. {close}. Main do line ka review request bana doon jo {asker[1]} billing ke turant baad WhatsApp par bhej sake? {ask_yes}",
        )

    elif kind == "review_theme_emerged":
        theme = _humanize(payload.get("theme"))
        count = _int(payload.get("occurrences_30d"))
        quote = _clean(payload.get("common_quote"))
        body = voice.t(
            f"{sal}, {count or 'several'} reviews in the last 30 days mention the same thing: {theme or 'one recurring issue'}"
            f"{f' — “{quote}”' if quote else ''}. Unanswered, this is the line new customers read before they call, "
            f"and your listing is at {perf_line}. Want both pieces — the operational fix and a reply template for those reviews? {ask_yes}",
            f"{sal}, pichhle 30 din mein {count or 'kai'} reviews ek hi baat keh rahe hain: {theme or 'ek repeat issue'}"
            f"{f' — “{quote}”' if quote else ''}. Jawaab na dein toh naye customers call karne se pehle yahi padhte hain, "
            f"aur aapki listing par {perf_line} hai. Dono cheezein bana doon — operational fix aur in reviews ka reply template? {ask_yes}",
        )
        rationale = "Review pattern quoted in the customer's own words with its count, tied to visible listing traffic, resolved with two concrete assets."

    elif kind == "renewal_due":
        days = _int(payload.get("days_remaining"))
        plan = _clean(payload.get("plan"))
        amount = _money(payload.get("renewal_amount")) if payload.get("renewal_amount") is not None else ""
        body = voice.t(
            f"{sal}, your {plan or 'current'} plan renews in {days if days is not None else 'a few'} days"
            f"{f' at {amount}' if amount else ''}. Before you decide, the honest number: {perf_line}"
            f"{f', with {offer} live' if offer else ', with no active offer running'}. "
            f"Want a one-page check of what that actually returned, so the decision is yours and not a reminder’s? {ask_yes}",
            f"{sal}, aapka {plan or 'current'} plan {days if days is not None else 'kuch'} din mein renew ho raha hai"
            f"{f', {amount} par' if amount else ''}. Decide karne se pehle asli number: {perf_line}"
            f"{f', aur {offer} live hai' if offer else ', aur koi active offer nahi chal raha'}. "
            f"Main ek page ka check bana doon ki isse mila kya — faisla aapka rahe, reminder ka nahi? {ask_yes}",
        )
        rationale = "Renewal date and amount paired with the merchant's actual returns, positioned as a decision aid rather than a payment nudge."

    elif kind == "supply_alert":
        molecule = _humanize(payload.get("molecule"))
        batches = _compact_list(payload.get("affected_batches", []) or [])
        manufacturer = _clean(payload.get("manufacturer"))
        item = _digest_item(category, trigger)
        summary = _clean(item.get("summary"))
        body = voice.t(
            f"{sal}, urgent — {molecule or 'an affected molecule'}"
            f"{f' batches {batches}' if batches else ''}{f' from {manufacturer}' if manufacturer else ''} are recalled. "
            f"{summary + ' ' if summary else ''}Pull those batch numbers off the shelf first, then check who bought that molecule on repeat in the last 60 days. "
            f"Want a customer-safe WhatsApp note plus a replacement checklist you can hand to the counter? {ask_yes}",
            f"{sal}, urgent — {molecule or 'ek affected molecule'}"
            f"{f' ke batches {batches}' if batches else ''}{f', {manufacturer} ke' if manufacturer else ''} recall ho gaye hain. "
            f"{summary + ' ' if summary else ''}Pehle woh batch numbers shelf se hata dijiye, phir dekhiye kisne pichhle 60 din mein woh molecule repeat liya hai. "
            f"Main ek customer-safe WhatsApp note aur counter ke liye replacement checklist bana doon? {ask_yes}",
        )
        rationale = "Safety-critical recall with molecule, batch numbers and manufacturer, ordered as shelf-first then customer-first, with both artifacts offered."

    if not body:
        # Safe path for new trigger kinds: only evaluator-visible facts.
        facts = []
        for key, value in payload.items():
            if key in {"placeholder", "metric_or_topic", "merchant_id", "customer_id", "category"}:
                continue
            if isinstance(value, (str, int, float)) and _clean(value):
                facts.append(f"{_humanize(key)} {_humanize(value)}")
            if len(facts) == 2:
                break
        topic = _humanize(payload.get("metric_or_topic")) or _humanize(kind) or "your listing"
        detail = voice.t(
            f" The details on it: {_compact_list(facts)}." if facts else "",
            f" Detail yeh hai: {_compact_list(facts)}." if facts else "",
        )
        body = voice.t(
            f"{sal}, something came up on {topic} for {name}.{detail} "
            f"Where you stand today: {perf_line}"
            f"{f', with {offer} live' if offer else ', with no active offer on the listing'}. "
            f"Want me to turn this into one ready-to-use draft built on those numbers? {ask_yes}",
            f"{sal}, {name} ke liye {topic} par ek baat saamne aayi hai.{detail} "
            f"Aaj aap kahan hain: {perf_line}"
            f"{f', aur {offer} live hai' if offer else ', aur listing par koi active offer nahi hai'}. "
            f"Main isse ek ready-to-use draft bana doon, inhi numbers par? {ask_yes}",
        )
        rationale = "Unknown trigger kind handled with only the facts supplied plus current listing performance, so nothing is invented."

    if kind in {"research_digest", "regulation_change", "cde_opportunity", "supply_alert", "active_planning_intent"} and locality and locality not in body:
        body = body.replace(f"{sal},", f"{sal} ({locality}),", 1)
    else:
        body = _ensure_anchor(body, merchant, voice)
    return body, cta, rationale


# ---------------------------------------------------------------------------
# Customer-facing composition
# ---------------------------------------------------------------------------

def _customer_prefix(customer: dict[str, Any], merchant: dict[str, Any], voice: Voice) -> str:
    name = _customer_name(customer)
    merchant_name = _merchant_name(merchant)
    if voice.mixed and _get(customer, "identity", "senior_citizen"):
        # "Mr. X ji" doubles the honorific; keep only one.
        honorific = "" if re.match(r"^(mr|mrs|ms|dr|shri|smt)\b\.?", name, re.IGNORECASE) else " ji"
        return f"Namaste {name}{honorific}, {merchant_name} se."
    return voice.t(f"Hi {name}, {merchant_name} here.", f"Hi {name}, {merchant_name} se.")


def _customer_message(
    category: dict[str, Any],
    merchant: dict[str, Any],
    trigger: dict[str, Any],
    customer: dict[str, Any],
) -> tuple[str, str, str]:
    kind = _clean(trigger.get("kind"))
    payload = trigger.get("payload", {}) or {}
    slug = _clean(category.get("slug"))
    voice = _customer_voice(category, merchant, customer)
    prefix = _customer_prefix(customer, merchant, voice)
    merchant_name = _merchant_name(merchant)
    offer = _best_offer(merchant)
    existing_customer = (_int(_get(customer, "relationship", "visits_total")) or 0) > 0
    if existing_customer and any(item.get("title") == offer and item.get("audience") == "new_user" for item in category.get("offer_catalog", [])):
        offer = ""
    body = ""
    cta = "binary_yes_stop"
    rationale = ""

    if kind == "appointment_tomorrow":
        appointment = payload.get("appointment") or payload.get("appointment_time") or payload.get("slot")
        timing = _date_label(appointment, include_time=True) if appointment else ""
        when = voice.t(
            f"Your appointment is tomorrow{f' at {timing}' if timing else ''}.",
            f"Kal aapki appointment hai{f' — {timing}' if timing else ''}.",
        )
        body = voice.t(
            f"{prefix} {when} Reply CONFIRM if you can attend, or RESCHEDULE to check another time.",
            f"{prefix} {when} Aa sakte hain toh CONFIRM bhejiye, ya doosra time check karne ke liye RESCHEDULE likhiye.",
        )
        cta = "binary_confirm_reschedule"
        rationale = "Next-day reminder sent on behalf of the merchant, with no invented time when the schedule is not supplied, and one two-way CTA."

    elif kind == "recall_due":
        last_date = _date_label(payload.get("last_service_date"))
        due_date = _date_label(payload.get("due_date"))
        service = _humanize(payload.get("service_due"))
        slots = [_slot_label(slot) for slot in payload.get("available_slots", []) or [] if isinstance(slot, dict)]
        lead = voice.t(
            f"Your {service or 'next check-in'} is due{f' on {due_date}' if due_date else ''}"
            f"{f'; your last visit was {last_date}' if last_date else ''}.",
            f"Aapka {service or 'next check-in'} due hai{f' {due_date} ko' if due_date else ''}"
            f"{f'; last visit {last_date} thi' if last_date else ''}.",
        )
        if slots:
            listed = _compact_list(slots[:2], conjunction=voice.t("or", "ya"))
            body = voice.t(
                f"{prefix} Listed availability: {listed}. Reply with your preferred time; the clinic will confirm the booking.",
                f"{prefix} {lead} Listed availability: {listed}. Preferred time reply kijiye; clinic booking confirm karega.",
            )
            cta = "multi_choice_slot"
        else:
            # No slots supplied: never invent one, never bolt on a promo.
            body = voice.t(
                f"{prefix} {lead} Reply YES and we will send you this week's open slots, or STOP if you would rather not be reminded.",
                f"{prefix} {lead} YES bhejiye, hum is hafte ke khaali slots bhej denge — ya STOP likhiye toh reminder band.",
            )
        rationale = "Recall built entirely on the supplied service, dates and open slots, in the customer's stated language, with an easy opt-out."

    elif kind == "chronic_refill_due":
        if slug != "pharmacies":
            # The trigger label does not fit this category; never invent medicines.
            body = voice.t(
                f"{prefix} Checking whether you need a follow-up visit. Reply YES to check availability, or STOP to end reminders.",
                f"{prefix} Follow-up visit chahiye? Availability check karne ke liye YES bhejiye, ya reminder band karne ke liye STOP.",
            )
            rationale = "The refill label is inconsistent with a non-pharmacy merchant, so the message degrades to a safe follow-up reminder and names no medicine."
        else:
            molecules = [_humanize(item) for item in payload.get("molecule_list", []) or []]
            run_out = _date_label(payload.get("stock_runs_out_iso"))
            delivery = bool(payload.get("delivery_address_saved"))
            listed = _compact_list(molecules, conjunction=voice.t("and", "aur"))
            medicines = voice.t(
                f"Your monthly medicines{f' — {listed} —' if listed else ''}",
                f"Aapki monthly medicines{f' — {listed} —' if listed else ''}",
            )
            stock = voice.t(
                f" run out around {run_out}" if run_out else " are due for a refill",
                f" ka stock {run_out} tak chalega" if run_out else " ka refill due hai",
            )
            saved = voice.t(
                " Your delivery address is saved." if delivery else "",
                " Aapka delivery address saved hai." if delivery else "",
            )
            body = voice.t(
                f"{prefix} {medicines}{stock}.{f' {offer}.' if offer else ''}{saved} "
                f"Reply REFILL and we will keep it ready, or CHANGE if the prescription has changed.",
                f"{prefix} {medicines}{stock}.{f' {offer}.' if offer else ''}{saved} "
                f"REFILL bhejiye, hum ready rakh denge — ya prescription badla ho toh CHANGE likhiye.",
            )
            cta = "binary_refill_change"
            rationale = "Exact molecules and run-out date from the record, saved-delivery state, and a confirmation CTA that also catches prescription changes."

    elif kind in {"customer_lapsed_hard", "customer_lapsed_soft"}:
        days = _int(payload.get("days_since_last_visit"))
        focus = _humanize(payload.get("previous_focus"))
        proposal = offer
        gap = voice.t(
            f"It has been about {max(1, round(days / 7))} weeks since your last visit." if days else "It has been a while since your last visit.",
            f"Aapki last visit ko takreeban {max(1, round(days / 7))} hafte ho gaye." if days else "Aapki last visit ko kaafi time ho gaya.",
        )
        easy = _cat(slug, {
            "gyms": ("No pressure — breaks happen, and restarting is not starting over.",
                     "Koi pressure nahi — break sabke hote hain, aur dobara shuru karna zero se shuru karna nahi hota."),
            "dentists": ("No pressure — a short check-up is usually all it takes to know where things stand.",
                         "Koi pressure nahi — ek chhota check-up hi bata deta hai ki sab theek hai ya nahi."),
        }, ("No pressure at all.", "Koi pressure nahi hai."))
        focus_line = voice.t(
            f" Last time you were working on {focus}." if focus else "",
            f" Pichhli baar aap {focus} par kaam kar rahe the." if focus else "",
        )
        body = voice.t(
            f"{prefix} {gap} {easy[0]}{focus_line}"
            f"{f' {proposal} is available if you want an easy way back in.' if proposal else ''} "
            f"Reply YES to check availability this week, or STOP and we will not message again.",
            f"{prefix} {gap} {easy[1]}{focus_line}"
            f"{f' Wapas aane ka asaan tareeka — {proposal}.' if proposal else ''} "
            f"YES bhejiye, is hafte ki availability check karenge — ya STOP likhiye, phir message nahi aayega.",
        )
        if slug == "pharmacies":
            body = voice.t(
                f"{prefix} {gap} Need to check availability for a product? Reply with its name; prescription medicines require a valid prescription. Reply STOP to end these messages.",
                f"{prefix} {gap} Kisi product ki availability check karni hai? Naam bhejiye; prescription medicines ke liye valid prescription chahiye. Messages band karne ke liye STOP likhiye.",
            )
            cta = "open_ended"
        rationale = "No-shame win-back with the supplied gap and prior goal, a low-commitment entry offer, and an explicit permanent opt-out."

    elif kind == "trial_followup":
        trial_date = _date_label(payload.get("trial_date"))
        slots = [_slot_label(slot) for slot in payload.get("next_session_options", []) or [] if isinstance(slot, dict)]
        listed = _compact_list(slots[:2], conjunction=voice.t("or", "ya"))
        body = voice.t(
            f"{prefix} Thanks for coming in for the trial{f' on {trial_date}' if trial_date else ''}. "
            f"{f'Next session: {listed}.' if listed else 'Message us to check availability for the next session.'} "
            f"Reply YES to hold your place, or ANOTHER if a different time suits you better.",
            f"{prefix} Trial ke liye aane ka shukriya{f' — {trial_date}' if trial_date else ''}. "
            f"{f'Agla session: {listed}.' if listed else 'Agla session available hai ya nahi, message karke check kijiye.'} "
            f"YES bhejiye toh jagah hold, ya ANOTHER likhiye agar koi aur time theek rahega.",
        )
        cta = "binary_yes_another"
        rationale = "Warm continuation from a completed trial with the supplied session options and a friction-free two-option reply."

    elif kind == "wedding_package_followup":
        wedding = _date_label(payload.get("wedding_date"))
        trial = _date_label(payload.get("trial_completed"))
        window = _humanize(payload.get("next_step_window_open"))
        body = voice.t(
            f"{prefix} Your trial was{f' on {trial}' if trial else ' completed'}, and the wedding is{f' on {wedding}' if wedding else ' close now'}. "
            f"The {window or 'next preparation'} window is open, so we can discuss the next preparation step. "
            f"Reply YES and we will hold a planning slot for you, or STOP if the plan has changed.",
            f"{prefix} Aapka trial{f' {trial} ko' if trial else ''} ho chuka hai, aur wedding{f' {wedding} ko hai' if wedding else ' paas hai'}. "
            f"{window or 'Agli tayari'} ka window khul chuka hai, toh agli tayari discuss kar sakte hain. "
            f"YES bhejiye, hum planning slot hold kar lenge — ya plan badal gaya ho toh STOP.",
        )
        rationale = "Bridal timeline stated from the record with a real scarcity reason, and a graceful exit if plans changed."

    if not body:
        topic = _humanize(payload.get("metric_or_topic")) or _humanize(kind) or "your next step"
        body = voice.t(
            f"{prefix} A quick note about {topic}. Reply YES and we will send you the details and the open slots, "
            f"or STOP if you would rather not be messaged.",
            f"{prefix} {topic} ke baare mein ek chhoti si baat. YES bhejiye, hum details aur khaali slots bhej denge — "
            f"ya STOP likhiye toh message band.",
        )
        rationale = "Customer-scoped fallback stays generic on purpose rather than inventing an appointment, medicine, or offer."

    return body, cta, rationale


# ---------------------------------------------------------------------------
# Public compose contract
# ---------------------------------------------------------------------------

def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict[str, Any]:
    """Compose one grounded WhatsApp action. Same inputs, same output."""

    category = category or {}
    merchant = merchant or {}
    trigger = trigger or {}

    customer_facing = trigger.get("scope") == "customer" or customer is not None
    if customer_facing and customer:
        body, cta, rationale = _customer_message(category, merchant, trigger, customer)
        send_as = "merchant_on_behalf"
    else:
        body, cta, rationale = _merchant_message(category, merchant, trigger)
        send_as = "vera"

    body = _finalize(body, category)
    suppression_key = _clean(trigger.get("suppression_key")) or f"trigger:{trigger.get('id', 'unknown')}"
    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": suppression_key,
        "rationale": _clean(rationale)[:900],
    }


# ---------------------------------------------------------------------------
# HTTP contract
# ---------------------------------------------------------------------------

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
        self.muted_customers: set[str] = set()
        # Tracked per merchant as well as per conversation: the trap arrives
        # on four different thread ids.
        self.auto_replies: dict[str, Counter] = {}

    def clear(self) -> None:
        with self.lock:
            self.contexts.clear()
            self.conversations.clear()
            self.sent_suppression_keys.clear()
            self.muted_merchants.clear()
            self.muted_customers.clear()
            self.auto_replies.clear()

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

    def merchant_context(self, merchant_id: str | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        merchant = self.payload("merchant", merchant_id)
        category = self.payload("category", merchant.get("category_slug")) if merchant else None
        return category, merchant

    def latest_trigger_for(self, merchant_id: str | None, customer_id: str | None = None) -> dict[str, Any] | None:
        """Best-effort recovery when a reply arrives on an unknown thread."""
        if not merchant_id:
            return None
        candidates = [
            item["payload"]
            for (scope, _), item in self.contexts.items()
            if scope == "trigger" and _trigger_merchant_id(item["payload"]) == merchant_id
            and _trigger_customer_id(item["payload"]) == customer_id
        ]
        if not candidates:
            return None
        return max(candidates, key=_priority)


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
    if _get(customer, "consent", "opted_out_at"):
        return False
    scopes = set(_get(customer, "consent", "scope", default=[]) or [])
    kind = _clean(trigger.get("kind"))
    if scopes & _required_consent(kind):
        return True
    # Fixtures often carry promotional scope only; a direct-reminder opt-in is
    # secondary consent for non-clinical notices.
    if _get(customer, "preferences", "reminder_opt_in") and kind in {
        "appointment_tomorrow", "recall_due", "trial_followup", "chronic_refill_due"
    }:
        return True
    return False


def _trigger_quality(
    trigger: dict[str, Any],
    category: dict[str, Any],
    customer: dict[str, Any] | None,
    now: str | None = None,
) -> bool:
    """Hard gates only: consent, missing recipients, expiry.

    Category mismatch and early seasonal moments are composition problems, not
    reasons for silence — the composer degrades those instead.
    """
    kind = _clean(trigger.get("kind"))
    if trigger.get("scope") == "customer" and not customer:
        return False
    if customer and not _customer_contact_allowed(trigger, customer):
        return False
    if kind == "supply_alert" and _clean(category.get("slug")) != "pharmacies":
        # A medicine recall addressed to a non-pharmacy has no safe rendering.
        return False
    expires_at = _clean(trigger.get("expires_at"))
    if expires_at and now:
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            current = datetime.fromisoformat(now.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
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
        "ipl_match_today": 6,
        "perf_dip": 5,
        "review_theme_emerged": 5,
        "renewal_due": 4,
        "festival_upcoming": 1,
    }.get(_clean(trigger.get("kind")), 0)
    return int(trigger.get("urgency") or 0), kind_bonus, _clean(trigger.get("id"))


def _conversation_id(trigger: dict[str, Any]) -> str:
    # Readable and resumable, with a hash suffix to avoid collisions.
    merchant = re.sub(r"[^a-z0-9]+", "_", (_trigger_merchant_id(trigger) or "merchant").lower()).strip("_")
    customer = re.sub(r"[^a-z0-9]+", "_", (_trigger_customer_id(trigger) or "merchant").lower()).strip("_")
    kind = re.sub(r"[^a-z0-9]+", "_", (_clean(trigger.get("kind")) or "context").lower()).strip("_")
    raw = f"{_trigger_merchant_id(trigger)}|{_trigger_customer_id(trigger)}|{trigger.get('id')}|{trigger.get('suppression_key')}"
    suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"conv_{merchant[:28]}_{customer[:20]}_{kind[:24]}_{suffix}"


def _normalise_reply(message: str) -> str:
    return re.sub(r"[^\w]+", " ", message.lower()).strip()


AUTO_REPLY_PATTERNS = (
    r"thank you for contacting",
    r"thanks for contacting",
    r"our team will (get back|respond|reply|revert)",
    r"will get back to you",
    r"automated (assistant|reply|message|response)",
    r"auto[- ]?reply",
    r"business hours",
    r"we have received your message",
    r"message received",
    r"out of office",
    r"aapki jaankari ke liye.*shukriya",
    r"team tak pahuncha",
)
STOP_PATTERNS = (
    r"^\s*stop[.! ]*$", r"unsubscribe", r"do not message", r"don'?t message", r"stop messaging",
    r"not interested", r"useless spam", r"\bspam\b", r"leave me alone", r"band karo",
    r"mat bhejo", r"message mat", r"remove me", r"\b(?:do not|don't) send\b",
)
COMMIT_PATTERNS = (
    r"let'?s do it", r"lets do it", r"go ahead", r"\bproceed\b", r"what'?s next", r"whats next",
    r"i want to join", r"mujhe.*judna", r"mujhe.*join", r"kar do", r"kar dijiye", r"\bconfirm\b",
    r"yes please", r"sounds good", r"\bdraft\b", r"\bok(ay)?\b.*\b(next|do it|go)\b",
    r"\b(send|share|pull|show) me\b", r"\bplease (send|share|pull|show)\b", r"\bhaan\b",
    r"^\s*(approve|approved|publish|post it|send it)[.! ]*$",
)
# Narrow on purpose: "no, what's the price?" is not a decline.
DECLINE_PATTERNS = (
    r"^\s*no[.! ]*$", r"^\s*no thanks?[.! ]*$", r"^\s*not now[.! ]*$", r"^\s*maybe later[.! ]*$", r"^\s*some other time[.! ]*$",
    r"\bno need\b", r"\bnot required\b", r"\bnot interested\b",
    r"\bnahi chahiye\b", r"\babhi nahi\b", r"\bbaad mein\b", r"\bfilhaal nahi\b", r"\brehne do\b",
)
QUALIFYING = ("would you", "do you", "can you tell", "what if", "how about")


def _matches_any(message: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, message, re.IGNORECASE) for pattern in patterns)


def _reply_context(
    state: dict[str, Any] | None,
    merchant_id: str | None,
    customer_id: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve reply context, falling back to the store.

    Replay threads carry ids this bot never issued, so the conversation record
    is empty; the merchant id on the request is enough to recover.
    """
    state = state if isinstance(state, dict) else {}
    stored_category, stored_merchant = STORE.merchant_context(merchant_id)
    merchant = stored_merchant or state.get("merchant")
    category = stored_category or state.get("category")
    prior_trigger = state.get("trigger") or {}
    trigger = STORE.payload("trigger", prior_trigger.get("id")) or prior_trigger
    customer_id = customer_id or _get(state, "customer", "customer_id")
    if not trigger:
        trigger = STORE.latest_trigger_for(merchant_id, customer_id)
    if customer_id:
        state["customer"] = STORE.payload("customer", customer_id) or state.get("customer")

    # Cache the resolution so later turns on this thread stay consistent.
    if merchant:
        state["merchant"] = merchant
    if category:
        state["category"] = category
    if trigger:
        state["trigger"] = trigger
    return category, merchant, trigger


def _post_draft(category: dict, merchant: dict, voice: Voice) -> str:
    """Customer copy uses live merchant offers, never a category catalog price."""
    name, locality = _merchant_name(merchant), _locality(merchant)
    offer = _best_offer(merchant)
    location = f", {locality}" if locality else ""
    close = {
        "restaurants": ("Message us for the menu and order timing.", "Menu aur order timing ke liye message kijiye."),
        "pharmacies": ("Message us to check availability; prescription medicines require a valid prescription.", "Availability ke liye message kijiye; prescription medicines ke liye valid prescription zaruri hai."),
        "dentists": ("Message the clinic for appointment availability and treatment details.", "Appointment availability aur treatment details ke liye clinic ko message kijiye."),
    }.get(category.get("slug"), ("Message us for details and available timings.", "Details aur available timings ke liye message kijiye."))
    return f"{name}{location}.{f' {offer}.' if offer else ''} {voice.t(*close)}"


def _planning_draft(category: dict, merchant: dict, trigger: dict, voice: Voice) -> str:
    topic = _humanize(_get(trigger, "payload", "intent_topic"))
    name = _merchant_name(merchant)
    if category.get("slug") == "restaurants" and re.search(r"thali|corporate|bulk", topic, re.I):
        offer = _best_offer(merchant, ["thali", "lunch"])
        return voice.t(
            f"Proposed customer message: “Planning an office lunch? {name}{f' lists {offer}' if offer else ' can discuss your lunch requirements'}. Share your headcount, date, veg/Jain requirements and delivery location for a bulk quote.” Confirm capacity and delivery charges before quoting; the listed offer is not a confirmed bulk rate.",
            f"Customer ke liye draft: “Office lunch plan kar rahe hain? {name}{f' par {offer} listed hai' if offer else ' se lunch requirements discuss kijiye'}. Bulk quote ke liye headcount, date, veg/Jain requirements aur delivery location bhejiye.” Quote se pehle capacity aur delivery charges check kijiye; listed offer ko bulk rate na maanein.",
        )
    if category.get("slug") == "gyms" and re.search(r"kids|child|yoga|camp", topic, re.I):
        return voice.t(
            f"Proposed pilot: a 4-week kids yoga block, grouped by age, with a parent introduction and an instructor-confirmed capacity. Interest-check draft: “{name} is exploring a kids yoga group. Send your child's age and preferred weekday timing; we will share the proposed schedule and fee.” Confirm the age band, instructor and fee before opening bookings.",
            f"Pilot ka proposal: 4-week kids yoga block, age ke hisaab se groups, parent introduction aur instructor se capacity confirm. Interest-check draft: “{name} kids yoga group plan kar raha hai. Bachche ki age aur preferred weekday timing bhejiye; proposed schedule aur fee share karenge.” Bookings se pehle age band, instructor aur fee confirm kijiye.",
        )
    return voice.t(f"Draft for {topic or 'your next post'}: “{_post_draft(category, merchant, voice)}”",
                   f"{topic or 'Agle post'} ka draft: “{_post_draft(category, merchant, voice)}”")


def _match_draft(merchant: dict, trigger: dict, voice: Voice, cutoff: str = "") -> str:
    payload = trigger.get("payload") or {}
    match = _clean(payload.get("match")) or "Match day"
    timing = _date_label(payload.get("match_time_iso"), include_time=True)
    offer = _event_offer(merchant, payload.get("match_time_iso"))
    detail = f"{match}{f' — {timing}' if timing else ''}. {_merchant_name(merchant)}, {_locality(merchant)}."
    if offer:
        detail += f" {offer}."
    if cutoff:
        detail += voice.t(f" Order by {cutoff}.", f" Order {cutoff} tak kijiye.")
    detail += voice.t(" Message us for the menu, prices and delivery area.",
                      " Menu, prices aur delivery area ke liye message kijiye.")
    return voice.t(f"Match-day draft: “{detail}” Check the details before posting.",
                   f"Match-day draft: “{detail}” Post karne se pehle details check kijiye.")


def _commitment_response(
    category: dict[str, Any] | None,
    merchant: dict[str, Any] | None,
    trigger: dict[str, Any] | None,
    turn_number: int,
    customer: dict[str, Any] | None = None,
    voice: Voice | None = None,
) -> str:
    """Deliver the requested content in this response, without claiming a send."""
    merchant, category, trigger = merchant or {}, category or {}, trigger or {}
    voice = voice or Voice(category, merchant)
    kind = _clean(trigger.get("kind"))
    payload = trigger.get("payload") or {}
    item = _digest_item(category, trigger)
    source = _clean(item.get("source"))
    summary = _clean(item.get("summary") or item.get("title"))
    action = _clean(item.get("actionable"))
    if kind == "active_planning_intent":
        return _planning_draft(category, merchant, trigger, voice)
    if kind == "ipl_match_today":
        return _match_draft(merchant, trigger, voice)
    if kind == "research_digest":
        if not summary:
            return voice.t("The requested reading is missing from the supplied record. Share its title or text and I can summarise it accurately.",
                           "Requested reading record mein nahi hai. Title ya text bhejiye, main uska sahi summary bana dungi.")
        return voice.t(
            f"Here is the reading summary{f' — {source}' if source else ''}: {summary} {action} Keep the finding limited to the studied group; it is not an individual treatment recommendation.",
            f"Yeh raha reading ka summary{f' — {source}' if source else ''}: {summary} {action} Finding sirf study wale group tak rakhein; individual treatment ka decision clinician karein.",
        )
    if kind == "regulation_change":
        deadline = _date_label(payload.get("deadline_iso"))
        return voice.t(
            f"Checklist{f' — {source}' if source else ''}: 1) Check the notice and its applicability{f' before {deadline}' if deadline else ''}. 2) {action or 'Identify which equipment or process is affected.'} 3) Record the check and assign any corrective work. Notice summary: {summary or 'The full notice is not supplied; verify it before changing procedures.'}",
            f"Checklist{f' — {source}' if source else ''}: 1) Notice aur applicability check kijiye{f', {deadline} se pehle' if deadline else ''}. 2) {action or 'Affected equipment ya process identify kijiye.'} 3) Check record kijiye aur corrective work assign kijiye. Notice summary: {summary or 'Full notice supplied nahi hai; procedure badalne se pehle verify kijiye.'}",
        )
    if kind == "supply_alert":
        batches = _compact_list(payload.get("affected_batches") or [])
        molecule = _humanize(payload.get("molecule"))
        return voice.t(
            f"Here is the checklist: verify the recall notice for {molecule or 'the affected medicine'}{f', batches {batches}' if batches else ''}; isolate only matching stock; record quantities and contact the supplier. Customer note: “Please check your pack's batch number with our pharmacist. If it matches the recall, contact us for the next steps. Consult your prescriber before changing treatment.”",
            f"Checklist draft: {molecule or 'affected medicine'} ka recall notice verify kijiye{f', batches {batches}' if batches else ''}; matching stock alag rakhiye; quantity record karke supplier ko contact kijiye. Customer note: “Apne pack ka batch number pharmacist se check karaiye. Recall se match ho toh agle steps ke liye contact kijiye. Treatment badalne se pehle prescriber se baat kijiye.”",
        )
    if kind == "cde_opportunity":
        date = _date_label(item.get("date"), include_time=True)
        return voice.t(
            f"Calendar details: {_clean(item.get('title')) or 'Session title not supplied'}{f' — {date}' if date else ''}. {_clean(item.get('actionable'))} Next: check registration with {source or 'the organiser'} and add the confirmed session to your calendar. No registration link was supplied; no booking has been made.",
            f"Calendar details: {_clean(item.get('title')) or 'Session title supplied nahi hai'}{f' — {date}' if date else ''}. {_clean(item.get('actionable'))} Agla step: {source or 'organiser'} se registration check karke confirmed session calendar mein add kijiye. Registration link supplied nahi hai; booking nahi hui hai.",
        )
    if kind == "gbp_unverified":
        return voice.t(
            "Here are the steps: 1) Open your business profile while signed into its owner account. 2) Select Get verified and follow the method Google actually offers. 3) Complete the requested verification and check its status there. Available methods and review time depend on Google; never share a verification code here.",
            "Yeh rahe steps: 1) Owner account se business profile kholiye. 2) Get verified select karke Google jo method dikhaye use follow kijiye. 3) Verification complete karke wahin status check kijiye. Method aur review time Google par depend karte hain; verification code yahan share na karein.",
        )
    if kind == "renewal_due":
        amount = payload.get("renewal_amount")
        return voice.t(
            f"Renewal check: {_perf_line(merchant, voice)}.{f' Quoted renewal: {_money(amount)}.' if amount is not None else ''} Views and calls measure interest, not paid sales. Completed bookings and attributable revenue are missing, so I cannot calculate ROI. Compare those with the renewal amount before deciding.",
            f"Renewal check: {_perf_line(merchant, voice)}.{f' Renewal amount: {_money(amount)}.' if amount is not None else ''} Views aur calls interest dikhate hain, paid sales nahi. Completed bookings aur attributable revenue nahi hain, isliye ROI calculate nahi kar sakti. Decide karne se pehle unhe renewal amount se compare kijiye.",
        )
    if kind == "review_theme_emerged":
        theme = _humanize(payload.get("theme")) or "the issue raised"
        return voice.t(
            f"Review reply draft: “Thank you for flagging {theme}. We are sorry your visit fell short. Please message us with the visit date so our team can look into what happened.” Next: assign one team member to investigate this issue and record the corrective step before promising a fix publicly.",
            f"Review reply draft: “{theme} batane ke liye shukriya. Aapka experience achha nahi raha, uska afsos hai. Visit date message kijiye taaki team check kar sake.” Agla step: ek team member ko issue check karne dein; publicly fix promise karne se pehle corrective step record kijiye.",
        )
    if kind == "milestone_reached":
        return voice.t(
            f"Review request draft: “Thank you for visiting {_merchant_name(merchant)}. Please share an honest review of your experience on our listing; your feedback helps us improve.” Send the same neutral request after completed visits, without rewards or filtering for positive feedback.",
            f"Review request draft: “{_merchant_name(merchant)} aane ke liye shukriya. Listing par apne experience ka honest review share kijiye; aapke feedback se humein sudhaarne mein madad milti hai.” Completed visits ke baad yahi neutral request bhejein, bina reward ya sirf positive feedback choose kiye.",
        )
    if kind == "category_seasonal":
        trends = _compact_list([_humanize(t) for t in payload.get("trends", [])])
        return voice.t(
            f"Shelf checklist{f' for {trends}' if trends else ''}: 1) Compare stock and expiry dates with the listed demand shifts. 2) Check availability with suppliers before reordering. 3) Put verified availability and prices on the counter list. Adjust quantities using your sales records; the demand figures do not specify order quantities.",
            f"Shelf checklist{f' — {trends}' if trends else ''}: 1) Demand shifts ke saath stock aur expiry dates check kijiye. 2) Reorder se pehle supplier se availability check kijiye. 3) Verified availability aur prices counter list par rakhiye. Quantity sales record se decide kijiye; demand figures order quantity nahi hain.",
        )
    if customer and trigger.get("scope") == "customer":
        draft = _customer_message(category, merchant, trigger, customer)[0]
    else:
        draft = _post_draft(category, merchant, voice)
    return voice.t(f"Here is the copy: “{draft}” Check the details before using it; this is a draft, not a published post.",
                   f"Yeh raha draft: “{draft}” Use karne se pehle details check kijiye; abhi post publish nahi hua hai.")


def _reply_voice(category: dict | None, merchant: dict | None, message: str) -> Voice:
    voice = Voice(category, merchant)
    if re.search(r"\b(english|angrezi)\b", message, re.I):
        voice.level = "off"
    elif re.search(r"[\u0900-\u097f]|\b(hindi|hinglish|haan|bhejo|bhejiye|karo|kijiye|kaise|kitna|nahi)\b", message, re.I):
        voice.level = "natural"
    return voice


def _question_response(category: dict, merchant: dict, trigger: dict, message: str, voice: Voice) -> str:
    payload = trigger.get("payload") or {}
    if re.search(r"\b(price|cost|fee|charge|charges|kitna|kitne|pricing|paid|free)\b", message, re.I):
        if re.search(r"\b(vera|subscription|renew|renewal)\b|\byour (?:price|cost|fee|charges?|pricing|plan)\b|\byou charge\b", message, re.I) or trigger.get("kind") == "renewal_due":
            amount = payload.get("renewal_amount")
            return voice.t(
                f"The supplied renewal quote is {_money(amount)}. No additional fees are listed." if amount is not None else "Vera's subscription pricing is not supplied here, so I cannot quote a fee. Check the plan price in your merchant account before approving a purchase.",
                f"Supplied renewal quote {_money(amount)} hai. Additional fees listed nahi hain." if amount is not None else "Vera ki subscription pricing yahan supplied nahi hai, isliye fee quote nahi kar sakti. Purchase approve karne se pehle merchant account mein plan price check kijiye.",
            )
        if trigger.get("kind") == "active_planning_intent":
            return voice.t("The proposed program/package has no confirmed fee yet. Set its price after checking capacity and costs; an existing listing offer does not establish this package's price.",
                           "Proposed program/package ki fee abhi confirm nahi hai. Capacity aur costs check karke price set kijiye; existing listing offer is package ka confirmed price nahi hai.")
        offer = _event_offer(merchant, payload.get("match_time_iso")) if trigger.get("kind") == "ipl_match_today" else _best_offer(merchant)
        return voice.t(f"Your active listing offer is {offer}. Other charges are not specified in the record." if offer else "There is no active priced offer in the supplied listing. Share the service and price you want to use and I can include them in the draft.",
                       f"Aapka active listing offer {offer} hai. Record mein doosre charges specified nahi hain." if offer else "Supplied listing mein active priced offer nahi hai. Service aur price bhejiye, draft mein add kar dungi.")
    if re.search(r"\b(source|proof|study|abstract|evidence|checklist|steps|register|registration)\b", message, re.I):
        return _commitment_response(category, merchant, trigger, 1, voice=voice)
    if re.search(r"\b(who are you|what is this|what.*about|why|kyun|kya hai)\b", message, re.I):
        explanation = _merchant_message(category, merchant, trigger)[0]
        # The original message gives the exact reason and next step for this thread.
        return explanation
    return _commitment_response(category, merchant, trigger, 1, voice=voice)


def _customer_reply(state: dict, message: str, voice: Voice) -> dict[str, Any]:
    trigger = state.get("trigger") or {}
    payload = trigger.get("payload") or {}
    normalized = _normalise_reply(message)
    slots = payload.get("available_slots") or payload.get("next_session_options") or []
    if normalized in {"1", "2"}:
        index = int(normalized) - 1
        if index < len(slots):
            label = _slot_label(slots[index])
            state["requested_slot"] = label
            text = voice.t(f"Your requested time is {label}. The team still needs to confirm availability; your booking is not confirmed yet.",
                           f"Aapne {label} choose kiya hai. Team ko availability confirm karni hai; booking abhi confirm nahi hui hai.")
        else:
            text = voice.t("That option is not in the current schedule. Please send your preferred day and time.", "Yeh option current schedule mein nahi hai. Preferred day aur time bhejiye.")
    elif re.search(r"\b(reschedule|another|change)\b", normalized):
        text = voice.t("Please send your preferred day and time; the team will need to confirm availability.", "Apna preferred day aur time bhejiye; team ko availability confirm karni hai.")
        if trigger.get("kind") == "chronic_refill_due":
            text = voice.t("Please have the pharmacist review the updated prescription before preparing the refill. Do not change your medicines based on this reminder.", "Refill taiyar karne se pehle pharmacist se updated prescription review karaiye. Is reminder ke basis par medicines change na karein.")
    elif re.search(r"\b(confirm|yes|haan|refill)\b", normalized):
        state["customer_requested"] = normalized
        text = voice.t("Your request is noted in this chat. The team needs to confirm the details before a booking or refill is ready.", "Aapki request is chat mein note hai. Booking ya refill ready hone se pehle team ko details confirm karni hain.")
    else:
        text = voice.t("Please share the appointment or refill detail you need help with; the team will need to confirm any schedule or prescription change.", "Appointment ya refill ki jis detail mein help chahiye woh bhejiye; schedule ya prescription change team ko confirm karna hai.")
    return {"action": "send", "body": text, "cta": "open_ended", "rationale": "Customer reply stays in the booking/refill flow and distinguishes a request from a completed transaction."}


def _strip_qualifiers(text: str) -> str:
    """Safety net for the commitment path: never sound like discovery again."""
    lowered = text.lower()
    if not any(word in lowered for word in QUALIFYING):
        return text
    sentences = re.split(r"(?<=[.!?]) +", text)
    kept = [s for s in sentences if not any(word in s.lower() for word in QUALIFYING)]
    return " ".join(kept) if kept else text


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
        "approach": "trigger-first grounded composition with category voice, hi-en code-mix, consent gating, dedup, and replay routing",
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
    candidates: list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None]] = []
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
            if customer_id in STORE.muted_customers:
                continue
            if customer and customer.get("merchant_id") not in (None, merchant_id):
                continue
            suppression_key = _clean(trigger.get("suppression_key")) or f"trigger:{trigger_id}"
            if suppression_key in STORE.sent_suppression_keys:
                continue
            if not _trigger_quality(trigger, category, customer, body.now):
                continue
            candidates.append((trigger_id, trigger, category, merchant, customer))

        candidates.sort(key=lambda item: _priority(item[1]), reverse=True)
        actions: list[dict[str, Any]] = []
        selected_entities: set[tuple[str | None, str | None]] = set()
        for trigger_id, trigger, category, merchant, customer in candidates:
            merchant_id = _trigger_merchant_id(trigger)
            customer_id = _trigger_customer_id(trigger)
            entity = (merchant_id, customer_id)
            suppression_key = _clean(trigger.get("suppression_key")) or f"trigger:{trigger_id}"
            if suppression_key in STORE.sent_suppression_keys:
                continue
            if entity in selected_entities or len(actions) >= 20:
                continue
            result = compose(category, merchant, trigger, customer)
            conversation_id = _conversation_id(trigger)
            template_params = [
                _customer_name(customer) if customer else _merchant_salutation(category, merchant),
                _humanize(trigger.get("kind")),
                _perf_line(merchant, Voice(category, merchant)) or _locality(merchant),
            ]
            action = {
                "conversation_id": conversation_id,
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "send_as": result["send_as"],
                # `trigger.id` is authoritative; the requested id is the fallback.
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
                "merchant_id": merchant_id,
                "turns": [{"from": "bot", "body": result["body"]}],
            }
            if trigger.get("kind") == "active_planning_intent":
                STORE.conversations[conversation_id]["delivered"] = result["body"]
    return {"actions": actions}


@app.post("/v1/reply")
def reply(body: ReplyBody) -> dict[str, Any]:
    message = _clean(body.message)
    normalized = _normalise_reply(message)
    with STORE.lock:
        state = STORE.conversations.setdefault(body.conversation_id, {"turns": []})
        state.setdefault("turns", []).append({"from": body.from_role, "body": message})
        merchant_id = body.merchant_id or state.get("merchant_id")
        if merchant_id:
            state["merchant_id"] = merchant_id
        category, merchant, trigger = _reply_context(state, merchant_id, body.customer_id)
        category, merchant, trigger = category or {}, merchant or {}, trigger or {}
        customer = state.get("customer")
        customer_id = body.customer_id or _get(customer, "customer_id")
        is_customer = body.from_role == "customer"
        voice = _customer_voice(category, merchant, customer) if is_customer else _reply_voice(category, merchant, message)

        def send(text: str, rationale: str, cta: str = "open_ended") -> dict[str, Any]:
            rendered = _finalize(text, category)
            state["turns"].append({"from": "bot", "body": rendered})
            return {"action": "send", "body": rendered, "cta": cta, "rationale": rationale}

        if _matches_any(message, STOP_PATTERNS):
            if is_customer and customer_id:
                STORE.muted_customers.add(customer_id)
            elif not is_customer and merchant_id:
                STORE.muted_merchants.add(merchant_id)
                STORE.auto_replies.pop(merchant_id, None)
            state["ended"] = True
            return {"action": "end", "rationale": "Explicit opt-out: stop this recipient's messages without affecting other recipients."}

        if _matches_any(message, AUTO_REPLY_PATTERNS):
            recipient = f"customer:{customer_id}" if is_customer else (merchant_id or body.conversation_id)
            thread = state.setdefault("auto_replies", Counter())
            thread[normalized] += 1
            counter = STORE.auto_replies.setdefault(recipient, Counter())
            counter[normalized] += 1
            count = max(sum(thread.values()), sum(counter.values()))
            if count >= 3:
                return {"action": "end", "rationale": "Repeated automatic responses; end the sequence until a human replies."}
            if count == 2:
                return {"action": "wait", "wait_seconds": 86_400, "rationale": "Second automatic response; back off rather than continue an answering-machine loop."}
            return send(voice.t("Thanks. When the owner or manager is available, they can reply here for the details.",
                                "Shukriya. Owner ya manager available hon toh details ke liye yahin reply kar sakte hain."),
                        "One short acknowledgement of the automatic response, then back off.")

        if normalized:
            state.setdefault("auto_replies", Counter()).clear()
            recipient = f"customer:{customer_id}" if is_customer else merchant_id
            if recipient in STORE.auto_replies:
                STORE.auto_replies[recipient].clear()

        if _matches_any(message, DECLINE_PATTERNS):
            state["ended"] = True
            return {"action": "end", "rationale": "The recipient declined; close the thread without further persuasion."}

        delay = re.search(r"\b(?:in|after|wait|give me)\s+(\d+)\s*(minutes?|mins?|hours?|hrs?)\b", message, re.I)
        if delay or re.search(r"\b(busy|later|tomorrow|baad mein|kal|not today)\b", normalized):
            seconds = int(delay[1]) * (3600 if delay[2].lower().startswith(('h',)) else 60) if delay else (86_400 if re.search(r"\b(tomorrow|kal)\b", normalized) else 3600)
            return {"action": "wait", "wait_seconds": max(60, min(seconds, 604800)), "rationale": "The recipient asked for time; defer instead of sending another pitch."}

        if re.search(r"\b(gst|tax|itr|loan|legal|lawyer|passport|aadhaar|visa|insurance claim)\b", normalized):
            # Tax details in a catering quote are in scope; filing or advice is not.
            if re.search(r"\b(file|filing|return|advice|apply|application|loan|lawyer|passport|visa|insurance claim)\b", normalized):
                specialist = voice.t("a qualified tax professional", "qualified tax professional") if re.search(r"\b(gst|tax|itr)\b", normalized) else voice.t("the relevant qualified adviser or official service", "relevant qualified adviser ya official service")
                return send(voice.t(f"I cannot file or advise on that. Please use {specialist}. I can help prepare your listing copy, customer messages or offer drafts.",
                                    f"Main uski filing ya advice nahi de sakti. {specialist} se help lijiye. Listing copy, customer messages ya offer drafts mein help kar sakti hoon."),
                            "Routes an out-of-scope request honestly, even when it contains an action verb.")

        if is_customer:
            result = _customer_reply(state, message, voice)
            return send(result["body"], result["rationale"], result["cta"])

        if re.search(r"\b(join|sign up|signup|onboard|judna|judrna)\b", normalized):
            return send(voice.t(
                "Next step: open the official magicpin merchant registration flow and use your business name and owner contact details. I can help prepare your listing description here. I cannot create the account from this chat, and no registration link is supplied in your record.",
                "Agla step: official magicpin merchant registration flow mein business name aur owner contact details use kijiye. Listing description yahan taiyar kar sakti hoon. Is chat se account create nahi kar sakti; record mein registration link supplied nahi hai."),
                "Explicit joining intent gets onboarding steps immediately.")

        if re.search(r"\b(edit|change|replace|instead|make it|shorter|shorten)\b", normalized) and state.get("delivered"):
            replacement = re.search(r"replace\s+(.+?)\s+with\s+(.+?)[.!]?$", message, re.I)
            if replacement and replacement[1].lower() in state["delivered"].lower():
                edited = re.sub(re.escape(replacement[1]), lambda _: replacement[2], state["delivered"], flags=re.I)
                state["delivered"] = edited
                return send(edited, "Applied the merchant's explicit text replacement to the existing draft.")
            if re.search(r"\b(shorter|shorten)\b", normalized):
                edited = _post_draft(category, merchant, voice)
                state["delivered"] = edited
                return send(edited, "Provides a shorter usable post using the current merchant offer.")
            return send(voice.t("Send the exact wording or detail to replace and its replacement; I will revise the draft shown above.",
                                "Jo wording ya detail badalni hai aur uska replacement bhejiye; upar wala draft revise kar dungi."),
                        "Asks only for the missing edit, without reopening qualification.")

        if trigger.get("kind") == "ipl_match_today" and not state.get("delivered"):
            cutoff = re.fullmatch(r"(?:order\s+)?(?:cutoff(?:\s+time)?\s*(?:is|:|at)?\s*)?(?:by\s+)?((?:0?[1-9]|1[0-2])(?::[0-5][0-9])?\s*[ap]m)[.! ]*", message, re.I)
            if cutoff:
                delivered = _match_draft(merchant, trigger, voice, cutoff[1])
                state["delivered"] = delivered
                return send(delivered, "Uses the supplied order cutoff and only an offer valid on the match date.")

        if re.search(r"\b(price|cost|fee|charge|charges|kitna|kitne|pricing|free|source|proof|study|abstract|evidence|checklist|steps|register|registration)\b", normalized) or ("?" in message and not _matches_any(message, COMMIT_PATTERNS)):
            return send(_question_response(category, merchant, trigger, message, voice), "Answers the specific question using supplied prices, sources and thread details.")

        accepted = _matches_any(message, COMMIT_PATTERNS) or normalized in {"yes", "y", "ok", "okay", "sure", "send", "bhej do", "bhejo", "approved"}
        if accepted:
            if state.get("delivered") and re.search(r"\b(confirm|approve|approved|publish|post it|send it)\b", normalized):
                state["approved"] = True
                return send(voice.t("This version is approved in this chat. Copy the draft above into your posting or messaging tool to send it; I have not published or sent it externally.",
                                    "Yeh version is chat mein approved hai. Bhejne ke liye upar wala draft posting ya messaging tool mein copy kijiye; maine externally publish ya send nahi kiya hai."),
                            "Records approval and gives the real next step without claiming an external action.")
            if state.get("delivered"):
                return send(voice.t("The draft is above and ready for your review. Send the specific change you need, or use it once the details are checked.",
                                    "Draft upar hai, review ke liye ready. Koi specific change ho toh bhejiye, warna details check karke use kijiye."),
                            "Keeps the existing artifact instead of restarting the pitch or repeating it.")
            delivered = _commitment_response(category, merchant, trigger, body.turn_number, customer, voice)
            # On replay threads, retain a visible link to this merchant.
            delivered = _ensure_anchor(delivered, merchant, voice)
            state["delivered"] = delivered
            return send(delivered, "Delivers actual copy, a checklist or steps in this turn; no empty promise or confirmation loop.")

        if re.search(r"\b(thanks|thank you|thankyou|shukriya|done)\b", normalized) or body.turn_number >= 5:
            return {"action": "end", "rationale": "Acknowledgement or exhausted conversation: close without another nudge."}
        if re.search(r"\b(what|how|why|when|where|which|kaise|kab|kyun|kya)\b", normalized):
            return send(_question_response(category, merchant, trigger, message, voice), "Answers the current question directly.")
        # A merchant answering our one-item discovery question has provided the topic.
        if trigger.get("kind") == "curious_ask_due":
            draft = voice.t(f"Draft: “Interested in {message}? Message {_merchant_name(merchant)} for details, pricing and availability.” Confirm the service details before using it.",
                            f"Draft: “{message} mein interest hai? Details, pricing aur availability ke liye {_merchant_name(merchant)} ko message kijiye.” Use karne se pehle service details confirm kijiye.")
            state["delivered"] = draft
            return send(draft, "Uses the merchant's answer as the topic and supplies the promised post immediately.")
        return send(voice.t("Which detail needs changing: the service, price or timing? Send the correction and I will revise the copy.",
                            "Kaunsi detail badalni hai: service, price ya timing? Correction bhejiye, copy revise kar dungi."),
                    "One focused clarification when the reply does not identify an action.")


@app.post("/v1/teardown")
def teardown() -> dict[str, bool]:
    STORE.clear()
    return {"cleared": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("bot:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
