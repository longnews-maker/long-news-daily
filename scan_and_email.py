"""
The Long News — scan and (occasionally) publish.

Surveys the past week's news via the Anthropic API (with web search enabled),
keeps only the few stories that might matter in a decade, a century, or a
millennium, and — only when something clears that bar — emails the edition
and publishes it to the web. Most runs publish nothing, by design. Every run
also emails the editor the near-misses (the "rejects") so the machine's
judgment can be audited; this is a tuning aid, meant to be removed later.

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

Search the web for the most significant news of the past week (use 2 to 4 searches). Look especially in the categories the Long News has always tracked. Three from science and technology: fundamental discoveries about life, matter, and the universe; machine intelligence, robotics, and space; biology, medicine, longevity, and disease. Three from the human world: the shape of power — geopolitics, demographics, migration, and how states rise and fall; the fate of the planet and its living systems; and belief and ideas — religion, ideology, law, rights, and lasting shifts in how people think and organize.

Do not be limited to these categories. The biggest miss is always the story nobody filed under "important" — a development that fits no obvious bucket. Weigh the human world as seriously as the technological: a treaty, a demographic tipping point, or a shift in what a billion people believe can be long news as surely as a discovery.

Select AT MOST 3 stories — but the expected result, most weeks, is ZERO. Silence is the normal, correct outcome and needs no justification. Most weeks nothing that happened will still matter in a decade; when that is true, return an empty list and publish nothing. Do NOT select a story to avoid an empty week. A month of silence followed by a single story that genuinely matters is the goal, not a failure.

Apply one test to every candidate, and be honest with yourself: will THIS SPECIFIC development be cited, taught, or felt ten years from now? Not "is it important this week" — almost everything that feels important this week fails this test. A crisis, a claim, a record, a milestone can dominate the news and still leave no trace in ten years. If you cannot make the ten-year case without leaning on an "if," a "could," or a hopeful projection, the honest answer is no. Before selecting anything, ask yourself plainly: am I choosing this because it truly clears the bar, or because I feel I should publish something? If it is the latter, select nothing.

For each story that survives that test, assign the LONGEST horizon it plausibly clears:
- "decade": will still be discussed in 10 years
- "century": will still shape lives in 100 years
- "millennium": a historian in 1,000 years might cite it

Each horizon must be earned on its own evidence. NEVER promote a story to a longer horizon to make a tier look populated — an empty tier, and an empty edition, are honest and expected.

Be a skeptical editor. Prefer primary developments (a result, a launch, a treaty, a first) over commentary about them. Always link to the specific article, never a section front or homepage that will change within hours. Never select two stories about the same underlying development.

Distinguish a headline from the force beneath it. A monthly statistic, a single report, or a scoreboard number is short news even when the underlying trend is long news — assign the horizon to the durable shift, not to today's figure, and never stretch to a longer horizon on the strength of an "if," a "could," or a "may."

Beware the breaking-news event. A death, a summit, an attack, an appointment, a single test, a crisis — however much coverage it commands this week — is almost never long news. Heavy coverage is evidence AGAINST selection, not for it: it means the story is loud today, which says nothing about whether it matters in a century. Do not let a week's dominant news cycle capture the edition. And beware the disguised event: a breaking story that asserts its own long-term significance ("first ever," "rewires everything," "for a generation") is still a breaking story. An asserted durable shift is not an actual one. If the significance depends on the event "holding" or being confirmed later, it has not happened yet — leave it out and wait. If it is genuinely long news, it will still be long news next month, confirmed.

Separately, ALWAYS return "rejects": the five or six strongest stories you seriously considered this week but did NOT select — the near-misses. For each, give a one-line reason it failed the test (short news, an unconfirmed claim, a breaking event, a hedge on "if"). This list matters most in a week where you select nothing: it is how the editor audits your silence. Populate it on every run, especially when "stories" is empty. Order rejects strongest first.

Respond with ONLY a JSON object, no markdown fences, no preamble:
{"stories":[{"headline":"...","source":"...","date":"...","url":"...","summary":"one sentence, max 25 words","horizon":"decade|century|millennium","why":"the long view - why it clears this horizon, max 30 words"}],"rejects":[{"headline":"...","source":"...","url":"...","reason":"why it did not clear the ten-year bar, max 25 words"}]}"""

# Appended to the prompt only when recent editions exist, so the machine
# doesn't republish a story it already ran unless it has genuinely advanced.
NO_REPEATS_TEMPLATE = """

You have published these stories in recent editions. Do NOT select any of them again unless there is a genuinely new, material development since it last ran (a new result, a reversal, a decision) — and if you do run it again, say what changed in the "why" field. Otherwise find different stories.

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


def recent_headlines(count: int = 10) -> str:
    """Pull headlines from the last `count` editions already saved in docs/.

    Scrapes the <h3> story titles from the saved HTML. Returns a bullet list,
    or "" if there are no editions yet (e.g. the very first run). Because
    editions are now occasional, this reads the last N editions whenever they
    were published, not a fixed time window.
    """
    import glob
    import re

    if not os.path.isdir(SITE_DIR):
        return ""
    files = sorted(
        (f for f in glob.glob(os.path.join(SITE_DIR, "*.html")) if os.path.basename(f)[0].isdigit()),
        reverse=True,
    )[:count]
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


def run_scan(recent: str = "") -> tuple[list[dict], list[dict]]:
    prompt = FILTER_PROMPT
    if recent:
        prompt += NO_REPEATS_TEMPLATE.format(headlines=recent)
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
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 4}
            ],
        },
        timeout=300,
    )
    response.raise_for_status()
    data = response.json()

    text = "\n".join(
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    )
    clean = text.replace("```json", "").replace("```", "").strip()
    start, end = clean.find("{"), clean.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("The scan returned no readable result.")
    parsed = json.loads(clean[start : end + 1])
    return parsed.get("stories", []), parsed.get("rejects", [])


def render_html(stories: list[dict], today: date) -> str:
    sections = []
    for horizon_id, label, years, color in HORIZONS:
        matches = [s for s in stories if s.get("horizon") == horizon_id]
        if not matches:
            continue  # occasional editions show only the strata that hit
        items = []
        for s in matches:
            headline = s.get("headline", "Untitled")
            url = s.get("url")
            head_html = (
                f'<a href="{url}" style="color:#1C2228;text-decoration:none;'
                f'border-bottom:1px solid {color};">{headline}</a>'
                if url
                else headline
            )
            meta = " &middot; ".join(x for x in [s.get("source"), s.get("date")] if x)
            items.append(
                f"""
                <div style="margin:18px 0 0;">
                  <div style="font-size:19px;font-weight:600;line-height:1.3;">{head_html}</div>
                  <div style="font-size:12px;color:#7A828A;margin-top:4px;">{meta}</div>
                  <div style="font-size:15px;line-height:1.5;margin-top:6px;color:#333A40;">{s.get('summary', '')}</div>
                  <div style="font-size:14px;line-height:1.5;margin-top:6px;color:{color};">
                    <strong>The long view —</strong> {s.get('why', '')}</div>
                </div>"""
            )
        sections.append(
            f"""
            <div style="border-left:3px solid {color};padding:4px 0 8px 18px;margin:26px 0;">
              <div style="font-size:11px;letter-spacing:2px;text-transform:uppercase;color:{color};">
                Will matter in 0{today.year + years}</div>
              <div style="font-size:22px;font-weight:700;font-family:Georgia,serif;">{label}</div>
              {''.join(items)}
            </div>"""
        )

    return f"""
    <div style="background:#F7F5F0;padding:32px 16px;">
      <div style="max-width:640px;margin:0 auto;font-family:Georgia,'Times New Roman',serif;color:#1C2228;">
        <div style="font-size:11px;letter-spacing:3px;text-transform:uppercase;color:#7A828A;">
          The Long News</div>
        <h1 style="font-size:32px;margin:10px 0 4px;font-weight:700;">
          And now, <em style="color:#B08D57;">the real news.</em></h1>
        <div style="font-size:13px;color:#7A828A;letter-spacing:1px;">{long_date(today)}</div>
        {''.join(sections)}
        <div style="border-top:1px solid
