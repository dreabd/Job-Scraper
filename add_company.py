#!/usr/bin/env python3
"""add_company.py: add a company to config.yaml from its careers link (or just its name).

Examples:
  python3 add_company.py https://job-boards.greenhouse.io/anthropic
  python3 add_company.py https://jobs.lever.co/palantir
  python3 add_company.py https://jobs.ashbyhq.com/openai
  python3 add_company.py https://crowdstrike.wd5.myworkdayjobs.com/crowdstrikecareers
  python3 add_company.py "https://eczd.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/jobs" \
      --name "Yum! Brands (Taco Bell)" --keywords "engineer,support,devops,reliability,operations,analyst"
  python3 add_company.py https://www.somecompany.com/careers     # finds the job board linked from the page
  python3 add_company.py "Old Mission Capital"                    # tries Greenhouse/Ashby/Lever by name

Options:
  --name "Display Name"   name to show on the page (otherwise guessed)
  --favorite              also add to favorites (alerts for this company)
  --dry-run               check and show what would be added, change nothing

It checks the board works, shows openings that pass your filters, then adds one line to the
`companies:` list. Commit and push config.yaml afterwards.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
import yaml

import watch
from check_boards import slug_variants, try_board

ROOT = Path(__file__).parent
CONFIG = ROOT / "config.yaml"
UA = {"User-Agent": "Mozilla/5.0 (jobwatch add_company)"}

PATTERNS = [
    ("greenhouse", re.compile(r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)")),
    ("workday", re.compile(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")),
    ("oracle", re.compile(r"([a-z0-9-]+\.fa(?:\.[a-z0-9]+)?\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-z-]+/sites/([A-Za-z0-9_]+)")),
]
SKIP_SLUGS = {"embed", "v1", "jobs", "job_board", "boards"}


def parse_link(text: str) -> dict | None:
    for ats, rx in PATTERNS:
        m = rx.search(text)
        if not m:
            continue
        if ats == "workday":
            return {"ats": ats, "tenant": m.group(1), "wd": m.group(2), "site": m.group(3)}
        if ats == "oracle":
            return {"ats": ats, "host": m.group(1), "site": m.group(2)}
        slug = m.group(1)
        if ats == "greenhouse" and slug == "embed":
            q = parse_qs(urlparse(text).query).get("for")
            slug = q[0] if q else slug
        if slug.lower() not in SKIP_SLUGS:
            return {"ats": ats, "slug": slug}
    return None


def find_on_page(url: str) -> dict | None:
    """Careers page on the company's own site: look for an embedded/linked job board."""
    try:
        html = requests.get(url, headers=UA, timeout=20).text
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Couldn't open {url}: {e}")
    return parse_link(html)


def guess_name(entry: dict, raw_jobs: list) -> str:
    if entry["ats"] == "greenhouse":
        try:
            d = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{entry['slug']}", headers=UA, timeout=15).json()
            if d.get("name"):
                return d["name"]
        except Exception:  # noqa: BLE001
            pass
    key = entry.get("slug") or entry.get("tenant") or entry.get("host", "").split(".")[0]
    return re.sub(r"[-_.]+", " ", key).title()


def already_listed(entry: dict, cfg: dict) -> dict | None:
    keys = ("ats", "slug", "tenant", "host", "site")
    for c in cfg.get("companies") or []:
        if all(str(c.get(k, "")).lower() == str(entry.get(k, "")).lower() for k in keys if k in entry):
            return c
    return None


def to_line(entry: dict) -> str:
    parts = [f"name: {json.dumps(entry['name'])}", f"ats: {entry['ats']}"]
    for k in ("slug", "tenant", "wd", "site", "host"):
        if k in entry:
            parts.append(f"{k}: {entry[k]}")
    if entry.get("keywords"):
        parts.append("keywords: [" + ", ".join(entry["keywords"]) + "]")
    return "  - {" + ", ".join(parts) + "}"


def insert_company(text: str, line: str) -> str:
    """Add the line at the end of the `companies:` list, before any trailing comment block."""
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("companies:"))
    end = start + 1
    last_item = start
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "\t", "#"))):
        if lines[end].lstrip().startswith("- "):
            last_item = end
        if lines[end].startswith("#") and lines[end - 1].strip() == "" and end > last_item + 1:
            break
        end += 1
    stamp = "  # --- added with add_company.py ---"
    if stamp not in lines:
        lines.insert(last_item + 1, stamp)
        last_item += 1
    else:
        last_item = max(last_item, lines.index(stamp))
    lines.insert(last_item + 1, line)
    return "\n".join(lines) + "\n"


def add_favorite(text: str, name: str) -> str | None:
    m = re.search(r"^favorites:[ \t]*\[(.*?)\][ \t]*$", text, re.M)
    if not m:
        return None
    items = [x for x in yaml.safe_load(f"[{m.group(1)}]") or []]
    if name in items:
        return text
    items.append(name)
    return text[:m.start()] + "favorites: " + json.dumps(items) + text[m.end():]


def main():
    ap = argparse.ArgumentParser(description="Add a company to jobwatch's config.yaml")
    ap.add_argument("target", help="careers link, job board link, or company name")
    ap.add_argument("--name", help="display name")
    ap.add_argument("--favorite", action="store_true", help="also add to favorites (alerts)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keywords", help='Oracle sites only: comma-separated searches, e.g. "engineer,support,devops"')
    args = ap.parse_args()

    cfg = yaml.safe_load(CONFIG.read_text())
    watch.CFG.update(cfg)
    t = args.target.strip()

    entry = parse_link(t)
    if not entry and t.startswith("http"):
        print("Not a job board link; looking for a job board on that page…")
        entry = find_on_page(t)
        if not entry:
            sys.exit("Couldn't find a Greenhouse, Lever, Ashby, Workday or Oracle board on that page.\n"
                     "Open one of their job postings and try that link instead.")
    if not entry:  # plain company name
        print(f"Searching Greenhouse, Ashby and Lever for “{t}”…")
        for slug in slug_variants(t, []):
            for ats in ("greenhouse", "ashby", "lever"):
                if try_board(ats, slug):
                    entry = {"ats": ats, "slug": slug}
                    break
            if entry:
                break
        if not entry:
            sys.exit(f"No board found for “{t}”. Try their careers page link instead.")
        args.name = args.name or t

    dup = already_listed(entry, cfg)
    if dup:
        print(f"Already in config.yaml as “{dup['name']}”.")
        if args.favorite and not args.dry_run:
            new = add_favorite(CONFIG.read_text(), dup["name"])
            if new:
                CONFIG.write_text(new); print(f"Added “{dup['name']}” to favorites.")
        return

    entry["name"] = args.name or "?"
    if args.keywords and entry["ats"] == "oracle":
        entry["keywords"] = [k.strip() for k in args.keywords.split(",") if k.strip()]
    print(f"Found {entry['ats']} board: " + ", ".join(f"{k}={v}" for k, v in entry.items() if k not in ("ats", "name")))
    try:
        jobs = watch.FETCHERS[entry["ats"]](entry)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"The board didn't load ({e}). Not added.")
    if not jobs:
        sys.exit("The board loaded but lists 0 jobs (it may be unused). Not added.")
    if not args.name:
        entry["name"] = guess_name(entry, jobs)
        for j in jobs:
            j.company = entry["name"]

    hits = [j for j in jobs if watch.evaluate(j, cfg)]
    print(f"{entry['name']}: {len(jobs)} open jobs, {len(hits)} pass your filters right now")
    for j in hits[:8]:
        print(f"   • {j.title} — {j.location}")
    if len(hits) > 8:
        print(f"   … and {len(hits) - 8} more")

    line = to_line(entry)
    if args.dry_run:
        print("\nWould add:\n" + line + ("\nand add to favorites" if args.favorite else ""))
        return
    text = insert_company(CONFIG.read_text(), line)
    if args.favorite:
        fav = add_favorite(text, entry["name"])
        if fav:
            text = fav
        else:
            print("(Couldn't edit favorites automatically; add the name to `favorites:` by hand.)")
    yaml.safe_load(text)  # make sure the file is still valid before writing
    CONFIG.write_text(text)
    print(f"\nAdded to config.yaml:\n{line}" + ("\n★ and to favorites" if args.favorite else ""))
    print('Next: git add config.yaml && git commit -m "Add ' + entry["name"] + '" && git push')


if __name__ == "__main__":
    main()