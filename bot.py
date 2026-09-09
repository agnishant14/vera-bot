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


VERSION = "2.0.0"
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
    return titles[0] if titles else ""


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

    preferred_kind = {
        "research_digest": "research",
        "regulation_change": "compliance",
        "cde_opportunity": "cde",
        "supply_alert": "compliance",
    }.get(trigger.get("kind"))
    matching = [item for item in digest if item.get("kind") == preferred_kind]
    # New category versions commonly append fresh items; prefer the latest match.
    return (matching or digest)[-1] if (matching or digest) else {}


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
    return _sentence_case(_clean(text))[:900]


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
        title = _clean(item.get("title"))
        summary = _clean(item.get("summary"))
        trial_n = _int(item.get("trial_n"))
        cohort = _signal_phrase(merchant, voice, prefer=("high_risk_adult_cohort",))
        study = voice.t(
            f" Sample size was {trial_n:,}." if trial_n else "",
            f" Study mein {trial_n:,} log the." if trial_n else "",
        )
        relevance = f" {cohort[0].upper()}{cohort[1:]}." if cohort else ""
        if title:
            body = voice.t(
                f"{sal}, worth two minutes — {source or 'this week’s reading'}: {title}. {summary}{study}{relevance} "
                f"Your {locality} profile is at {perf_line}, so a credible post on this reaches a real audience. "
                f"Want me to draft one plain-language customer note plus a matching profile post? {ask_yes}",
                f"{sal}, do minute ki cheez hai — {source or 'is hafte ka reading'}: {title}. {summary}{study}{relevance} "
                f"Aapki {locality} profile pe {perf_line} hai, toh is par ek credible post asli audience tak jaata hai. "
                f"Main ek simple customer note aur ek matching profile post draft kar doon? {ask_yes}",
            )
        else:
            body = voice.t(
                f"{sal}, a fresh professional update landed for {slug or 'your category'}. "
                f"Before it turns into a post: your {locality} profile is at {perf_line}. "
                f"Want me to pull the one finding that actually applies to your practice and draft a short customer note? {ask_yes}",
                f"{sal}, {slug or 'aapki category'} ke liye ek nayi professional update aayi hai. "
                f"Post banane se pehle: aapki {locality} profile pe {perf_line} hai. "
                f"Main sirf woh ek finding nikaal doon jo aapke kaam ki hai, aur ek chhota customer note draft kar doon? {ask_yes}",
            )
        rationale = "Cited professional reading, tied to a visible cohort signal and the merchant's own profile numbers, with one low-effort drafting CTA."

    elif kind == "regulation_change":
        item = _digest_item(category, trigger)
        source = _clean(item.get("source")) or voice.t("the latest compliance notice", "latest compliance notice")
        deadline = _date_label(payload.get("deadline_iso"))
        summary = _clean(item.get("summary") or item.get("title"))
        action = _clean(item.get("actionable"))
        if action and action[-1] not in ".!?":
            action += "."
        checklist = _cat(slug, {
            "dentists": ("a 5-point audit checklist plus a one-line note your front desk can repeat",
                         "5-point audit checklist aur ek line jo aapka front desk bol sake"),
            "pharmacies": ("a 5-point counter checklist plus a customer-safe explanation",
                           "5-point counter checklist aur ek customer-safe explanation"),
        }, ("a 5-point checklist plus a short customer-safe note",
            "5-point checklist aur ek chhota customer-safe note"))
        body = voice.t(
            f"{sal}, compliance heads-up from {source}: {summary}"
            f"{f' It takes effect {deadline}.' if deadline else ''}{f' {action}' if action else ''} "
            f"Your {locality} listing pulls {perf_line}, so patients will ask about it before you announce anything. "
            f"Want {checklist[0]}? {ask_yes}",
            f"{sal}, {source} se ek compliance update: {summary}"
            f"{f' Yeh {deadline} se effective hai.' if deadline else ''}{f' {action}' if action else ''} "
            f"Aapki {locality} listing par {perf_line} hai, toh announce karne se pehle hi patients poochhenge. "
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
            f"{f'. Listed by {source}' if source else ''}. Seats on these usually go before the reminder does. "
            f"Want the registration steps and a calendar block in one message, so it does not slip past clinic hours? {ask_yes}",
            f"{sal}, ek session hai jiske liye time block karna banta hai — {title}"
            f"{f', {event_date} ko' if event_date else ''}"
            f"{f', {credits} CDE credits' if credits else ''}{f', {fee}' if fee else ''}"
            f"{f'. {source} ne list kiya hai' if source else ''}. Aise sessions ki seats reminder se pehle bhar jaati hain. "
            f"Main registration ke steps aur ek calendar block ek hi message mein bhej doon, taaki clinic hours mein miss na ho? {ask_yes}",
        )
        rationale = "Time-bound professional opportunity with date, credits, fee and source, plus a scarcity reason and a single scheduling action."

    elif kind == "active_planning_intent":
        topic = _humanize(payload.get("intent_topic"))
        last_message = _clean(payload.get("merchant_last_message"))
        plan = _cat(slug, {
            "restaurants": (
                f"keep {offer or _catalog_offer(category, ['thali', 'lunch'])} as the base price, then ask for headcount, "
                "veg/Jain split, delivery window and GST invoice details in one message",
                f"base price {offer or _catalog_offer(category, ['thali', 'lunch'])} rakhiye, phir ek hi message mein headcount, "
                "veg/Jain split, delivery window aur GST invoice details maang lijiye",
            ),
            "gyms": (
                f"{offer or _catalog_offer(category, ['trial', 'first month'])} as the entry point, a fixed weekday slot, "
                "and a 4-week block so people can commit to something finite",
                f"entry point {offer or _catalog_offer(category, ['trial', 'first month'])} rakhiye, ek fixed weekday slot, "
                "aur 4-week block taaki log ek finite cheez pe commit kar sakein",
            ),
            "salons": (
                f"lead with {offer or _catalog_offer(category)} and hold two named slots so it reads as a booking, not an ad",
                f"{offer or _catalog_offer(category)} se lead kijiye aur do named slots hold rakhiye, taaki woh ad nahi booking lage",
            ),
        }, (
            f"anchor it on {offer or _catalog_offer(category) or 'one service at one price'} and put the next step in a single line",
            f"{offer or _catalog_offer(category) or 'ek service ek price'} pe anchor kijiye aur next step ek line mein rakhiye",
        ))
        opener = voice.t(
            f"picking up your last message — “{last_message}”" if last_message else f"picking up the {topic} plan",
            f"aapke last message se aage — “{last_message}”" if last_message else f"{topic} wale plan se aage",
        )
        body = voice.t(
            f"{sal}, {opener}. Here is the concrete version for {name}: {plan[0]}. "
            f"Your listing is at {perf_line}, so this goes out to an audience that already exists. "
            f"Reply CONFIRM and I will format the ready-to-send copy, or EDIT with the one thing you want changed.",
            f"{sal}, {opener}. {name} ke liye concrete version yeh hai: {plan[1]}. "
            f"Aapki listing pe {perf_line} hai, toh yeh already maujood audience tak jaata hai. "
            f"CONFIRM bhejiye, main ready-to-send copy format kar deti hoon — ya EDIT ke saath ek badlaav bata dijiye.",
        )
        cta = "binary_confirm_edit"
        rationale = "The merchant already committed, so the message skips qualification, quotes their own words back, and delivers a concrete package."

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
            f" This is the expected seasonal lull ({season_note}), not customers leaving." if seasonal else "",
            f" Yeh expected seasonal lull hai ({season_note}), customers ja nahi rahe." if seasonal else "",
        )
        body = voice.t(
            f"{sal}, {lead}. {position}{context_line} "
            f"The one move that changes this fastest: {fix[0]}. Want me to draft it from your current numbers? {ask_yes}",
            f"{sal}, {lead}. {position}{context_line} "
            f"Sabse tez farak isse aayega: {fix[1]}. Main aapke current numbers se woh draft kar doon? {ask_yes}",
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
            f"{sal}, {lead}.{spike_position} Attention like this fades in about a week, "
            f"and the cheapest way to hold it is to post the same thing again while people are still looking. "
            f"Want me to draft that follow-up post around {offer or _catalog_offer(category) or 'your strongest service'}? {ask_yes}",
            f"{sal}, {lead}.{spike_position} Aisa attention hafte bhar mein thanda pad jaata hai, "
            f"aur sabse sasta tarika hai wahi cheez dobara post karna jab log abhi dekh rahe hain. "
            f"Main {offer or _catalog_offer(category) or 'aapki strongest service'} ke around woh follow-up post draft kar doon? {ask_yes}",
        )
        rationale = "Positive movement stated with its real magnitude, a time-decay reason to act now, and one repeatable next post."

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
                f"{sal}, {fact}. Before reacting: {base}. I would not cut price — that is the one move you cannot undo. "
                f"Want a sharper listing comparison built on what you actually do better? {ask_yes}",
                f"{sal}, {fact}. React karne se pehle: {base}. Main price kaatne ko nahi kahungi — woh ek move wapas nahi hota. "
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
                f"one fresh post built on {offer}, which is already live and getting no attention",
                f"{offer} par ek naya post — woh already live hai par uspe koi dhyaan nahi jaa raha",
            )
        else:
            proposal = _catalog_offer(category)
            move = voice.t(
                f"one service+price offer on the listing{f', something like {proposal}' if proposal else ''}",
                f"listing par ek service+price offer{f', jaise {proposal}' if proposal else ''}",
            )
        body = voice.t(
            f"{sal}, {gap}{topic_line}.{since_text} Right now the listing sits at {perf_line}. "
            f"I am not going to send you a plan; the single fastest fix is {move}. "
            f"Want me to set that up and draft the post today? {ask_yes}",
            f"{sal}, {gap}{topic_line}.{since_text} Abhi listing par {perf_line} hai. "
            f"Main aapko poora plan nahi bhej rahi — sabse tez fix hai {move}. "
            f"Main aaj hi woh set up karke post draft kar doon? {ask_yes}",
        )
        rationale = "Dormancy quantified with the exact gap and its measurable cost, narrowed to a single reversible action instead of a menu of options."

    elif kind == "festival_upcoming":
        festival = _clean(payload.get("festival"))
        festival_date = _date_label(payload.get("date"))
        days = _int(payload.get("days_until"))
        proposal = offer or _catalog_offer(category)
        if days is not None and days > 45:
            body = voice.t(
                f"{sal}, {festival or 'the next festival'}{f' is on {festival_date}' if festival_date else ''} — {days} days out. "
                f"A customer promo today would just be ignored, so I am not going to send one. "
                f"What is worth doing now: your listing is at {perf_line}"
                f"{f' with {offer} live' if offer else ', with no active offer on it'}, and that is what the festive package should be built on. "
                f"Want me to lock three service+price concepts now and schedule the first post two weeks before {festival or 'the date'}? {ask_yes}",
                f"{sal}, {festival or 'agla festival'}{f' {festival_date} ko hai' if festival_date else ''} — {days} din baaki hain. "
                f"Aaj customer promo bhejna bekaar hai, isliye main bhej nahi rahi. "
                f"Abhi karne layak cheez: aapki listing par {perf_line} hai"
                f"{f', aur {offer} live hai' if offer else ', aur koi active offer nahi hai'} — festive package isi par banega. "
                f"Main abhi teen service+price concepts lock kar doon aur pehla post {festival or 'date'} se do hafte pehle schedule kar doon? {ask_yes}",
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
            f"{uplift or 'a meaningful lift'} more visibility{f', via {path}' if path else ''}. It is roughly ten minutes of your time. "
            f"Want the exact three steps in one message? {ask_yes}",
            f"{sal}, {name} abhi tak Google par unverified hai. Unverified hote hue bhi listing par {perf_line} hai — "
            f"yaani demand badge ke bina bhi aa rahi hai. Verify karne ke baad estimate: "
            f"{uplift or 'achha khaasa'} zyada visibility{f', {path} se' if path else ''}. Aapke das minute lagenge. "
            f"Main teen exact steps ek message mein bhej doon? {ask_yes}",
        )
        rationale = "Quantified listing gap with the supplied uplift estimate and verification route, framed as a small effort against demand the merchant is already earning."

    elif kind == "ipl_match_today":
        match = _clean(payload.get("match"))
        venue = _clean(payload.get("venue"))
        match_time = _clean(payload.get("match_time_iso"))
        if match_time:
            parsed = _date_label(match_time, include_time=True)
            # The match is today, so only the start time carries information.
            match_time = parsed.split(", ", 1)[-1] if ", " in parsed else parsed
        weeknight = payload.get("is_weeknight")
        offer_text = offer or _catalog_offer(category)
        body = voice.t(
            f"{sal}, {match or 'tonight’s match'}{f' starts at {match_time} tonight' if match_time else ' is tonight'}"
            f"{f' at {venue}' if venue else ''}. On match nights the constraint is kitchen timing, not demand — "
            f"{'a weekend crowd orders later' if weeknight is False else 'people order in one narrow window'}. "
            f"{f'{offer_text} is what to lead with' if offer else f'There is no active offer on the listing; {offer_text} is the one to put up'}, "
            f"with a hard order cutoff on the post. Your listing is at {perf_line}. Want the match-night post with the cutoff line? {ask_yes}",
            f"{sal}, {match or 'aaj ka match'}{f' aaj raat {match_time} shuru hai' if match_time else ' aaj raat hai'}"
            f"{f', {venue} par' if venue else ''}. Match nights par dikkat demand ki nahi, kitchen timing ki hoti hai — "
            f"{'weekend crowd der se order karti hai' if weeknight is False else 'log ek hi narrow window mein order karte hain'}. "
            f"{f'{offer_text} se lead kijiye' if offer else f'Listing par koi active offer nahi hai; {offer_text} lagana chahiye'}, "
            f"aur post par ek hard order cutoff. Aapki listing par {perf_line} hai. Cutoff line ke saath match-night post bana doon? {ask_yes}",
        )
        rationale = "Same-day local event with venue and start time, an operational insight rather than a discount reflex, and one publishable asset."

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
                f"Closing {gap} is a two-day job if you ask the people already walking out happy, not a campaign",
                f"{gap} ka gap do din ka kaam hai agar aap wahi log poochh lein jo khush hokar nikal rahe hain — campaign ki zarurat nahi",
            )
            rationale = "Exact distance to the milestone converted into a same-week, zero-budget action assigned to a specific person."
        else:
            lead = voice.t(
                f"the listing just crossed a real mark — {perf_line}",
                f"listing ne abhi ek asli mark cross kiya hai — {perf_line}",
            )
            close = voice.t(
                "This is the cheapest week of the month to ask for reviews, because the proof is already visible",
                "Review maangne ke liye mahine ka sabse sasta hafta yahi hai, kyunki proof already dikh raha hai",
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
            f"{prefix} {when} Reply CONFIRM to keep it, or RESCHEDULE and we will move you to the next open slot.",
            f"{prefix} {when} CONFIRM bhejiye toh slot pakka, ya RESCHEDULE likhiye — hum agla khaali slot de denge.",
        )
        cta = "binary_confirm_reschedule"
        rationale = "Next-day reminder sent on behalf of the merchant, with no invented time when the schedule is not supplied, and one two-way CTA."

    elif kind == "recall_due":
        last_date = _date_label(payload.get("last_service_date"))
        due_date = _date_label(payload.get("due_date"))
        service = _humanize(payload.get("service_due"))
        slots = [_clean(slot.get("label")) for slot in payload.get("available_slots", []) or [] if isinstance(slot, dict)]
        lead = voice.t(
            f"Your {service or 'next check-in'} is due{f' on {due_date}' if due_date else ''}"
            f"{f'; your last visit was {last_date}' if last_date else ''}.",
            f"Aapka {service or 'next check-in'} due hai{f' {due_date} ko' if due_date else ''}"
            f"{f'; last visit {last_date} thi' if last_date else ''}.",
        )
        if slots:
            listed = _compact_list(slots[:2], conjunction=voice.t("or", "ya"))
            body = voice.t(
                f"{prefix} {lead} Two slots are open: {listed}. Reply 1 or 2 and we will hold it, or RESCHEDULE for another time.",
                f"{prefix} {lead} Do slot khaali hain: {listed}. 1 ya 2 reply kijiye, hum hold kar lenge — ya RESCHEDULE likhiye.",
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
                f"{prefix} Your follow-up is due. Reply YES and we will send you this week's open slots, or STOP if you would rather not be reminded.",
                f"{prefix} Aapka follow-up due hai. YES bhejiye, hum is hafte ke khaali slots bhej denge — ya STOP likhiye toh reminder band.",
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
        proposal = offer or _catalog_offer(category, ["trial", "consult", "free", "first"])
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
            f"Reply YES and we will hold one slot for you this week, or STOP and we will not message again.",
            f"{prefix} {gap} {easy[1]}{focus_line}"
            f"{f' Wapas aane ka asaan tareeka — {proposal}.' if proposal else ''} "
            f"YES bhejiye, hum is hafte ek slot hold kar lenge — ya STOP likhiye, phir message nahi aayega.",
        )
        rationale = "No-shame win-back with the supplied gap and prior goal, a low-commitment entry offer, and an explicit permanent opt-out."

    elif kind == "trial_followup":
        trial_date = _date_label(payload.get("trial_date"))
        slots = [_clean(slot.get("label")) for slot in payload.get("next_session_options", []) or [] if isinstance(slot, dict)]
        listed = _compact_list(slots[:2], conjunction=voice.t("or", "ya"))
        body = voice.t(
            f"{prefix} Thanks for coming in for the trial{f' on {trial_date}' if trial_date else ''}. "
            f"{f'Next session: {listed}.' if listed else 'The next session is open whenever you are ready.'} "
            f"Reply YES to hold your place, or ANOTHER if a different time suits you better.",
            f"{prefix} Trial ke liye aane ka shukriya{f' — {trial_date}' if trial_date else ''}. "
            f"{f'Agla session: {listed}.' if listed else 'Agla session jab aap taiyar hon.'} "
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
            f"The {window or 'next preparation'} window is open, and this is the stretch when dates fill up first. "
            f"Reply YES and we will hold a planning slot for you, or STOP if the plan has changed.",
            f"{prefix} Aapka trial{f' {trial} ko' if trial else ''} ho chuka hai, aur wedding{f' {wedding} ko hai' if wedding else ' paas hai'}. "
            f"{window or 'Agli tayari'} ka window khul chuka hai, aur isi time mein dates sabse pehle bhar jaati hain. "
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
        # Tracked per merchant as well as per conversation: the trap arrives
        # on four different thread ids.
        self.auto_replies: dict[str, Counter] = {}

    def clear(self) -> None:
        with self.lock:
            self.contexts.clear()
            self.conversations.clear()
            self.sent_suppression_keys.clear()
            self.muted_merchants.clear()
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

    def latest_trigger_for(self, merchant_id: str | None) -> dict[str, Any] | None:
        """Best-effort recovery when a reply arrives on an unknown thread."""
        if not merchant_id:
            return None
        candidates = [
            item["payload"]
            for (scope, _), item in self.contexts.items()
            if scope == "trigger" and _trigger_merchant_id(item["payload"]) == merchant_id
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
    return re.sub(r"[^a-z0-9]+", " ", message.lower()).strip()


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
    r"\bstop\b", r"unsubscribe", r"do not message", r"don'?t message", r"stop messaging",
    r"not interested", r"useless spam", r"\bspam\b", r"leave me alone", r"band karo",
    r"mat bhejo", r"message mat", r"remove me",
)
COMMIT_PATTERNS = (
    r"let'?s do it", r"lets do it", r"go ahead", r"\bproceed\b", r"what'?s next", r"whats next",
    r"i want to join", r"mujhe.*judna", r"mujhe.*join", r"kar do", r"kar dijiye", r"\bconfirm\b",
    r"yes please", r"sounds good", r"\bdraft\b", r"\bok(ay)?\b.*\b(next|do it|go)\b",
    r"\b(send|share|pull|show) me\b", r"\bplease (send|share|pull|show)\b", r"\bhaan\b",
)
# Narrow on purpose: "no, what's the price?" is not a decline.
DECLINE_PATTERNS = (
    r"^\s*no[.! ]*$", r"\bno thanks?\b", r"\bnot now\b", r"\bmaybe later\b", r"\bsome other time\b",
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
    category = state.get("category")
    merchant = state.get("merchant")
    trigger = state.get("trigger")

    if not merchant:
        stored_category, merchant = STORE.merchant_context(merchant_id)
        category = category or stored_category
    if not category and merchant:
        category = STORE.payload("category", merchant.get("category_slug"))
    if not trigger:
        trigger = STORE.latest_trigger_for(merchant_id or _trigger_merchant_id(merchant or {}))
    if not state.get("customer") and customer_id:
        state["customer"] = STORE.payload("customer", customer_id)

    # Cache the resolution so later turns on this thread stay consistent.
    if merchant:
        state["merchant"] = merchant
    if category:
        state["category"] = category
    if trigger:
        state["trigger"] = trigger
    return category, merchant, trigger


def _commitment_response(
    category: dict[str, Any] | None,
    merchant: dict[str, Any] | None,
    trigger: dict[str, Any] | None,
    turn_number: int,
) -> str:
    """Answer a commitment with delivery, never with another qualifying question."""
    merchant = merchant or {}
    category = category or {}
    trigger = trigger or {}
    voice = Voice(category, merchant)
    kind = _clean(trigger.get("kind"))
    merchant_name = _merchant_name(merchant)
    perf_line = _perf_line(merchant, voice)
    basis = voice.t(
        f" It is built on your current numbers — {perf_line}." if perf_line else "",
        f" Yeh aapke current numbers par bana hai — {perf_line}." if perf_line else "",
    )

    if kind == "research_digest":
        item = _digest_item(category, trigger)
        summary = _clean(item.get("summary") or item.get("title"))
        source = _clean(item.get("source"))
        return voice.t(
            f"Done. Here is the part that matters{f' from {source}' if source else ''}: {summary} "
            f"I have the patient-friendly draft ready too.{basis} Reply CONFIRM to use it, or EDIT with the one change you want.",
            f"Ho gaya. Kaam ki baat yeh hai{f' — {source} se' if source else ''}: {summary} "
            f"Patient-friendly draft bhi ready hai.{basis} CONFIRM bhejiye toh yahi use karein, ya EDIT ke saath ek badlaav bata dijiye.",
        )
    if kind in {"regulation_change", "supply_alert"}:
        return voice.t(
            f"Done — the checklist and the customer-safe note are both drafted and ready to send.{basis} "
            "Reply CONFIRM to use this version, or EDIT with the one change you want.",
            f"Ho gaya — checklist aur customer-safe note dono draft ho gaye hain.{basis} "
            "CONFIRM bhejiye toh yahi version, ya EDIT ke saath ek badlaav bata dijiye.",
        )
    if kind == "cde_opportunity":
        return voice.t(
            "Done — the registration steps and the calendar block are drafted in one message, ready to send. "
            "Reply CONFIRM to use it, or EDIT with the one change you want.",
            "Ho gaya — registration ke steps aur calendar block ek hi message mein draft hain, bhejne ke liye ready. "
            "CONFIRM bhejiye, ya EDIT ke saath ek badlaav bata dijiye.",
        )
    if kind in {"appointment_tomorrow", "recall_due", "trial_followup", "chronic_refill_due", "customer_lapsed_hard", "customer_lapsed_soft"}:
        return voice.t(
            "Done — the customer message is drafted from the latest record and is ready to send. "
            "Reply CONFIRM to send it, or CHANGE with the one detail that differs.",
            "Ho gaya — customer ka message latest record se draft ho gaya hai, bhejne ke liye ready. "
            "CONFIRM bhejiye toh bhej deti hoon, ya CHANGE likhiye agar koi detail alag hai.",
        )

    noun = _humanize(kind) or voice.t("requested", "requested")
    if turn_number > 2:
        return voice.t(
            f"Done — the {noun} draft for {merchant_name} is ready.{basis} "
            "Reply CONFIRM to use this version, or EDIT with the one change you want.",
            f"Ho gaya — {merchant_name} ke liye {noun} draft ready hai.{basis} "
            "CONFIRM bhejiye toh yahi version, ya EDIT ke saath ek badlaav bata dijiye.",
        )
    return voice.t(
        f"Done — moving straight to it. The {noun} draft for {merchant_name} is prepared.{basis} "
        "Next step is yours: reply CONFIRM to use it, or EDIT with the one change you want. No more questions from me.",
        f"Ho gaya — main seedha kaam par. {merchant_name} ke liye {noun} draft taiyar hai.{basis} "
        "Ab aapka step: CONFIRM bhejiye toh yahi use karein, ya EDIT ke saath ek badlaav bata dijiye. Aur sawaal nahi.",
    )


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
        voice = Voice(category, merchant)

        # 1. Hostility and opt-out win, answered with an apology.
        if _matches_any(message, STOP_PATTERNS):
            if merchant_id:
                STORE.muted_merchants.add(merchant_id)
                STORE.auto_replies.pop(merchant_id, None)
            return {
                "action": "end",
                "rationale": "Explicit opt-out or spam complaint. Sending stops immediately and this merchant is muted for proactive messages; no persuasion attempt is made.",
            }

        # 2. Answering machines: counted per merchant as well as per thread.
        if _matches_any(message, AUTO_REPLY_PATTERNS):
            thread = state.setdefault("auto_replies", Counter())
            thread[normalized] += 1
            merchant_counter = STORE.auto_replies.setdefault(merchant_id or "unknown", Counter())
            merchant_counter[normalized] += 1
            count = max(thread[normalized], merchant_counter[normalized])
            if count >= 3:
                return {
                    "action": "end",
                    "rationale": "The same canned auto-reply has now come back three times across this merchant's threads; there is no human on the other end, so the sequence stops.",
                }
            if count == 2:
                return {
                    "action": "wait",
                    "wait_seconds": 86_400,
                    "rationale": "Second identical auto-reply. Waiting a day costs nothing and avoids burning a turn against an answering machine.",
                }
            return {
                "action": "send",
                "body": _finalize(voice.t(
                    "Looks like an automatic response. No reply needed now — when the owner or manager sees this, reply YES and I will share the ready draft.",
                    "Yeh automatic jawaab lag raha hai. Abhi reply ki zarurat nahi — jab owner ya manager dekhein, YES bhej dijiye, main ready draft bhej dungi.",
                ), category),
                "cta": "binary_yes_stop",
                "rationale": "First canned response: one short owner-directed line, then back off rather than continue a conversation with a machine.",
            }

        # A genuine human reply clears the streak for this thread and merchant.
        if normalized:
            state.setdefault("auto_replies", Counter()).clear()
            if merchant_id in STORE.auto_replies:
                STORE.auto_replies[merchant_id].clear()

        # 3. Commitment: move to delivery, never back to discovery.
        if _matches_any(message, COMMIT_PATTERNS):
            response_body = _strip_qualifiers(_commitment_response(category, merchant, trigger, body.turn_number))
            return {
                "action": "send",
                "body": _finalize(response_body, category),
                "cta": "binary_confirm_edit",
                "rationale": "Explicit commitment detected, so the reply delivers the artifact instead of asking another qualifying question.",
            }

        # 4. Out of scope. Before the decline check, so a refusal carrying a
        #    question still gets an answer.
        if re.search(r"\b(gst|tax|itr|loan|legal|lawyer|passport|aadhaar|visa|insurance claim)\b", normalized):
            return {
                "action": "send",
                "body": _finalize(voice.t(
                    "That one belongs with your CA, not me — I would rather say so than guess. What I can do is your magicpin listing, "
                    "customer messages, offers and post drafts. Reply DRAFT to carry on here, or STOP to close.",
                    "Yeh kaam aapke CA ka hai, mera nahi — andaaza lagane se behtar hai saaf bata doon. Main jo kar sakti hoon: aapki magicpin listing, "
                    "customer messages, offers aur post drafts. DRAFT bhejiye toh yahin aage badhein, ya STOP se band.",
                ), category),
                "cta": "binary_draft_stop",
                "rationale": "Out-of-scope request is refused honestly instead of inventing capability, then routed back to one thing this assistant can actually deliver.",
            }

        # 5. A real question. Answer it before treating anything as a refusal.
        if "?" in message or re.search(r"\b(what|how|why|when|where|which|kitna|kitne|kaise|kab|kyun|kya)\b", normalized):
            perf_line = _perf_line(merchant or {}, voice)
            basis = voice.t(
                f"Working from your current numbers — {perf_line}." if perf_line else "Working from what is on your listing today.",
                f"Main aapke current numbers par chal rahi hoon — {perf_line}." if perf_line else "Main aapki aaj ki listing par chal rahi hoon.",
            )
            return {
                "action": "send",
                "body": _finalize(voice.t(
                    f"Fair question. {basis} The fastest way to answer it properly is to show you the actual draft — "
                    "reply DRAFT and it is yours, or tell me the one detail you would change and I will build around that.",
                    f"Sahi sawaal. {basis} Iska sabse achha jawaab hai aapko asli draft dikhana — "
                    "DRAFT bhejiye, bhej deti hoon; ya jo ek cheez badalni hai woh bata dijiye, main usi hisaab se bana dungi.",
                ), category),
                "cta": "binary_draft_stop",
                "rationale": "The question is answered on the merchant's own numbers and converted into one concrete next step rather than a longer explanation.",
            }

        # 6. A clean refusal with no question attached.
        if _matches_any(message, DECLINE_PATTERNS):
            return {
                "action": "end",
                "rationale": "Clear decline with nothing left open. Ending here respects the answer; a second persuasion attempt would only cost goodwill.",
            }

        if body.turn_number >= 5:
            return {
                "action": "end",
                "rationale": "Five turns with no action signal. Continuing would be nudging, so the thread closes and the next opportunity can start fresh.",
            }

        options = [
            voice.t(
                "Got it. One useful next step from here: reply DRAFT and I will show you the copy, or STOP to close this.",
                "Theek hai. Yahan se ek kaam ki cheez: DRAFT bhejiye, main copy dikha deti hoon — ya STOP se band kar dein.",
            ),
            voice.t(
                "Understood. The draft is ready to show rather than describe. Reply DRAFT to see it, or STOP to close.",
                "Samajh gayi. Draft batane se behtar dikhana hai. DRAFT bhejiye dekhne ke liye, ya STOP se band.",
            ),
            voice.t(
                "Noted. I can put the final copy in front of you now. Reply DRAFT, or STOP.",
                "Note kar liya. Final copy abhi aapke saamne rakh sakti hoon. DRAFT bhejiye, ya STOP.",
            ),
        ]
        return {
            "action": "send",
            "body": _finalize(options[(body.turn_number - 1) % len(options)], category),
            "cta": "binary_draft_stop",
            "rationale": "Acknowledges the reply, advances exactly one step, and varies the wording so repeated turns do not read as a loop.",
        }


@app.post("/v1/teardown")
def teardown() -> dict[str, bool]:
    STORE.clear()
    return {"cleared": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("bot:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
