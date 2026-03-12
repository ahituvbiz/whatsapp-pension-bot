"""
רובייקטיבי - Core Pension Analysis Module
==========================================
Shared logic between streamlit-app/app.py and whatsapp-bot/main.py.
"""

import math
import re
from datetime import datetime, date


def sentences_to_lines(text: str) -> str:
    """הופך כל משפט (נקודה+רווח) לשורה חדשה. מדלג על מספרים עשרוניים."""
    return re.sub(r'\. (?!\d)', '.\n', text)


# ─── System Prompt ───
SYSTEM_PROMPT = """You are an expert at extracting structured data from Israeli pension fund reports (דוחות פנסיה).
Given a PDF of an Israeli pension report, extract ALL data into a strict JSON structure.

Return ONLY valid JSON (no markdown, no backticks, no explanation) with this exact structure:
{
  "header": {
    "report_date": "string",
    "report_period": "string (e.g. שנתי 2024 or רבעון 3 2024)",
    "fund_name": "string",
    "member_name": "string",
    "member_id": "string",
    "employer": "string",
    "report_keywords": ["list of keywords found near the top of the document or before Table A, specifically look for: מפורט, כללית, יסוד, מקיפה, משלימה"]
  },
  "expected_payments": [
    {"label": "string (e.g. קצבה חודשית הצפויה לך בפרישה בגיל 67)", "amount": number_or_null}
  ],
  "movements": [
    {"label": "string", "amount": number}
  ],
  "fees": [
    {"label": "string", "rate": "string (e.g. 1.00%)"}
  ],
  "investment_tracks": [
    {"track_name": "string", "return_rate": "string (e.g. 14.35%)"}
  ],
  "deposits": [
    {
      "employer": "string or null",
      "deposit_date": "string",
      "salary_month": "string",
      "salary": number_or_null,
      "employee_contribution": number,
      "employer_contribution": number,
      "severance": number,
      "total": number
    }
  ],
  "deposits_total": {
    "salary": number_or_null,
    "employee_contribution": number,
    "employer_contribution": number,
    "severance": number,
    "total": number
  },
  "late_deposits": [
    {
      "employer": "string or null",
      "deposit_date": "string",
      "salary_month": "string",
      "salary": number_or_null,
      "employee_contribution": number,
      "employer_contribution": number,
      "severance": number,
      "total": number
    }
  ]
}

Rules:
- Extract ALL rows, do not skip any deposit rows
- IMPORTANT: Preserve the sign of amounts exactly as shown. Use NEGATIVE numbers for losses, deductions, fees, and insurance costs (e.g. -1234). Use positive numbers for gains, deposits, and balances. The minus sign is critical information
- If a field is missing or shows "-", use null
- salary_month format: "MM/YYYY"
- deposit_date format: "DD/MM/YYYY"
- Keep Hebrew text exactly as shown
- For movements section, include opening balance, deposits, returns/losses, fees, insurance costs, actuarial adjustments, and closing balance
- For fees, include deposit fee %, savings fee %, and investment expenses % if shown
- IMPORTANT: Some reports have deposits made AFTER the reporting period ended (פירוט הפקדות שהופקדו לאחר תום השנה/הרבעון). These must go in "late_deposits", NOT in "deposits". The "deposits_total" should only sum the regular deposits (matching the amount in movements section under "כספים שהופקדו לקרן")
- In deposits_total, include a "salary" field that sums ALL salary values from the regular deposits
- If there are no late deposits, set "late_deposits" to an empty array []"""

USER_PROMPT = "Extract all data from this Israeli pension report into the JSON structure specified. Return ONLY the JSON."


# ─── Helper Functions ───

def format_number(n):
    """Format number with Hebrew locale style, preserve negatives."""
    if n is None or n == "":
        return "—"
    try:
        num = float(n)
        negative = num < 0
        abs_num = abs(num)
        if abs_num == int(abs_num):
            formatted = f"{int(abs_num):,}"
        else:
            formatted = f"{abs_num:,.2f}"
        return f"-{formatted}" if negative else formatted
    except (ValueError, TypeError):
        return "—"


def g(gender, male, female):
    """Return male or female form based on gender."""
    return female if gender == "אשה" else male


def get_payment_value(payments, keyword):
    """Find a payment amount by matching Hebrew keyword in the label."""
    for p in payments:
        if keyword in p.get("label", ""):
            return p.get("amount") or 0
    return 0


def get_movement_value(movements, keyword):
    """Find a movement amount by matching Hebrew keyword in the label."""
    for m in movements:
        if keyword in m.get("label", ""):
            return abs(m.get("amount") or 0)
    return 0


# ─── Report Validation ───

def detect_deposit_source(data):
    """Auto-detect if deposits are from employee, self-employed, or both.
    Returns: 'שכיר', 'עצמאי', or 'שכיר + עצמאי'"""
    deposits = data.get("deposits", []) + data.get("late_deposits", [])
    if not deposits:
        return "שכיר"  # default

    has_employer = False
    has_self = False

    for dep in deposits:
        employer_contrib = dep.get("employer_contribution", 0) or 0
        employee_contrib = dep.get("employee_contribution", 0) or 0
        if employer_contrib != 0:
            has_employer = True
        elif employee_contrib != 0:
            # Has employee contribution but no employer contribution = עצמאי
            has_self = True

    if has_employer and has_self:
        return "שכיר + עצמאי"
    elif has_self:
        return "עצמאי"
    else:
        return "שכיר"


def validate_report(data):
    """Validate that this is a condensed comprehensive pension fund report.
    Returns (is_valid, error_message) tuple."""

    header = data.get("header", {})
    keywords = [kw.lower() if isinstance(kw, str) else "" for kw in header.get("report_keywords", [])]
    fund_name = header.get("fund_name", "").lower()

    # Check for detailed (non-condensed) report
    if any("מפורט" in kw for kw in keywords):
        return False, "הרובוט לא יודע לנתח אלא דוחות מקוצרים של קרן פנסיה מקיפה."

    # Check for supplementary pension fund (כללית / יסוד)
    all_text = " ".join(keywords) + " " + fund_name
    if "כללית" in all_text or "יסוד" in all_text or "משלימה" in all_text:
        return False, "הרובוט לא למד לנתח דוחות שאינם של קרן פנסיה מקיפה."

    # Check Table A has at least 6 rows (including rows with null/zero amounts, e.g. waived survivors)
    payments = data.get("expected_payments", [])
    if len(payments) < 6:
        return False, "הרובוט לא למד לנתח דוחות שאינם של קרן פנסיה מקיפה."

    # Check savings fee not above 0.5%
    fees = data.get("fees", [])
    for f in fees:
        label = f.get("label", "")
        if "חיסכון" in label or "צבירה" in label:
            try:
                rate = float(str(f.get("rate", "0")).replace("%", "").strip())
                if rate > 0.5:
                    return False, "הרובוט לא למד לנתח דוחות שאינם של קרן פנסיה מקיפה."
            except (ValueError, TypeError):
                pass

    return True, ""


# ─── Analysis Engine ───

def compute_analysis(data, user_profile):
    """Compute all analysis values from pension data."""

    analysis = {}

    payments = data.get("expected_payments", [])
    movements = data.get("movements", [])
    deposits_total = data.get("deposits_total", {})
    header = data.get("header", {})

    # ── Extract key values from Table A ──
    pension_at_67 = get_payment_value(payments, "פרישה") or get_payment_value(payments, "67")
    spouse_pension = get_payment_value(payments, "אלמן")
    orphan_pension = get_payment_value(payments, "יתום")
    disability_pension = get_payment_value(payments, "נכות מלאה") or get_payment_value(payments, "נכות")
    premium_waiver = get_payment_value(payments, "שחרור")

    # ── Extract from Table B ──
    closing_balance = movements[-1].get("amount", 0) if movements else 0
    death_insurance_cost = get_movement_value(movements, "מוות") or get_movement_value(movements, "שאירים")

    # ── Extract from Table E totals ──
    all_deps = data.get("deposits", []) + data.get("late_deposits", [])
    computed_salary = sum(d.get("salary", 0) or 0 for d in all_deps)
    computed_deposits = sum(d.get("total", 0) or 0 for d in all_deps)

    total_deposits = deposits_total.get("total", 0) or computed_deposits or 0
    total_salary = computed_salary if computed_salary > 0 else (deposits_total.get("salary", 0) or 0)

    # ── Store raw values ──
    analysis["pension_at_67"] = pension_at_67
    analysis["spouse_pension"] = spouse_pension
    analysis["orphan_pension"] = orphan_pension
    analysis["disability_pension"] = disability_pension
    analysis["premium_waiver"] = premium_waiver
    analysis["death_insurance_cost"] = death_insurance_cost
    analysis["closing_balance"] = closing_balance
    analysis["fund_name"] = header.get("fund_name", "")
    analysis["total_deposits"] = total_deposits
    analysis["total_salary"] = total_salary
    analysis["report_period"] = header.get("report_period", "")

    # ── Detect deposit source ──
    analysis["deposit_source"] = detect_deposit_source(data)

    # ── 1. Age Estimate (NPER) ──
    if pension_at_67 and closing_balance and pension_at_67 > 0 and closing_balance > 0:
        try:
            rate = 0.0386
            gender = user_profile.get("gender", "גבר")
            multiplier = 196 if gender == "גבר" else 194
            fv_target = pension_at_67 * multiplier
            nper = math.log(fv_target / closing_balance) / math.log(1 + rate)
            age_at_report = 67 - nper

            # Add elapsed time since report date
            elapsed_years = 0
            report_date_str = header.get("report_date", "")
            try:
                for fmt in ("%d.%m.%Y", "%d/%m/%Y"):
                    try:
                        report_date = datetime.strptime(report_date_str, fmt).date()
                        today = date.today()
                        elapsed_years = (today - report_date).days / 365.25
                        break
                    except ValueError:
                        continue
            except Exception:
                pass

            analysis["estimated_age"] = round(age_at_report + elapsed_years, 1)
            analysis["age_at_report"] = round(age_at_report, 1)
        except (ValueError, ZeroDivisionError):
            pass

    # ── 2. Insured Income ──
    analysis["can_calc_income"] = False
    deposit_source = analysis.get("deposit_source", "שכיר")
    all_deps = data.get("deposits", []) + data.get("late_deposits", [])

    if deposit_source == "שכיר":
        if premium_waiver and premium_waiver > 0:
            insured_deposit = premium_waiver / 0.94
            total_salary = 0
            total_deposits = 0
            for d in all_deps:
                sal = d.get("salary", 0) or 0
                tot = d.get("total", 0) or 0
                if sal > 0 and tot > 0:
                    r = tot / sal
                    if 0.10 < r < 0.30:
                        total_salary += sal
                        total_deposits += tot
            if total_salary > 0:
                deposit_rate = total_deposits / total_salary
                insured_income = insured_deposit / deposit_rate
                analysis["insured_income"] = round(insured_income)
                analysis["insured_deposit"] = round(insured_deposit)
                analysis["deposit_rate"] = round(deposit_rate * 100, 2)
                analysis["can_calc_income"] = True

    elif deposit_source == "עצמאי":
        orphan = analysis.get("orphan_pension", 0)
        disability = analysis.get("disability_pension", 0)
        if premium_waiver and premium_waiver > 0:
            insured_deposit = premium_waiver / 0.94
            opt1 = insured_deposit / 0.16
            disability_ref = (disability / 0.75) if disability > 0 else 0
            orphan_ref = (orphan / 0.40) if orphan > 0 else 0
            opt2 = max(disability_ref, orphan_ref)
            insured_income = max(opt1, opt2)
            if insured_income > 0:
                deposit_rate = insured_deposit / insured_income
                analysis["insured_income"] = round(insured_income)
                analysis["insured_deposit"] = round(insured_deposit)
                analysis["deposit_rate"] = round(deposit_rate * 100, 2)
                analysis["deposit_rate_non_default"] = abs(deposit_rate - 0.16) > 0.005
                analysis["can_calc_income"] = True

    return analysis


# ─── Insurance Checks ───

def check_insurance(analysis, user_profile):
    """Run all insurance coverage checks. Returns list of (type, message) tuples."""
    warnings = []

    orphan = analysis.get("orphan_pension", 0)
    disability = analysis.get("disability_pension", 0)
    spouse = analysis.get("spouse_pension", 0)
    premium_waiver = analysis.get("premium_waiver", 0)
    death_cost = analysis.get("death_insurance_cost", 0)
    fund_name = analysis.get("fund_name", "")
    age = analysis.get("estimated_age", 50)
    insured_income = analysis.get("insured_income", 0)
    can_calc = analysis.get("can_calc_income", False)

    gender = user_profile.get("gender", "גבר")
    marital = user_profile.get("marital_status", "נשוי/אה")
    has_kids = user_profile.get("has_minor_children", False)

    is_male = gender == "גבר"
    is_single = marital == "רווק/ה"
    is_divorced = marital == "גרוש/ה"
    is_widowed = marital == "אלמן/ה"
    is_married = marital == "נשוי/אה"
    no_dependents = is_single or ((is_divorced or is_widowed) and not has_kids)

    ata = g(gender, "אתה", "את")
    nimtsa = g(gender, "נמצא", "נמצאת")
    tsabart = g(gender, "שצברת", "שצברת")
    tukhal = g(gender, "תוכל", "תוכלי")
    shkol = g(gender, "שקול", "שיקלי")
    pne = g(gender, "פנה", "פני")
    tirtse = g(gender, "תרצה", "תרצי")
    titstarekh = g(gender, "תצטרך", "תצטרכי")
    matsbekh = g(gender, "מצבך", "מצבך")
    avurekh = g(gender, "עבורך", "עבורך")

    # ── Check 6: Settled pension - non-selected fund ──
    non_selected_funds = ["מנורה", "הראל", "הפניקס", "כלל", "מקפת"]
    is_non_selected = any(f in fund_name for f in non_selected_funds)

    if is_non_selected and disability == 0:
        warnings.append(("⚠️", f"{g(gender, 'שים', 'שימי')} לב קרן הפנסיה שלך איננה פעילה. הכסף {tsabart} ממשיך לצבור תשואה אך אין לך כיסויים ביטוחיים. אם יש לך קרן פנסיה נוספת {shkol} לאחד אותן. החברה המנהלת רשאית לגבות את דמי ניהול מקסימליים מקרן פנסיה לא פעילה."))
        return warnings

    # ── Check 7: Settled pension - selected fund ──
    selected_funds = ["מיטב", "אלטשולר", "מור", "אינפיניטי"]
    is_selected = any(f in fund_name for f in selected_funds)

    if is_selected and disability == 0:
        warnings.append(("⚠️", f"""{g(gender, 'שים', 'שימי')} לב קרן הפנסיה שלך איננה פעילה. הכסף {tsabart} ממשיך לצבור תשואה אך אין לך כיסויים ביטוחיים. אם יש לך קרן פנסיה נוספת {shkol} לאחד אותן. במידה והצטרפת לקרן כ"קרן נבחרת" דמי הניהול לא יועלו עד 10 שנים מההצטרפות."""))
        return warnings

    # ── Check 2: Waived survivors WITH insurability ──
    if orphan == 0 and death_cost > 0:
        if is_married:
            msg = f"""{g(gender, 'שים', 'שימי')} לב, אין לך כיסוי שארים. עליך לעדכן את קרן הפנסיה ש{g(gender, 'אתה נשוי', 'את נשואה')} בכדי שיפעילו את כיסוי השארים."""
            warnings.append(("🔴", msg))
        else:
            msg = f"""וויתרת על ביטוח שארים וכך הגדלת את הפנסיה העתידית שלך.
הוויתור תקף לשנתיים.
אם {matsbekh} המשפחתי השתנה {g(gender, 'זכור', 'זכרי')} לעדכן את הקרן.
כל עוד הוא לא משתנה כדאי לחדש את הוויתור על כיסוי השארים לפני תום השנתיים."""
            warnings.append(("ℹ️", msg))

    # ── Check 3: Waived survivors WITHOUT insurability ──
    if orphan == 0 and disability > 0 and death_cost == 0:
        if is_married:
            msg = f"""{g(gender, 'שים', 'שימי')} לב, אין לך כיסוי שארים. עליך לעדכן את קרן הפנסיה ש{g(gender, 'אתה נשוי', 'את נשואה')} בכדי שיפעילו את כיסוי השארים."""
            warnings.append(("🔴", msg))
        else:
            msg = f"""{g(gender, 'שים', 'שימי')} לב {ata} {nimtsa} בוויתור על שארים אבל אם {tirtse} לעשות ביטוח שארים {titstarekh} להתחיל מחדש את תקופת האכשרה.
מומלץ לפנות לקרן הפנסיה כבר עכשיו ולבקש לרכוש ברות ביטוח."""
            warnings.append(("🔴", msg))

    # ── Check 4: Single/divorced/widowed without kids - paying survivors ──
    if no_dependents and orphan > 0:
        report_period = analysis.get("report_period", "")
        annual_cost = death_cost
        cost_note = ""
        if death_cost > 0:
            if "רבעון 1" in report_period or "רבעון ראשון" in report_period:
                annual_cost = death_cost * 4
                cost_note = f" (במונחים שנתיים: ₪{format_number(annual_cost)})"
            elif "רבעון 2" in report_period or "רבעון שני" in report_period:
                annual_cost = death_cost * 2
                cost_note = f" (במונחים שנתיים: ₪{format_number(annual_cost)})"
            elif "רבעון 3" in report_period or "רבעון שלישי" in report_period:
                annual_cost = round(death_cost * 4 / 3)
                cost_note = f" (במונחים שנתיים: ₪{format_number(annual_cost)})"
            elif "רבעון 4" in report_period or "רבעון רביעי" in report_period:
                pass  # Q4 = annual, no note needed

            msg = f"""{g(gender, 'שים', 'שימי')} לב – {g(gender, 'לחוסך שאין לו', 'לחוסכת שאין לה')} בן/ת זוג ולא ילדים אין טעם לשלם ביטוח שארים.
בדוח רואים ש{g(gender, 'שילמת', 'שילמת')} ₪{format_number(death_cost)} על ביטוח שארים שהוא מיותר לגמרי {avurekh} (זה ממש כסף שהולך לפח).{cost_note}

מומלץ לפנות לקרן ולבקש לוותר על ביטוח שארים.
הוויתור יהיה תקף לשנתיים, ו{tukhal} לחדש אותו במידה ו{matsbekh} המשפחתי לא ישתנה.
במידה ו{matsbekh} המשפחתי ישתנה בתוך שנתיים אלו, {pne} אל הקרן {g(gender, 'וחדש', 'וחדשי')} את ביטוח השארים.
זה לא ידרוש הצהרת בריאות ולא תקופת אכשרה חדשה."""
            warnings.append(("💡", msg))
        else:
            # death_cost is 0 but orphan > 0 — can't determine actual cost
            msg = f"""{g(gender, 'שים', 'שימי')} לב – {g(gender, 'לחוסך שאין לו', 'לחוסכת שאין לה')} בן/ת זוג ולא ילדים אין טעם לשלם ביטוח שארים.
מומלץ לפנות לקרן ולבקש לוותר על ביטוח שארים. כך {g(gender, 'תגדיל', 'תגדילי')} את הפנסיה העתידית שלך.
הוויתור יהיה תקף לשנתיים, ו{tukhal} לחדש אותו במידה ו{matsbekh} המשפחתי לא ישתנה.
במידה ו{matsbekh} המשפחתי ישתנה בתוך שנתיים אלו, {pne} אל הקרן {g(gender, 'וחדש', 'וחדשי')} את ביטוח השארים.
זה לא ידרוש הצהרת בריאות ולא תקופת אכשרה חדשה."""
            warnings.append(("💡", msg))

    # ── Check 5: Divorced/widowed WITH kids - paying spouse insurance ──
    if (is_divorced or is_widowed) and has_kids and spouse > 0:
        msg = f"""{g(gender, 'שים', 'שימי')} לב {ata} {g(gender, 'משלם', 'משלמת')} ביטוח שארים על בן/ת זוג וזה מיותר לגמרי {avurekh} (זה ממש כסף שהולך לפח).
אם {g(gender, 'תפנה', 'תפני')} אל קרן הפנסיה {g(gender, 'ותבקש', 'ותבקשי')} לוותר על ביטוח השארים לבן/ת זוג {g(gender, 'תגדיל', 'תגדילי')} את הפנסיה העתידית שלך.
הוויתור יהיה תקף לשנתיים, ו{tukhal} לחדש אותו במידה ו{matsbekh} המשפחתי לא ישתנה.
במידה ו{matsbekh} המשפחתי ישתנה בתוך שנתיים אלו, {pne} אל הקרן {g(gender, 'וחדש', 'וחדשי')} את ביטוח השארים.
זה לא ידרוש הצהרת בריאות ולא תקופת אכשרה חדשה."""
        warnings.append(("💡", msg))

    # ── Check 1: Not default (maximum coverage) plan ──
    not_default = False
    if disability > 0 and orphan > 0:
        ratio = orphan / disability
        expected_ratio = 40 / 75
        if abs(ratio - expected_ratio) > 0.02:
            not_default = True
        if is_male and age < 40 and orphan < expected_ratio * disability * 0.98:
            not_default = True
        if spouse < 1.5 * orphan * 0.98:
            not_default = True

    if can_calc and insured_income > 0 and disability > 0:
        threshold = 0.9 * 0.75 * insured_income
        if disability < threshold * 0.98:
            not_default = True

    if not_default:
        warnings.append(("⚠️", f"{g(gender, 'שים', 'שימי')} לב – מסלול הביטוח שבו {ata} {nimtsa} ככל הנראה איננו מסלול הביטוח עם הכיסוי המקסימלי. מומלץ לוודא שגובה הכיסויים לסיכוני הנכות והשארים מספיקים.\nמומלץ להיעזר באיש מקצוע אובייקטיבי שאין לו אינטרס למכור לכם ביטוחים – כלומר ביועץ פנסיוני."))

    return warnings


# ─── Fee Analysis ───

FUND_PLANS = {
    "אלטשולר שחם": [
        (1.0, 0.22), (1.2, 0.20), (1.7, 0.15),
        (2.0, 0.12), (2.2, 0.10), (2.7, 0.05),
    ],
    "מיטב דש": [
        (1.0, 0.22), (1.2, 0.20), (1.7, 0.15), (2.0, 0.12),
    ],
    "אינפיניטי": [
        (1.0, 0.22), (1.2, 0.20),
    ],
    "מור": [
        (1.0, 0.22),
    ],
}
ADVISOR_PLAN = (1.0, 0.145)
MAX_FEES = (6.0, 0.5)


def extract_fee_rates(data):
    """Extract deposit and savings fee rates from fees section."""
    fees = data.get("fees", [])
    deposit_fee = 0.0
    savings_fee = 0.0
    for f in fees:
        label = f.get("label", "")
        try:
            rate_val = float(str(f.get("rate", "0")).replace("%", "").strip())
        except (ValueError, TypeError):
            rate_val = 0.0
        if "הפקדה" in label:
            deposit_fee = rate_val
        elif "חיסכון" in label or "צבירה" in label:
            savings_fee = rate_val
    return deposit_fee, savings_fee


def calc_annual_fee(deposit_fee_pct, savings_fee_pct, annual_deposit, avg_savings):
    """Calculate annual fee amount from rates and base amounts."""
    return (deposit_fee_pct / 100) * annual_deposit + (savings_fee_pct / 100) * avg_savings


# ─── Investment Analysis ───

EQUITY_TRACKS = {
    "אינפיניטי": {
        "tracks": ["מדדי מניות", "משולב סחיר", "הלכה", "s&p500"],
        "recommendation": "כרובוט אני אוהב מסלולים שמנוהלים על ידי רובוטים. גם בני אדם מודים שכמעט תמיד אנחנו מנצחים אותם בהשקעות. מסלול הלכה של אינפיניטי מנוהל על ידי רובוט שמחקה מדד מניות בפיזור גלובלי (MSCI World All Countries). במסלול הזה הרובוט מפזר את ההשקעה בין אלפי חברות מעשרות מדינות. אני ממליץ לשלב בין המסלול הזה למסלול שמשקיע במניות ישראליות - מסלול משולב סחיר.",
    },
    "אלטשולר שחם": {
        "tracks": ["מניות", "עוקב מדדי מניות", "s&p500"],
        "recommendation": "הניהול של אלטשולר לא הוכיח את עצמו. כרובוט אני מעדיף מסלול שבו נותנים לרובוטים להשקיע עם מינימום התערבות של בני אדם. מסלול עוקב מדדי מניות באלטשולר עונה על ההגדרה הזו אם כי פיזור הסיכונים בו לא מספיק לדעתי בהתחשב במשקל הגבוה של מניות אמריקאיות שהן גם מניות שמתומחרות עם מידה רבה יותר של אופטימיות.",
    },
    "הפניקס": {
        "tracks": ["מניות סחיר", "עוקב מדדי מניות", "מניות", "s&p500"],
        "recommendation": "כרובוט אני מעדיף רובוטים שמנהלים השקעות. גם בני אדם חושבים שאנחנו כמעט תמיד מנצחים אותם. אבל בני האדם שבנו את המסלולים של הפניקס שמנוהלים על ידי רובוטים, לא בחרו במדדי מניות עם פיזור סיכונים מספיק. מעבר לפיזור הלא מספיק, מדדי המניות הללו כוללים בעיקר מניות אמריקאיות שמתומחרות עם מידה רבה יותר של אופטימיות. בלית ברירה אני בוחר לתת אמון בבני אדם ואני ממליץ על מסלול מנייתי מנוהל ולא פאסיבי - מסלול מניות.",
    },
    "הראל": {
        "tracks": ["מניות", "מניות סחיר", "עוקב מדדי מניות", "קיימות", "s&p500"],
        "recommendation": "כרובוט אני מעדיף רובוטים שמנהלים השקעות. גם בני אדם חושבים שאנחנו כמעט תמיד מנצחים אותם. אבל בני האדם שבנו את המסלולים של הראל שמנוהלים על ידי רובוטים, לא בחרו במדדי מניות עם פיזור סיכונים מספיק. מעבר לפיזור הלא מספיק, מדדי המניות הללו כוללים בעיקר מניות אמריקאיות שמתומחרות עם מידה רבה יותר של אופטימיות. בלית ברירה אני בוחר לתת אמון בבני אדם ואני ממליץ על מסלול מנייתי מנוהל ולא פאסיבי - מסלול מניות.",
    },
    "כלל": {
        "tracks": ["מניות סחיר", "עוקב מדדי מניות", "מניות", "s&p500"],
        "recommendation": "כרובוט אני מעדיף רובוטים שמנהלים השקעות. גם בני אדם חושבים שאנחנו כמעט תמיד מנצחים אותם. אבל בני האדם שבנו את המסלולים של כלל שמנוהלים על ידי רובוטים, לא בחרו במדדי מניות עם פיזור סיכונים מספיק. מעבר לפיזור הלא מספיק, מדדי המניות הללו כוללים בעיקר מניות שמתומחרות עם מידה רבה יותר של אופטימיות. בלית ברירה אני בוחר לתת אמון בבני אדם ואני ממליץ על מסלול מנייתי מנוהל ולא פאסיבי - מסלול מניות.",
    },
    "מגדל": {
        "tracks": ["עוקב מדדי מניות", "מניות", "מניות סחיר", "s&p500"],
        "recommendation": "כרובוט אני מעדיף רובוטים שמנהלים השקעות. גם בני אדם חושבים שאנחנו כמעט תמיד מנצחים אותם. אבל בני האדם שבנו את המסלולים של מגדל שמנוהלים על ידי רובוטים, לא בחרו במדדי מניות עם פיזור סיכונים מספיק. מעבר לפיזור הלא מספיק, מדדי המניות הללו כוללים בעיקר מניות אמריקאיות שמתומחרות עם מידה רבה יותר של אופטימיות. בלית ברירה אני בוחר לתת אמון בבני אדם ואני ממליץ על מסלול מנייתי מנוהל ולא פאסיבי - מסלול מניות.",
    },
    "מור": {
        "tracks": ["מניות", "מניות סחיר", "עוקב מדדי מניות", "s&p500"],
        "recommendation": "אני לא מכיר את מדיניות ההשקעה של מסלול מניות סחיר ומדדי מניות. במידה והם לא מספקים פיזור רחב אני מעדיף את המסלול המנוהל.",
    },
    "מיטב דש": {
        "tracks": ["מניות", "מניות סחיר", "משולב סחיר", "עוקב מדדי מניות", "קיימות", "s&p500"],
        "recommendation": "כרובוט אני מעדיף רובוטים שמנהלים השקעות. גם בני אדם חושבים שאנחנו כמעט תמיד מנצחים אותם. אבל בני האדם שבנו את המסלולים של מיטב דש שמנוהלים על ידי רובוטים, לא בחרו במדדי מניות עם פיזור מספיק. מעבר לפיזור הלא מספיק, מדדי המניות הללו כוללים בעיקר מניות אמריקאיות שמתומחרות עם מידה רבה יותר של אופטימיות. בלית ברירה אני בוחר לתת אמון בבני אדם ואני ממליץ על מסלול מנייתי מנוהל ולא פאסיבי - מסלול מניות.",
    },
    "מנורה": {
        "tracks": ["עוקב מדדי מניות", "מנייתי/מניות", "מנייתי", "מניות", "מניות סחיר", "קיימות", "s&p500"],
        "recommendation": "כרובוט אני מעדיף רובוטים שמנהלים השקעות. גם בני אדם חושבים שאנחנו כמעט תמיד מנצחים אותם. זו הסיבה שאני ממליץ על מסלול עוקב מדדי מניות שמספק פיזור השקעה רחב עם מינימום שיקול דעת לבני אדם.",
    },
}

# Funds where "מדדי מניות" track should trigger concentration warning
MADEDEI_WARNING_FUNDS = ["אינפיניטי", "הפניקס", "הראל", "מגדל", "מיטב דש"]


def find_fund_key(fund_name):
    """Match PDF fund name to our equity tracks dictionary."""
    for key in EQUITY_TRACKS:
        if key in fund_name:
            return key
    return None


def is_equity_track(track_name, fund_key):
    """Check if a track name is an equity track for the given fund."""
    if not fund_key or fund_key not in EQUITY_TRACKS:
        return False
    equity_names = EQUITY_TRACKS[fund_key]["tracks"]

    def normalize(s):
        s = s.lower().strip()
        s = s.replace(" ", "").replace("&", "&").replace("&amp;", "&")
        return s

    track_norm = normalize(track_name)
    for eq in equity_names:
        eq_norm = normalize(eq)
        if eq_norm in track_norm or track_norm in eq_norm:
            return True
    equity_keywords = ["מנייתי", "מניות", "s&p", "500", "הלכה"]
    track_lower = track_name.lower()
    return any(kw in track_lower for kw in equity_keywords)


def is_age_related_track(track_name):
    """Check if track is an age-based default (בני 50 ומטה, בני 50-60, etc.)."""
    age_keywords = ["בני 50", "50 ומטה", "50-60", "עד 50", "60 ומעלה", "כללי"]
    return any(kw in track_name for kw in age_keywords)


# ─── Government Employer Check ───

GOV_EMPLOYERS = [
    "משרד ראש הממשלה", "משרד האוצר", "משרד האנרגיה", "משרד הביטחון",
    "משרד הבינוי", "משרד השיכון", "משרד הבריאות", "משרד ההתיישבות",
    "משרד החדשנות", "משרד המדע", "משרד הטכנולוגיה", "משרד החוץ",
    "משרד החקלאות", "משרד הכלכלה", "משרד התעשייה",
    "משרד המודיעין", "משרד המורשת", "משרד המשפטים", "משרד הנגב",
    "משרד הגליל", "משרד העבודה", "משרד העלייה", "משרד הקליטה",
    "משרד הפנים", "משרד הרווחה", "משרד התחבורה", "משרד התיירות",
    "משרד התפוצות", "משרד התקשורת", "משרד התרבות", "משרד הספורט",
    "משרד ירושלים", "המשרד לביטחון לאומי", "המשרד להגנת הסביבה",
    "המשרד לשוויון חברתי", "המשרד לשירותי דת", "המשרד לשיתוף פעולה",
    "המטה לביטחון לאומי", "מערך הסייבר", "המנהל האזרחי",
    "מתאם פעולות הממשלה", "הרשות הלאומית לביטחון קהילתי",
    "רשות האוכלוסין", "רשות ההגירה", "רשות האכיפה והגבייה",
    "רשות האסדרה", "רשות החברות הממשלתיות", "רשות החשמל",
    "רשות המאגר הביומטרי", "רשות המים", "רשות המסים",
    "רשות הספנות", "רשות הנמלים", "רשות העתיקות", "רשות הפטנטים",
    "רשות השירות הלאומי", "רשות התאגידים", "רשות התחרות",
    "רשות התעופה האזרחית", "רשות מקרקעי ישראל", "רשות ניירות ערך",
    "רשות שדות התעופה", "רשות שוק ההון", "הרשות הארצית לתחבורה",
    "הרשות הלאומית לבטיחות", "הרשות הממשלתית להתחדשות עירונית",
    "הרשות לאיסור הלבנת הון", "הרשות לאכיפה במקרקעין",
    "הרשות להגנה על עדים", "הרשות להגנת הפרטיות",
    "הרשות להגנת הצרכן", "הרשות לזכויות ניצולי השואה",
    "הרשות לפיתוח והתיישבות הבדואים", "הרשות לשיקום האסיר",
    "פרקליטות המדינה", "הסניגוריה הציבורית", "סיוע משפטי",
    "האפוטרופוס הכללי", "הממונה על הליכי חדלות פירעון",
    "הממונה על העזרה המשפטית", "נציבות תלונות",
    "הרשות השופטת", "בתי הדין הרבניים", "בתי הדין השרעיים",
    "בתי הדין לאוכלוסין", "הכנסת", "בית הנשיא",
    "מבקר המדינה", "נציב תלונות הציבור", "ועדת הבחירות המרכזית",
    "נציבות שירות המדינה", "נציבות שוויון זכויות",
    "הלשכה המרכזית לסטטיסטיקה", "המכון הגיאולוגי",
    "המרכז למחקר גרעיני", "המרכז למיפוי ישראל",
    "השירות המטאורולוגי", "מכון התקנים", "לשכת העיתונות הממשלתית",
    "בנק ישראל", "המוסד לביטוח לאומי", "ביטוח לאומי",
    "שירות התעסוקה", "יד ושם", "הרבנות הראשית",
    "רשם הזוגיות", "נתיב", "מינהל התכנון", "מערך הגיור",
    "מערך הדיגיטל הלאומי", "המינהל לחינוך התיישבותי",
    "המטה הלאומי להגנה על ילדים", "האגף לאסדרת מקצועות",
    "האגף לרישוי כלי ירייה", "היחידה הממשלתית לחופש המידע",
    "היחידה הממשלתית לתיאום המאבק בגזענות",
    "דורות", "המרכז הגריאטרי", "המרכז הקהילתי לבריאות הנפש",
    "המרכז הרפואי בני ציון", "המרכז הרפואי זיו", "מרחבים",
    "המרכז הרפואי לבריאות הנפש מזור", "המרכז הרפואי וולפסון",
    "אברבנאל", "מרכז רפואי פדה", "פוריה",
]

GOV_ADVISORY_URL = "https://drive.google.com/file/d/1XJQNljx97nxO1b3P791QUl9XR5aSCCC1/view"


def is_gov_employer(employer_name):
    """Check if employer matches a government body."""
    if not employer_name:
        return False
    employer_lower = employer_name.strip()
    for gov in GOV_EMPLOYERS:
        if gov in employer_lower or employer_lower in gov:
            return True
    return False
