"""
רובייקטיבי — WhatsApp Bot (Flask + Twilio)
==========================================
Adapter לוואטסאפ לתסריט השיחה של רובייקטיבי.
Twilio Sandbox: ללא כפתורים אמיתיים — fallback לבחירת מספרים.
"""

import os
import json
import re
import base64
import requests
import anthropic
from collections import defaultdict
from datetime import datetime, timedelta
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient

from core.pension_core import (
    SYSTEM_PROMPT, USER_PROMPT,
    format_number, g,
    get_payment_value, get_movement_value,
    detect_deposit_source, validate_report,
    compute_analysis, check_insurance,
    FUND_PLANS, ADVISOR_PLAN, MAX_FEES,
    extract_fee_rates, calc_annual_fee,
    EQUITY_TRACKS, MADEDEI_WARNING_FUNDS,
    find_fund_key, is_equity_track, is_age_related_track,
    GOV_EMPLOYERS, is_gov_employer,
    sentences_to_lines,
)

app = Flask(__name__)

# ─── Configuration ───
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
MAX_PDF_SIZE_KB    = 400
MAX_WEEKLY_REPORTS = 4
CONSENT_TIMEOUT    = 3600  # שניות

TERMS_URL          = "https://example.com/terms"        # placeholder
ADVISOR_URL        = "https://meshulam.co.il/s/9b71389b-1564-a6c6-5f08-883647e04c53"
ADVISOR_EXPLAIN_URL= "https://example.com/advisor"      # placeholder

CONSENT_TEXT = (
    "קראתי את תנאי השימוש ואני מקבל אותם. "
    "אני מודע לכך שהשימוש רובייקטיבי איננו ייעוץ פנסיוני "
    "ושכל החלטה ופעולה שאעשה הם על אחריותי בלבד"
)

TOPIC_KEYS   = ["insurance", "deposits", "fees", "investment"]
TOPIC_LABELS = {
    "insurance":  "כיסויים ביטוחיים",
    "deposits":   "הפקדות",
    "fees":       "דמי ניהול",
    "investment": "מסלול השקעה",
}

# ─── In-memory session store ───
sessions: dict[str, dict] = {}


def _get_week_start() -> datetime:
    now = datetime.now()
    days_since_sunday = (now.weekday() + 1) % 7
    sunday = now - timedelta(days=days_since_sunday)
    return sunday.replace(hour=0, minute=0, second=0, microsecond=0)


def get_session(phone: str) -> dict:
    if phone not in sessions:
        sessions[phone] = {
            "state":            "welcome",
            "gender":           None,
            "marital_status":   None,
            "has_minor_children": False,
            "consent_ts":       None,
            "week_start":       None,
            "reports_week":     0,
            "failed_attempts":  0,
            "analysis":         None,   # dict עם 4 sections
            "topics_read":      [],
        }
    return sessions[phone]


def refresh_quota(session: dict):
    current_week = _get_week_start()
    if session["week_start"] is None or session["week_start"] < current_week:
        session["week_start"]   = current_week
        session["reports_week"] = 0


def quota_ok(session: dict) -> bool:
    refresh_quota(session)
    return session["reports_week"] < MAX_WEEKLY_REPORTS


def consent_expired(session: dict) -> bool:
    if not session.get("consent_ts"):
        return True
    return (datetime.now() - session["consent_ts"]).total_seconds() > CONSENT_TIMEOUT


# ─── Twilio helpers ───

EQUITY_IMAGE_URL = "https://ahituvbiz.github.io/robjectivi-landing/madedei_maniut.png"

def send_wa(to: str, from_: str, body: str):
    client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    client.messages.create(body=body, from_=from_, to=to)

def send_wa_media(to: str, from_: str, media_url: str):
    client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    client.messages.create(media_url=[media_url], from_=from_, to=to)


def download_twilio_media(media_url: str) -> bytes | None:
    resp = requests.get(media_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
    return resp.content if resp.status_code == 200 else None


# ─── Claude API ───

def call_claude(pdf_bytes: bytes):
    if not ANTHROPIC_API_KEY:
        return None, "מפתח API לא הוגדר."
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}},
                {"type": "text", "text": USER_PROMPT},
            ]}],
        )
    except anthropic.BadRequestError as e:
        err = str(e.message) if hasattr(e, "message") else str(e)
        if "credit balance" in err.lower():
            return None, "שגיאה: אין מספיק קרדיט ב-API."
        return None, "שגיאה בעיבוד הדוח. נסה שוב."
    except anthropic.APIError:
        return None, "שגיאה בתקשורת עם שרת ה-AI. נסה שוב מאוחר יותר."
    text  = "".join(b.text for b in message.content if hasattr(b, "text"))
    clean = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(clean), None
    except json.JSONDecodeError:
        return None, "שגיאה בפענוח התשובה מה-AI. נסה שוב."


# ─── Analysis helpers ───

def build_insurance_text(data, analysis, user_profile):
    lines = []
    gender = user_profile.get("gender", "גבר")
    age = analysis.get("estimated_age")
    if age:
        lines.append(f"גיל משוער: {age:.0f}")
    if analysis.get("can_calc_income"):
        lines.append(f"הכנסה מבוטחת: ₪{analysis['insured_income']:,} | שיעור הפקדה: {analysis['deposit_rate']}%")
    warnings = check_insurance(analysis, user_profile)
    if warnings:
        for icon, msg in warnings:
            lines.append(f"\n{icon} {msg}")
        dv = analysis.get("disability_pension", 0)
        if dv > 0:
            lines.append(f"\nבמקרה נכות 75%+ הקרן תשלם קצבה חודשית של ₪{format_number(dv)}.")
    else:
        sw = "לאשתך" if gender == "גבר" else "לבעלך"
        sv = format_number(analysis.get("spouse_pension", 0))
        ov = format_number(analysis.get("orphan_pension", 0))
        tot = (analysis.get("spouse_pension", 0) or 0) + (analysis.get("orphan_pension", 0) or 0)
        dv = format_number(analysis.get("disability_pension", 0))
        lines.append(f"\n✅ נראה ש{g(gender, 'אתה', 'את')} {g(gender, 'נמצא', 'נמצאת')} במסלול עם כיסויים מקסימליים.")
        lines.append(f"הקרן מבטיחה {sw} קצבה של ₪{sv} לכל החיים.")
        lines.append(f"בנוסף ₪{ov}/חודש עד שהילד הקטן יגיע ל-21. סה\"כ: ₪{format_number(tot)}.")
        lines.append(f"במקרה נכות 75%+: קצבה חודשית של ₪{dv}.")
    return sentences_to_lines("\n".join(lines)) or "לא נמצאו נתוני כיסויים בדוח."


def build_deposit_text(data, analysis, user_profile):
    lines = []
    gender = user_profile.get("gender", "גבר")
    deposit_source = detect_deposit_source(data)
    deposits = data.get("deposits", []) + data.get("late_deposits", [])
    if not deposits:
        return "אין נתוני הפקדות להצגה."
    report_period = data.get("header", {}).get("report_period", "")
    report_date   = data.get("header", {}).get("report_date", "")
    report_year = None
    for text in [report_period, report_date]:
        m = re.search(r"20\d{2}", text)
        if m:
            report_year = int(m.group()); break
    if not report_year:
        return "לא ניתן לזהות את שנת הדוח."
    vf = "total" if deposit_source == "עצמאי" else "salary"
    monthly = defaultdict(list)
    for dep in deposits:
        sm = dep.get("salary_month", "")
        val = dep.get(vf) or dep.get("salary") or dep.get("total")
        if not val or not sm: continue
        try:
            p = sm.split("/")
            if len(p) == 2:
                mn, yr = int(p[0]), int(p[1])
                if yr < 100: yr += 2000
                if yr == report_year and 1 <= mn <= 12:
                    monthly[mn].append(float(val))
        except: continue
    if not monthly:
        return "אין נתוני הפקדות לשנת הדוח."
    pem = 12
    if "רבעון 1" in report_period or "רבעון ראשון" in report_period: pem = 3
    elif "רבעון 2" in report_period or "רבעון שני" in report_period: pem = 6
    elif "רבעון 3" in report_period or "רבעון שלישי" in report_period: pem = 9
    display = list(range(1, pem + 1))
    totals = [sum(monthly.get(m, [])) for m in display if monthly.get(m)]
    if totals:
        avg = sum(totals) / len(totals)
        lbl = "הפקדה ממוצעת" if deposit_source == "עצמאי" else "שכר ממוצע"
        lines.append(f"{lbl}: ₪{format_number(round(avg))}")
        lines.append(f"חודשים עם הפקדות בשנת {report_year}: {len(totals)}/{len(display)}")
    missing = [m for m in range(1, pem) if not monthly.get(m)]
    if missing:
        ms = ", ".join(str(m) for m in missing)
        if deposit_source == "עצמאי":
            lines.append(f"\n⚠️ לא נמצאו הפקדות לחודשים: {ms}. {g(gender,'וודא','וודאי')} שלא ביצעת הפקדות שלא נקלטו.")
        else:
            lines.append(f"\n⚠️ לא נמצאו הפקדות לחודשים: {ms}. יש לוודא שהמעסיק הפקיד את כל ההפקדות.")
    dr = analysis.get("deposit_rate", 0)
    if analysis.get("can_calc_income") and deposit_source == "שכיר" and dr < 18.48:
        lines.append(f"\n⚠️ {g(gender,'שים','שימי')} לב, שיעור ההפקדות נראה נמוך מהמינימום לפי חוק.")
    return sentences_to_lines("\n".join(lines)) or "אין נתוני הפקדות להצגה."


def build_fee_text(data, analysis, user_profile):
    lines = []
    gender = user_profile.get("gender", "גבר")
    cb = analysis.get("closing_balance", 0)
    if not cb or cb <= 0: return "אין מספיק נתונים לבחינת דמי ניהול."
    df, sf = extract_fee_rates(data)
    if df == 0 and sf == 0: return "לא נמצאו נתוני דמי ניהול בדוח."
    td = analysis.get("total_deposits", 0)
    rp = analysis.get("report_period", "")
    if "רבעון 1" in rp or "רבעון ראשון" in rp: ad = td * 4
    elif "רבעון 2" in rp or "רבעון שני" in rp: ad = td * 2
    elif "רבעון 3" in rp or "רבעון שלישי" in rp: ad = round(td * 4 / 3)
    else: ad = td
    md = ad / 12 if ad else 0
    avgs = cb * 1.02 + 6 * md
    cf   = calc_annual_fee(df, sf, ad, avgs)
    mxf  = calc_annual_fee(MAX_FEES[0], MAX_FEES[1], ad, avgs)
    opts = []
    for fn, plans in FUND_PLANS.items():
        for d, s in plans:
            opts.append((fn, d, s, calc_annual_fee(d, s, ad, avgs)))
    adv = calc_annual_fee(ADVISOR_PLAN[0], ADVISOR_PLAN[1], ad, avgs)
    chp = min([cf] + [o[3] for o in opts] + [adv])
    lines.append(f"דמי הניהול שלך: {df}% מהפקדה + {sf}% מצבירה")
    lines.append(f"עלות שנתית צפויה: ₪{format_number(round(cf))}")
    opts.sort(key=lambda x: x[3])
    cff = opts[0][3] if opts else None
    cfd = opts[0][1] if opts else None
    cfs = opts[0][2] if opts else None
    cfn = list(set(o[0] for o in opts if cff and abs(o[3]-cff)<1)) if cff else []
    saving = cf - chp
    if cf <= chp * 1.05 or saving < 100:
        lines.append("✅ דמי הניהול שלך ברמה תחרותית מאוד!")
    elif cff is not None and cff < cf:
        fn_str = " / ".join(cfn)
        lines.append(f"💡 בקרן {fn_str} {g(gender,'תוכל','תוכלי')} לקבל {cfd}% הפקדה + {cfs}% צבירה.")
        lines.append(f"חיסכון שנתי צפוי: ₪{format_number(round(cf-cff))}.")
        if adv < cf:
            lines.append(f"💡 יועץ פנסיוני יוכל להשיג 1% הפקדה + 0.145% צבירה → חיסכון ₪{format_number(round(cf-adv))}.")
    fn_str = analysis.get("fund_name", "")
    if "מנורה" in fn_str:
        ac = get_movement_value(data.get("movements", []), "אקטוארי")
        lines.append(f"\n⚠️ {g(gender,'שים','שימי')} לב — נוכה ₪{format_number(round(ac))} בגלל הגרעון האקטוארי של מנורה.")
        lines.append(f"{g(gender,'שקול','שיקלי')} לעבור לקרן פנסיה אחרת.")
    return sentences_to_lines("\n".join(lines))


def build_investment_text(data, analysis, user_profile):
    lines = []
    gender = user_profile.get("gender", "גבר")
    tracks = data.get("investment_tracks", [])
    if not tracks: return "אין נתוני מסלולי השקעה בדוח."
    lines.append("מסלולי ההשקעה שלך:")
    for t in tracks:
        lines.append(f"  • {t.get('track_name','')} — {t.get('return_rate','')}")
    age = analysis.get("estimated_age")
    if not age:
        return sentences_to_lines("\n".join(lines))
    fk = find_fund_key(analysis.get("fund_name", ""))
    mult = 196 if gender == "גבר" else 194
    names = [t.get("track_name","") for t in tracks]
    eq, neq, sp500, madedei, halacha, age_track = [], [], False, False, False, False
    cash_tracks = []
    for n in names:
        nl = n.lower()
        if "s&p" in nl or "500" in nl: sp500 = True
        if "מדדי מניות" in n: madedei = True
        if "הלכה" in n: halacha = True
        if "כספי" in n or "שקלי" in n: cash_tracks.append(n)
        if fk and is_equity_track(n, fk): eq.append(n)
        else: neq.append(n)
        if is_age_related_track(n): age_track = True
    if age <= 52:
        if neq:
            p67  = analysis.get("pension_at_67", 0)
            cb   = analysis.get("closing_balance", 0)
            yrs  = 67 - age
            ifv  = cb * ((1.0525) ** yrs) if yrs > 0 else cb
            ip   = round(ifv / mult)
            lines.append(f"\n💡 בהתחשב בגילך — {g(gender,'תבחר','תבחרי')} במסלולים מנייתיים בלבד.")
            lines.append(f"קצבה מהכספים שצברת: ₪{format_number(p67)}.")
            lines.append(f"שיפור תשואה ב-1.25% → קצבה ₪{format_number(ip)}.")
    else:
        lines.append("\n💡 בשנים שנותרו לפרישה — הפחת בהדרגה את החשיפה למניות.")
    if sp500:
        lines.append(f"\n⚠️ {g(gender,'אתה מושקע','את מושקעת')} ב-S&P 500 — ריכוזיות גבוהה.")
    if madedei and fk in MADEDEI_WARNING_FUNDS:
        lines.append("\n⚠️ מסלול מדדי מניות — ריכוז גבוה, שקול חלופות.")
    if halacha and age <= 52 and fk != "אינפיניטי":
        lines.append(f"\n⚠️ מסלול הלכה בקרן שלך — חשיפה לא מקסימלית. {g(gender,'שקול','שיקלי')} לעבור למסלול הלכה מנייתי.")
    if age <= 52 and fk and EQUITY_TRACKS[fk]["recommendation"]:
        lines.append(f"\n🤖 המסלול המומלץ בקרן שלך:\n{EQUITY_TRACKS[fk]['recommendation']}")
    for ct in cash_tracks:
        lines.append(
            f"\n⚠️ מסלול {ct} שלך הוא ללא שום חשיפה למניות. "
            "זה יפגע בפנסיה העתידית שלך. "
            "אני ממליץ שתשקול ברצינות לשנות מסלול השקעה."
        )
    return sentences_to_lines("\n".join(lines))


def analyze_pdf(pdf_bytes: bytes, user_profile: dict):
    """ניתוח PDF. מחזיר (sections_dict, error_str)."""
    data, error = call_claude(pdf_bytes)
    if error: return None, error
    is_valid, ve = validate_report(data)
    if not is_valid: return None, f"⚠️ {ve}"
    ds = detect_deposit_source(data)
    if ds == "שכיר + עצמאי":
        return None, "⚠️ עדיין לא למדתי לנתח דוח עם גם הפקדות שכיר וגם עצמאי."
    analysis = compute_analysis(data, user_profile)
    sections = {
        "insurance":  {"title": "🛡️ כיסויים ביטוחיים", "text": build_insurance_text(data, analysis, user_profile)},
        "deposits":   {"title": "💰 הפקדות",             "text": build_deposit_text(data, analysis, user_profile)},
        "fees":       {"title": "🏷️ דמי ניהול",          "text": build_fee_text(data, analysis, user_profile)},
        "investment": {
            "title": "📈 מסלול השקעה",
            "text": build_investment_text(data, analysis, user_profile),
            "show_equity_image": any(
                ("s&p" in t.get("track_name", "").lower() or
                 "500" in t.get("track_name", "").lower() or
                 "מדדי מניות" in t.get("track_name", "") or
                 "עוקב מדדי מניות" in t.get("track_name", ""))
                for t in data.get("investment_tracks", [])
            ),
        },
    }
    # עובד מדינה?
    employer = data.get("header", {}).get("employer", "")
    if not is_gov_employer(employer):
        for dep in data.get("deposits", []):
            de = dep.get("employer", "")
            if de and is_gov_employer(de):
                employer = de; break
    gender = user_profile.get("gender", "גבר")
    if is_gov_employer(employer):
        sections["_gov_note"] = f"💡 החשב הכללי מעודד עובדי מדינה לקחת ייעוץ פנסיוני עם סבסוד של 600 ש\"ח. {g(gender,'נצל','נצלי')} את ההטבה!"
    # הקצבה הצפויה (הנתון הראשון בטבלא א) — לטקסט הפתיחה
    sections["_pension_at_67"] = analysis.get("pension_at_67", 0) or 0
    return sections, None


# ─── Topics menu helpers (WhatsApp text fallback) ───

def build_topics_menu(unread: list[str]) -> str:
    lines = ["הניתוח מוכן! שלח/י מספר לבחירת נושא:"]
    idx = 1
    for k in TOPIC_KEYS:
        if k in unread:
            lines.append(f"  {idx}. {TOPIC_LABELS[k]}")
            idx += 1
    return "\n".join(lines)


def menu_index_to_key(inp: str, unread: list[str]) -> str | None:
    """ממיר קלט מספרי לkeyname."""
    try:
        n = int(inp.strip())
        available = [k for k in TOPIC_KEYS if k in unread]
        if 1 <= n <= len(available):
            return available[n - 1]
    except ValueError:
        pass
    # נסה match ישיר
    for k in unread:
        if TOPIC_LABELS[k] in inp or k in inp:
            return k
    return None


def cta_messages() -> list[str]:
    return [
        f"🔗 יש עדיין שאלות על הדוח? תן ליועץ להסביר:\n{ADVISOR_URL}",
        f"🔗 רוצה לקבל הסבר על ייעוץ פנסיוני מלא:\n{ADVISOR_EXPLAIN_URL}",
    ]


# ─── Webhook ───

@app.route("/webhook", methods=["POST"])
def webhook():
    incoming = request.values.get("Body", "").strip()
    from_num = request.values.get("From", "")
    to_num   = request.values.get("To", "")
    num_media= int(request.values.get("NumMedia", 0))

    session = get_session(from_num)
    resp    = MessagingResponse()
    state   = session["state"]

    # ── WELCOME ──────────────────────────────────────────────────────
    if state == "welcome":
        resp.message(
            "שלום! אני רובייקטיבי 🤖 — בוט שמנתח דוחות פנסיה בצורה אובייקטיבית."
        )
        # שליחת תמונת רישיון
        license_msg = resp.message()
        license_msg.media("https://ahituvbiz.github.io/robjectivi-landing/license.png")
        resp.message(
            f"לפני שנתחיל, יש לאשר את תנאי השימוש.\n"
            f"קרא/י את התנאים ושלח/י את ההודעה שבתחתית הדף:\n{TERMS_URL}"
        )
        session["state"] = "consent"
        session["failed_attempts"] = 0
        return str(resp)

    # ── CONSENT ──────────────────────────────────────────────────────
    if state == "consent":
        if incoming == CONSENT_TEXT:
            session["consent_ts"]      = datetime.now()
            session["failed_attempts"] = 0
            session["state"]           = "gender"
            resp.message("תודה על האישור! ✅")
            resp.message("כדי לנתח את הדוח בצורה מדויקת, מה המין שלך?\nשלח *1* לגבר או *2* לאישה")
        else:
            session["failed_attempts"] += 1
            if session["failed_attempts"] >= 3:
                session["state"]           = "welcome"
                session["failed_attempts"] = 0
                resp.message(
                    "לא קיבלתי אישור תקין. יש ללחוץ על הכפתור בתחתית דף התנאים כדי לשלוח את האישור."
                )
                # Restart
                resp.message("שלום! אני רובייקטיבי 🤖 — בוט שמנתח דוחות פנסיה בצורה אובייקטיבית.")
                resp.message(f"לאישור תנאי שימוש:\n{TERMS_URL}")
                session["state"] = "consent"
            else:
                resp.message(
                    "לא קיבלתי אישור תקין לתנאי השימוש. יש ללחוץ על הכפתור בתחתית דף התנאים כדי לשלוח את האישור."
                )
        return str(resp)

    # ── GENDER ───────────────────────────────────────────────────────
    if state == "gender":
        if incoming in ["1", "גבר", "זכר"]:
            session["gender"] = "גבר"
        elif incoming in ["2", "אישה", "נקבה", "אשה"]:
            session["gender"] = "אשה"
        else:
            resp.message("לא הבנתי. שלח *1* לגבר או *2* לאישה")
            return str(resp)
        session["state"]           = "marital"
        session["failed_attempts"] = 0
        resp.message(
            "מה המצב המשפחתי שלך?\n"
            "*1* רווק/ה\n*2* נשוי/אה\n*3* גרוש/ה\n*4* אלמן/ה"
        )
        return str(resp)

    # ── MARITAL ──────────────────────────────────────────────────────
    if state == "marital":
        mm = {
            "1": "רווק/ה", "רווק": "רווק/ה", "רווקה": "רווק/ה",
            "2": "נשוי/אה", "נשוי": "נשוי/אה", "נשואה": "נשוי/אה",
            "3": "גרוש/ה", "גרוש": "גרוש/ה", "גרושה": "גרוש/ה",
            "4": "אלמן/ה", "אלמן": "אלמן/ה", "אלמנה": "אלמן/ה",
        }
        matched = mm.get(incoming.lower())
        if not matched:
            resp.message("לא הבנתי.\n*1* רווק/ה\n*2* נשוי/אה\n*3* גרוש/ה\n*4* אלמן/ה")
            return str(resp)
        session["marital_status"]  = matched
        session["failed_attempts"] = 0
        if matched in ("גרוש/ה", "אלמן/ה"):
            session["state"] = "children"
            resp.message("האם יש לך ילדים מתחת לגיל 21?\nשלח *כן* או *לא*")
        else:
            session["state"] = "awaiting_pdf"
            resp.message("תודה! כעת שלח/י אלי את קובץ ה-PDF של דוח קרן הפנסיה ואנתח אותו עבורך.")
        return str(resp)

    # ── CHILDREN ─────────────────────────────────────────────────────
    if state == "children":
        if incoming in ["כן", "yes", "1"]:
            session["has_minor_children"] = True
        elif incoming in ["לא", "no", "2"]:
            session["has_minor_children"] = False
        else:
            resp.message("שלח *כן* או *לא*")
            return str(resp)
        session["state"]           = "awaiting_pdf"
        session["failed_attempts"] = 0
        resp.message("תודה! כעת שלח/י אלי את קובץ ה-PDF של דוח קרן הפנסיה ואנתח אותו עבורך.")
        return str(resp)

    # ── AWAITING PDF ─────────────────────────────────────────────────
    if state == "awaiting_pdf":
        if num_media == 0:
            session["failed_attempts"] += 1
            if session["failed_attempts"] >= 3:
                session["state"]           = "welcome"
                session["failed_attempts"] = 0
                resp.message("לא הצלחתי לקבל קובץ PDF. אפשר להתחיל מחדש — פשוט שלח/י הודעה כלשהי.")
            else:
                resp.message("כדי שאוכל לנתח, אני צריך לקבל קובץ PDF של דוח קרן הפנסיה. שלח/י את הקובץ בבקשה.")
            return str(resp)

        media_url    = request.values.get("MediaUrl0", "")
        content_type = request.values.get("MediaContentType0", "")
        if "pdf" not in content_type.lower():
            resp.message("הקובץ שנשלח אינו PDF. שלח/י את הדוח בפורמט PDF בלבד.")
            return str(resp)

        # בדיקת מכסה
        if not quota_ok(session):
            resp.message(
                f"הגעת למכסה של {MAX_WEEKLY_REPORTS} דוחות לשבוע. "
                "אפשר לשלוח דוחות נוספים החל מיום ראשון הקרוב."
            )
            for cta in cta_messages():
                send_wa(from_num, to_num, cta)
            return str(resp)

        # שלח "מנתח"
        resp.message("קיבלתי את הדוח, מנתח... ⏳")
        session["state"] = "processing"

        # הורד PDF
        pdf_bytes = download_twilio_media(media_url)
        if not pdf_bytes:
            send_wa(from_num, to_num, "שגיאה בהורדת הקובץ. נסה לשלוח שוב.")
            session["state"] = "awaiting_pdf"
            return str(resp)
        if len(pdf_bytes) / 1024 > MAX_PDF_SIZE_KB:
            send_wa(from_num, to_num, f"הקובץ גדול מדי. דוח פנסיה רגיל שוקל עד {MAX_PDF_SIZE_KB}KB.")
            session["state"] = "awaiting_pdf"
            return str(resp)

        # נתח
        user_profile = {
            "gender":             session.get("gender", "גבר"),
            "marital_status":     session.get("marital_status", "נשוי/אה"),
            "has_minor_children": session.get("has_minor_children", False),
        }
        sections, error = analyze_pdf(pdf_bytes, user_profile)

        if error:
            send_wa(from_num, to_num, error)
            session["state"] = "awaiting_pdf"
            return str(resp)

        # שמור ניתוח
        refresh_quota(session)
        session["reports_week"] += 1
        session["analysis"]     = sections
        session["topics_read"]  = []
        session["state"]        = "results_menu"

        # שלח הקדמה לפני התפריט
        pension_val = sections.get("_pension_at_67", 0)
        if pension_val:
            intro = (
                f"לפני שנתחיל אני רוצה לענות על השאלה הכי נפוצה – "
                f"₪{format_number(pension_val)} זו לגמרי לא הפנסיה שצפויה לך.\n"
                f"הנתון הזה חושב בהנחה שלא תעבוד יותר עד גיל 67 ושהתשואה תהיה כ4%.\n"
                f"מכיוון שזה תרחיש לא סביר, המספר הזה חסר משמעות. תתעלם ממנו!"
            )
            send_wa(from_num, to_num, intro)

        # שלח תפריט
        unread = [k for k in TOPIC_KEYS if k not in session["topics_read"]]
        send_wa(from_num, to_num, build_topics_menu(unread))
        return str(resp)

    # ── RESULTS MENU ─────────────────────────────────────────────────
    if state == "results_menu":
        unread = [k for k in TOPIC_KEYS if k not in session["topics_read"]]
        if not unread:
            session["state"] = "post_analysis"
            resp.message("רוצה לשלוח דוח נוסף לניתוח?\nשלח *כן* או *לא*")
            return str(resp)

        key = menu_index_to_key(incoming, unread)
        if not key:
            resp.message(build_topics_menu(unread))
            return str(resp)

        # הצג נושא
        session["topics_read"].append(key)
        sec = session["analysis"].get(key, {})
        title = sec.get("title", "")
        text  = sec.get("text", "")
        resp.message(f"{title}\n\n{text}")

        # תמונת מדדי מניות / S&P 500
        if key == "investment" and sec.get("show_equity_image"):
            send_wa_media(from_num, to_num, EQUITY_IMAGE_URL)

        # הערת עובד מדינה
        gov = session["analysis"].get("_gov_note")
        if gov and key == "investment":
            send_wa(from_num, to_num, gov)

        # עדכן unread
        unread_after = [k for k in TOPIC_KEYS if k not in session["topics_read"]]
        if not unread_after:
            session["state"] = "post_analysis"
            send_wa(from_num, to_num, "רוצה לשלוח דוח נוסף לניתוח?\nשלח *כן* או *לא*")
        else:
            session["state"] = "results_view"
            idx = 1
            lines = ["רוצה לקרוא נושא נוסף?"]
            for k in TOPIC_KEYS:
                if k in unread_after:
                    lines.append(f"  {idx}. {TOPIC_LABELS[k]}")
                    idx += 1
            lines.append(f"  {idx}. סיימתי")
            send_wa(from_num, to_num, "\n".join(lines))
        return str(resp)

    # ── RESULTS VIEW ─────────────────────────────────────────────────
    if state == "results_view":
        unread = [k for k in TOPIC_KEYS if k not in session["topics_read"]]

        # "סיימתי" — הספרה הבאה אחרי הנושאים
        done_idx = str(len(unread) + 1)
        if incoming.strip() == done_idx or "סיימתי" in incoming:
            session["state"] = "post_analysis"
            resp.message("רוצה לשלוח דוח נוסף לניתוח?\nשלח *כן* או *לא*")
            return str(resp)

        key = menu_index_to_key(incoming, unread)
        if not key:
            idx = 1
            lines = ["רוצה לקרוא נושא נוסף?"]
            for k in TOPIC_KEYS:
                if k in unread:
                    lines.append(f"  {idx}. {TOPIC_LABELS[k]}")
                    idx += 1
            lines.append(f"  {idx}. סיימתי")
            resp.message("\n".join(lines))
            return str(resp)

        session["topics_read"].append(key)
        sec   = session["analysis"].get(key, {})
        title = sec.get("title", "")
        text  = sec.get("text", "")
        resp.message(f"{title}\n\n{text}")

        # תמונת מדדי מניות / S&P 500
        if key == "investment" and sec.get("show_equity_image"):
            send_wa_media(from_num, to_num, EQUITY_IMAGE_URL)

        # עובד מדינה
        gov = session["analysis"].get("_gov_note")
        if gov and key == "investment":
            send_wa(from_num, to_num, gov)

        unread_after = [k for k in TOPIC_KEYS if k not in session["topics_read"]]
        if not unread_after:
            session["state"] = "post_analysis"
            send_wa(from_num, to_num, "רוצה לשלוח דוח נוסף לניתוח?\nשלח *כן* או *לא*")
        else:
            idx = 1
            lines = ["רוצה לקרוא נושא נוסף?"]
            for k in TOPIC_KEYS:
                if k in unread_after:
                    lines.append(f"  {idx}. {TOPIC_LABELS[k]}")
                    idx += 1
            lines.append(f"  {idx}. סיימתי")
            send_wa(from_num, to_num, "\n".join(lines))
        return str(resp)

    # ── POST ANALYSIS ─────────────────────────────────────────────────
    if state == "post_analysis":
        if incoming in ["כן", "yes", "1"]:
            if not quota_ok(session):
                resp.message(
                    f"הגעת למכסה של {MAX_WEEKLY_REPORTS} דוחות לשבוע. "
                    "אפשר לשלוח דוחות נוספים החל מיום ראשון הקרוב."
                )
                for cta in cta_messages():
                    send_wa(from_num, to_num, cta)
                return str(resp)
            if consent_expired(session):
                # יותר משעה — חזרה לתחילה
                session["state"]           = "consent"
                session["failed_attempts"] = 0
                resp.message("שלום! אני רובייקטיבי 🤖 — בוט שמנתח דוחות פנסיה בצורה אובייקטיבית.")
                resp.message(f"לפני שנתחיל, יש לאשר שוב את תנאי השימוש:\n{TERMS_URL}")
            else:
                # פחות משעה — חזרה לשאלת מין
                session["state"]            = "gender"
                session["analysis"]         = None
                session["topics_read"]      = []
                session["failed_attempts"]  = 0
                resp.message("כדי לנתח את הדוח בצורה מדויקת, מה המין שלך?\nשלח *1* לגבר או *2* לאישה")
        elif incoming in ["לא", "no", "2"]:
            session["state"] = "welcome"
            resp.message("תודה שהשתמשת ברובייקטיבי! אם תרצה לנתח דוח נוסף בעתיד, פשוט שלח/י הודעה.")
            for cta in cta_messages():
                send_wa(from_num, to_num, cta)
        else:
            resp.message("שלח *כן* או *לא*")
        return str(resp)

    # fallback
    session["state"] = "welcome"
    resp.message("שלום! שלח/י הודעה כלשהי כדי להתחיל.")
    return str(resp)


# ─── Health check ───
@app.route("/", methods=["GET"])
def health():
    return "רובייקטיבי WhatsApp Bot is running! 🤖"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
