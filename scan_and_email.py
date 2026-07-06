"""
The Long News — daily scan and email.

Searches today's news via the Anthropic API (with web search enabled),
keeps only the stories that might matter in a decade, a century, or a
millennium, emails the edition, and publishes it to the web.

Required environment variables:
  ANTHROPIC_API_KEY    — from the Claude Console (platform.claude.com)
  GMAIL_APP_PASSWORD   — a Gmail "app password" (requires 2-step verification)
  EMAIL_FROM           — the Gmail address sending the edition
  EMAIL_TO             — where the edition should arrive (can equal EMAIL_FROM)
"""

import json
import os
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

# ————————————————————————————————————————————————
# The filter
# ————————————————————————————————————————————————

FILTER_PROMPT = """You are the research assistant for The Long News, the Long Now Foundation project edited by Kirk Citron. Its filter, from his 2010 TED talk "And now, the real news": in the long run, some news stories are more important than others. Almost all of today's headlines — politics-of-the-day, markets, sports, celebrity, crime — will not matter in a hundred years. A few will.

Search the web for today's and this week's news (use 2 to 4 searches). Look especially in the categories the Long News has always tracked. Three from science and technology: fundamental discoveries about life, matter, and the universe; machine intelligence, robotics, and space; biology, medicine, longevity, and disease. Three from the human world: the shape of power — geopolitics, demographics, migration, and how states rise and fall; the fate of the planet and its living systems; and belief and ideas — religion, ideology, law, rights, and lasting shifts in how people think and organize.

Do not be limited to these categories. The biggest miss is always the story nobody filed under "important" — a development that fits no obvious bucket. Weigh the human world as seriously as the technological: a treaty, a demographic tipping point, or a shift in what a billion people believe can be long news as surely as a discovery.

Select at most 6 stories. For each, assign the LONGEST horizon it plausibly clears:
- "decade": will still be discussed in 10 years
- "century": will still shape lives in 100 years
- "millennium": a historian in 1,000 years might cite it

Be a skeptical editor. Most days produce zero millennium stories. Prefer primary developments (a result, a launch, a treaty, a first) over commentary about them. Always link to the specific article, never a section front or homepage that will change within hours.

Distinguish a headline from the force beneath it. A monthly statistic, a single report, or a scoreboard number is short news even when the underlying trend is long news — assign the horizon to the durable shift, not to today's figure, and never stretch to a longer horizon on the strength of an "if."

Respond with ONLY a JSON object, no markdown fences, no preamble:
{"stories":[{"headline":"...","source":"...","date":"...","url":"...","summary":"one sentence, max 25 words","horizon":"decade|century|millennium","why":"the long view - why it clears this horizon, max 30 words"}]}"""

# Appended to the prompt only when recent editions exist, so the machine
# doesn't republish a story it already ran unless it has genuinely advanced.
NO_REPEATS_TEMPLATE = """

You have published these stories in the last {days} days. Do NOT select any of them again unless there is a genuinely new, material development since it last ran (a new result, a reversal, a decision) — and if you do run it again, say what changed in the "why" field. Otherwise find different stories.

Already published:
{headlines}"""

HORIZONS = [
    ("decade", "A Decade", 10, "#D9A441"),
    ("century", "A Century", 100, "#B08D57"),
    ("millennium", "A Millennium", 1000, "#6FA08B"),
]


def long_date(d: date) -> str:
    """Long Now five-digit year style: 06 July 02026."""
    return f"{d.day:02d} {d.strftime('%B')} 0{d.year}"


def recent_headlines(days: int = 7) -> str:
    """Pull headlines from the last `days` editions already saved in docs/.

    Scrapes the <h3> story titles from the saved HTML. Returns a bullet list,
    or "" if there are no recent editions yet (e.g. the very first run).
    """
    import glob
    import re

    if not os.path.isdir(SITE_DIR):
        return ""
    files = sorted(
        (f for f in glob.glob(os.path.join(SITE_DIR, "*.html")) if os.path.basename(f)[0].isdigit()),
        reverse=True,
    )[:days]
    heads: list[str] = []
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                html = fh.read()
        except OSError:
            continue
        for match in re.findall(r"<h3>(?:<a[^>]*>)?(.*?)(?:</a>)?</h3>", html):
            clean = re.sub(r"<[^>]+>", "", match).strip()
            if clean and clean != "The record":
                heads.append(clean)
    seen, unique = set(), []
    for h in heads:
        if h not in seen:
            seen.add(h)
            unique.append(h)
    return "\n".join(f"- {h}" for h in unique)


def run_scan(recent: str = "") -> list[dict]:
    prompt = FILTER_PROMPT
    if recent:
        prompt += NO_REPEATS_TEMPLATE.format(days=7, headlines=recent)
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 2000,
            "messages": [
                {
                    "role": "user",
                    "content": f"Today is {date.today().strftime('%A, %d %B %Y')}. "
                    + prompt,
                }
            ],
            "tools": [
                {"type": "web_search_20250305", "name":
