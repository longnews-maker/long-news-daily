"""
The Long News — daily scan and email.

Searches today's news via the Anthropic API (with web search enabled),
keeps only the stories that might matter in a decade, a century, or a
millennium, and emails the edition.

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

Search the web for today's and this week's news (use 2 to 4 searches). Look especially in the categories the Long News has always tracked: space exploration and settlement; machine intelligence and robot science; biotech, nanomedicine, and longevity; feeding the world, water, energy, and climate shifts; demographic and geopolitical realignment; and fundamental discoveries about life and the universe. But do not be limited to these — the biggest miss is always the story nobody filed under "important."

Select at most 6 stories. For each, assign the LONGEST horizon it plausibly clears:
- "decade": will still be discussed in 10 years
- "century": will still shape lives in 100 years
- "millennium": a historian in 1,000 years might cite it

Be a skeptical editor. Most days produce zero millennium stories. Prefer primary developments (a result, a launch, a treaty, a first) over commentary about them.

Respond with ONLY a JSON object, no markdown fences, no preamble:
{"stories":[{"headline":"...","source":"...","date":"...","url":"...","summary":"one sentence, max 25 words","horizon":"decade|century|millennium","why":"the long view - why it clears this horizon, max 30 words"}]}"""

HORIZONS = [
    ("decade", "A Decade", 10, "#D9A441"),
    ("century", "A Century", 100, "#B08D57"),
    ("millennium", "A Millennium", 1000, "#6FA08B"),
]


def long_date(d: date) -> str:
    """Long Now five-digit year style: 06 July 02026."""
    return f"{d.day:02d} {d.strftime('%B')} 0{d.year}"


def run_scan() -> list[dict]:
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
                    + FILTER_PROMPT,
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
    return json.loads(clean[start : end + 1]).get("stories", [])


def render_html(stories: list[dict], today: date) -> str:
    sections = []
    for horizon_id, label, years, color in HORIZONS:
        matches = [s for s in stories if s.get("horizon") == horizon_id]
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
        body = (
            "".join(items)
            if items
            else f'<div style="font-size:14px;color:#9AA1A7;font-style:italic;margin-top:10px;">No {horizon_id}-scale stories today.</div>'
        )
        sections.append(
            f"""
            <div style="border-left:3px solid {color};padding:4px 0 8px 18px;margin:26px 0;">
              <div style="font-size:11px;letter-spacing:2px;text-transform:uppercase;color:{color};">
                Will matter in 0{today.year + years}</div>
              <div style="font-size:22px;font-weight:700;font-family:Georgia,serif;">{label}</div>
              {body}
            </div>"""
        )

    empty_note = (
        '<p style="font-style:italic;color:#7A828A;">Nothing cleared the filter today. That is a finding, not a failure.</p>'
        if not stories
        else ""
    )

    return f"""
    <div style="background:#F7F5F0;padding:32px 16px;">
      <div style="max-width:640px;margin:0 auto;font-family:Georgia,'Times New Roman',serif;color:#1C2228;">
        <div style="font-size:11px;letter-spacing:3px;text-transform:uppercase;color:#7A828A;">
          The Long News &middot; Daily edition</div>
        <h1 style="font-size:32px;margin:10px 0 4px;font-weight:700;">
          And now, <em style="color:#B08D57;">the real news.</em></h1>
        <div style="font-size:13px;color:#7A828A;letter-spacing:1px;">{long_date(today)}</div>
        {empty_note}
        {''.join(sections)}
        <div style="border-top:1px solid #D8D4CA;margin-top:32px;padding-top:12px;
                    font-size:12px;color:#9AA1A7;">
          Selected by machine, to be judged by an editor. In the long run,
          some news stories are more important than others.</div>
      </div>
    </div>"""


def render_plain(stories: list[dict], today: date) -> str:
    lines = [f"THE LONG NEWS — {long_date(today)}", ""]
    if not stories:
        lines.append("Nothing cleared the filter today.")
    for horizon_id, label, years, _ in HORIZONS:
        matches = [s for s in stories if s.get("horizon") == horizon_id]
        if not matches:
            continue
        lines += [f"{label.upper()} — will matter in 0{today.year + years}", ""]
        for s in matches:
            lines += [
                f"* {s.get('headline', '')} ({s.get('source', '')})",
                f"  {s.get('summary', '')}",
                f"  The long view: {s.get('why', '')}",
                f"  {s.get('url', '')}",
                "",
            ]
    return "\n".join(lines)


def send_email(stories: list[dict], today: date) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"The Long News — {long_date(today)}"
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = os.environ["EMAIL_TO"]
    msg.attach(MIMEText(render_plain(stories, today), "plain"))
    msg.attach(MIMEText(render_html(stories, today), "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(os.environ["EMAIL_FROM"], os.environ["GMAIL_APP_PASSWORD"])
        server.send_message(msg)


# ————————————————————————————————————————————————
# The website (published via GitHub Pages from /docs)
# ————————————————————————————————————————————————

SITE_DIR = "docs"


def render_page(stories: list[dict], today: date, record: list[tuple[str, str]]) -> str:
    sections = []
    for horizon_id, label, years, color in HORIZONS:
        matches = [s for s in stories if s.get("horizon") == horizon_id]
        items = []
        for s in matches:
            headline = s.get("headline", "Untitled")
            url = s.get("url")
            head_html = (
                f'<a href="{url}">{headline}</a>' if url else headline
            )
            meta = " &middot; ".join(x for x in [s.get("source"), s.get("date")] if x)
            items.append(
                f"""<article class="story">
  <h3>{head_html}</h3>
  <p class="meta">{meta}</p>
  <p class="summary">{s.get('summary', '')}</p>
  <p class="why" style="color:{color}"><strong>The long view —</strong> {s.get('why', '')}</p>
</article>"""
            )
        body = (
            "".join(items)
            if items
            else f'<p class="empty">No {horizon_id}-scale stories today.</p>'
        )
        sections.append(
            f"""<section class="stratum" style="border-left-color:{color}">
  <p class="h-year" style="color:{color}">Will matter in 0{today.year + years}</p>
  <h2>{label}</h2>
  {body}
</section>"""
        )

    record_html = ""
    if record:
        links = "".join(
            f'<a href="{fname}">{label}</a>' for fname, label in record
        )
        record_html = f'<footer class="record"><h3>The record</h3>{links}</footer>'

    empty_note = (
        '<p class="empty">Nothing cleared the filter today. That is a finding, not a failure.</p>'
        if not stories
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Long News — {long_date(today)}</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,700&family=Spectral:ital,wght@0,300;0,400;0,600;1,400&display=swap');
body {{ margin:0; background:#101418; color:#E8E4D8; font-family:'Spectral',Georgia,serif; font-weight:300; }}
.shell {{ max-width:760px; margin:0 auto; padding:0 20px 80px; }}
header.masthead {{ padding:56px 0 28px; border-bottom:1px solid #2A3138; }}
.eyebrow {{ font-size:12px; letter-spacing:.28em; text-transform:uppercase; color:#8A9299; margin:0 0 14px; }}
h1 {{ font-family:'Fraunces',Georgia,serif; font-weight:700; font-size:clamp(40px,7vw,64px); line-height:.95; margin:0; color:#EFEBDF; }}
h1 em {{ font-style:italic; font-weight:400; color:#B08D57; }}
.edition-date {{ font-size:14px; letter-spacing:.12em; color:#8A9299; margin:18px 0 0; }}
.stratum {{ border-left:3px solid; padding:4px 0 8px 22px; margin:34px 0; }}
.h-year {{ font-size:13px; letter-spacing:.2em; text-transform:uppercase; margin:0 0 2px; }}
h2 {{ font-family:'Fraunces',Georgia,serif; font-size:24px; font-weight:600; margin:0; }}
.story {{ margin:22px 0 0; }}
.story h3 {{ font-family:'Fraunces',Georgia,serif; font-size:20px; font-weight:600; line-height:1.25; margin:0; }}
.story h3 a {{ color:#EFEBDF; text-decoration:none; border-bottom:1px solid #3A424A; }}
.story h3 a:hover {{ border-bottom-color:#D9A441; }}
.meta {{ font-size:13px; color:#8A9299; margin:5px 0 0; }}
.summary {{ font-size:16px; line-height:1.55; margin:8px 0 0; color:#CFD4D2; }}
.why {{ font-size:15px; line-height:1.5; margin:8px 0 0; }}
.empty {{ color:#5E6870; font-style:italic; font-size:15px; }}
.record {{ margin-top:60px; border-top:1px solid #2A3138; padding-top:20px; }}
.record h3 {{ font-size:13px; letter-spacing:.22em; text-transform:uppercase; color:#8A9299; font-weight:400; margin:0 0 10px; }}
.record a {{ display:block; color:#B9BFC2; text-decoration:none; font-size:15px; padding:4px 0; }}
.record a:hover {{ color:#D9A441; }}
.colophon {{ margin-top:40px; font-size:13px; color:#5E6870; }}
</style>
</head>
<body>
<div class="shell">
  <header class="masthead">
    <p class="eyebrow">The Long News &middot; Daily edition</p>
    <h1>And now, <em>the real news.</em></h1>
    <p class="edition-date">{long_date(today)}</p>
  </header>
  {empty_note}
  {''.join(sections)}
  {record_html}
  <p class="colophon">Selected by machine, to be judged by an editor.
  In the long run, some news stories are more important than others.</p>
</div>
</body>
</html>"""


def build_site(stories: list[dict], today: date) -> None:
    os.makedirs(SITE_DIR, exist_ok=True)
    edition_file = f"{today.isoformat()}.html"

    # Gather past editions (dated files already in docs/), newest first.
    past = sorted(
        (
            f
            for f in os.listdir(SITE_DIR)
            if f.endswith(".html") and f[0].isdigit() and f != edition_file
        ),
        reverse=True,
    )
    record = [(edition_file, f"{long_date(today)} — today")] + [
        (f, long_date(date.fromisoformat(f[:-5]))) for f in past
    ]

    page = render_page(stories, today, record)
    with open(os.path.join(SITE_DIR, edition_file), "w") as fh:
        fh.write(page)
    with open(os.path.join(SITE_DIR, "index.html"), "w") as fh:
        fh.write(page)


if __name__ == "__main__":
    today = date.today()
    stories = run_scan()
    send_email(stories, today)
    build_site(stories, today)
    print(f"Edition of {long_date(today)}: {len(stories)} stories — emailed and published.")
