#!/usr/bin/env python3
"""
local_test.py — Quick smoke test for all 5 endpoints.
Run this before submission to catch endpoint issues.

Usage:
    # Terminal 1: start the server
    python app.py
    
    # Terminal 2: run tests
    python local_test.py
"""

import json
import time
import requests
import sys

BASE = "http://localhost:8080"


def check(resp, expected_status=200):
    if resp.status_code != expected_status:
        print(f"  FAIL: got {resp.status_code}, expected {expected_status}")
        print(f"  Body: {resp.text[:300]}")
        return False
    return True


def run_tests():
    results = []

    # 1. Healthz
    print("\n[1] GET /v1/healthz")
    r = requests.get(f"{BASE}/v1/healthz")
    ok = check(r)
    if ok:
        d = r.json()
        assert d["status"] == "ok", f"status={d['status']}"
        print(f"  OK: uptime={d['uptime_seconds']}s contexts={d['contexts_loaded']}")
    results.append(("healthz", ok))

    # 2. Metadata
    print("\n[2] GET /v1/metadata")
    r = requests.get(f"{BASE}/v1/metadata")
    ok = check(r)
    if ok:
        d = r.json()
        print(f"  OK: team={d.get('team_name')} model={d.get('model')}")
    results.append(("metadata", ok))

    # 3. Push category context
    print("\n[3] POST /v1/context (category)")
    cat_payload = {
        "scope": "category",
        "context_id": "dentists",
        "version": 1,
        "payload": {
            "slug": "dentists",
            "voice": {"tone": "peer_clinical", "vocab_taboo": ["guaranteed"], "code_mix": "hindi_english_natural",
                      "salutation_examples": ["Dr. {first_name}"]},
            "offer_catalog": [{"id": "den_001", "title": "Dental Cleaning @ ₹299", "value": "299",
                                "audience": "new_user", "type": "service_at_price"}],
            "peer_stats": {"avg_rating": 4.4, "avg_ctr": 0.030, "avg_reviews": 62},
            "digest": [{"id": "d_jida_fluoride", "kind": "research",
                        "title": "3-month fluoride recall cuts caries 38% better",
                        "source": "JIDA Oct 2026, p.14", "trial_n": 2100,
                        "patient_segment": "high_risk_adults",
                        "summary": "38% lower caries recurrence with 3-month vs 6-month recall."}],
            "patient_content_library": [],
            "seasonal_beats": [{"month_range": "Nov-Feb", "note": "exam-stress bruxism spike"}],
            "trend_signals": [{"query": "clear aligners delhi", "delta_yoy": 0.62}],
        },
        "delivered_at": "2026-04-29T10:00:00Z",
    }
    r = requests.post(f"{BASE}/v1/context", json=cat_payload)
    ok = check(r)
    if ok:
        d = r.json()
        assert d["accepted"] is True
        print(f"  OK: ack_id={d.get('ack_id')}")
    results.append(("context_category", ok))

    # 4. Idempotency check
    print("\n[4] POST /v1/context (same version → 409)")
    r = requests.post(f"{BASE}/v1/context", json=cat_payload)
    ok = check(r, 409)
    if ok:
        d = r.json()
        assert d["accepted"] is False
        assert d["reason"] == "stale_version"
        print(f"  OK: correctly rejected stale version")
    results.append(("context_idempotency", ok))

    # 5. Push merchant context
    print("\n[5] POST /v1/context (merchant)")
    mer_payload = {
        "scope": "merchant",
        "context_id": "m_001_drmeera_dentist_delhi",
        "version": 1,
        "payload": {
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "category_slug": "dentists",
            "identity": {"name": "Dr. Meera's Dental Clinic", "city": "Delhi", "locality": "Lajpat Nagar",
                         "verified": True, "languages": ["en", "hi"], "owner_first_name": "Meera"},
            "subscription": {"status": "active", "plan": "Pro", "days_remaining": 82},
            "performance": {"window_days": 30, "views": 2410, "calls": 18, "ctr": 0.021,
                            "delta_7d": {"views_pct": 0.18, "calls_pct": -0.05}},
            "offers": [{"id": "o_meera_001", "title": "Dental Cleaning @ ₹299", "status": "active"}],
            "conversation_history": [],
            "customer_aggregate": {"total_unique_ytd": 540, "lapsed_180d_plus": 78,
                                   "retention_6mo_pct": 0.38, "high_risk_adult_count": 124},
            "signals": ["stale_posts:22d", "ctr_below_peer_median", "high_risk_adult_cohort"],
        },
        "delivered_at": "2026-04-29T10:01:00Z",
    }
    r = requests.post(f"{BASE}/v1/context", json=mer_payload)
    ok = check(r)
    if ok:
        d = r.json()
        assert d["accepted"] is True
        print(f"  OK: ack_id={d.get('ack_id')}")
    results.append(("context_merchant", ok))

    # 6. Push trigger context
    print("\n[6] POST /v1/context (trigger)")
    trg_payload = {
        "scope": "trigger",
        "context_id": "trg_test_research_001",
        "version": 1,
        "payload": {
            "id": "trg_test_research_001",
            "scope": "merchant",
            "kind": "research_digest",
            "source": "external",
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "customer_id": None,
            "payload": {"category": "dentists", "top_item_id": "d_jida_fluoride"},
            "urgency": 2,
            "suppression_key": "test:research:dentists:001",
            "expires_at": "2099-12-31T00:00:00Z",
        },
        "delivered_at": "2026-04-29T10:02:00Z",
    }
    r = requests.post(f"{BASE}/v1/context", json=trg_payload)
    ok = check(r)
    if ok:
        print(f"  OK: ack_id={r.json().get('ack_id')}")
    results.append(("context_trigger", ok))

    # 7. Healthz with loaded contexts
    print("\n[7] GET /v1/healthz (contexts loaded)")
    r = requests.get(f"{BASE}/v1/healthz")
    ok = check(r)
    if ok:
        d = r.json()
        c = d["contexts_loaded"]
        print(f"  OK: category={c['category']} merchant={c['merchant']} trigger={c['trigger']}")
        assert c["category"] >= 1
        assert c["merchant"] >= 1
        assert c["trigger"] >= 1
    results.append(("healthz_loaded", ok))

    # 8. Tick — should produce an action
    print("\n[8] POST /v1/tick (expect 1 action)")
    tick_payload = {
        "now": "2026-04-29T10:05:00Z",
        "available_triggers": ["trg_test_research_001"],
    }
    r = requests.post(f"{BASE}/v1/tick", json=tick_payload)
    ok = check(r)
    conv_id = None
    if ok:
        d = r.json()
        actions = d.get("actions", [])
        print(f"  OK: {len(actions)} action(s)")
        if actions:
            a = actions[0]
            conv_id = a.get("conversation_id")
            print(f"  conv_id={conv_id}")
            print(f"  send_as={a.get('send_as')} cta={a.get('cta')}")
            print(f"  body={a.get('body','')[:120]}...")
            assert a.get("body"), "body is empty"
            assert a.get("send_as") in ("vera", "merchant_on_behalf")
    results.append(("tick_action", ok and bool(actions)))

    # 9. Tick again — suppressed, should produce no action
    print("\n[9] POST /v1/tick (suppressed → empty)")
    r = requests.post(f"{BASE}/v1/tick", json=tick_payload)
    ok = check(r)
    if ok:
        d = r.json()
        print(f"  OK: {len(d.get('actions',[]))} actions (should be 0)")
    results.append(("tick_suppressed", ok))

    if conv_id:
        # 10. Reply — engaged merchant
        print(f"\n[10] POST /v1/reply (engaged)")
        reply_payload = {
            "conversation_id": conv_id,
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "from_role": "merchant",
            "message": "Yes please send the abstract",
            "received_at": "2026-04-29T10:10:00Z",
            "turn_number": 2,
        }
        r = requests.post(f"{BASE}/v1/reply", json=reply_payload)
        ok = check(r)
        if ok:
            d = r.json()
            print(f"  OK: action={d.get('action')} body={d.get('body','')[:100]}...")
            assert d.get("action") in ("send", "wait", "end")
        results.append(("reply_engaged", ok))

        # 11. Reply — auto-reply detection
        print(f"\n[11] POST /v1/reply (auto-reply detection)")
        auto_reply = {
            "conversation_id": conv_id + "_auto",
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "from_role": "merchant",
            "message": "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly.",
            "received_at": "2026-04-29T10:11:00Z",
            "turn_number": 2,
        }
        r = requests.post(f"{BASE}/v1/reply", json=auto_reply)
        ok = check(r)
        if ok:
            d = r.json()
            print(f"  OK: action={d.get('action')} (should be send/wait for auto-reply)")
        results.append(("reply_auto_detect", ok))

        # 12. Reply — hostile
        print(f"\n[12] POST /v1/reply (hostile → end)")
        hostile = {
            "conversation_id": conv_id + "_hostile",
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "from_role": "merchant",
            "message": "Stop messaging me. This is useless.",
            "received_at": "2026-04-29T10:12:00Z",
            "turn_number": 2,
        }
        r = requests.post(f"{BASE}/v1/reply", json=hostile)
        ok = check(r)
        if ok:
            d = r.json()
            print(f"  OK: action={d.get('action')} (should be end)")
            assert d.get("action") == "end", f"Expected end, got {d.get('action')}"
        results.append(("reply_hostile", ok))

    print("\n" + "="*50)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"RESULT: {passed}/{total} tests passed")
    for name, ok in results:
        print(f"  {'✓' if ok else '✗'} {name}")
    return passed == total


if __name__ == "__main__":
    print("Vera Bot — Local Endpoint Tests")
    print(f"Testing {BASE}")
    try:
        # Quick connection check
        requests.get(f"{BASE}/v1/healthz", timeout=3)
    except Exception:
        print(f"\nERROR: Cannot connect to {BASE}")
        print("Start the server first: python app.py")
        sys.exit(1)
    success = run_tests()
    sys.exit(0 if success else 1)
