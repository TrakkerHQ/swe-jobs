#!/usr/bin/env python3
"""Pull Trakker's SWE postings and regenerate the README tables.

Runs once a day from .github/workflows/refresh.yml. Each run:

1. fetches every SWE intern/new-grad posting Trakker considers new in the
   last 14 days (the API's maximum window, filtered server-side, so the
   response is small),
2. merges them into data/jobs.json, keyed by Trakker's job id, so a posting
   stays listed after it leaves the fetch window,
3. drops a recent posting the API no longer returns (it closed or was
   reclassified), and anything older than MAX_AGE_DAYS,
4. rewrites the tables between the <!-- TABLE_*_START/END --> markers in
   README.md and leaves everything else in the file alone.

Standard library only, so the workflow needs no install step.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "jobs.json"
README_PATH = ROOT / "README.md"

BASE_URL = os.environ.get("TRAKKER_API_URL", "https://api.trakkerhq.com")
TIMEOUT_SECONDS = 60

# The server's cap (MAX_SKILL_WINDOW_HOURS in tracker/api/main.py). Fetching
# the whole window every day, rather than just the last 24h, is what lets a
# missed run heal itself and what makes closure detection possible at all.
FETCH_WINDOW_HOURS = 24 * 14
# Only a posting comfortably inside the fetch window can be judged "gone" by
# its absence. One near the edge may be missing only because it aged out.
RECONCILE_MAX_AGE_DAYS = 12
# How long a posting stays listed. Past the fetch window nothing can confirm
# it is still open, so this is a trade between a long list and a stale one.
MAX_AGE_DAYS = 30

# Fields published to the repository. The API row carries more (prestige,
# classification internals); none of it leaves this script.
PUBLIC_FIELDS = ("id", "company_display", "title", "location", "url",
                 "skill_level", "country", "updated_at", "date_discovered")

# The app's primary button (primary-700 pill, DM Sans Bold, arrow-up-right),
# with the text outlined to paths so it renders the same without the font.
APPLY_BUTTON = "assets/apply.svg"

# Trakker role categories this repository lists.
CATEGORIES = ['SWE']

SECTIONS = {
    # marker name: (country group, skill_level)
    "USA_INTERN": ("usa", "intern"),
    "USA_NEWGRAD": ("usa", "entry"),
    "INTL_INTERN": ("intl", "intern"),
    "INTL_NEWGRAD": ("intl", "entry"),
}


def fetch() -> list[dict]:
    token = os.environ.get("TRAKKER_API_KEY")
    if not token:
        sys.exit("TRAKKER_API_KEY is not set (add it under Settings -> Secrets -> Actions).")
    params = {
        "since_hours": FETCH_WINDOW_HOURS,
        "categories": CATEGORIES,
        # Explicit, so a row the classifier has not finished is never published.
        "levels": ["intern", "entry"],
    }
    url = f"{BASE_URL.rstrip('/')}/skill/jobs?" + urllib.parse.urlencode(params, doseq=True)
    # Cloudflare in front of the API answers 403 to urllib's default
    # "Python-urllib/3.x" agent before the request ever reaches Trakker.
    request = urllib.request.Request(url, headers={
        "X-Trakker-Api-Key": token,
        "User-Agent": "trakker-swe-jobs/1.0 (+https://github.com/TrakkerHQ/swe-jobs)",
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read())["jobs"]
    except urllib.error.HTTPError as err:
        if err.code == 401:
            sys.exit("Trakker rejected the token. It was probably regenerated; update the TRAKKER_API_KEY secret.")
        sys.exit(f"Trakker returned HTTP {err.code}: {err.reason}")
    except urllib.error.URLError as err:
        sys.exit(f"Could not reach Trakker: {err.reason}")


def posted_at(job: dict) -> datetime | None:
    # The source's own posted date when the connector has one, else when
    # Trakker first saw it. Same precedence the board uses.
    for key in ("updated_at", "date_discovered"):
        value = job.get(key)
        if value:
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                continue
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def age_days(job: dict, now: datetime) -> int | None:
    when = posted_at(job)
    return None if when is None else max(0, (now - when).days)


def load_stored() -> dict[int, dict]:
    if not DATA_PATH.exists():
        return {}
    return {row["id"]: row for row in json.loads(DATA_PATH.read_text())}


def merge(stored: dict[int, dict], fetched: list[dict], now: datetime) -> dict[int, dict]:
    fetched_ids = {job["id"] for job in fetched}
    merged = {}
    for job_id, row in stored.items():
        age = age_days(row, now)
        if age is None or age > MAX_AGE_DAYS:
            continue
        if age <= RECONCILE_MAX_AGE_DAYS and job_id not in fetched_ids:
            continue
        merged[job_id] = row
    for job in fetched:
        # Only http(s) links reach a public table.
        if not str(job.get("url") or "").startswith(("https://", "http://")):
            continue
        if not job.get("company_display") or not job.get("title"):
            continue
        merged[job["id"]] = {key: job.get(key) for key in PUBLIC_FIELDS}
    return merged


def guard(stored: dict[int, dict], fetched: list[dict], now: datetime) -> None:
    """Refuse to publish when the fetch looks like an outage, not a quiet day.

    An expired token, a scraper failure or an API incident all look like "every
    recent posting closed". A stale list beats an empty one, so the run fails,
    the README keeps yesterday's content, and GitHub emails the repo owner.
    """
    recent = [row for row in stored.values()
              if (age := age_days(row, now)) is not None and age <= RECONCILE_MAX_AGE_DAYS]
    if len(recent) >= 10 and len(fetched) < 0.4 * len(recent):
        sys.exit(
            f"Fetched {len(fetched)} postings but {len(recent)} recent ones are stored. "
            "Refusing to publish; check the API before re-running."
        )


def cell(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("|", "\\|")).strip() or "—"


def render_table(rows: list[dict], now: datetime) -> str:
    lines = ["| Company | Position | Location | Posting | Age |",
             "|---|---|---|---|---|"]
    for row in rows:
        href = row["url"].replace('"', "%22")
        lines.append(
            f"| **{cell(row['company_display'])}** | {cell(row['title'])} | {cell(row['location'])} "
            f"| <a href=\"{href}\"><img src=\"{APPLY_BUTTON}\" alt=\"Apply\" height=\"36\"/></a> "
            f"| {age_days(row, now) or 0}d |"
        )
    return "\n".join(lines)


def country_group(row: dict) -> str:
    return "intl" if row.get("country") == "international" else "usa"


def render_readme(merged: dict[int, dict], now: datetime) -> None:
    readme = README_PATH.read_text()
    rows = sorted(merged.values(),
                  key=lambda r: (age_days(r, now) or 0, (r["company_display"] or "").lower()))
    for name, (group, level) in SECTIONS.items():
        section = [r for r in rows if country_group(r) == group and r.get("skill_level") == level]
        pattern = re.compile(rf"(<!-- TABLE_{name}_START -->).*?(<!-- TABLE_{name}_END -->)", re.S)
        if not pattern.search(readme):
            sys.exit(f"README.md is missing the TABLE_{name}_START/END markers.")
        readme = pattern.sub(lambda m: f"{m.group(1)}\n{render_table(section, now)}\n{m.group(2)}", readme)
        readme = re.sub(rf"(<!-- COUNT_{name} -->)\d*", rf"\g<1>{len(section)}", readme)
    readme = re.sub(r"(<!-- UPDATED -->).*", rf"\g<1>{now:%Y-%m-%d}", readme)
    README_PATH.write_text(readme)


def main() -> None:
    now = datetime.now(timezone.utc)
    stored = load_stored()
    fetched = fetch()
    guard(stored, fetched, now)
    merged = merge(stored, fetched, now)
    DATA_PATH.parent.mkdir(exist_ok=True)
    DATA_PATH.write_text(json.dumps(sorted(merged.values(), key=lambda r: r["id"]), indent=2) + "\n")
    render_readme(merged, now)
    new_today = sum(1 for job_id in merged if job_id not in stored)
    print(f"{len(merged)} listed, {new_today} new since the last run.")


if __name__ == "__main__":
    main()
