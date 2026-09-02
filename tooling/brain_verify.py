#!/usr/bin/env python3
"""Verify closed, read-only contracts attached to critical Brain memories."""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlparse

SUPPORTED = {"file-exists", "file-contains", "valid-until"}


def validate_contract_uri(contract: str) -> dict[str, str]:
    parsed = urlparse(contract)
    if parsed.scheme not in SUPPORTED or parsed.path != "v1":
        raise ValueError(f"unsupported verification contract: {contract!r}")
    query = {key: values[-1] for key, values in parse_qs(parsed.query, strict_parsing=True).items()}
    if parsed.scheme in {"file-exists", "file-contains"}:
        raw_path = query.get("path", "")
        pure = PurePosixPath(raw_path)
        if (
            not raw_path or "\\" in raw_path or pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise ValueError("contract path escapes the allowed root")
        if parsed.scheme == "file-contains" and not query.get("text"):
            raise ValueError("file-contains requires non-empty text")
    elif parsed.scheme == "valid-until":
        try:
            date.fromisoformat(query.get("date", ""))
        except ValueError as exc:
            raise ValueError("valid-until requires an ISO date") from exc
    return {"kind": parsed.scheme, **query}


def _confined_file(root: Path, relative: str) -> Path:
    candidate = (root / PurePosixPath(relative)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("contract path escapes the allowed root") from exc
    return candidate


def verify_contracts(root: str | Path, contracts, *, today: date | None = None) -> dict:
    root_path = Path(root).resolve()
    current_date = today or date.today()
    results = []
    for contract in contracts:
        descriptor = validate_contract_uri(str(contract))
        kind = descriptor["kind"]
        passed = False
        reason = ""
        if kind == "valid-until":
            passed = current_date <= date.fromisoformat(descriptor["date"])
            reason = "within validity window" if passed else "validity window expired"
        else:
            target = _confined_file(root_path, descriptor["path"])
            if kind == "file-exists":
                passed = target.is_file()
                reason = "file exists" if passed else "file is missing"
            else:
                if target.is_file() and target.stat().st_size <= 1_000_000:
                    passed = descriptor["text"] in target.read_text(encoding="utf-8")
                    reason = "expected text found" if passed else "expected text absent"
                else:
                    reason = "file missing or exceeds read limit"
        results.append({"contract": str(contract), "status": "valid" if passed else "stale", "reason": reason})
    valid = sum(item["status"] == "valid" for item in results)
    return {"total": len(results), "valid": valid, "stale": len(results) - valid, "results": results}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--contract", action="append", default=[])
    args = parser.parse_args()
    report = verify_contracts(args.root, args.contract)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["stale"] == 0 else 1)


if __name__ == "__main__":
    main()
