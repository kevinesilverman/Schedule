"""Build the encrypted calendar dashboard for sched.kevinsilverman.com.

Runs inside GitHub Actions. Secrets come from environment variables only:
  CAL_ICS_URL    - private iCal feed address (never logged, never committed)
  DASH_PASSCODE  - lock-screen passcode (never logged, never committed)
Output goes to ./_site (index.html + CNAME), which is deployed with
actions/deploy-pages and never committed to the repo.
"""
import base64
import html
import io
import json
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events
import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from PIL import Image, ImageDraw, ImageFont

TZ = ZoneInfo("America/Chicago")
DAYS_AHEAD = 5
PBKDF2_ITER = 250000
OWNER_EMAILS = {e.strip().lower() for e in os.environ.get(
    "OWNER_EMAILS", "kevin.e.silverman@gmail.com").split(",") if e.strip()}
CUSTOM_DOMAIN = "sched.kevinsilverman.com"
PRIORITY_SHEET = "https://docs.google.com/spreadsheets/d/1Yw4gwfwqyjX2R9VPXIo7sesJyK6rbSVEC52_aKI3dPg/edit"
DRIVE_FOLDER = "https://drive.google.com/drive/folders/1xKzLi5RT3Z8MCSdZpTnfvqIxZqMWH6_k"
PFS_XLSX = "https://drive.google.com/drive/folders/1ZZnzgwPmqGbV4DoCGf7Y3zJcqpJn-UoA"
STALE_AFTER = timedelta(hours=3)
OUT = os.environ.get("OUT_DIR", "_site")


def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------- fetch ----------
def fetch_ics():
    url = os.environ.get("CAL_ICS_URL", "").strip()
    if not url:
        fail("CAL_ICS_URL secret is not set")
    if url.startswith("file://"):  # local testing only
        return open(url[7:], "rb").read()
    try:
        r = requests.get(url, timeout=30)
    except requests.RequestException as e:
        fail(f"calendar fetch failed ({type(e).__name__})")  # don't echo the URL
    if r.status_code != 200 or b"BEGIN:VCALENDAR" not in r.content[:2000]:
        fail(f"calendar fetch failed (HTTP {r.status_code})")
    return r.content


# ---------- private inbox data (written by the hourly Claude task) ----------
def load_private():
    """Return (inbox_dict_or_None, contacts_list, note). Never fails the build."""
    if not os.environ.get("INBOX_KEY", "").strip():
        return None, [], "Inbox data not connected yet"
    sys.path.insert(0, os.path.dirname(__file__))
    try:
        import payload
        inbox = payload.dec_file("data/inbox.enc")
        contacts = payload.dec_file("data/contacts.enc") or []
        return inbox, contacts, ""
    except Exception as e:  # corrupt / wrong key: show calendar anyway
        print(f"WARNING: could not read private data ({type(e).__name__})", file=sys.stderr)
        return None, [], "Inbox data could not be read"


def parse_dt(v):
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=TZ)).astimezone(TZ)
    except Exception:
        return None


# ---------- events ----------
def as_local(v, is_end=False):
    """Return (datetime in CT, all_day flag)."""
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=TZ)
        return v.astimezone(TZ), False
    return datetime(v.year, v.month, v.day, tzinfo=TZ), True


def declined_by_owner(ev):
    atts = ev.get("ATTENDEE")
    if atts is None:
        return False
    if not isinstance(atts, list):
        atts = [atts]
    for a in atts:
        email = str(a).lower().replace("mailto:", "")
        if email in OWNER_EMAILS and str(a.params.get("PARTSTAT", "")).upper() == "DECLINED":
            return True
    return False


def collect(ics_bytes, today):
    cal = icalendar.Calendar.from_ical(ics_bytes)
    if not any(True for _ in cal.walk("VEVENT")):
        fail("calendar feed contained no events; refusing to publish an empty calendar")
    start = datetime(today.year, today.month, today.day, tzinfo=TZ)
    end = start + timedelta(days=DAYS_AHEAD + 1)
    out = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        if str(ev.get("STATUS", "")).upper() == "CANCELLED" or declined_by_owner(ev):
            continue
        s, all_day = as_local(ev.decoded("DTSTART"))
        if ev.get("DTEND") is not None:
            e, _ = as_local(ev.decoded("DTEND"))
        elif ev.get("DURATION") is not None:
            e = s + ev.decoded("DURATION")
        else:
            e = s + (timedelta(days=1) if all_day else timedelta(0))
        transparent = str(ev.get("TRANSP", "")).upper() == "TRANSPARENT"
        if transparent and (e - s) >= timedelta(days=28):
            continue  # all-year / multi-month placeholder
        out.append({
            "title": str(ev.get("SUMMARY", "(no title)")),
            "loc": str(ev.get("LOCATION", "") or ""),
            "start": s, "end": e, "all_day": all_day,
        })
    out.sort(key=lambda x: (x["start"], not x["all_day"]))
    return out


# ---------- render ----------
def fmt_t(d):
    return d.strftime("%I:%M %p").lstrip("0")


def icon_data_uri():
    img = Image.new("RGB", (180, 180), "#2a78d6")
    d = ImageDraw.Draw(img)
    font = None
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
        if os.path.exists(p):
            font = ImageFont.truetype(p, 78)
            break
    font = font or ImageFont.load_default()
    bb = d.textbbox((0, 0), "KS", font=font)
    w, h = bb[2] - bb[0], bb[3] - bb[1]
    d.text(((180 - w) / 2 - bb[0], (180 - h) / 2 - bb[1]), "KS", fill="white", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


HEAD_META = """<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Kevin's Dashboard</title>
<meta name="robots" content="noindex, nofollow">
<meta name="theme-color" media="(prefers-color-scheme: light)" content="#f9f9f7">
<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#0d0d0d">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Kevin's Dashboard">
<link rel="apple-touch-icon" href="__ICON__">
<link rel="icon" type="image/png" href="__ICON__">"""

TOKENS = """:root{--page:#f9f9f7;--surface:#fcfcfb;--text:#1c1c1a;--muted:#6e6e68;--faint:#9a9a93;--hair:rgba(0,0,0,.08);--accent:#2a78d6;--pill:rgba(42,120,214,.10);--green:#008300;--dim:#a3a39c;--err:#b3261e}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--page:#0d0d0d;--surface:#1a1a19;--text:#ecece8;--muted:#a0a099;--faint:#77776f;--hair:rgba(255,255,255,.1);--accent:#3987e5;--pill:rgba(57,135,229,.16);--green:#34a634;--dim:#5c5c56;--err:#f2867d}}
:root[data-theme="dark"]{--page:#0d0d0d;--surface:#1a1a19;--text:#ecece8;--muted:#a0a099;--faint:#77776f;--hair:rgba(255,255,255,.1);--accent:#3987e5;--pill:rgba(57,135,229,.16);--green:#34a634;--dim:#5c5c56;--err:#f2867d}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--text);font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}"""

DASH_CSS = """body{font-size:15px;line-height:1.45}
.wrap{max-width:760px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:1.35rem;margin:0 0 2px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:.83rem}
.card{background:var(--surface);border:1px solid var(--hair);border-radius:16px;padding:16px 18px;margin:18px 0 16px}
.card h2{font-size:.95rem;margin:0 0 6px}
.day{padding:10px 0;border-top:1px solid var(--hair)}
.day:first-of-type{border-top:0}
.dh{font-size:.78rem;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.today .dh{color:var(--accent)}
.pill{background:var(--pill);color:var(--accent);border-radius:20px;padding:1px 8px;font-size:.7rem;margin-left:6px;text-transform:none;letter-spacing:0}
.ev{display:flex;gap:10px;margin-top:6px}
.ev .t{color:var(--accent);font-variant-numeric:tabular-nums;font-size:.85rem;white-space:nowrap;min-width:92px;flex:0 0 auto;font-weight:500}
.ev .n{font-size:.9rem;min-width:0;overflow-wrap:anywhere}
.ev .loc{color:var(--muted);font-size:.8rem}
.ev.allday .t{color:var(--green)}
.ev.past{opacity:.55}
.ev.past .name{text-decoration:line-through;text-decoration-color:var(--dim)}
.ev.past .t{color:var(--dim)}
.clear{color:var(--faint);font-size:.85rem;margin-top:4px;font-style:italic}
footer{color:var(--faint);font-size:.74rem;text-align:center}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:16px}
.stat{background:var(--surface);border:1px solid var(--hair);border-radius:14px;padding:10px 12px}
.stat b{display:block;font-size:1.35rem;font-variant-numeric:tabular-nums}
.stat span{color:var(--muted);font-size:.75rem}
.item{display:block;padding:9px 0;border-top:1px solid var(--hair);color:inherit;text-decoration:none}
.item:first-of-type{border-top:0}
.item .top{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.item .who{font-weight:600;font-size:.9rem}
.item .when{color:var(--faint);font-size:.75rem;margin-left:auto}
.item .subj{font-size:.88rem;overflow-wrap:anywhere}
.item .sum{color:var(--muted);font-size:.82rem;overflow-wrap:anywhere}
.tag{font-size:.66rem;font-weight:600;border-radius:6px;padding:1px 6px;text-transform:uppercase;letter-spacing:.04em}
.tag.gmail{background:rgba(0,131,0,.12);color:var(--green)}
.tag.carmel{background:rgba(204,85,0,.13);color:#b44a00}
.tag.t1{background:rgba(179,38,30,.12);color:var(--err)}
.tag.t2,.tag.t3,.tag.kind{background:var(--pill);color:var(--accent)}
.note{color:var(--faint);font-size:.82rem;font-style:italic}
.warn{color:var(--err);font-size:.82rem}
.ev .src{font-size:.66rem;color:#b44a00;font-weight:600;margin-left:6px}
details.admin summary{cursor:pointer;font-weight:600;font-size:.9rem;list-style:none}
details.admin summary::-webkit-details-marker{display:none}
.btn{display:inline-block;margin:8px 8px 0 0;padding:8px 12px;border-radius:10px;border:1px solid var(--hair);color:var(--accent);text-decoration:none;font-size:.85rem;font-weight:600}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .tag.carmel,:root:not([data-theme="light"]) .ev .src{color:#f0a060}}
:root[data-theme="dark"] .tag.carmel,:root[data-theme="dark"] .ev .src{color:#f0a060}"""


E = html.escape


def _when(v):
    d = parse_dt(v)
    if not d:
        return ""
    return d.strftime("%a ") + fmt_t(d) if d.date() != datetime.now(TZ).date() else fmt_t(d)


def _item(it, show_tier=False):
    acct = (it.get("account") or "").lower()
    tags = f'<span class="tag {E(acct)}">{E(it.get("account", ""))}</span>' if acct else ""
    if it.get("kind"):
        tags += f' <span class="tag kind">{E(it["kind"])}</span>'
    if show_tier and it.get("tier"):
        tags += f' <span class="tag t{E(str(it["tier"]))}">Tier {E(str(it["tier"]))}</span>'
    href = it.get("link") or ""
    tag = "a" if href.startswith("https://") else "div"
    attr = f' href="{E(href)}" target="_blank" rel="noopener"' if tag == "a" else ""
    return (f'<{tag} class="item"{attr}><div class="top">{tags}<span class="who">{E(it.get("from", ""))}</span>'
            f'<span class="when">{E(_when(it.get("when")))}</span></div>'
            f'<div class="subj">{E(it.get("subject", ""))}</div>'
            + (f'<div class="sum">{E(it["summary"])}</div>' if it.get("summary") else "")
            + f"</{tag}>")


def _card(title, body):
    return f'<div class="card"><h2>{title}</h2>{body}</div>'


def private_sections(inbox, contacts, note, now):
    if inbox is None:
        return _card("Inbox", f'<div class="note">{E(note)}</div>'), 0, "&ndash;"
    out = []
    gen = parse_dt(inbox.get("generated_at"))
    stale = (not gen) or (now - gen) > STALE_AFTER
    if stale:
        out.append(f'<div class="card"><div class="warn">Inbox data last refreshed '
                   f'{E(gen.strftime("%a %b %d, ") + fmt_t(gen)) if gen else "unknown"} &mdash; hourly check may be failing.</div></div>')
    pri = inbox.get("priority") or []
    if pri:
        out.append(_card("Priority people", "".join(_item(x, True) for x in pri)))
    att = inbox.get("needs_attention") or []
    noise = inbox.get("noise_summary") or ""
    out.append(_card("Needs attention",
                     ("".join(_item(x) for x in att) or '<div class="note">Nothing needs attention.</div>')
                     + (f'<div class="note" style="margin-top:8px">{E(noise)}</div>' if noise else "")))
    hn = inbox.get("holdings_news") or []
    if hn:
        out.append(_card("Holdings news", "".join(_item(x) for x in hn)))
    mt = inbox.get("meetings_added") or []
    if mt:
        rows = []
        for m in mt:
            href = m.get("event_link") or m.get("link") or ""
            a = f' href="{E(href)}" target="_blank" rel="noopener"' if href.startswith("https://") else ""
            rows.append(f'<a class="item"{a}><div class="top"><span class="tag {E((m.get("account") or "").lower())}">{E(m.get("account", ""))}</span>'
                        f'<span class="who">{E(m.get("with", ""))}</span><span class="when">{E(_when(m.get("start")))}</span></div>'
                        f'<div class="subj">{E(m.get("where", ""))}</div><div class="sum">Added as tentative &middot; from &ldquo;{E(m.get("subject", ""))}&rdquo;</div></a>')
        out.append(_card("Meetings found in email", "".join(rows)))
    cm = inbox.get("commitments") or []
    if cm:
        rows = []
        for c in cm:
            href = c.get("link") or ""
            a = f' href="{E(href)}" target="_blank" rel="noopener"' if href.startswith("https://") else ""
            due = f'<span class="when">due {E(c["due"])}</span>' if c.get("due") else ""
            rows.append(f'<a class="item"{a}><div class="top"><span class="tag {E((c.get("account") or "").lower())}">{E(c.get("account", ""))}</span>'
                        f'<span class="who">{E(c.get("to", ""))}</span>{due}</div><div class="subj">{E(c.get("text", ""))}</div></a>')
        out.append(_card("Promises I made", "".join(rows)))
    errs = inbox.get("errors") or []
    if errs:
        out.append(_card("Check needed", "".join(f'<div class="warn">{E(x)}</div>' for x in errs)))
    return "".join(out), len(att) + len(pri), sum((inbox.get("volume") or {}).values()) if inbox.get("volume") else "&ndash;"


def admin_section(contacts):
    new = [c for c in contacts if c.get("status") == "new"]
    rows = "".join(
        f'<div class="item"><div class="top"><span class="who">{E(((c.get("first") or "") + " " + (c.get("last") or "")).strip() or c.get("email", ""))}</span>'
        f'<span class="when">{E(c.get("account", ""))}</span></div><div class="sum">{E(" · ".join(x for x in [c.get("title"), c.get("company"), c.get("email"), c.get("phone")] if x))}</div></div>'
        for c in new[-15:][::-1])
    return (f'<div class="card"><details class="admin"><summary>&#9881;&#xFE0E; Admin</summary>'
            f'<a class="btn" href="{PRIORITY_SHEET}" target="_blank" rel="noopener">Priority people list</a>'
            f'<a class="btn" href="{DRIVE_FOLDER}" target="_blank" rel="noopener">Dashboard folder</a>'
            f'<a class="btn" href="{PFS_XLSX}" target="_blank" rel="noopener">PFS contacts sheet</a>'
            f'<div class="note" style="margin-top:10px">Paste or upload a CSV/XLSX into the Priority sheet (columns: name, email, tier 1&ndash;3, note). '
            f'Takes effect on the next hourly check.</div>'
            f'<h2 style="margin-top:14px">New contacts for PFS ({len(new)})</h2>'
            + (rows or '<div class="note">None waiting.</div>')
            + "</details></div>")


def render_dashboard(events, now, icon, inbox=None, contacts=(), note=""):
    # Carmel (Outlook) meetings arrive in the private payload
    extra = [dict(x, src="Carmel") for x in (inbox or {}).get("carmel_events") or []]
    extra += list((inbox or {}).get("extra_events") or [])   # other Google calendars (UWM, personal, family…)
    for ce in extra:
        s, e = parse_dt(ce.get("start")), parse_dt(ce.get("end"))
        if s:
            events.append({"title": ce.get("title", "(no title)"), "loc": ce.get("loc", ""),
                           "start": s, "end": e or s, "all_day": bool(ce.get("all_day")), "src": ce.get("src", "")})
    events.sort(key=lambda x: (x["start"], not x["all_day"]))
    today = now.date()
    days = []
    for i in range(DAYS_AHEAD + 1):
        d = today + timedelta(days=i)
        d0 = datetime(d.year, d.month, d.day, tzinfo=TZ)
        d1 = d0 + timedelta(days=1)
        todays = [e for e in events if e["start"] < d1 and e["end"] > d0
                  or (e["start"] == e["end"] and d0 <= e["start"] < d1)]
        rows = []
        for e in todays:
            if e["all_day"]:
                t, past, cls = "All day", False, "ev allday"
            else:
                t = f"{fmt_t(e['start'])}&ndash;{fmt_t(e['end'])}" if e["end"] > e["start"] else fmt_t(e["start"])
                if e["start"].date() < d:
                    t = f"until {fmt_t(e['end'])}"
                past = e["end"] <= now
                cls = "ev past" if past else "ev"
            loc = f' <span class="loc">&middot; {html.escape(e["loc"])}</span>' if e["loc"] else ""
            src = f'<span class="src">{E(e["src"])}</span>' if e.get("src") else ""
            rows.append(f'<div class="{cls}"><div class="t">{t}</div>'
                        f'<div class="n"><span class="name">{html.escape(e["title"])}</span>{src}{loc}</div></div>')
        label = d.strftime("%A, %b ") + str(d.day)
        pill = '<span class="pill">Today</span>' if i == 0 else ""
        body = "".join(rows) or '<div class="clear">Clear day</div>'
        days.append(f'<div class="day{" today" if i == 0 else ""}"><div class="dh">{label}{pill}</div>{body}</div>')
    upcoming = sum(1 for e in events if not e["all_day"] and e["end"] > now)
    stamp = now.strftime("%a %b ") + str(now.day) + ", " + fmt_t(now)
    priv, n_att, vol = private_sections(inbox, list(contacts), note, now)
    stats = (f'<div class="stats"><div class="stat"><b>{upcoming}</b><span>meetings ahead</span></div>'
             f'<div class="stat"><b>{n_att}</b><span>need attention</span></div>'
             f'<div class="stat"><b>{vol}</b><span>emails, last day</span></div></div>')
    return (f"<!doctype html><html lang=\"en\"><head>{HEAD_META.replace('__ICON__', icon)}"
            f"<style>{TOKENS}\n{DASH_CSS}</style></head><body><div class=\"wrap\">"
            f"<header><h1>Kevin&rsquo;s Dashboard</h1><div class=\"sub\">Updated {stamp} CT &middot; refreshes hourly</div></header>"
            f"{stats}{priv}"
            f"<div class=\"card\"><h2>Week ahead</h2>{''.join(days)}</div>"
            f"{admin_section(list(contacts))}"
            f"<footer>Google Calendar &middot; Gmail &middot; Carmel Outlook</footer></div></body></html>")


# ---------- encrypt + lock screen ----------
LOCK_CSS = """html,body{height:100%}
body{display:flex;align-items:center;justify-content:center;padding:16px}
.lock{width:100%;max-width:340px;background:var(--surface);border:1px solid var(--hair);border-radius:18px;padding:28px 22px;text-align:center}
.logo{width:64px;height:64px;border-radius:15px;display:block;margin:0 auto 14px}
h1{font-size:1.15rem;margin:0 0 4px}
p{color:var(--muted);font-size:.88rem;margin:0 0 18px}
input{width:100%;font:inherit;font-size:1.1rem;text-align:center;padding:12px;border-radius:12px;border:1px solid var(--hair);background:var(--page);color:var(--text);outline:none}
input:focus{border-color:var(--accent)}
button{margin-top:12px;width:100%;font:inherit;font-weight:600;padding:12px;border:0;border-radius:12px;background:var(--accent);color:#fff;cursor:pointer}
button:disabled{opacity:.6}
.msg{min-height:1.2em;font-size:.83rem;margin-top:10px;color:var(--muted)}
.msg.err{color:var(--err)}"""

LOCK_JS = r"""(function(){
var SALT="__SALT__",IV="__IV__",CT="__CT__",ITER=__ITER__;
var K_CODE="ks_dash_code";
function b(s){return Uint8Array.from(atob(s),function(c){return c.charCodeAt(0)})}
function get(k){try{return localStorage.getItem(k)}catch(e){return null}}
function set(k,v){try{localStorage.setItem(k,v)}catch(e){}}
function del(k){try{localStorage.removeItem(k)}catch(e){}}
async function unlock(code){
 var base=await crypto.subtle.importKey("raw",new TextEncoder().encode(code),"PBKDF2",false,["deriveKey"]);
 var key=await crypto.subtle.deriveKey({name:"PBKDF2",salt:b(SALT),iterations:ITER,hash:"SHA-256"},base,{name:"AES-GCM",length:256},false,["decrypt"]);
 var pt=await crypto.subtle.decrypt({name:"AES-GCM",iv:b(IV)},key,b(CT));
 return new TextDecoder().decode(pt);
}
function show(h){document.open();document.write(h);document.close()}
var msg=document.getElementById("msg"),pc=document.getElementById("pc"),go=document.getElementById("go");
var saved=get(K_CODE);
if(saved){msg.textContent="Opening…";
 unlock(saved).then(show).catch(function(){del(K_CODE);msg.textContent=""})}
document.getElementById("f").addEventListener("submit",function(e){
 e.preventDefault();var code=pc.value.trim();if(!code)return;
 go.disabled=true;msg.className="msg";msg.textContent="Checking…";
 unlock(code).then(function(h){
  /* remember this device after the first correct entry */
  set(K_CODE,code);
  show(h);
 }).catch(function(){go.disabled=false;pc.value="";msg.className="msg err";msg.textContent="Incorrect password"});
});
if(!saved)pc.focus();
})();"""


def lock_page(dashboard_html, passcode, icon):
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=PBKDF2_ITER).derive(passcode.encode())
    ct = AESGCM(key).encrypt(iv, dashboard_html.encode(), None)
    b64 = lambda x: base64.b64encode(x).decode()
    js = (LOCK_JS.replace("__SALT__", b64(salt)).replace("__IV__", b64(iv))
          .replace("__CT__", b64(ct)).replace("__ITER__", str(PBKDF2_ITER)))
    return (f"<!doctype html><html lang=\"en\"><head>{HEAD_META.replace('__ICON__', icon)}"
            f"<style>{TOKENS}\n{LOCK_CSS}</style></head><body>"
            f"<form class=\"lock\" id=\"f\" autocomplete=\"off\"><img class=\"logo\" src=\"{icon}\" alt=\"\">"
            f"<h1>Kevin&rsquo;s Dashboard</h1><p>Enter password</p>"
            f"<input id=\"pc\" type=\"password\" autocomplete=\"current-password\" autocapitalize=\"off\" spellcheck=\"false\" aria-label=\"Passcode\">"
            f"<button id=\"go\" type=\"submit\">Unlock</button><div class=\"msg\" id=\"msg\"></div></form>"
            f"<script>{js}</script></body></html>")


def main():
    passcode = os.environ.get("DASH_PASSCODE", "").strip()
    if not passcode:
        fail("DASH_PASSCODE secret is not set")
    now = datetime.now(TZ)
    events = collect(fetch_ics(), now.date())
    inbox, contacts, note = load_private()
    icon = icon_data_uri()
    page = lock_page(render_dashboard(events, now, icon, inbox, contacts, note), passcode, icon)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "index.html"), "w") as f:
        f.write(page)
    with open(os.path.join(OUT, "CNAME"), "w") as f:
        f.write(CUSTOM_DOMAIN + "\n")
    print(f"Built encrypted dashboard: {len(events)} events in window.")


if __name__ == "__main__":
    main()
