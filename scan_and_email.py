"""
The Long News — scan and (occasionally) publish.

Surveys the past week's news via the Anthropic API (with web search enabled),
keeps only the few stories that might matter in a decade, a century, or a
millennium, and — only when something clears that bar — emails the edition
and publishes it to the web. Most runs publish nothing, by design; on those
weeks it emails the editor the near-misses so the silence can be audited.

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
        <div style="border-top:1px solid #D8D4CA;margin-top:32px;padding-top:12px;
                    font-size:12px;color:#9AA1A7;">
          Selected by machine, to be judged by an editor. In the long run,
          some news stories are more important than others.</div>
      </div>
    </div>"""


def render_plain(stories: list[dict], today: date) -> str:
    lines = [f"THE LONG NEWS — {long_date(today)}", ""]
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


def render_rejects_plain(rejects: list[dict], today: date) -> str:
    lines = [
        f"THE LONG NEWS — REJECTS — {long_date(today)}",
        "",
        "Nothing cleared the bar this week. These are the strongest stories the",
        "scan considered and set aside — the near-misses, so you can audit the",
        "silence. If one of these should have run, that is the signal to loosen",
        "the filter.",
        "",
    ]
    for r in rejects:
        lines += [
            f"* {r.get('headline', '')} ({r.get('source', '')})",
            f"  Why not: {r.get('reason', '')}",
            f"  {r.get('url', '')}",
            "",
        ]
    return "\n".join(lines)


def render_rejects_html(rejects: list[dict], today: date) -> str:
    items = []
    for r in rejects:
        headline = r.get("headline", "Untitled")
        url = r.get("url")
        head_html = (
            f'<a href="{url}" style="color:#1C2228;text-decoration:none;'
            f'border-bottom:1px solid #B0A99A;">{headline}</a>'
            if url
            else headline
        )
        items.append(
            f"""
            <div style="margin:16px 0 0;">
              <div style="font-size:17px;font-weight:600;line-height:1.3;color:#3A3A38;">{head_html}</div>
              <div style="font-size:12px;color:#8A867C;margin-top:3px;">{r.get('source', '')}</div>
              <div style="font-size:14px;line-height:1.5;margin-top:5px;color:#6A665C;">
                <em>Why not —</em> {r.get('reason', '')}</div>
            </div>"""
        )
    return f"""
    <div style="background:#F2EFE8;padding:32px 16px;">
      <div style="max-width:640px;margin:0 auto;font-family:Georgia,'Times New Roman',serif;color:#3A3A38;">
        <div style="font-size:11px;letter-spacing:3px;text-transform:uppercase;color:#8A867C;">
          The Long News &middot; Rejects</div>
        <h1 style="font-size:26px;margin:10px 0 4px;font-weight:700;color:#2A2A28;">
          Nothing cleared the bar this week.</h1>
        <div style="font-size:13px;color:#8A867C;letter-spacing:1px;">{long_date(today)}</div>
        <p style="font-size:15px;line-height:1.55;color:#6A665C;margin:16px 0 0;">
          These are the strongest stories the scan considered and set aside —
          the near-misses. If one of them should have run, that is your signal
          the filter is too tight.</p>
        {''.join(items)}
        <div style="border-top:1px solid #D5D0C5;margin-top:28px;padding-top:12px;
                    font-size:12px;color:#A5A093;">
          Sent to the editor only. Not published.</div>
      </div>
    </div>"""


def send_rejects(rejects: list[dict], today: date) -> None:
    """Email the near-miss list to the editor only. No site build, no publish."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "This week's Long News rejects"
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = os.environ["EMAIL_TO"]
    msg.attach(MIMEText(render_rejects_plain(rejects, today), "plain"))
    msg.attach(MIMEText(render_rejects_html(rejects, today), "html"))

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
        if not matches:
            continue  # occasional editions show only the strata that hit
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
        sections.append(
            f"""<section class="stratum" style="border-left-color:{color}">
  <p class="h-year" style="color:{color}">Will matter in 0{today.year + years}</p>
  <h2>{label}</h2>
  {''.join(items)}
</section>"""
        )

    record_html = ""
    if record:
        links = "".join(
            f'<a href="{fname}">{label}</a>' for fname, label in record
        )
        record_html = f'<footer class="record"><h3>The record</h3>{links}</footer>'

    empty_note = ""

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
    <p class="eyebrow">The Long News</p>
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
    stories, rejects = run_scan(recent=recent_headlines())
    if not stories:
        # No long news this week. Don't publish — but email the editor the
        # near-misses so the silence can be audited.
        if rejects:
            send_rejects(rejects, today)
            print(f"{long_date(today)}: nothing cleared the bar. Rejects emailed ({len(rejects)}).")
        else:
            print(f"{long_date(today)}: nothing cleared the bar and no rejects returned.")
        raise SystemExit(0)
    send_email(stories, today)
    build_site(stories, today)
    print(f"Edition of {long_date(today)}: {len(stories)} stories — emailed and published.")
