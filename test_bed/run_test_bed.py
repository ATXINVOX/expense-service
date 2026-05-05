#!/usr/bin/env python3
"""
Run scenarios from expense_dashboard_test_bed.v1.json against the expense API.

Usage:
  python3 run_test_bed.py --list
  python3 run_test_bed.py --print-curl --case DASH-02-preset-week

  export EXPENSE_BASE_URL=http://localhost:8000
  export EXPENSE_SID=<X-Frappe-SID>
  python3 run_test_bed.py --run --case DASH-03-preset-month
  python3 run_test_bed.py --run-all

Requires Python 3.9+ (stdlib only).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

_BED = Path(__file__).resolve().parent / "expense_dashboard_test_bed.v1.json"


def load_bed() -> dict:
    with open(_BED, encoding="utf-8") as f:
        return json.load(f)


def substitute_headers(headers: dict, sid: str | None) -> dict:
    out = {}
    for k, v in (dict(headers or {}).items()):
        if isinstance(v, str):
            out[k] = v.replace("{{SID}}", sid or "")
        else:
            out[k] = v
    return out


def list_cases(bed: dict) -> None:
    for c in bed["cases"]:
        print(f"{c['id']}\t{', '.join(c.get('tags', []))}\t{c['title']}")


def print_curl(base: str, case: dict, sid: str | None) -> None:
    req = case["request"]
    path = req["path"]
    url = base.rstrip("/") + path
    headers = substitute_headers(req.get("headers") or {}, sid)
    method = (req.get("method") or "GET").upper()
    print(f"# {case['id']}: {case['title']}")
    line = f"curl -sS -X {method} {json.dumps(url)}"
    for hk, hv in headers.items():
        line += " \\\n  -H " + json.dumps(f"{hk}: {hv}")
    print(line)
    print()


def _payload_from_response(parsed: dict) -> dict:
    if isinstance(parsed.get("message"), dict):
        return parsed["message"]
    return parsed


def validate_expect(case_id: str, status: int, parsed: object, exp: dict) -> tuple[bool, list[str]]:
    errs: list[str] = []
    want = exp.get("http_status")
    ok = True
    if want is not None:
        if isinstance(want, list):
            ok = status in want
        else:
            ok = status == want
        if not ok:
            errs.append(f"HTTP {status} not in expected {want}")

    if not isinstance(parsed, dict):
        return ok and not errs, errs

    payload = _payload_from_response(parsed)

    keys_subset = exp.get("payload_keys_subset") or exp.get("message_keys_subset")
    if keys_subset and isinstance(payload, dict):
        missing = [k for k in keys_subset if k not in payload]
        if missing:
            ok = False
            errs.append(f"missing keys: {missing}")

    must_not = exp.get("payload_must_not_have_keys")
    if must_not and isinstance(payload, dict):
        bad = [k for k in must_not if k in payload]
        if bad:
            ok = False
            errs.append(f"unexpected keys present: {bad}")

    equals = exp.get("payload_equals")
    if equals and isinstance(payload, dict):
        for k, v in equals.items():
            if payload.get(k) != v:
                ok = False
                errs.append(f"payload[{k!r}] want {v!r} got {payload.get(k)!r}")

    return ok, errs


def run_case(base: str, case: dict, sid: str | None) -> int:
    req = case["request"]
    path = req["path"]
    url = base.rstrip("/") + path
    method = (req.get("method") or "GET").upper()
    headers = substitute_headers(req.get("headers") or {}, sid)

    data = None
    if method != "GET" and req.get("body") is not None:
        data = json.dumps(req["body"]).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")

    hreq = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(hreq, timeout=60) as resp:
            status = resp.status
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status = e.code
        text = e.read().decode("utf-8", errors="replace")

    try:
        parsed = json.loads(text) if text else {}
    except json.JSONDecodeError:
        parsed = {"_raw": text[:500]}

    exp = case.get("expect") or {}
    ok, errs = validate_expect(case["id"], status, parsed, exp)

    label = "OK" if ok else "FAIL"
    print(f"CASE {case['id']} -> HTTP {status} {label}")
    for e in errs:
        print(f"  ! {e}")
    try:
        print(json.dumps(parsed, indent=2)[:5000])
    except Exception:
        print(str(parsed)[:2000])

    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Expense dashboard test bed runner")
    ap.add_argument("--list", action="store_true", help="List case ids")
    ap.add_argument("--print-curl", action="store_true", help="Print curl for one case")
    ap.add_argument("--run", action="store_true", help="Execute one case")
    ap.add_argument("--run-all", action="store_true", help="Execute every case (needs SID except auth-negative)")
    ap.add_argument("--case", type=str, default=None, help="Case id")
    args = ap.parse_args()
    bed = load_bed()

    if args.list:
        list_cases(bed)
        return 0

    base = os.environ.get("EXPENSE_BASE_URL", "http://localhost:8000")
    sid = os.environ.get("EXPENSE_SID") or os.environ.get("TEST_SESSION_ID")

    if args.print_curl:
        if not args.case:
            print("Need --case <id>", file=sys.stderr)
            return 2
        case = next((c for c in bed["cases"] if c["id"] == args.case), None)
        if not case:
            print("Unknown case", file=sys.stderr)
            return 2
        print_curl(base, case, sid)
        if not sid and "{{SID}}" in json.dumps(case.get("request", {}).get("headers", {})):
            print("# Set EXPENSE_SID for authenticated cases.", file=sys.stderr)
        return 0

    if args.run:
        if not args.case:
            print("Need --case <id>", file=sys.stderr)
            return 2
        case = next((c for c in bed["cases"] if c["id"] == args.case), None)
        if not case:
            print("Unknown case", file=sys.stderr)
            return 2
        if case["id"] != "DASH-09-no-session" and not sid:
            print("Set EXPENSE_SID (or TEST_SESSION_ID).", file=sys.stderr)
            return 2
        return run_case(base, case, sid)

    if args.run_all:
        rc = 0
        for case in bed["cases"]:
            use_sid = sid
            if case["id"] == "DASH-09-no-session":
                use_sid = None
            elif not sid:
                print("SKIP (no EXPENSE_SID):", case["id"], file=sys.stderr)
                rc = 1
                continue
            print("---")
            if run_case(base, case, use_sid):
                rc = 1
        return rc

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
