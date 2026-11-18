#!/usr/bin/env python3
"""check_boards.py — find which candidate companies have a working job board AND currently post
roles that pass your filters. Run on your own computer (not in GitHub Actions):

    python3 check_boards.py                 # reads candidates.txt, uses filters from config.yaml
    python3 check_boards.py --min 1         # only suggest companies with >= 1 matching opening (default)

For each candidate it tries Greenhouse, Lever and Ashby with a few slug spellings. Output:
  - a ranked table: company, board, total openings, openings that pass your filters
  - new_companies.yaml: ready-to-paste config entries for companies with matches
Companies already in config.yaml are skipped.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yaml

import watch  # reuse the same filters as the real scan

ROOT = Path(__file__).parent
UA = {"User-Agent": "jobwatch-checker/1.0 (personal job search)"}


def slug_variants(name: str, hints: list[str]) -> list[str]:
    base = re.sub(r"[^a-z0-9 ]", "", name.lower().replace("&", "and")).split()
    out = hints + ["".join(base), "-".join(base)]
    seen, res = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s); res.append(s)
    return res


def try_board(ats: str, slug: str):
    try:
        if ats == "greenhouse":
            r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", headers=UA, timeout=20)
        elif ats == "lever":
            r = requests.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}, headers=UA, timeout=20)
        else:
            r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", headers=UA, timeout=20)
        if r.status_code != 200:
            return None
        data = r.json()
        jobs = data if isinstance(data, list) else data.get("jobs", [])
        return jobs if jobs else None  # an empty board is usually a dead/old one
    except Exception:  # noqa: BLE001
        return None


def count_matches(name: str, ats: str, raw: list, cfg: dict) -> tuple[int, list[str]]:
    c = {"name": name, "ats": ats, "slug": "x"}
    if ats == "greenhouse":
        jobs = [watch.Job(name, j.get("title", ""), (j.get("location") or {}).get("name", ""),
                          j.get("absolute_url", ""), str(j.get("id")), None) for j in raw]
    elif ats == "lever":
        jobs = [watch.Job(name, j.get("text", ""), (j.get("categories") or {}).get("location", "") or "",
                          j.get("hostedUrl", ""), j.get("id", ""), None,
                          remote_hint=j.get("workplaceType") == "remote") for j in raw]
    else:
        jobs = [watch.Job(name, j.get("title", ""), j.get("location", ""), j.get("jobUrl", ""), j.get("id", ""),
                          None, remote_hint=bool(j.get("isRemote"))) for j in raw if j.get("isListed", True)]
    hits = [j.title for j in jobs if watch.evaluate(j, cfg)]
    return len(hits), hits


def check(entry, cfg):
    name, hints = entry
    for slug in slug_variants(name, hints):
        for ats in ("greenhouse", "ashby", "lever"):
            raw = try_board(ats, slug)
            if raw:
                n, hits = count_matches(name, ats, raw, cfg)
                return name, ats, slug, len(raw), n, hits
    return name, None, None, 0, 0, []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=int, default=1, help="minimum matching openings to suggest a company")
    ap.add_argument("--file", default="candidates.txt")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    watch.CFG.update(cfg)
    have = {c["name"].lower() for c in cfg["companies"]} | {str(c.get("slug", "")).lower() for c in cfg["companies"]}

    entries = []
    for line in (ROOT / args.file).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, _, hint = line.partition("|")
        name = name.strip()
        hints = [h.strip() for h in hint.split(",") if h.strip()]
        if name.lower() in have or any(h.lower() in have for h in hints):
            continue
        entries.append((name, hints))
    print(f"checking {len(entries)} candidates (this takes a few minutes)…", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(lambda e: check(e, cfg), entries))

    found = sorted([r for r in results if r[1]], key=lambda r: -r[4])
    missing = [r[0] for r in results if not r[1]]
    print(f"\n{'company':32} {'board':10} {'slug':28} {'open':>5} {'match':>5}")
    for name, ats, slug, total, n, hits in found:
        print(f"{name[:32]:32} {ats:10} {slug[:28]:28} {total:5} {n:5}" + (f"   e.g. {hits[0][:50]}" if hits else ""))

    keep = [r for r in found if r[4] >= args.min]
    lines = ["  # --- added by check_boards.py (matching openings at check time in brackets) ---"]
    lines += [f"  - {{name: {json.dumps(n)}, ats: {a}, slug: {s}}}   # [{m}]"
              for n, a, s, _, m, _ in keep]
    (ROOT / "new_companies.yaml").write_text("\n".join(lines) + "\n")
    print(f"\n{len(found)} boards found, {len(keep)} with >= {args.min} matching openings -> new_companies.yaml")
    print(f"{len(found) - len(keep)} boards work but have no matching openings right now (not added).")
    print(f"{len(missing)} not found on Greenhouse/Lever/Ashby: {', '.join(missing)}")
    print("\nNext: paste new_companies.yaml into the `companies:` list in config.yaml.")


if __name__ == "__main__":
    main()
