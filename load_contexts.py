"""Load the expanded magicpin challenge contexts into a running Vera API."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request


ROOT = Path(__file__).resolve().parent
SCOPES = (
    ("category", "categories", "slug"),
    ("merchant", "merchants", "merchant_id"),
    ("customer", "customers", "customer_id"),
    ("trigger", "triggers", "id"),
)


def default_expanded_dir() -> Path:
    candidates = (
        ROOT / "expanded",
        ROOT.parent / "magicpin-ai-challenge" / "expanded",
        ROOT.parent / "magicpin" / "expanded",
    )
    for candidate in candidates:
        if (candidate / "test_pairs.json").is_file():
            return candidate
    raise FileNotFoundError(
        "Expanded fixtures not found. Pass --expanded-dir /path/to/magicpin-ai-challenge/expanded"
    )


def api_call(base_url: str, method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code == 409:
            return json.loads(body)
        raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {body}") from exc
    except error.URLError as exc:
        raise RuntimeError(
            f"Could not reach {base_url}. Start the server first with: "
            "python3 -m uvicorn bot:app --host 0.0.0.0 --port 8080"
        ) from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk-load expanded contexts into Vera.")
    parser.add_argument("--url", default="http://localhost:8080", help="Running Vera API base URL")
    parser.add_argument("--expanded-dir", type=Path, help="Path to the expanded challenge folder")
    parser.add_argument("--version", type=int, default=1, help="Context version to push")
    parser.add_argument("--tick", action="store_true", help="Call /v1/tick after loading")
    parser.add_argument(
        "--now",
        default="2026-04-26T10:00:00Z",
        help="Simulated ISO timestamp used by --tick",
    )
    args = parser.parse_args()

    expanded = args.expanded_dir or default_expanded_dir()
    delivered_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    trigger_ids: list[str] = []
    totals: dict[str, int] = {}

    for scope, folder, identity_key in SCOPES:
        count = 0
        for path in sorted((expanded / folder).glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            context_id = payload[identity_key]
            result = api_call(
                args.url,
                "POST",
                "/v1/context",
                {
                    "scope": scope,
                    "context_id": context_id,
                    "version": args.version,
                    "payload": payload,
                    "delivered_at": delivered_at,
                },
            )
            if result.get("accepted") or result.get("reason") == "stale_version":
                count += 1
            if scope == "trigger":
                trigger_ids.append(context_id)
        totals[scope] = count
        print(f"Loaded {count} {scope} contexts")

    health = api_call(args.url, "GET", "/v1/healthz")
    print("Health:", json.dumps(health, ensure_ascii=False))

    if args.tick:
        result = api_call(
            args.url,
            "POST",
            "/v1/tick",
            {"now": args.now, "available_triggers": trigger_ids},
        )
        print(f"Tick returned {len(result.get('actions', []))} actions")
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
