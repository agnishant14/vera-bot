"""Generate the 30-line static submission from the expanded challenge data."""

from __future__ import annotations

import json
from pathlib import Path

from bot import compose


ROOT = Path(__file__).resolve().parent
EXPANDED = ROOT / "expanded"


def load_directory(name: str, key: str) -> dict[str, dict]:
    output = {}
    for path in (EXPANDED / name).glob("*.json"):
        item = json.loads(path.read_text(encoding="utf-8"))
        output[item[key]] = item
    return output


def main() -> None:
    categories = load_directory("categories", "slug")
    merchants = load_directory("merchants", "merchant_id")
    customers = load_directory("customers", "customer_id")
    triggers = load_directory("triggers", "id")
    pairs = json.loads((EXPANDED / "test_pairs.json").read_text(encoding="utf-8"))["pairs"]

    output = ROOT / "submission.jsonl"
    with output.open("w", encoding="utf-8") as handle:
        for pair in pairs:
            merchant = merchants[pair["merchant_id"]]
            trigger = triggers[pair["trigger_id"]]
            customer = customers.get(pair.get("customer_id"))
            result = compose(categories[merchant["category_slug"]], merchant, trigger, customer)
            handle.write(json.dumps({"test_id": pair["test_id"], **result}, ensure_ascii=False) + "\n")
    print(f"Wrote {len(pairs)} rows to {output}")


if __name__ == "__main__":
    main()
