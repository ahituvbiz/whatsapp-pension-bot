"""
רובייקטיבי - WhatsApp Bot (Flask + Twilio)
===========================================
Converted from Streamlit app to Flask webhook for WhatsApp via Twilio.

Flow:
1. User sends "שלום" or any message → bot asks for gender + marital status
2. User answers → bot asks for PDF
3. User sends PDF → bot analyzes and returns results as text messages
"""

import os
import json
import re
import base64
import requests
import anthropic
from collections import defaultdict
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
)

app = Flask(__name__)

# ─── Configuration ───
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
MAX_PDF_SIZE_KB = 400

# ─── In-memory session store ───
# Key: WhatsApp phone number, Value: dict with conversation state
sessions = {}


def get_session(phone):
    if phone not in sessions:
        sessions[phone] = {
            "state": "welcome",  # welcome → gender → marital → kids → waiting_pdf → done
            "gender": None,
            "marital_status": None,
            "has_minor_children": False,
        }
    return sessions[phone]




# ─── Anthropic API Call ───

def call_anthropic(pdf_bytes):
    if not ANTHROPIC_API_KEY:
        return None, "מפתח API לא הוגדר."

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": USER_PROMPT},
                ],
            }],
        )
    except anthropic.BadRequestError as e:
        error_msg = str(e.message) if hasattr(e, 'message') else str(e)
        if "credit balance" in error_msg.lower():
            return None, "שגיאה: אין מספיק קרדיט ב-API."
        return None, "שגיאה בעיבוד הדוח. נסה שוב."
    except anthropic.APIError:
        return None, "שגיאה בתקשורת עם שרת ה-AI. נסה שוב מאוחר יותר."

    text = "".join(block.text for block in message.content if hasattr(block, "text"))
    clean = text.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(clean), None
    except json.JSONDecodeError:
        return None, "שגיאה בפענוח התשובה מה-AI. נסה שוב."




def build_fee_text(data, analysis, user_profile):
    """Build fee analysis as text for WhatsApp."""
    lines = []
    gender = user_profile.get("gender", "גבר")

    closing_balance = analysis.get("closing_balance", 0)
    if not closing_balance or closing_balance <= 0:
        return "אין מספיק נתונים לבחינת דמי ניהול."

    deposit_fee, savings_fee = extract_fee_rates(data)
    if deposit_fee == 0 and savings_fee == 0:
        return "לא נמצאו נתוני דמי ניהול בדוח."

    total_deposits = analysis.get("total_deposits", 0)
    report_period = analysis.get("report_period", "")
    if "רבעון 1" in report_period or "רבעון ראשון" in report_period:
        annual_deposit = total_deposits * 4
    elif "רבעון 2" in report_period or "רבעון שני" in report_period:
        annual_deposit = total_deposits * 2
    elif "רבעון 3" in report_period or "רבעון שלישי" in report_period:
        annual_deposit = round(total_deposits * 4 / 3)
    else:
        annual_deposit = total_deposits

    monthly_deposit = annual_deposit / 12
    avg_savings = closing_balance * 1.02 + 6 * monthly_deposit
    current_fee = calc_annual_fee(deposit_fee, savings_fee, annual_deposit, avg_savings)
    max_fee = calc_annual_fee(MAX_FEES[0], MAX_FEES[1], annual_deposit, avg_savings)

    all_options = []
    for fund_name, plans in FUND_PLANS.items():
        for dep, sav in plans:
            fee = calc_annual_fee(dep, sav, annual_deposit, avg_savings)
            all_options.append((fund_name, dep, sav, fee))

    adv_fee = calc_annual_fee(ADVISOR_PLAN[0], ADVISOR_PLAN[1], annual_deposit, avg_savings)
    all_fees = [current_fee] + [o[3] for o in all_options] + [adv_fee]
    cheapest_fee = min(all_fees)

    lines.append(f"דמי הניהול שלך: {deposit_fee}% מהפקדה + {savings_fee}% מצבירה")
    lines.append(f"עלות שנתית צפויה: ₪{format_number(round(current_fee))}")

    all_options.sort(key=lambda x: x[3])
    cheapest_fund_fee = all_options[0][3] if all_options else None
    cheapest_fund_dep = all_options[0][1] if all_options else None
    cheapest_fund_sav = all_options[0][2] if all_options else None
    cheapest_fund_names = list(set(o[0] for o in all_options if abs(o[3] - cheapest_fund_fee) < 1)) if cheapest_fund_fee else []

    advisor_is_cheapest = adv_fee < cheapest_fund_fee if cheapest_fund_fee else False
    max_saving = current_fee - cheapest_fee

    if current_fee <= cheapest_fee * 1.05 or max_saving < 100:
        lines.append("✅ דמי הניהול שלך ברמה תחרותית מאוד!")
    elif advisor_is_cheapest and adv_fee < current_fee:
        fund_saving = current_fee - cheapest_fund_fee
        adv_saving = current_fee - adv_fee
        fund_names_str = " / ".join(cheapest_fund_names)
        lines.append(f"💡 בקרן הפנסיה של {fund_names_str} {g(gender, 'תוכל', 'תוכלי')} לקבל דמי ניהול של {cheapest_fund_dep}% מהפקדה + {cheapest_fund_sav}% מצבירה.")
        lines.append(f"{g(gender, 'תוכל', 'תוכלי')} לחסוך בשנה הבאה בערך ₪{format_number(round(fund_saving))}.")
        lines.append(f"💡 יועץ פנסיוני יוכל להשיג לך דמי ניהול של 1% על ההפקדה ו-0.145% מהצבירה. כך {g(gender, 'תחסוך', 'תחסכי')} בשנה הבאה בערך ₪{format_number(round(adv_saving))}.")
    elif cheapest_fund_fee is not None and cheapest_fund_fee < current_fee:
        fund_saving = current_fee - cheapest_fund_fee
        fund_names_str = " / ".join(cheapest_fund_names)
        lines.append(f"💡 בקרן הפנסיה של {fund_names_str} {g(gender, 'תוכל', 'תוכלי')} לקבל דמי ניהול של {cheapest_fund_dep}% מהפקדה + {cheapest_fund_sav}% מצבירה.")
        lines.append(f"{g(gender, 'תוכל', 'תוכלי')} לחסוך בשנה הבאה בערך ₪{format_number(round(fund_saving))}.")
        if adv_fee < current_fee:
            adv_saving = current_fee - adv_fee
            lines.append(f"💡 יועץ פנסיוני יוכל להשיג לך דמי ניהול של 1% על ההפקדה ו-0.145% מהצבירה. כך {g(gender, 'תחסוך', 'תחסכי')} בשנה הבאה בערך ₪{format_number(round(adv_saving))}.")

    # Menorah warning
    fund_name_str = analysis.get("fund_name", "")
    if "מנורה" in fund_name_str:
        actuarial_cost = get_movement_value(data.get("movements", []), "אקטוארי")
        lines.append(f"\n⚠️ {g(gender, 'שים', 'שימי')} לב, בדוח מופיע שהורידו לך ₪{format_number(round(actuarial_cost))} בתקופת הדוח בגלל הגרעון האקטוארי של הקרן.")
        lines.append("קרן הפנסיה של מנורה היא באופן כמעט עקבי עם האיזון האקטוארי הגרוע ביותר.")
        lines.append(f"{g(gender, 'שקול', 'שיקלי')} לעבור לקרן פנסיה אחרת.")

    return "\n".join(lines)




def build_investment_text(data, analysis, user_profile):
    """Build investment analysis as text for WhatsApp."""
    lines = []
    gender = user_profile.get("gender", "גבר")

    tracks = data.get("investment_tracks", [])
    if not tracks:
        return "אין נתוני מסלולי השקעה בדוח."

    lines.append("מסלולי ההשקעה שלך:")
    for row in tracks:
        lines.append(f"  • {row.get('track_name', '')} — {row.get('return_rate', '')}")

    age = analysis.get("estimated_age")
    if not age:
        lines.append("לא ניתן לחשב גיל משוער לצורך ניתוח.")
        return "\n".join(lines)

    fund_name = analysis.get("fund_name", "")
    fund_key = find_fund_key(fund_name)
    multiplier = 196 if gender == "גבר" else 194

    user_track_names = [t.get("track_name", "") for t in tracks]
    equity_tracks_list = []
    non_equity_tracks = []
    has_sp500 = False
    has_madedei = False
    has_age_track = False
    has_halacha = False

    for name in user_track_names:
        name_lower = name.lower().strip()
        if "s&p" in name_lower or "s&amp;p" in name_lower or "500" in name_lower:
            has_sp500 = True
        if "מדדי מניות" in name or "עוקב מדדי מניות" in name:
            has_madedei = True
        if "הלכה" in name:
            has_halacha = True
        if fund_key and is_equity_track(name, fund_key):
            equity_tracks_list.append(name)
        else:
            non_equity_tracks.append(name)
        if is_age_related_track(name):
            has_age_track = True

    if age <= 52:
        if non_equity_tracks:
            pension_at_67 = analysis.get("pension_at_67", 0)
            closing_balance = analysis.get("closing_balance", 0)
            years_to_67 = 67 - age
            improved_fv = closing_balance * ((1 + 0.0525) ** years_to_67) if years_to_67 > 0 else closing_balance
            improved_pension = round(improved_fv / multiplier)

            lines.append(f"\n💡 בהתחשב בגילך אני ממליץ {g(gender, 'שתבחר', 'שתבחרי')} במסלול או במסלולים מנייתיים בלבד.")
            lines.append(f"הקצבה מהכספים {g(gender, 'שצברת', 'שצברת')} עד סוף תקופת הדוח היא ₪{format_number(pension_at_67)}.")
            lines.append(f"אם {g(gender, 'תשפר', 'תשפרי')} את התשואה ב-1.25% הקצבה תגדל ל-₪{format_number(improved_pension)}.")

            if equity_tracks_list and has_age_track:
                non_eq_names = ", ".join(non_equity_tracks)
                lines.append(f"\nℹ️ הכסף שלך מפוצל בין מסלול מנייתי לבין מסלול שאיננו מנייתי ({non_eq_names}).")
    else:
        lines.append("\n💡 בהתחשב בשנים שנותרו לך לפרישה יש לבחון איך להתאים את שיעור החשיפה למניות כך שהוא יפחת באופן הדרגתי.")

    if has_sp500:
        lines.append(f"\n⚠️ {g(gender, 'אתה מושקע', 'את מושקעת')} במדד S&P 500. מדד זה סובל מריכוזיות גבוהה ותימחור אופטימי.")

    if has_madedei and fund_key in MADEDEI_WARNING_FUNDS:
        lines.append("\n⚠️ מסלול מדדי מניות הוא מסלול עם ריכוז גבוה וסיכון שלא בטוח שמתאים לכספי פנסיה.")

    if has_halacha and age <= 52 and fund_key != "אינפיניטי":
        lines.append(f"\n⚠️ במסלול ההלכה בקרן שלך החשיפה למניות איננה מקסימלית. {g(gender, 'שקול', 'שיקלי')} לעבור למסלול הלכה עם חשיפה מנייתית מקסימלית.")

    if age <= 52 and fund_key and EQUITY_TRACKS[fund_key]["recommendation"]:
        lines.append(f"\n🤖 המסלול המומלץ בקרן שלך:\n{EQUITY_TRACKS[fund_key]['recommendation']}")

    return "\n".join(lines)


# ─── Deposit Analysis ───

def build_deposit_text(data, analysis, user_profile):
    """Build deposit analysis as text for WhatsApp."""
    lines = []
    gender = user_profile.get("gender", "גבר")
    deposit_source = detect_deposit_source(data)

    deposits = data.get("deposits", [])
    late_deposits = data.get("late_deposits", [])
    all_deposits = deposits + late_deposits

    if not all_deposits:
        return "אין נתוני הפקדות להצגה."

    report_period = data.get("header", {}).get("report_period", "")
    report_date = data.get("header", {}).get("report_date", "")
    report_year = None
    year_match = re.search(r'20\d{2}', report_period)
    if year_match:
        report_year = int(year_match.group())
    elif report_date:
        year_match = re.search(r'20\d{2}', report_date)
        if year_match:
            report_year = int(year_match.group())

    if not report_year:
        return "לא ניתן לזהות את שנת הדוח."

    value_field = "total" if deposit_source == "עצמאי" else "salary"
    monthly_salaries = defaultdict(list)

    for dep in all_deposits:
        sm = dep.get("salary_month", "")
        value = dep.get(value_field) or dep.get("salary") or dep.get("total")
        if not value:
            continue
        if not sm:
            dd = dep.get("deposit_date", "")
            if dd:
                try:
                    parts = dd.replace(".", "/").split("/")
                    if len(parts) == 3:
                        sm = f"{parts[1]}/{parts[2]}"
                except Exception:
                    pass
        if not sm:
            continue
        try:
            parts = sm.split("/")
            if len(parts) == 2:
                month_num = int(parts[0])
                year_num = int(parts[1])
                if year_num < 100:
                    year_num += 2000
                if year_num == report_year and 1 <= month_num <= 12:
                    monthly_salaries[month_num].append(float(value))
        except (ValueError, IndexError):
            continue

    if not monthly_salaries:
        return "אין נתוני הפקדות לשנת הדוח."

    # Period end month
    period_end_month = 12
    if "רבעון 1" in report_period or "רבעון ראשון" in report_period:
        period_end_month = 3
    elif "רבעון 2" in report_period or "רבעון שני" in report_period:
        period_end_month = 6
    elif "רבעון 3" in report_period or "רבעון שלישי" in report_period:
        period_end_month = 9

    display_months = list(range(1, period_end_month + 1))
    all_totals = [sum(monthly_salaries.get(m, [])) for m in display_months if monthly_salaries.get(m)]

    if all_totals:
        avg_val = sum(all_totals) / len(all_totals)
        months_with_deposits = len(all_totals)
        total_expected = len(display_months)
        avg_label = "הפקדה ממוצעת" if deposit_source == "עצמאי" else "שכר ממוצע"
        lines.append(f"{avg_label}: ₪{format_number(round(avg_val))}")
        lines.append(f"חודשים עם הפקדות בשנת {report_year}: {months_with_deposits}/{total_expected}")

    expected_months = list(range(1, period_end_month))
    missing_expected = [m for m in expected_months if not monthly_salaries.get(m)]
    if missing_expected:
        missing_str = ", ".join(str(m) for m in missing_expected)
        if deposit_source == "עצמאי":
            lines.append(f"\n⚠️ לא נמצאו הפקדות עבור חודשים: {missing_str}. {g(gender, 'וודא', 'וודאי')} שלא ביצעת הפקדות שלא נקלטו.")
        else:
            lines.append(f"\n⚠️ לא נמצאו הפקדות עבור חודשים: {missing_str}. יש לוודא שהמעסיק הפקיד את כל ההפקדות.")

    # Low deposit rate warning
    deposit_rate_val = analysis.get("deposit_rate", 0)
    can_calc_val = analysis.get("can_calc_income", False)
    if can_calc_val and deposit_source == "שכיר" and deposit_rate_val < 18.48:
        lines.append(f"\n⚠️ {g(gender, 'שים', 'שימי')} לב, שיעור ההפקדות מתוך השכר נראה נמוך מהמינימום לפי חוק (6%+6.5%+6%).")
        lines.append(f"ייתכן שמופקד לך לפנסיה גם על החזרי הוצאות. אחרת {g(gender, 'בדוק', 'בדקי')} מה הסיבה.")

    return "\n".join(lines)




# ─── Build Full Analysis Message ───

def build_full_analysis(data, user_profile):
    """Run all analyses and return list of WhatsApp messages."""
    messages = []

    # Validate
    is_valid, validation_error = validate_report(data)
    if not is_valid:
        return [f"⚠️ {validation_error}"]

    deposit_source = detect_deposit_source(data)
    if deposit_source == "שכיר + עצמאי":
        return ["⚠️ עדיין לא למדתי לנתח דוח פנסיה שבוצעו אליה גם הפקדות כשכיר וגם הפקדות כעצמאי."]

    analysis = compute_analysis(data, user_profile)
    gender = user_profile.get("gender", "גבר")

    # ── Message 1: Insurance ──
    ins_lines = ["🛡️ *בחינת הכיסויים הביטוחיים בקרן*\n"]

    age = analysis.get("estimated_age")
    if age:
        ins_lines.append(f"גיל משוער: {age:.0f}")

    if analysis.get("can_calc_income"):
        income = analysis["insured_income"]
        rate = analysis["deposit_rate"]
        ins_lines.append(f"הכנסה מבוטחת: ₪{income:,} | שיעור הפקדה: {rate}%")

    warnings = check_insurance(analysis, user_profile)
    if warnings:
        for icon, msg in warnings:
            ins_lines.append(f"\n{icon} {msg}")
        disability_val = analysis.get("disability_pension", 0)
        if disability_val > 0:
            ins_lines.append(f"\nבמקרה של נכות של 75% ומעלה הקרן תשלם קצבה חודשית של ₪{format_number(disability_val)}.")
    else:
        spouse_word = "לאשתך" if gender == "גבר" else "לבעלך"
        spouse_val = format_number(analysis.get("spouse_pension", 0))
        orphan_val = format_number(analysis.get("orphan_pension", 0))
        total_survivors = (analysis.get("spouse_pension", 0) or 0) + (analysis.get("orphan_pension", 0) or 0)
        disability_val = format_number(analysis.get("disability_pension", 0))
        nimtsa = g(gender, "נמצא", "נמצאת")

        ins_lines.append(f"\n✅ מהדוח נראה ש{g(gender, 'אתה', 'את')} {nimtsa} במסלול ביטוח עם כיסויים מקסימליים.")
        ins_lines.append(f"הקרן מבטיחה {spouse_word} קצבה של ₪{spouse_val} לכל החיים.")
        ins_lines.append(f"בנוסף ₪{orphan_val} לחודש עד שהילד הקטן יגיע ל-21, סה\"כ ₪{format_number(total_survivors)}.")
        ins_lines.append(f"במקרה של נכות של 75% ומעלה: קצבה חודשית של ₪{disability_val}.")

    insured_income = analysis.get("insured_income", 0)
    if insured_income and insured_income > 20000:
        ins_lines.append("\n💡 מומלץ לערוך בחינה מקיפה של הכיסויים. ייתכן שניתן לצמצם כיסויים ולחסוך. מומלץ להיעזר ביועץ פנסיוני.")

    messages.append("\n".join(ins_lines))

    # ── Message 2: Deposits ──
    dep_text = build_deposit_text(data, analysis, user_profile)
    messages.append(f"💰 *בדיקת הפקדות*\n\n{dep_text}")

    # ── Message 3: Fees ──
    fee_text = build_fee_text(data, analysis, user_profile)
    messages.append(f"🏷️ *בחינת דמי ניהול*\n\n{fee_text}")

    # ── Message 4: Investment ──
    inv_text = build_investment_text(data, analysis, user_profile)
    messages.append(f"📈 *בחינת מסלולי השקעה*\n\n{inv_text}")

    # ── Government employer ──
    employer = data.get("header", {}).get("employer", "")
    if not is_gov_employer(employer):
        for dep in data.get("deposits", []):
            dep_employer = dep.get("employer", "")
            if dep_employer and is_gov_employer(dep_employer):
                employer = dep_employer
                break
    if is_gov_employer(employer):
        messages.append(f"💡 החשב הכללי מעודד עובדי מדינה לקחת ייעוץ פנסיוני אובייקטיבי עם סבסוד של 600 ש\"ח. {g(gender, 'נצל', 'נצלי')} את ההטבה!")

    # ── CTA ──
    messages.append("🗣️ רוצה שיועץ פנסיוני יעבור איתך על הדוח?\nhttps://meshulam.co.il/s/9b71389b-1564-a6c6-5f08-883647e04c53")

    # ── Disclaimer ──
    messages.append("⚠️ הניתוח מבוסס על בינה מלאכותית ועלולות ליפול בו טעויות. אין להסתמך עליו כייעוץ פנסיוני.")

    return messages


# ─── Download PDF from Twilio Media ───

def download_twilio_media(media_url):
    """Download media file from Twilio (requires auth)."""
    response = requests.get(
        media_url,
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
    )
    if response.status_code == 200:
        return response.content
    return None


# ─── Send long message via Twilio REST API (for proactive messages) ───

def send_whatsapp_message(to_number, from_number, body):
    """Send a WhatsApp message using Twilio REST API."""
    client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    client.messages.create(
        body=body,
        from_=from_number,
        to=to_number,
    )


# ─── Webhook Route ───

@app.route("/webhook", methods=["POST"])
def webhook():
    """Handle incoming WhatsApp messages from Twilio."""
    incoming_msg = request.values.get("Body", "").strip()
    from_number = request.values.get("From", "")
    to_number = request.values.get("To", "")
    num_media = int(request.values.get("NumMedia", 0))

    session = get_session(from_number)
    resp = MessagingResponse()

    # ── State Machine ──

    if session["state"] == "welcome":
        resp.message("שלום! 👋 אני רובייקטיבי, רובוט לניתוח דוחות פנסיה.\n\nמה המין שלך?\nשלח *1* לגבר או *2* לאשה")
        session["state"] = "gender"
        return str(resp)

    elif session["state"] == "gender":
        if incoming_msg in ["1", "גבר"]:
            session["gender"] = "גבר"
        elif incoming_msg in ["2", "אשה"]:
            session["gender"] = "אשה"
        else:
            resp.message("לא הבנתי. שלח *1* לגבר או *2* לאשה")
            return str(resp)

        resp.message("מה הסטטוס המשפחתי שלך?\n*1* נשוי/אה\n*2* רווק/ה\n*3* גרוש/ה\n*4* אלמן/ה")
        session["state"] = "marital"
        return str(resp)

    elif session["state"] == "marital":
        marital_map = {
            "1": "נשוי/אה", "נשוי": "נשוי/אה", "נשואה": "נשוי/אה",
            "2": "רווק/ה", "רווק": "רווק/ה", "רווקה": "רווק/ה",
            "3": "גרוש/ה", "גרוש": "גרוש/ה", "גרושה": "גרוש/ה",
            "4": "אלמן/ה", "אלמן": "אלמן/ה", "אלמנה": "אלמן/ה",
        }
        matched = marital_map.get(incoming_msg.lower())
        if not matched:
            resp.message("לא הבנתי. שלח מספר 1-4:\n*1* נשוי/אה\n*2* רווק/ה\n*3* גרוש/ה\n*4* אלמן/ה")
            return str(resp)

        session["marital_status"] = matched

        if matched in ["גרוש/ה", "אלמן/ה"]:
            resp.message("האם יש לך ילדים מתחת לגיל 21?\nשלח *כן* או *לא*")
            session["state"] = "kids"
        else:
            resp.message("מעולה! 📄 שלח לי עכשיו את דוח הפנסיה שלך בפורמט PDF.")
            session["state"] = "waiting_pdf"
        return str(resp)

    elif session["state"] == "kids":
        if incoming_msg in ["כן", "yes", "1"]:
            session["has_minor_children"] = True
        elif incoming_msg in ["לא", "no", "2"]:
            session["has_minor_children"] = False
        else:
            resp.message("שלח *כן* או *לא*")
            return str(resp)

        resp.message("מעולה! 📄 שלח לי עכשיו את דוח הפנסיה שלך בפורמט PDF.")
        session["state"] = "waiting_pdf"
        return str(resp)

    elif session["state"] == "waiting_pdf":
        if num_media == 0:
            resp.message("לא קיבלתי קובץ. שלח לי את דוח הפנסיה בפורמט PDF.")
            return str(resp)

        # Download the PDF
        media_url = request.values.get("MediaUrl0", "")
        content_type = request.values.get("MediaContentType0", "")

        if "pdf" not in content_type.lower():
            resp.message("הקובץ שנשלח אינו PDF. שלח לי את הדוח בפורמט PDF בלבד.")
            return str(resp)

        # Send "analyzing" message
        resp.message("מנתח את הדוח באמצעות AI... ⏳ (עשוי לקחת עד דקה)")

        # Download PDF
        pdf_bytes = download_twilio_media(media_url)
        if not pdf_bytes:
            # Send error via Twilio REST API since we already responded
            send_whatsapp_message(from_number, to_number, "שגיאה בהורדת הקובץ. נסה לשלוח שוב.")
            return str(resp)

        # Check size
        if len(pdf_bytes) / 1024 > MAX_PDF_SIZE_KB:
            send_whatsapp_message(from_number, to_number, "הקובץ גדול מדי. דוח פנסיה רגיל שוקל עד 400KB.")
            return str(resp)

        # Call Anthropic
        data, error = call_anthropic(pdf_bytes)
        if error:
            send_whatsapp_message(from_number, to_number, error)
            return str(resp)

        # Build analysis
        user_profile = {
            "gender": session.get("gender", "גבר"),
            "marital_status": session.get("marital_status", "נשוי/אה"),
            "has_minor_children": session.get("has_minor_children", False),
        }

        analysis_messages = build_full_analysis(data, user_profile)

        # Send each section as a separate message
        for msg_text in analysis_messages:
            send_whatsapp_message(from_number, to_number, msg_text)

        # Allow re-analysis
        session["state"] = "waiting_pdf"
        return str(resp)

    # Fallback: reset
    else:
        session["state"] = "welcome"
        return webhook()


# ─── Health check ───
@app.route("/", methods=["GET"])
def health():
    return "רובייקטיבי WhatsApp Bot is running! 🤖"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
