#!/usr/bin/env python3
"""jobwatch: poll public ATS job-board APIs, filter, push new matches to your phone.

Sources (all official public endpoints meant for job listings):
  Greenhouse  https://boards-api.greenhouse.io/v1/boards/<slug>/jobs
  Lever       https://api.lever.co/v0/postings/<slug>?mode=json
  Ashby       https://api.ashbyhq.com/posting-api/job-board/<slug>
  Workday     POST https://<tenant>.<wd>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs

Env vars:
  NTFY_TOPIC   ntfy.sh topic name (keep it long/random; it acts as a password)
  NTFY_SERVER  optional, default https://ntfy.sh
  DRY_RUN=1    print matches instead of notifying
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
STATE = ROOT / "seen.json"
MATCHES = ROOT / "docs" / "matches.json"   # read by docs/index.html (GitHub Pages)
KEEP_DAYS = 45
UA = {"User-Agent": "jobwatch/1.0 (personal job alert script)"}
TIMEOUT = 25


@dataclass
class Job:
    company: str
    title: str
    location: str
    url: str
    uid: str
    posted: datetime | None
    text: str = ""            # plain-text description (may be empty)
    remote_hint: bool = False
    salary: str = ""
    flags: list[str] = field(default_factory=list)
    years: int | None = None
    verdict: str = ""


# ---------------------------------------------------------------- helpers
def strip_html(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_dt(s) -> datetime | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):  # epoch ms (Lever)
        return datetime.fromtimestamp(s / 1000, tz=timezone.utc)
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- fetchers
def fetch_greenhouse(c) -> list[Job]:
    data = get(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs", params={"content": "true"})
    out = []
    for j in data.get("jobs", []):
        out.append(Job(
            company=c["name"], title=j.get("title", ""),
            location=(j.get("location") or {}).get("name", ""),
            url=j.get("absolute_url", ""), uid=f"gh:{c['slug']}:{j.get('id')}",
            posted=parse_dt(j.get("first_published") or j.get("updated_at")),
            text=strip_html(j.get("content", "")),
        ))
    return out


def fetch_lever(c) -> list[Job]:
    data = get(f"https://api.lever.co/v0/postings/{c['slug']}", params={"mode": "json"})
    out = []
    for j in data:
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location", "")]
        text = " ".join([j.get("descriptionPlain", "")] +
                        [strip_html(l.get("content", "")) for l in j.get("lists", [])] +
                        [j.get("additionalPlain", "")])
        sal = j.get("salaryRange") or {}
        out.append(Job(
            company=c["name"], title=j.get("text", ""), location=" / ".join(filter(None, locs)),
            url=j.get("hostedUrl", ""), uid=f"lv:{c['slug']}:{j.get('id')}",
            posted=parse_dt(j.get("createdAt")), text=text,
            remote_hint=(j.get("workplaceType") == "remote"),
            salary=f"${sal['min']:,}–${sal['max']:,}" if sal.get("min") and sal.get("max") else "",
        ))
    return out


def fetch_ashby(c) -> list[Job]:
    data = get(f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}",
               params={"includeCompensation": "true"})
    out = []
    for j in data.get("jobs", []):
        if not j.get("isListed", True):
            continue
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations", []) or []]
        comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
        out.append(Job(
            company=c["name"], title=j.get("title", ""), location=" / ".join(filter(None, locs)),
            url=j.get("jobUrl", ""), uid=f"ab:{c['slug']}:{j.get('id')}",
            posted=parse_dt(j.get("publishedAt")),
            text=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
            remote_hint=bool(j.get("isRemote")) or j.get("workplaceType") == "Remote",
            salary=comp,
        ))
    return out


def workday_search(api: str, term: str) -> list[dict]:
    """POST a Workday search, backing off on 429 (Too Many Requests)."""
    for attempt in range(4):
        r = requests.post(api, timeout=TIMEOUT, json={"appliedFacets": {}, "limit": 20, "offset": 0,
                                                      "searchText": term},
                          headers={**UA, "Content-Type": "application/json",
                                   "Accept": "application/json", "Accept-Language": "en-US"})
        if r.status_code == 429 and attempt < 3:
            wait = r.headers.get("Retry-After", "")
            time.sleep(int(wait) if wait.isdigit() else 5 * 2 ** attempt)  # 5s, 10s, 20s
            continue
        r.raise_for_status()
        return r.json().get("jobPostings", [])
    return []


def fetch_workday(c) -> list[Job]:
    base = f"https://{c['tenant']}.{c['wd']}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{c['tenant']}/{c['site']}/jobs"
    out, seen = [], set()
    # Workday ranks search results by relevance, not date, so use narrow terms that
    # match the target titles; each returns few enough results that new ones aren't missed.
    terms = ["site reliability", "devops", "support engineer", "production",
             "application support", "operations engineer"]
    failures = []
    for term in terms:
        time.sleep(1.5)  # Workday rate-limits bursts (HTTP 429)
        try:
            postings = workday_search(api, term)
        except Exception as e:  # noqa: BLE001
            failures.append(e)
            continue
        for j in postings:
            path = j.get("externalPath", "")
            if path in seen:
                continue
            seen.add(path)
            posted_on = (j.get("postedOn") or "").lower()   # "Posted Today", "Posted Yesterday", "Posted 3 Days Ago"
            now = datetime.now(timezone.utc)
            if "today" in posted_on:
                posted = now
            elif "yesterday" in posted_on:
                posted = now - timedelta(days=1)
            else:
                m = re.search(r"(\d+)", posted_on)
                posted = now - timedelta(days=int(m.group(1))) if m else None
            out.append(Job(
                company=c["name"], title=j.get("title", ""), location=j.get("locationsText", ""),
                url=f"{base}/{c['site']}{path}", uid=f"wd:{c['tenant']}:{path}", posted=posted,
                remote_hint="remote" in (j.get("locationsText", "") + j.get("remoteType", "")).lower(),
            ))
    if failures and len(failures) == len(terms):
        raise failures[0]
    if failures:
        print(f"  ! {c['name']} (workday): {len(failures)}/{len(terms)} searches failed: {failures[0]}",
              file=sys.stderr)
    return out


def fetch_oracle(c) -> list[Job]:
    """Oracle Recruiting Cloud (Candidate Experience). Config: host, site, job_url (optional)."""
    host, site = c["host"], c["site"]
    url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
           f"?onlyData=true&expand=requisitionList.secondaryLocations"
           f"&finder=findReqs;siteNumber={site},limit=100,sortBy=POSTING_DATES_DESC")
    data = get(url)
    items = data.get("items") or []
    reqs = (items[0].get("requisitionList") if items else None) or []
    job_base = c.get("job_url", f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/")
    out = []
    for j in reqs:
        locs = [j.get("PrimaryLocation", "")] + [s.get("Name", "") for s in j.get("secondaryLocations") or []]
        wt = (j.get("WorkplaceType") or "") + " " + (j.get("WorkplaceTypeCode") or "")
        out.append(Job(
            company=c["name"], title=j.get("Title", ""), location=" / ".join(filter(None, locs)),
            url=f"{job_base}{j.get('Id')}", uid=f"or:{host}:{site}:{j.get('Id')}",
            posted=parse_dt(j.get("PostedDate")),
            text=strip_html(j.get("ShortDescriptionStr", "")),
            remote_hint="remote" in wt.lower() or "remote" in (j.get("PrimaryLocation") or "").lower(),
        ))
    return out


def oracle_detail(job: Job, c) -> None:
    try:
        jid = job.uid.rsplit(":", 1)[1]
        d = get(f"https://{c['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                f"?expand=all&onlyData=true&finder=ById;Id=%22{jid}%22,siteNumber={c['site']}")
        it = (d.get("items") or [{}])[0]
        job.text = strip_html(" ".join(str(it.get(k) or "") for k in
                                       ("ExternalDescriptionStr", "ExternalQualificationsStr",
                                        "ExternalResponsibilitiesStr", "CorporateDescriptionStr")))
    except Exception as e:  # noqa: BLE001
        print(f"  ! oracle detail failed for {job.url}: {e}", file=sys.stderr)


# Set by main(): config + already-seen ids (so HTML scrapers skip detail pages they already saw).
CFG: dict = {}
SEEN: dict = {}


def title_passes(title: str) -> bool:
    t = title.lower()
    return (any(re.search(p, t) for p in CFG.get("title_include", []))
            and not any(re.search(p, t) for p in CFG.get("title_exclude", [])))


def fetch_talentbrew(c) -> list[Job]:
    """Radancy/TalentBrew career sites (e.g. kaiserpermanentejobs.org).
    Config: base, list_path (a category page), max_pages."""
    base = c["base"].rstrip("/")
    out, seen_links = [], set()
    for page in range(1, c.get("max_pages", 10) + 1):
        url = f"{base}{c['list_path']}" + (f"/{page}" if page > 1 else "")
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        links = re.findall(r'<a[^>]+href="(/job/[^"]+?/(\d{6,}))"[^>]*>(.*?)</a>', r.text, re.S)
        new = [l for l in links if l[0] not in seen_links]
        if not new:
            break
        for path, jid, inner in new:
            seen_links.add(path)
            title = strip_html(re.sub(r"<span[^>]*job-location[^>]*>.*?</span>", " ", inner, flags=re.S))
            uid = f"tb:{base}:{jid}"
            if uid in SEEN or not title_passes(title):
                out.append(Job(c["name"], title, "", base + path, uid, None))
                continue
            time.sleep(1)  # be polite
            try:
                d = requests.get(base + path, headers=UA, timeout=TIMEOUT).text
            except Exception as e:  # noqa: BLE001
                print(f"  ! talentbrew detail failed {path}: {e}", file=sys.stderr)
                continue
            txt = strip_html(re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", d, flags=re.S | re.I))
            m = re.search(r"Date Posted\s*:?\s*(\d{1,2}/\d{1,2}/\d{4})", txt)
            posted = (datetime.strptime(m.group(1), "%m/%d/%Y").replace(tzinfo=timezone.utc)
                      if m else None)
            wl = re.search(r"Worker Location\s*:?\s*(\w+)", txt)
            loc = re.search(r"Primary Location\s*:?\s*(.{0,80}?)(?:Additional Locations|Worker Location|Date Posted|Scheduled|$)", txt)
            city = path.split("/")[2].replace("-", " ")
            location = (loc.group(1).strip() if loc else city.title())
            remote = bool(wl and wl.group(1).lower() == "remote")
            if remote:
                location += " (Remote)"
            sal = re.search(r"\$\s?[\d,]{5,}(?:\.\d+)?\s*-\s*\$?\s?[\d,]{5,}(?:\.\d+)?\s*/\s*(?:year|hour)", txt)
            out.append(Job(c["name"], title, location, base + path, uid, posted, txt[:20000],
                           remote_hint=remote, salary=sal.group(0) if sal else ""))
        time.sleep(1)
    return out


def workday_detail(job: Job, c) -> None:
    """Fetch description for a Workday job that survived title/location filters."""
    try:
        path = job.url.split(f"/{c['site']}", 1)[1]
        d = get(f"https://{c['tenant']}.{c['wd']}.myworkdayjobs.com/wday/cxs/{c['tenant']}/{c['site']}{path}")
        info = d.get("jobPostingInfo", {})
        job.text = strip_html(info.get("jobDescription", ""))
        job.location = info.get("location", job.location) + " " + " ".join(info.get("additionalLocations", []) or [])
    except Exception as e:  # noqa: BLE001
        print(f"  ! workday detail failed for {job.url}: {e}", file=sys.stderr)


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
            "workday": fetch_workday, "oracle": fetch_oracle, "talentbrew": fetch_talentbrew}


# ---------------------------------------------------------------- filters
YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*(\d{1,2})\s*)?\+?\s*years?", re.I)
US_RE = re.compile(r"\b(us|u\.s\.|usa|united states|americas|north america|anywhere)\b", re.I)
NON_US_RE = re.compile(r"\b(canada|uk|united kingdom|london|emea|europe|eu|india|apac|singapore|"
                       r"australia|germany|ireland|poland|brazil|mexico|japan|israel|netherlands)\b", re.I)
STATE_LOCK_RE = re.compile(r"remote\s*[-–(,]\s*(?!ca\b|california)([a-z .]+?)(?:\)|$| only)", re.I)


def required_years(text: str) -> int | None:
    """Smallest 'N+ years' minimum found in the text (best guess at the core requirement)."""
    mins = []
    for m in YEARS_RE.finditer(text):
        n = int(m.group(1))
        if 0 < n <= 15:
            mins.append(n)
    return min(mins) if mins else None


def location_ok(job: Job, cfg) -> tuple[bool, str]:
    loc = job.location.lower()
    if any(p in loc for p in cfg["local_places"]):
        return True, "local"
    is_remote = job.remote_hint or "remote" in loc or "anywhere" in loc
    if not is_remote:
        return False, ""
    if NON_US_RE.search(loc) and not US_RE.search(loc):
        return False, ""
    # "Remote - Texas" style single-state lock (not CA) -> fail
    m = STATE_LOCK_RE.search(job.location)
    if m and not US_RE.search(m.group(1)) and len(m.group(1).strip()) > 2 and "california" not in loc:
        return False, ""
    if re.search(r"\b(ca|california)\b", loc) or US_RE.search(loc):
        return True, "remote-US"
    return True, "remote (country unclear)"


def evaluate(job: Job, cfg) -> bool:
    t = job.title.lower()
    if not any(re.search(p, t) for p in cfg["title_include"]):
        return False
    if any(re.search(p, t) for p in cfg["title_exclude"]):
        return False
    comp = job.company.lower()
    if any(s in comp for s in cfg["skip_companies"]):
        return False
    if any(s[0] in comp and s[1] in t for s in cfg.get("skip_roles", [])):
        return False
    ok, kind = location_ok(job, cfg)
    if not ok:
        return False
    if kind == "remote (country unclear)":
        job.flags.append("remote—check US/CA eligibility")
    return True


def score_text(job: Job) -> bool:
    txt = job.text.lower()
    job.years = required_years(job.text)
    if job.years is not None and job.years >= 5:
        return False
    if re.search(r"\b(clearance|ts/sci|secret clearance)\b", txt):
        job.flags.append("clearance")
    if "kubernetes" in txt or "k8s" in txt:
        # crude: required if it appears before any 'preferred/nice to have' heading
        pref = re.search(r"(preferred|nice to have|bonus|plus)", txt)
        k = txt.find("kubernetes") if "kubernetes" in txt else txt.find("k8s")
        job.flags.append("K8s required?" if (not pref or k < pref.start()) else "K8s preferred")
    if re.search(r"\b(contract|contractor|w2|c2c|1099)\b", txt + " " + job.title.lower()):
        job.flags.append("contract?")
    if not job.salary:
        m = re.search(r"\$\s?\d{2,3}(?:,\d{3}|k)(?:\s*(?:-|–|to)\s*\$?\s?\d{2,3}(?:,\d{3}|k))?", job.text, re.I)
        job.salary = m.group(0) if m else ""
    if job.years is None:
        job.verdict = "MAYBE (years not stated)"
    elif job.years >= 3:
        job.verdict = "GO (warm channel only)"
    else:
        job.verdict = "GO"
    return True


# ---------------------------------------------------------------- notify
def job_lines(job: Job) -> list[str]:
    age = ""
    if job.posted:
        mins = int((datetime.now(timezone.utc) - job.posted).total_seconds() // 60)
        age = f"{mins // 60}h{mins % 60:02d}m ago" if mins < 48 * 60 else job.posted.date().isoformat()
    lines = [
        f"{job.company} · {job.location}",
        f"{job.verdict} · yrs: {job.years if job.years is not None else '?'}"
        + (f" · {job.salary}" if job.salary else ""),
        f"posted {age}" if age else "",
        ("flags: " + ", ".join(job.flags)) if job.flags else "",
    ]
    return [l for l in lines if l]


def send_email(jobs: list[Job]) -> None:
    """One digest email per run. Env: SMTP_USER, SMTP_PASS (Gmail app password), EMAIL_TO (optional)."""
    import smtplib
    from email.message import EmailMessage
    user, pw = os.environ["SMTP_USER"], os.environ["SMTP_PASS"]
    to = os.environ.get("EMAIL_TO") or user
    go = sum(j.verdict == "GO" for j in jobs)
    msg = EmailMessage()
    msg["Subject"] = (f"jobwatch: {len(jobs)} new match{'es' if len(jobs) != 1 else ''}"
                      + (f" ({go} GO)" if go else "") + f" — {jobs[0].title} @ {jobs[0].company}")
    msg["From"], msg["To"] = user, to
    msg.set_content("\n\n".join(
        f"{j.title}\n" + "\n".join(job_lines(j)) + f"\n{j.url}" for j in jobs))
    esc = html.escape
    rows = "".join(
        f'<div style="margin:0 0 18px"><a href="{esc(j.url)}" style="font-size:16px;font-weight:600">'
        f'{esc(j.title)}</a><br>' + "<br>".join(esc(l) for l in job_lines(j)) + "</div>" for j in jobs)
    msg.add_alternative(f'<div style="font-family:-apple-system,Arial,sans-serif;font-size:14px">{rows}</div>',
                        subtype="html")
    with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), 465, timeout=TIMEOUT) as s:
        s.login(user, pw)
        s.send_message(msg)


def notify_ntfy(job: Job) -> None:
    topic = os.environ["NTFY_TOPIC"]
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    body = "\n".join(job_lines(job))
    requests.post(f"{server}/{topic}", data=body.encode(), timeout=TIMEOUT, headers={
        "Title": f"{job.title} — {job.company}".encode("utf-8").decode("latin-1", "ignore"),
        "Click": job.url, "Tags": "briefcase",
        "Priority": "high" if job.verdict == "GO" else "default",
    })


def save_matches(new: list[Job]) -> None:
    """Append new matches to docs/matches.json (newest first), dropping ones older than KEEP_DAYS."""
    now = datetime.now(timezone.utc)
    data = json.loads(MATCHES.read_text()) if MATCHES.exists() else {"jobs": []}
    known = {j["id"] for j in data["jobs"]}
    added = [{
        "id": j.uid, "title": j.title, "company": j.company, "location": j.location.strip(),
        "url": j.url, "verdict": j.verdict, "years": j.years, "salary": j.salary,
        "flags": j.flags, "remote": bool(j.remote_hint or "remote" in j.location.lower()),
        "posted": j.posted.isoformat() if j.posted else None, "found": now.isoformat(),
    } for j in new if j.uid not in known]
    cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat()
    jobs = added + [j for j in data["jobs"] if j["found"] >= cutoff]
    if added or len(jobs) != len(data["jobs"]) or not MATCHES.exists():
        MATCHES.parent.mkdir(exist_ok=True)
        MATCHES.write_text(json.dumps({"updated": now.isoformat(), "jobs": jobs}, indent=1))


# ---------------------------------------------------------------- main
def main() -> int:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    first_run = not state
    CFG.clear(); CFG.update(cfg)
    SEEN.clear(); SEEN.update(state)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg.get("lookback_hours", 24))
    now_iso = datetime.now(timezone.utc).isoformat()

    def run(c):
        try:
            return c, FETCHERS[c["ats"]](c), None
        except Exception as e:  # noqa: BLE001
            return c, [], e

    # Fast JSON boards in parallel; Workday one company at a time (it rate-limits bursts).
    fast = [c for c in cfg["companies"] if c["ats"] != "workday"]
    slow = [c for c in cfg["companies"] if c["ats"] == "workday"]
    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(run, fast))
    results += [run(c) for c in slow]

    matches, errors, total = [], [], 0
    for c, jobs, err in results:
        if err:
            errors.append(f"{c['name']} ({c['ats']}): {err}")
            continue
        total += len(jobs)
        for j in jobs:
            if j.uid in state:
                continue
            state[j.uid] = now_iso
            if j.posted and j.posted < cutoff:
                continue
            if not evaluate(j, cfg):
                continue
            if c["ats"] == "workday":
                workday_detail(j, c)
            elif c["ats"] == "oracle":
                oracle_detail(j, c)
            if score_text(j):
                matches.append(j)

    # On the very first run, record everything but only alert on jobs inside the lookback window.
    order = {"GO": 0, "GO (warm channel only)": 1}
    matches.sort(key=lambda j: (order.get(j.verdict, 2), -(j.posted or cutoff).timestamp()))
    print(f"{datetime.now():%Y-%m-%d %H:%M} scanned {total} jobs from "
          f"{len(results) - len(errors)} boards; {len(matches)} new matches"
          + (" (first run)" if first_run else ""))
    for e in errors:
        print("  ! " + e, file=sys.stderr)
    for j in matches:
        print(f"\n[{j.title}]\n" + "\n".join(job_lines(j)) + f"\n{j.url}")
    if matches and not os.environ.get("DRY_RUN"):
        if os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS"):
            try:
                send_email(matches)
                print("emailed digest")
            except Exception as e:  # noqa: BLE001
                print(f"  ! email failed: {e}", file=sys.stderr)
        if os.environ.get("NTFY_TOPIC"):
            for j in matches[:15]:
                notify_ntfy(j)
                time.sleep(0.5)

    save_matches(matches)

    # prune state older than 60 days
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    state = {k: v for k, v in state.items() if v >= old}
    STATE.write_text(json.dumps(state, indent=0, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
