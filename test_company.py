#!/usr/bin/env python3
"""test_company.py: check what jobwatch sees for specific companies, without saving anything.

  python3 test_company.py yum                 # any company in config.yaml whose name contains "yum"
  python3 test_company.py "hudson river" kaiser
  python3 test_company.py crowdstrike --all   # also list every dropped job with the reason
  python3 test_company.py anthropic --hours 72   # also show which matches count as "new" (posted in last 72h)

For each company: how many jobs were fetched, which pass your filters (with verdict, years and fit),
and why the rest were dropped. Read-only: seen.json, docs/ and alerts are never touched.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import watch

ROOT = Path(__file__).parent


def why_dropped(j: watch.Job, cfg: dict) -> str | None:
    """Same checks as watch.evaluate, but returns the reason a job fails (None = passes)."""
    t = j.title.lower()
    if not any(re.search(p, t) for p in cfg["title_include"]):
        return "title not a target role"
    for p in cfg["title_exclude"]:
        if re.search(p, t):
            return f"title excluded ({p})"
    comp = j.company.lower()
    if any(s in comp for s in cfg.get("skip_companies") or []):
        return "company on skip list"
    if any(s[0] in comp and s[1] in t for s in cfg.get("skip_roles") or []):
        return "role on skip list"
    if watch.NON_US_RE.search(j.title) and not watch.US_RE.search(j.title):
        return "non-US location in title"
    ok, _ = watch.location_ok(j, cfg)
    if not ok:
        return f"location ({j.location or 'none listed'})"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="+", help="part of a company name from config.yaml")
    ap.add_argument("--all", action="store_true", help="list every dropped job and why")
    ap.add_argument("--hours", type=float, help="mark which matches were posted within this many hours")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    watch.CFG.update(cfg)
    picked = [c for c in cfg["companies"] if any(n.lower() in c["name"].lower() for n in args.names)]
    if not picked:
        sys.exit("No company in config.yaml matches that. Names in config: "
                 + ", ".join(c["name"] for c in cfg["companies"][:30]) + ", …")

    for c in picked:
        print(f"\n━━ {c['name']}  ({c['ats']})")
        try:
            jobs = watch.FETCHERS[c["ats"]](c)
        except Exception as e:  # noqa: BLE001
            print(f"   ✗ FAILED to load: {e}")
            continue
        print(f"   fetched {len(jobs)} jobs")
        if not jobs:
            print("   (board loaded but returned nothing; it may be unused, or the keyword searches found nothing)")
            continue

        passed, dropped = [], []
        for j in jobs:
            reason = why_dropped(j, cfg)
            if reason:
                dropped.append((j, reason))
                continue
            if c["ats"] == "workday":
                watch.workday_detail(j, c)
            elif c["ats"] == "oracle":
                watch.oracle_detail(j, c)
            if not watch.score_text(j):
                dropped.append((j, f"requires {j.years}+ years"))
                continue
            a = watch.analyze(j)
            if c.get("min_fit") and a["fit"] < c["min_fit"]:
                dropped.append((j, f"fit {a['fit']}% below min_fit {c['min_fit']}"))
                continue
            passed.append((j, a))

        cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours) if args.hours else None
        print(f"   ✓ {len(passed)} pass your filters")
        for j, a in sorted(passed, key=lambda x: -x[1]["fit"]):
            age = ""
            if j.posted:
                h = (datetime.now(timezone.utc) - j.posted).total_seconds() / 3600
                age = f"posted {h:.0f}h ago" if h < 72 else f"posted {j.posted:%b %d}"
            new = "  [NEW]" if cutoff and j.posted and j.posted >= cutoff else ""
            print(f"     • {j.title}  —  {j.location}{new}")
            print(f"       {j.verdict} · yrs {j.years if j.years is not None else '?'} · {a['fit']}% fit · {a['workplace']}"
                  + (f" · {j.salary}" if j.salary else "") + (f" · {age}" if age else "")
                  + (f" · flags: {', '.join(j.flags)}" if j.flags else ""))
            print(f"       {j.url}")

        counts = Counter(r if not r.startswith(("title excluded", "location", "requires", "fit ")) else r.split(" (")[0].split(" below")[0]
                         for _, r in dropped)
        print(f"   ✗ {len(dropped)} dropped: " + ", ".join(f"{n} {r}" for r, n in counts.most_common()))
        near = [(j, r) for j, r in dropped if r != "title not a target role"]
        show = dropped if args.all else near[:10]
        if show:
            print("   " + ("all dropped jobs:" if args.all else "closest misses (target title, but dropped):"))
            for j, r in show:
                print(f"     - {j.title}  —  {r}")
        elif not args.all:
            print("   (no near misses; add --all to see every dropped title)")


if __name__ == "__main__":
    main()
