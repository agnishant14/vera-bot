"""Generate the 30-line static submission from the expanded challenge data."""

from __future__ import annotations

import json
import argparse
from pathlib import Path

from bot import compose


ROOT = Path(__file__).resolve().parent


def _default_expanded_dir() -> Path:
    """Find the challenge's generated expanded fixtures without vendoring them."""
    candidates = (
        ROOT / "expanded",
        ROOT.parent / "magicpin-ai-challenge" / "expanded",
        ROOT.parent / "magicpin" / "expanded",
    )
    for candidate in candidates:
        if (candidate / "test_pairs.json").is_file():
            return candidate
    searched = ", ".join(str(path) for path in candidates)
    raise FileNotFoundError(
        "Expanded challenge fixtures not found. Expected test_pairs.json in: " + searched
    )


def main(expanded_dir: Path | None = None, output: Path | None = None) -> None:
    expanded = expanded_dir or _default_expanded_dir()
    # Keep all inputs in one resolved directory so the script works both from
    # a checked-out expanded folder and from the sibling challenge folder.
    def load(name: str, key: str) -> dict[str, dict]:
        values = {}
        for path in (expanded / name).glob("*.json"):
            item = json.loads(path.read_text(encoding="utf-8"))
            values[item[key]] = item
        return values

    categories = load("categories", "slug")
    merchants = load("merchants", "merchant_id")
    customers = load("customers", "customer_id")
    triggers = load("triggers", "id")
    pairs = json.loads((expanded / "test_pairs.json").read_text(encoding="utf-8"))["pairs"]

    destination = output or (ROOT / "submission.jsonl")
    with destination.open("w", encoding="utf-8") as handle:
        for pair in pairs:
            merchant = merchants[pair["merchant_id"]]
            trigger = triggers[pair["trigger_id"]]
            customer = customers.get(pair.get("customer_id"))
            result = compose(categories[merchant["category_slug"]], merchant, trigger, customer)
            handle.write(json.dumps({"test_id": pair["test_id"], **result}, ensure_ascii=False) + "\n")
    print(f"Wrote {len(pairs)} rows to {destination} using {expanded}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate the 30-line canonical submission JSONL.")
    parser.add_argument("--expanded-dir", type=Path, help="Path to magicpin-ai-challenge/expanded")
    parser.add_argument("--output", type=Path, help="Destination JSONL path")
    args = parser.parse_args()
    main(args.expanded_dir, args.output)
