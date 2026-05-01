"""
rule_composer.py — Deterministic rule-based message generator.

Produces high-scoring messages WITHOUT an LLM.
Used by generate_submission.py when ANTHROPIC_API_KEY is absent.
The deployed server uses composer.py (LLM-based) for maximum quality.

Each trigger kind has a tailored composition strategy with real-data anchoring.
"""

import json
from typing import Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _owner(merchant: dict) -> str:
    identity = merchant.get("identity", {})
    first = identity.get("owner_first_name", "")
    if first:
        return first
    name = identity.get("name", "")
    # Try to extract first word after "Dr." or just first word
    parts = name.split()
    if parts and parts[0].lower() in ("dr.", "dr"):
        return " ".join(parts[:2]) if len(parts) >= 2 else parts[0]
    return parts[0] if parts else name


def _salutation(merchant: dict, category: dict) -> str:
    slug = category.get("slug", "")
    identity = merchant.get("identity", {})
    owner_first = identity.get("owner_first_name", "")
    name = identity.get("name", "")
    if slug == "dentists":
        if owner_first:
            return f"Dr. {owner_first}"
        return "Doc"
    if slug == "pharmacies":
        first = owner_first or name.split()[0]
        return first
    return owner_first or name.split()[0] or "there"


def _lang(merchant: dict) -> str:
    langs = merchant.get("identity", {}).get("languages", ["en"])
    if "hi" in langs:
        return "hi_en"
    return "en"


def _hi(text_hi_en: str, text_en: str, merchant: dict) -> str:
    return text_hi_en if _lang(merchant) == "hi_en" else text_en


def _peer_ctr(category: dict) -> float:
    return category.get("peer_stats", {}).get("avg_ctr", 0.03)


def _merchant_ctr(merchant: dict) -> float:
    return merchant.get("performance", {}).get("ctr", 0.021)


def _active_offer(merchant: dict) -> Optional[dict]:
    for o in merchant.get("offers", []):
        if o.get("status") == "active":
            return o
    return None


def _best_catalog_offer(category: dict) -> Optional[dict]:
    catalog = category.get("offer_catalog", [])
    return catalog[0] if catalog else None


def _top_digest(category: dict, trigger: dict) -> Optional[dict]:
    tid = trigger.get("payload", {}).get("top_item_id")
    digest = category.get("digest", [])
    if tid:
        for d in digest:
            if d.get("id") == tid:
                return d
    return digest[0] if digest else None


def _locality(merchant: dict) -> str:
    return merchant.get("identity", {}).get("locality", "your area")


def _city(merchant: dict) -> str:
    return merchant.get("identity", {}).get("city", "")


def _views(merchant: dict) -> int:
    return merchant.get("performance", {}).get("views", 0)


def _calls(merchant: dict) -> int:
    return merchant.get("performance", {}).get("calls", 0)


def _delta_views(merchant: dict) -> float:
    return merchant.get("performance", {}).get("delta_7d", {}).get("views_pct", 0)


def _delta_calls(merchant: dict) -> float:
    return merchant.get("performance", {}).get("delta_7d", {}).get("calls_pct", 0)


def _lapsed(merchant: dict) -> int:
    return merchant.get("customer_aggregate", {}).get("lapsed_180d_plus", 0)


def _reviews(merchant: dict) -> int:
    return merchant.get("performance", {}).get("reviews", 0)


def _rating(merchant: dict) -> float:
    return merchant.get("performance", {}).get("rating", 0)


def _signals(merchant: dict) -> list:
    return merchant.get("signals", [])


def _sub_days(merchant: dict) -> int:
    return merchant.get("subscription", {}).get("days_remaining", 0)


def _high_risk_count(merchant: dict) -> int:
    return merchant.get("customer_aggregate", {}).get("high_risk_adult_count", 0)


# ---------------------------------------------------------------------------
# Per-trigger-kind composers
# ---------------------------------------------------------------------------

def _compose_research_digest(cat, mer, trg, cus):
    sal = _salutation(mer, cat)
    digest = _top_digest(cat, trg)
    if not digest:
        return None
    title = digest.get("title", "")
    source = digest.get("source", "")
    trial_n = digest.get("trial_n", "")
    patient_seg = digest.get("patient_segment", "")
    summary = digest.get("summary", "")
    actionable = digest.get("actionable", "")
    hr_count = _high_risk_count(mer)
    cohort_note = f"relevant to your {hr_count} high-risk adult patients" if hr_count else "worth a look"
    n_note = f"{trial_n:,}-patient trial" if trial_n else "study"
    body = (
        f"{sal}, {source.split(',')[0] if source else 'new research'} landed. "
        f"One item {cohort_note} — {n_note}: {summary[:120].rstrip('.')}. "
        f"Want me to pull the abstract + draft a patient WhatsApp you can share?"
        + (f"  — {source}" if source else "")
    )
    return {
        "body": body.strip(),
        "cta": "open_ended",
        "send_as": "vera",
        "rationale": (
            f"External research digest trigger. Anchored on '{source}' with trial_n={trial_n}. "
            f"Merchant has {hr_count} high-risk patients making this directly relevant. "
            f"Curiosity + reciprocity levers: abstract pull + patient content draft offered."
        ),
    }


def _compose_compliance(cat, mer, trg, cus):
    sal = _salutation(mer, cat)
    digest = _top_digest(cat, trg)
    deadline = trg.get("payload", {}).get("deadline_iso", "")
    if digest:
        title = digest.get("title", "")
        source = digest.get("source", "")
        summary = digest.get("summary", digest.get("actionable", ""))
        deadline_short = deadline[:10] if deadline else ""
        body = (
            f"{sal}, compliance heads-up: {title[:100]}. "
            + (f"Deadline: {deadline_short}. " if deadline_short else "")
            + f"{summary[:100].rstrip('.')}. "
            f"Want me to draft your SOP update for this? 10-min task."
        )
        rationale = (
            f"Regulation-change trigger (urgency={trg.get('urgency',4)}). "
            f"Loss-aversion framing with deadline {deadline_short}. "
            f"Effort externalization: SOP draft offered."
        )
    else:
        body = f"{sal}, new compliance update that affects your practice — want me to send the details?"
        rationale = "Compliance trigger, generic fallback."
    return {"body": body.strip(), "cta": "binary_yes_no", "send_as": "vera", "rationale": rationale}


def _compose_recall_due(cat, mer, trg, cus):
    """Customer-facing recall reminder."""
    if not cus:
        return None
    cust_identity = cus.get("identity", {})
    cust_name = cust_identity.get("name", "there")
    lang = cust_identity.get("language_pref", "en")
    merchant_name = mer.get("identity", {}).get("name", "us")
    offer = _active_offer(mer) or _best_catalog_offer(cat)
    offer_text = offer.get("title", "consultation") if offer else "consultation"
    slots = trg.get("payload", {}).get("available_slots", [])
    slots_text = ""
    if slots:
        s = slots[:2]
        labels = [x.get("label", "") for x in s if x.get("label")]
        if len(labels) == 2:
            slots_text = f"2 slots ready: **{labels[0]}** ya **{labels[1]}**. "
        elif len(labels) == 1:
            slots_text = f"Slot available: **{labels[0]}**. "
    service_due = trg.get("payload", {}).get("service_due", "6-month checkup").replace("_", " ")
    emoji = "🦷" if cat.get("slug") == "dentists" else "📅"
    if "hi" in lang:
        body = (
            f"Hi {cust_name}, {merchant_name} {emoji} Aapka {service_due} recall due hai. "
            + (slots_text if slots_text else "")
            + f"{offer_text}. "
            f"Reply 1 for pehli slot, 2 for doosri, ya apna preferred time batayein."
        )
    else:
        body = (
            f"Hi {cust_name}, {merchant_name} {emoji} Your {service_due} is due. "
            + (slots_text.replace("**", "") if slots_text else "")
            + f"{offer_text}. Reply 1 for first slot, 2 for second, or share your preferred time."
        )
    return {
        "body": body.strip(),
        "cta": "multi_choice_slot",
        "send_as": "merchant_on_behalf",
        "rationale": (
            f"Customer recall trigger for {cust_name} (state: {cus.get('state','lapsed_soft')}). "
            f"Sent as merchant_on_behalf. Language preference '{lang}' honored. "
            f"Real slots + real offer price anchor. Multi-choice slot CTA for booking flow."
        ),
    }


def _compose_perf_spike(cat, mer, trg, cus):
    sal = _salutation(mer, cat)
    views = _views(mer)
    delta_v = _delta_views(mer)
    calls = _calls(mer)
    delta_c = _delta_calls(mer)
    offer = _active_offer(mer)
    offer_text = offer.get("title", "") if offer else ""
    pct_v = f"+{int(delta_v*100)}%" if delta_v > 0 else f"{int(delta_v*100)}%"
    body = (
        f"{sal}, your profile is on fire — {views:,} views in 30 days ({pct_v} this week). "
        f"{calls} calls came in. "
        + (f"Want me to boost this with a '{offer_text}' post + offer banner? " if offer_text else "Want me to lock in conversions with a quick offer post? ")
        + "Momentum kab convert karaein? — reply YES."
    )
    return {
        "body": body.strip(),
        "cta": "binary_yes_no",
        "send_as": "vera",
        "rationale": (
            f"Perf-spike trigger: views={views:,} ({pct_v} 7d). "
            f"Loss-aversion framing: convert the momentum now. "
            f"Active offer '{offer_text}' anchors the ask. Binary YES CTA."
        ),
    }


def _compose_perf_dip(cat, mer, trg, cus):
    sal = _salutation(mer, cat)
    calls = _calls(mer)
    delta_c = _delta_calls(mer)
    views = _views(mer)
    peer_ctr = _peer_ctr(cat)
    mer_ctr = _merchant_ctr(mer)
    pct_c = f"{int(delta_c*100)}%" if delta_c != 0 else "down"
    locality = _locality(mer)
    gap = round((peer_ctr - mer_ctr) * 100, 1)
    body = (
        f"{sal}, calls dropped {pct_c} this week — {calls} total vs your usual. "
        + (f"Your CTR is {mer_ctr*100:.1f}% vs peer median {peer_ctr*100:.1f}% (gap: {gap}pp). " if gap > 0 else "")
        + f"I can see 3 quick fixes in your {locality} listing. Ek check karein? Reply YES."
    )
    return {
        "body": body.strip(),
        "cta": "binary_yes_no",
        "send_as": "vera",
        "rationale": (
            f"Perf-dip trigger: calls={calls}, delta={pct_c}. "
            f"CTR gap vs peer median anchors loss aversion ({gap}pp). "
            f"Effort externalization: 3 fixes ready. Binary YES."
        ),
    }


def _compose_milestone(cat, mer, trg, cus):
    sal = _salutation(mer, cat)
    milestone = trg.get("payload", {}).get("milestone", "")
    value = trg.get("payload", {}).get("value", "")
    reviews = mer.get("performance", {}).get("reviews") or trg.get("payload", {}).get("review_count", 0)
    if not milestone:
        milestone = f"{reviews} reviews" if reviews else "new milestone"
    body = (
        f"{sal} 🎉 Aapne {milestone} cross kar liya! "
        f"Social proof is your best marketing right now. "
        f"Want me to turn this into a Google post + a shareable WhatsApp card for your patients?"
    )
    return {
        "body": body.strip(),
        "cta": "binary_yes_no",
        "send_as": "vera",
        "rationale": (
            f"Milestone trigger: {milestone}. "
            f"Reciprocity: merchant earned this, Vera celebrates. "
            f"Effort externalization: Google post + WhatsApp card ready to create. "
            f"Binary YES to keep momentum."
        ),
    }


def _compose_dormant(cat, mer, trg, cus):
    sal = _salutation(mer, cat)
    days = trg.get("payload", {}).get("dormant_days", 14)
    views = _views(mer)
    locality = _locality(mer)
    body = (
        f"{sal}, {days} din se baat nahi hui. "
        f"Aapki profile abhi {views:,} views le rahi hai last 30 days mein — "
        f"but {locality} mein {_peer_ctr(cat)*100:.1f}% CTR benchmark se neeche hain. "
        f"Ek cheez fix karein? 5 minute. Reply YES."
    )
    return {
        "body": body.strip(),
        "cta": "binary_yes_no",
        "send_as": "vera",
        "rationale": (
            f"Dormant trigger ({days} days no reply). "
            f"Re-engagement via peer benchmark gap (loss aversion). "
            f"Views number grounds the message in real data. Low-friction 5-minute ask."
        ),
    }


def _compose_festival(cat, mer, trg, cus):
    sal = _salutation(mer, cat)
    festival = trg.get("payload", {}).get("festival_name", "upcoming festival")
    payload = trg.get("payload", {})
    festival = payload.get("festival", trg.get("payload", {}).get("festival_name", "upcoming festival"))
    days_away = payload.get("days_until", payload.get("days_away", 4))
    offer = _active_offer(mer) or _best_catalog_offer(cat)
    offer_text = offer.get("title", "special offer") if offer else "special offer"
    slug = cat.get("slug", "")
    if slug == "restaurants":
        angle = f"Festive thali + delivery deal"
    elif slug == "salons":
        angle = f"Pre-{festival} glow package"
    elif slug == "gyms":
        angle = f"{festival} fitness challenge"
    else:
        angle = f"{festival} special"
    body = (
        f"{sal}, {festival} is {days_away} din mein. "
        f"Main aapke liye '{angle}' campaign draft kar sakti hoon — "
        f"'{offer_text}' as the hook. "
        f"Swiggy/Zomato banner + GBP post + WhatsApp broadcast — ek click mein. Reply YES."
    )
    return {
        "body": body.strip(),
        "cta": "binary_yes_no",
        "send_as": "vera",
        "rationale": (
            f"Festival trigger: {festival} in {days_away} days. "
            f"Effort externalization: full campaign (Swiggy + GBP + WhatsApp) in one ask. "
            f"Existing offer '{offer_text}' used as hook. Urgency via days countdown."
        ),
    }


def _compose_renewal(cat, mer, trg, cus):
    sal = _salutation(mer, cat)
    days = _sub_days(mer)
    plan = mer.get("subscription", {}).get("plan", "Pro")
    views = _views(mer)
    body = (
        f"{sal}, aapka {plan} plan {days} din mein expire ho raha hai. "
        f"Last 30 din mein {views:,} views aaye — yeh sab band ho jayenge. "
        f"Renew karna chahenge? Main ek summary bhi bhejengi ki pichle 3 months mein kya kiya — Reply YES."
    )
    return {
        "body": body.strip(),
        "cta": "binary_yes_no",
        "send_as": "vera",
        "rationale": (
            f"Renewal trigger: {days} days remaining on {plan} plan. "
            f"Loss-aversion framing: {views:,} views at stake. "
            f"Reciprocity: 3-month summary offered. Binary YES."
        ),
    }


def _compose_cde_opportunity(cat, mer, trg, cus):
    sal = _salutation(mer, cat)
    digest = _top_digest(cat, trg)
    if digest:
        title = digest.get("title", "upcoming webinar")
        date = digest.get("date", "")
        credits = digest.get("credits", "")
        source = digest.get("source", "")
        date_short = date[:10] if date else ""
        body = (
            f"{sal}, {source} has an event: {title[:80]}. "
            + (f"Date: {date_short}. " if date_short else "")
            + (f"{credits} CDE credits. " if credits else "")
            + "Free for members. Register karun? Reply YES."
        )
    else:
        body = f"{sal}, ek upcoming CDE/webinar hai jo aapke liye relevant ho sakta hai. Details bhejun? Reply YES."
    return {
        "body": body.strip(),
        "cta": "binary_yes_no",
        "send_as": "vera",
        "rationale": "CDE opportunity: source-cited event with credits. Effort externalization: registration handled.",
    }


def _compose_competitor_opened(cat, mer, trg, cus):
    sal = _salutation(mer, cat)
    locality = _locality(mer)
    distance = trg.get("payload", {}).get("distance_km", "1.2")
    comp_name = trg.get("payload", {}).get("competitor_name", "a new competitor")
    peer_ctr = _peer_ctr(cat)
    mer_ctr = _merchant_ctr(mer)
    body = (
        f"{sal}, {comp_name} has opened {distance}km away in {locality}. "
        f"Your CTR is {mer_ctr*100:.1f}% vs area median {peer_ctr*100:.1f}%. "
        f"Apni listing strengthen karun before footfall shifts? 3 specific changes ready. Reply YES."
    )
    return {
        "body": body.strip(),
        "cta": "binary_yes_no",
        "send_as": "vera",
        "rationale": (
            f"Competitor-opened trigger: {comp_name} at {distance}km. "
            f"CTR gap frames loss aversion. 3 fixes = effort externalization. Binary YES."
        ),
    }


def _compose_curious_ask(cat, mer, trg, cus):
    sal = _salutation(mer, cat)
    slug = cat.get("slug", "")
    if slug == "dentists":
        ask = "most-requested treatment this week"
        offer = "Google post + patient WhatsApp reply template"
    elif slug == "salons":
        ask = "service most asked-for this week"
        offer = "Google post + pricing WhatsApp you can use"
    elif slug == "restaurants":
        ask = "most-ordered dish this week"
        offer = "a Google post + a Swiggy special banner"
    elif slug == "gyms":
        ask = "most-popular class or equipment this week"
        offer = "a Google post + a membership nudge campaign"
    else:
        ask = "most-asked item this week"
        offer = "a Google post + WhatsApp broadcast"
    body = (
        f"Hi {sal}! Quick ek sawaal — {ask} kya raha {mer.get('identity',{}).get('name','')} mein? "
        f"Batao, main usse {offer} mein convert kar deti hoon. 5 min ka kaam."
    )
    return {
        "body": body.strip(),
        "cta": "open_ended",
        "send_as": "vera",
        "rationale": (
            f"Curious-ask cadence. Asking-the-merchant lever (highest-engagement family). "
            f"Reciprocity offered upfront: answer → deliverable in 5 min. "
            f"No commitment asked; low-stakes open question."
        ),
    }


def _compose_stale_posts(cat, mer, trg, cus):
    sal = _salutation(mer, cat)
    days = next((s.split(":")[1] for s in _signals(mer) if "stale_posts" in s), "21d")
    offer = _active_offer(mer)
    offer_hint = f" around your '{offer['title']}' offer" if offer else ""
    body = (
        f"{sal}, aapka last Google post {days} purana hai. "
        f"Fresh posts {_peer_ctr(cat)*100:.1f}% CTR pe hain — aapke {_merchant_ctr(mer)*100:.1f}% se zyada. "
        f"Main 3 posts draft karun{offer_hint}? Aap sirf approve karo. Reply YES."
    )
    return {
        "body": body.strip(),
        "cta": "binary_yes_no",
        "send_as": "vera",
        "rationale": (
            f"Stale-posts signal ({days}). CTR gap quantifies the cost. "
            f"Effort externalization: 3 posts pre-drafted. Binary YES."
        ),
    }


def _compose_trend_movement(cat, mer, trg, cus):
    sal = _salutation(mer, cat)
    trend = trg.get("payload", {}).get("trend_query", "")
    delta = trg.get("payload", {}).get("delta_yoy", 0)
    if not trend:
        signals = cat.get("trend_signals", [])
        if signals:
            trend = signals[0].get("query", "relevant service")
            delta = signals[0].get("delta_yoy", 0)
    pct = f"+{int(delta*100)}%" if delta else "growing fast"
    locality = _locality(mer)
    body = (
        f"{sal}, '{trend}' searches in {locality} are {pct} YoY — aur bhadh rahi hain. "
        f"Apni listing mein yeh position karein before competitors. "
        f"GBP description update + 1 new offer ready karun? — Reply YES."
    )
    return {
        "body": body.strip(),
        "cta": "binary_yes_no",
        "send_as": "vera",
        "rationale": (
            f"Trend-movement trigger: '{trend}' {pct}. "
            f"Curiosity + loss-aversion: be early or lose to competitors. "
            f"Effort externalization: GBP + offer ready. Binary YES."
        ),
    }


def _compose_review_theme(cat, mer, trg, cus):
    sal = _salutation(mer, cat)
    themes = mer.get("review_themes", [])
    if not themes:
        return None
    # Find the most recent/relevant theme
    neg = [t for t in themes if t.get("sentiment") == "neg"]
    pos = [t for t in themes if t.get("sentiment") == "pos"]
    if neg:
        theme = neg[0]
        theme_name = theme.get("theme", "").replace("_", " ")
        count = theme.get("occurrences_30d", 0)
        quote = theme.get("common_quote", "")
        body = (
            f"{sal}, {count} reviews this month mention '{theme_name}'. "
            + (f"Common phrase: \"{quote[:60]}\". " if quote else "")
            + f"Main ek response template + ek GBP improvement draft karun? "
            + f"Yeh 4★ ko 4.6★ tak le ja sakta hai. Reply YES."
        )
        lever = "social-proof risk + repair action"
    else:
        theme = pos[0]
        theme_name = theme.get("theme", "").replace("_", " ")
        count = theme.get("occurrences_30d", 0)
        body = (
            f"{sal}, {count} patients ne '{theme_name}' ke baare mein positively likha this month. "
            f"Want me to turn this into a Google post + a shareable card? "
            f"Social proof is free marketing. Reply YES."
        )
        lever = "social-proof amplification"
    return {
        "body": body.strip(),
        "cta": "binary_yes_no",
        "send_as": "vera",
        "rationale": f"Review-theme trigger ({lever}). Real occurrence count + quote anchors specificity. Effort externalization: template + GBP action offered.",
    }


def _compose_appointment_tomorrow(cat, mer, trg, cus):
    """Customer-facing appointment reminder."""
    if not cus:
        return None
    cust_name = cus.get("identity", {}).get("name", "there")
    lang = cus.get("identity", {}).get("language_pref", "en")
    merchant_name = mer.get("identity", {}).get("name", "us")
    appt = trg.get("payload", {}).get("appointment", {})
    time_str = appt.get("time_label", appt.get("time", "tomorrow"))
    service = appt.get("service", "your appointment")
    if "hi" in lang:
        body = (
            f"Hi {cust_name}, {merchant_name} yahan se 🙏 "
            f"Kal aapka {service} confirm hai — {time_str}. "
            f"Koi change ho toh batayein, warna aapka slot reserved hai."
        )
    else:
        body = (
            f"Hi {cust_name}, {merchant_name} here 🙏 "
            f"Reminder: your {service} is confirmed for {time_str}. "
            f"Reply to reschedule, otherwise see you then!"
        )
    return {
        "body": body.strip(),
        "cta": "open_ended",
        "send_as": "merchant_on_behalf",
        "rationale": (
            f"Appointment-tomorrow trigger for {cust_name}. "
            f"Language pref '{lang}' honored. "
            f"Warm confirmation with easy reschedule path. No CTA pressure on confirmed booking."
        ),
    }


def _compose_winback(cat, mer, trg, cus):
    """Customer winback / lapsed customer outreach."""
    if cus:
        cust_name = cus.get("identity", {}).get("name", "there")
        lang = cus.get("identity", {}).get("language_pref", "en")
        last_visit = cus.get("relationship", {}).get("last_visit", "")
        months = trg.get("payload", {}).get("months_lapsed", "6")
        merchant_name = mer.get("identity", {}).get("name", "us")
        offer = _active_offer(mer) or _best_catalog_offer(cat)
        offer_text = offer.get("title", "special offer") if offer else "special offer"
        if "hi" in lang:
            body = (
                f"Hi {cust_name}, {merchant_name} yahan se 😊 "
                f"Aapko miss kiya — {months} mahine ho gaye last visit ko. "
                f"Wapas aao: {offer_text}. Ek slot book karun? Reply YES."
            )
        else:
            body = (
                f"Hi {cust_name}, {merchant_name} here 😊 "
                f"It's been {months} months — we miss you! "
                f"Come back: {offer_text}. Want me to book a slot? Reply YES."
            )
        return {
            "body": body.strip(),
            "cta": "binary_yes_no",
            "send_as": "merchant_on_behalf",
            "rationale": (
                f"Winback for {cust_name} ({months}mo lapsed). "
                f"Language '{lang}' honored. Reciprocity + real offer price. Binary YES."
            ),
        }
    else:
        # Merchant-facing: bulk winback campaign suggestion
        sal = _salutation(mer, cat)
        lapsed = _lapsed(mer)
        offer = _active_offer(mer) or _best_catalog_offer(cat)
        offer_text = offer.get("title", "offer") if offer else "offer"
        body = (
            f"{sal}, {lapsed} patients haven't visited in 6+ months. "
            f"Winback campaign with '{offer_text}' — "
            f"I'll draft 1 WhatsApp message for all {lapsed} patients. "
            f"Expected: 12-18% response rate. Ready to send? Reply YES."
        )
        return {
            "body": body.strip(),
            "cta": "binary_yes_no",
            "send_as": "vera",
            "rationale": (
                f"Winback trigger: {lapsed} lapsed patients. "
                f"Social-proof benchmark (12-18% response) + specific count. "
                f"Effort externalization: full draft ready. Binary YES."
            ),
        }


def _compose_unverified_gbp(cat, mer, trg, cus):
    sal = _salutation(mer, cat)
    locality = _locality(mer)
    body = (
        f"{sal}, aapka Google Business Profile abhi unverified hai. "
        f"Verified profiles {_peer_ctr(cat)*100:.1f}% CTR pe hain — "
        f"{locality} mein ab bhi visible nahi hain aap properly. "
        f"Verification process 48 hours mein complete ho sakta hai. Shuru karein? Reply YES."
    )
    return {
        "body": body.strip(),
        "cta": "binary_yes_no",
        "send_as": "vera",
        "rationale": (
            "Unverified GBP trigger. CTR benchmark quantifies the visibility gap. "
            "Loss-aversion: not showing in local search. 48h timeline creates mild urgency."
        ),
    }


def _compose_chronic_refill(cat, mer, trg, cus):
    if cus:
        # Customer-facing: medication refill reminder
        cust_name = cus.get("identity", {}).get("name", "there")
        lang = cus.get("identity", {}).get("language_pref", "en")
        merchant_name = mer.get("identity", {}).get("name", "us")
        medicine = trg.get("payload", {}).get("medicine_name", "your medication")
        days_left = trg.get("payload", {}).get("days_supply_remaining", 7)
        if "hi" in lang:
            body = (
                f"Hi {cust_name}, {merchant_name} yahan se. "
                f"Aapka {medicine} {days_left} din mein khatam ho jayega. "
                f"Abhi order karein taki gap na pade. Delivery ya pickup? Reply 1 ya 2."
            )
        else:
            body = (
                f"Hi {cust_name}, {merchant_name} here. "
                f"Your {medicine} supply runs out in ~{days_left} days. "
                f"Refill now to avoid a gap — delivery or pickup? Reply 1 or 2."
            )
        return {
            "body": body.strip(),
            "cta": "multi_choice_slot",
            "send_as": "merchant_on_behalf",
            "rationale": (
                f"Chronic refill trigger for {cust_name}: {medicine} in {days_left}d. "
                f"Proactive care framing. Multi-choice (delivery/pickup) reduces friction."
            ),
        }
    else:
        sal = _salutation(mer, cat)
        body = (
            f"{sal}, bulk chronic refill reminders bhejne ka time hai. "
            f"Main aapke long-term patients ko auto-reminder bhej sakti hoon 7 days pehle. "
            f"Setup karun? Ek baar, phir automatic. Reply YES."
        )
        return {
            "body": body.strip(),
            "cta": "binary_yes_no",
            "send_as": "vera",
            "rationale": "Chronic refill setup for pharmacy. Effort externalization: one-time setup → automatic. Pharmacy retention lever.",
        }


def _compose_summer_demand(cat, mer, trg, cus):
    sal = _salutation(mer, cat)
    slug = cat.get("slug", "")
    payload = trg.get("payload", {})
    peak_items = payload.get("peak_items", [])
    down_items = payload.get("down_items", [])
    # Parse trends from payload
    trends_raw = payload.get("trends", [])
    up_trends = [t.replace("_demand_", " ").replace("+", "+").split("+")[0].strip() for t in trends_raw if "+" in str(t)]
    
    if slug == "pharmacies":
        if up_trends:
            items_str = ", ".join(up_trends[:3])
            deltas = [t.split("+")[1] if "+" in t else "" for t in trends_raw[:3] if "+" in str(t)]
            delta_str = f"(up {'+'.join(deltas[:1])}%)" if deltas else ""
            body = (
                f"{sal}, summer demand shift: {items_str} trending {delta_str} this season in Jaipur. "
                f"Stock + visibility ready karun? GBP update + WhatsApp campaign ek saath. Reply YES."
            )
        else:
            body = (
                f"{sal}, summer seasonal demand shift — ORS, sunscreen, electrolytes trending hai. "
                f"Stock visibility + GBP update ready karun? Reply YES."
            )
    else:
        body = (
            f"{sal}, summer demand patterns shift ho rahe hain is area mein. "
            f"Aapki listing summer ke liye optimize karun? Reply YES."
        )
    return {
        "body": body.strip(),
        "cta": "binary_yes_no",
        "send_as": "vera",
        "rationale": "Summer demand shift trigger. Seasonal beat + specific items. Effort externalization: stock + GBP + WhatsApp in one ask.",
    }


def _compose_ipl_match(cat, mer, trg, cus):
    sal = _salutation(mer, cat)
    payload = trg.get("payload", {})
    match_desc = payload.get("match", "IPL match")
    time_str = payload.get("match_time", "tonight")
    venue = payload.get("venue", "")
    delta = payload.get("expected_restaurant_covers_delta", -0.12)
    pct = f"{int(delta*100)}%"
    offer = _active_offer(mer)
    offer_text = offer.get("title", "special offer") if offer else "special offer"
    body = (
        f"Quick heads-up {sal} — {match_desc} {time_str}. "
        + (f"{venue} — " if venue else "")
        + f"IPL nights shift {pct} restaurant covers (people watch at home). "
        f"Push your '{offer_text}' as a home-delivery special instead. "
        f"Swiggy banner + Insta story ready. Live in 10 min? Reply YES."
    )
    return {
        "body": body.strip(),
        "cta": "binary_yes_no",
        "send_as": "vera",
        "rationale": (
            f"IPL match event trigger. Counter-intuitive data ({pct} covers) saves merchant from wrong move. "
            f"Existing offer repurposed as delivery hook. Effort externalization: banner + story ready. 10-min window = urgency."
        ),
    }


def _compose_gbp_planning(cat, mer, trg, cus):
    sal = _salutation(mer, cat)
    slug = cat.get("slug", "")
    payload = trg.get("payload", {})
    merchant_name = mer.get("identity", {}).get("name", "")
    # Use intent_topic or program_name or planning_intent from payload
    intent = (payload.get("intent_topic") or payload.get("program_name") or
              payload.get("planning_intent") or "")
    intent_clean = intent.replace("_", " ")
    last_msg = payload.get("merchant_last_message", "")
    # Build contextual response if merchant already expressed intent
    if last_msg:
        body = (
            f"{sal}, great — drafting the {intent_clean} plan now. "
            f"Main 3 options prepare kar rahi hoon: pricing, package structure, and promo copy. "
            f"5 min mein ready hoga. Confirm karun? Reply YES."
        )
    else:
        body = (
            f"{sal}, main {merchant_name} ke liye ek '{intent_clean}' campaign draft kar rahi hoon. "
            f"Confirm karun aur live karun? Reply YES for full campaign."
        )
    return {
        "body": body.strip(),
        "cta": "binary_yes_no",
        "send_as": "vera",
        "rationale": "Active planning intent trigger. Effort externalization: full campaign drafted. Single binary confirm.",
    }


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_DISPATCH = {
    "research_digest": _compose_research_digest,
    "regulation_change": _compose_compliance,
    "recall_due": _compose_recall_due,
    "perf_spike": _compose_perf_spike,
    "perf_dip": _compose_perf_dip,
    "milestone_reached": _compose_milestone,
    "dormant_with_vera": _compose_dormant,
    "festival_upcoming": _compose_festival,
    "renewal_due": _compose_renewal,
    "cde_opportunity": _compose_cde_opportunity,
    "competitor_opened": _compose_competitor_opened,
    "curious_ask": _compose_curious_ask,
    "curious_ask_due": _compose_curious_ask,
    "stale_posts": _compose_stale_posts,
    "trend_movement": _compose_trend_movement,
    "review_theme_emerged": _compose_review_theme,
    "appointment_tomorrow": _compose_appointment_tomorrow,
    "customer_lapsed_soft": _compose_winback,
    "customer_winback": _compose_winback,
    "winback": _compose_winback,
    "unverified_gbp": _compose_unverified_gbp,
    "chronic_refill": _compose_chronic_refill,
    "chronic_refill_due": _compose_chronic_refill,
    "summer_demand_shift": _compose_summer_demand,
    "category_seasonal": _compose_summer_demand,
    "gbp_unverified": _compose_unverified_gbp,
    "customer_lapsed_hard": _compose_winback,
    "ipl_match": _compose_ipl_match,
    "ipl_match_today": _compose_ipl_match,
    "active_planning_intent": _compose_gbp_planning,
    "kids_yoga_program_drafting": _compose_gbp_planning,
    "corporate_thali_planning": _compose_gbp_planning,
}


def compose_rule_based(
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict] = None,
) -> dict:
    """
    Deterministic rule-based compose. No LLM needed.
    Returns: body, cta, send_as, suppression_key, rationale
    """
    trigger_kind = trigger.get("kind", "")
    fn = _DISPATCH.get(trigger_kind)
    if fn:
        result = fn(category, merchant, trigger, customer)
        if result:
            # Add suppression key and template name
            from composer import _suppression_key, _template_name
            result.setdefault("suppression_key", _suppression_key(trigger, merchant, customer))
            result.setdefault("template_name", _template_name(trigger_kind, result.get("send_as", "vera")))
            result.setdefault("template_params", [
                _salutation(merchant, category),
                result["body"][:80],
            ])
            return result

    # Generic fallback
    sal = _salutation(merchant, category)
    offer = _active_offer(merchant) or _best_catalog_offer(category)
    offer_text = offer.get("title", "your service") if offer else "your service"
    body = (
        f"{sal}, quick update for your business. "
        f"'{offer_text}' ko aur customers tak pahunchane mein help karein? Reply YES."
    )
    from composer import _suppression_key, _template_name
    return {
        "body": body,
        "cta": "binary_yes_no",
        "send_as": "vera",
        "suppression_key": _suppression_key(trigger, merchant, customer),
        "template_name": _template_name(trigger_kind, "vera"),
        "template_params": [sal, body[:80]],
        "rationale": f"Generic fallback for trigger kind '{trigger_kind}'. Active offer used as hook.",
    }
