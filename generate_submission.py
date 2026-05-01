"""
generate_submission.py — Produce submission.jsonl for the 30 canonical test pairs.
Uses LLM-based composer when ANTHROPIC_API_KEY is set; deterministic rule-based otherwise.
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(__file__))

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
USE_LLM = bool(ANTHROPIC_API_KEY)

if USE_LLM:
    from composer import compose
    print("Mode: LLM composer (Claude API)")
else:
    from rule_composer import compose_rule_based as compose
    print("Mode: Rule-based composer (no API key)")

EXPANDED = os.path.join(os.path.dirname(__file__), "..", "magicpin", "expanded")
OUT = os.path.join(os.path.dirname(__file__), "submission.jsonl")

def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def main():
    test_pairs = load_json(os.path.join(EXPANDED, "test_pairs.json"))["pairs"]
    categories = {}
    for fname in os.listdir(os.path.join(EXPANDED, "categories")):
        if fname.endswith(".json"):
            cat = load_json(os.path.join(EXPANDED, "categories", fname))
            categories[cat["slug"]] = cat
    merchants = {}
    for fname in os.listdir(os.path.join(EXPANDED, "merchants")):
        if fname.endswith(".json"):
            m = load_json(os.path.join(EXPANDED, "merchants", fname))
            merchants[m["merchant_id"]] = m
    customers = {}
    for fname in os.listdir(os.path.join(EXPANDED, "customers")):
        if fname.endswith(".json"):
            c = load_json(os.path.join(EXPANDED, "customers", fname))
            customers[c["customer_id"]] = c
    triggers = {}
    for fname in os.listdir(os.path.join(EXPANDED, "triggers")):
        if fname.endswith(".json"):
            t = load_json(os.path.join(EXPANDED, "triggers", fname))
            triggers[t["id"]] = t

    results = []
    print(f"\nGenerating {len(test_pairs)} test pairs...\n")
    for i, pair in enumerate(test_pairs):
        test_id = pair["test_id"]
        trigger_id = pair["trigger_id"]
        merchant_id = pair["merchant_id"]
        customer_id = pair.get("customer_id")
        trigger = triggers.get(trigger_id)
        merchant = merchants.get(merchant_id)
        customer = customers.get(customer_id) if customer_id else None
        if not trigger or not merchant:
            print(f"  SKIP {test_id}")
            continue
        cat_slug = merchant.get("category_slug")
        category = categories.get(cat_slug)
        if not category:
            print(f"  SKIP {test_id}: no category '{cat_slug}'")
            continue
        print(f"  [{i+1:02d}/{len(test_pairs)}] {test_id} | {cat_slug:12s} | {trigger.get('kind','?')}")
        try:
            result = compose(category, merchant, trigger, customer)
            row = {
                "test_id": test_id, "merchant_id": merchant_id, "trigger_id": trigger_id,
                "customer_id": customer_id, "body": result["body"],
                "cta": result.get("cta","open_ended"), "send_as": result.get("send_as","vera"),
                "suppression_key": result.get("suppression_key",""),
                "rationale": result.get("rationale",""),
                "template_name": result.get("template_name",""),
                "template_params": result.get("template_params",[]),
            }
            results.append(row)
            print(f"    → {result['body'][:130]}")
        except Exception as e:
            print(f"    ERROR: {e}")
            import traceback; traceback.print_exc()
        if USE_LLM:
            time.sleep(0.5)

    with open(OUT, "w", encoding="utf-8") as f:
        for row in results:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"\nWrote {len(results)} rows → {OUT}")

if __name__ == "__main__":
    main()
