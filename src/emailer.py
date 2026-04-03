"""
Email Notification Module
Sends an HTML digest via Gmail SMTP.

Requires Railway Pro plan (SMTP ports 465/587 are blocked on Hobby/Free plans).
Optional: Set DASHBOARD_URL in Railway Variables to link directly to the dashboard.
"""

import logging
import smtplib
import os
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

# UMSOM logo embedded as base64 so it renders in all email clients without
# needing a publicly-hosted URL.
LOGO_SRC = "data:image/gif;base64,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"


def format_currency(value) -> str:
    try:
        amount = int(str(value).replace(",", "").replace("$", ""))
        return f"${amount:,}"
    except (ValueError, TypeError):
        return str(value) if value else "Not specified"


def _days_until(close_date_str: str) -> str:
    """Return a colour-coded days-until HTML snippet for a close date."""
    if not close_date_str:
        return ""
    try:
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y"):
            try:
                close = datetime.strptime(close_date_str, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        else:
            return close_date_str
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        days = (close - today).days
        if days < 0:
            return "<span style='color:#c0392b;font-weight:bold;'>Closed</span>"
        if days == 0:
            return "<span style='color:#c0392b;font-weight:bold;'>Closes TODAY</span>"
        if days <= 7:
            return f"<span style='color:#e67e22;font-weight:bold;'>{days}d left</span>"
        if days <= 30:
            return f"<span style='color:#f39c12;'>{days}d left</span>"
        return f"<span style='color:#27ae60;'>{days}d left</span>"
    except Exception:
        return close_date_str


def _get_conf(m) -> int:
    """Extract confidence score from a Match object or dict, with legacy fallback."""
    c = getattr(m, "confidence_score", None)
    if c is None:
        c = m.get("confidence_score") if isinstance(m, dict) else None
    if c is None:
        raw = getattr(m, "match_score", None)
        if raw is None and isinstance(m, dict):
            raw = m.get("match_score", 0)
        c = min(round((raw or 0) * 8), 99)
    return int(c)


def _conf_color(conf: int) -> str:
    if conf >= 60:
        return "#2e7d32"   # green
    if conf >= 35:
        return "#e65100"   # orange
    return "#757575"       # grey


def _conf_bg(conf: int) -> str:
    if conf >= 60:
        return "#e8f5e9"
    if conf >= 35:
        return "#fff3e0"
    return "#f5f5f5"


def _get_matched_keywords(m) -> list:
    kws = getattr(m, "matched_keywords", None)
    if kws is None and isinstance(m, dict):
        kws = m.get("matched_keywords", [])
    return kws or []


def _get_match_type(m) -> str:
    mt = getattr(m, "match_type", None)
    if mt is None and isinstance(m, dict):
        mt = m.get("match_type", "keyword")
    return mt or "keyword"


def build_html_email(matched_results: list, run_date: str, dashboard_url: str = "") -> str:
    total_grants = len(matched_results)
    total_faculty_matches = sum(len(r["matches"]) for r in matched_results)

    # Sort by best confidence score descending, then by faculty count
    def _best_conf(r):
        return _get_conf(r["matches"][0]) if r["matches"] else 0
    sorted_results = sorted(matched_results, key=lambda r: (_best_conf(r), len(r["matches"])), reverse=True)

    # --- Stats bar: top agencies ---
    agencies = {}
    for r in matched_results:
        ag = r["grant"].get("agency", "Unknown") or "Unknown"
        agencies[ag] = agencies.get(ag, 0) + 1
    top_agencies = sorted(agencies.items(), key=lambda x: x[1], reverse=True)[:4]
    agency_pills = "".join(
        f'<span style="background:#e8f4fd;color:#1a4b6e;padding:3px 10px;border-radius:12px;'        f'font-size:12px;margin:2px;display:inline-block;">{ag} ({n})</span>'
        for ag, n in top_agencies
    )

    # ── Build one card per grant ────────────────────────────────────────────
    grant_cards_html = ""
    for result in sorted_results:
        grant   = result["grant"]
        matches = result["matches"]

        deadline_html = _days_until(grant.get("close_date", ""))
        award_str  = format_currency(grant.get("award_ceiling")) if grant.get("award_ceiling") else ""
        agency_str = grant.get("agency", "") or ""
        number_str = grant.get("number", "") or ""

        meta_parts = []
        if agency_str: meta_parts.append(agency_str)
        if number_str: meta_parts.append(number_str)
        if award_str:  meta_parts.append(f"Up to {award_str}")
        meta_html = " &nbsp;&middot;&nbsp; ".join(meta_parts)

        deadline_cell = deadline_html if deadline_html else (grant.get("close_date","") or "—")

        # ── Faculty rows — one per matched faculty member ───────────────────
        faculty_rows_html = ""
        for m in matches:
            conf   = _get_conf(m)
            col    = _conf_color(conf)
            bg     = _conf_bg(conf)
            mtype  = _get_match_type(m)
            kws    = _get_matched_keywords(m)

            fname = getattr(m, "faculty_name", None) or (m.get("faculty_name","") if isinstance(m, dict) else "")
            furl  = getattr(m, "faculty_url",  None) or (m.get("faculty_url", "") if isinstance(m, dict) else "")
            fdept = getattr(m, "faculty_department", None) or (m.get("faculty_department","") if isinstance(m, dict) else "")

            # Match type badge
            if mtype == "both":
                type_badge = '<span style="background:#e8f5e9;color:#2e7d32;border:1px solid #a5d6a7;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:600;">keyword + AI</span>'
            elif mtype == "semantic":
                type_badge = '<span style="background:#e3f2fd;color:#1565c0;border:1px solid #90caf9;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:600;">AI match</span>'
            else:
                type_badge = '<span style="background:#f3e5f5;color:#6a1b9a;border:1px solid #ce93d8;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:600;">keyword</span>'

            # Keyword pills
            if kws:
                kw_html = "".join(
                    f'<span style="display:inline-block;background:#f0f4ff;color:#1a3a6b;border:1px solid #c5d0e8;'                    f'padding:2px 8px;border-radius:10px;font-size:11px;margin:2px 2px 2px 0;">{kw}</span>'
                    for kw in kws
                )
            else:
                kw_html = '<span style="color:#aaa;font-size:11px;font-style:italic;">semantic similarity match</span>'

            # Faculty name — linked if URL available
            if furl:
                name_html = f'<a href="{furl}" style="color:#1a3a6b;text-decoration:none;font-weight:600;font-size:13px;">{fname}</a>'
            else:
                name_html = f'<span style="font-weight:600;font-size:13px;color:#1a3a6b;">{fname}</span>'

            dept_html = f'<div style="color:#777;font-size:11px;margin-top:1px;">{fdept}</div>' if fdept else ""

            faculty_rows_html += f"""
            <tr>
              <td style="padding:8px 12px 8px 0;vertical-align:top;width:34%;border-bottom:1px solid #f0f0f0;">
                {name_html}{dept_html}
              </td>
              <td style="padding:8px 12px;vertical-align:top;width:14%;border-bottom:1px solid #f0f0f0;text-align:center;">
                <span style="display:inline-block;background:{bg};color:{col};border:1px solid {col}44;
                  padding:4px 10px;border-radius:20px;font-size:13px;font-weight:700;">{conf}%</span>
                <div style="margin-top:4px;">{type_badge}</div>
              </td>
              <td style="padding:8px 0 8px 8px;vertical-align:top;border-bottom:1px solid #f0f0f0;">
                {kw_html}
              </td>
            </tr>"""

        grant_cards_html += f"""
      <div style="background:#fff;border:1px solid #dde4ed;border-radius:8px;margin-bottom:20px;overflow:hidden;">

        <!-- Grant header -->
        <div style="background:#1a2e45;padding:14px 20px;">
          <a href="{grant.get('link','#')}" style="color:#ffffff;text-decoration:none;font-size:15px;font-weight:700;line-height:1.4;display:block;">
            {grant['title'][:160]}{'...' if len(grant.get('title','')) > 160 else ''}
          </a>
          <div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;">
            <span style="color:#a8c4e0;font-size:12px;">{meta_html}</span>
            <span style="margin-left:auto;white-space:nowrap;">{deadline_cell}</span>
          </div>
        </div>

        <!-- Faculty match table -->
        <div style="padding:12px 20px 4px;">
          <div style="font-size:11px;color:#888;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px;">
            {len(matches)} Faculty Match{'es' if len(matches) != 1 else ''}
          </div>
          <table style="width:100%;border-collapse:collapse;">
            <thead>
              <tr style="border-bottom:2px solid #e8edf5;">
                <th style="padding:4px 12px 6px 0;text-align:left;font-size:11px;color:#555;font-weight:600;text-transform:uppercase;letter-spacing:0.4px;">Faculty Member</th>
                <th style="padding:4px 12px 6px;text-align:center;font-size:11px;color:#555;font-weight:600;text-transform:uppercase;letter-spacing:0.4px;">Confidence</th>
                <th style="padding:4px 0 6px 8px;text-align:left;font-size:11px;color:#555;font-weight:600;text-transform:uppercase;letter-spacing:0.4px;">Matched Keywords</th>
              </tr>
            </thead>
            <tbody>
              {faculty_rows_html}
            </tbody>
          </table>
        </div>

      </div>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#eef1f5;font-family:Arial,sans-serif;">
  <div style="max-width:860px;margin:0 auto;padding:24px 16px;">

    <!-- Header -->
    <div style="background:#1a2e45;border-radius:8px 8px 0 0;overflow:hidden;">
      <!-- Logo bar -->
      <div style="background:#ffffff;padding:14px 24px;border-bottom:3px solid #c8a84b;">
        <img src="{LOGO_SRC}" alt="University of Maryland School of Medicine" width="200" height="50"
             style="display:block;" />
      </div>
      <!-- Title bar -->
      <div style="padding:20px 24px 18px;">
        <h1 style="margin:0;font-size:18px;font-weight:700;color:#ffffff;letter-spacing:0.3px;line-height:1.3;">
          University of Maryland School of Medicine<br>
          <span style="color:#c8a84b;">AI Grant Match Application Notification</span>
        </h1>
        <p style="margin:8px 0 0;color:#a8c4e0;font-size:13px;">{run_date}</p>
      </div>
    </div>

    <!-- Stats row -->
    <div style="background:#fff;padding:16px 24px;border-left:1px solid #dde4ed;border-right:1px solid #dde4ed;
                border-bottom:2px solid #e8edf2;display:flex;gap:0;">
      <div style="flex:1;text-align:center;padding:8px 0;">
        <div style="font-size:30px;font-weight:bold;color:#1a2e45;">{total_grants}</div>
        <div style="font-size:12px;color:#666;margin-top:2px;">Grant Opportunities</div>
      </div>
      <div style="flex:1;text-align:center;padding:8px 0;border-left:1px solid #eee;">
        <div style="font-size:30px;font-weight:bold;color:#1a2e45;">{total_faculty_matches:,}</div>
        <div style="font-size:12px;color:#666;margin-top:2px;">Faculty Matches</div>
      </div>
      <div style="flex:2;padding:12px 20px;border-left:1px solid #eee;">
        <div style="font-size:11px;color:#888;margin-bottom:6px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;">Funding Organizations</div>
        <div>{agency_pills}</div>
      </div>
    </div>

    <!-- Grant cards -->
    <div style="margin-top:16px;">
      {grant_cards_html}
    </div>

    <!-- Footer -->
    <div style="background:#1a2e45;padding:20px 24px;border-radius:8px;margin-top:8px;">
      <img src="{LOGO_SRC}" alt="University of Maryland School of Medicine" width="160" height="40"
           style="display:block;margin-bottom:14px;opacity:0.9;" />
      <p style="margin:0 0 10px;color:#a8c4e0;font-size:12px;line-height:1.6;">
        Grant data sourced from over 30 funding organizations and matched to SOM Faculty Profiles.
      </p>
      <p style="margin:0 0 10px;color:#a8c4e0;font-size:12px;line-height:1.6;">
        The information provided in this application is intended to notify SOM faculty of potential grant
        opportunities that may align with their subject matter expertise. This tool should be used in
        conjunction with other methods for identifying funding opportunities and should not serve as the
        sole source for grant discovery.
      </p>
      <p style="margin:0;color:#c8a84b;font-size:12px;line-height:1.6;font-style:italic;">
        To optimize AI-driven grant recommendations used in this app, please periodically review and
        refine the keywords in your SOM Faculty Profile.
      </p>
    </div>

  </div>
</body>
</html>"""


def build_text_body(matched_results: list, run_date: str, dashboard_url: str = "") -> str:
    total_grants = len(matched_results)
    total_faculty = sum(len(r["matches"]) for r in matched_results)
    lines = [
        "University of Maryland School of Medicine",
        "AI Grant Match Application Notification",
        f"Report Date: {run_date}",
        "=" * 70,
        f"{total_grants} grant opportunit{'ies' if total_grants != 1 else 'y'} matched to {total_faculty:,} SOM faculty members.",
        "",
    ]
    if dashboard_url:
        lines += [f"Full dashboard: {dashboard_url}", ""]

    sorted_results = sorted(matched_results, key=lambda r: len(r["matches"]), reverse=True)
    for result in sorted_results:
        grant   = result["grant"]
        matches = result["matches"]
        lines.append(f"GRANT: {grant['title']}")
        lines.append(f"  Link:    {grant.get('link','N/A')}")
        if grant.get("agency"):
            lines.append(f"  Agency:  {grant['agency']}")
        if grant.get("close_date"):
            lines.append(f"  Closes:  {grant['close_date']}")
        if grant.get("award_ceiling"):
            lines.append(f"  Award:   {format_currency(grant['award_ceiling'])}")
        lines.append(f"  Matched Faculty ({len(matches)}):")
        for m in matches:
            conf  = _get_conf(m)
            fname = getattr(m, "faculty_name", None) or (m.get("faculty_name","") if isinstance(m, dict) else "")
            fdept = getattr(m, "faculty_department", None) or (m.get("faculty_department","") if isinstance(m, dict) else "")
            kws   = _get_matched_keywords(m)
            kw_str = ", ".join(kws) if kws else "semantic similarity match"
            dept_str = f" ({fdept})" if fdept else ""
            lines.append(f"    - {fname}{dept_str}  [{conf}% confidence]")
            lines.append(f"      Keywords: {kw_str}")
        lines.append("")

    lines += [
        "-" * 70,
        "Grant data sourced from over 30 funding organizations and matched to SOM Faculty Profiles.",
        "",
        "The information provided in this application is intended to notify SOM faculty of potential",
        "grant opportunities that may align with their subject matter expertise. This tool should be",
        "used in conjunction with other methods for identifying funding opportunities and should not",
        "serve as the sole source for grant discovery.",
        "",
        "To optimize AI-driven grant recommendations used in this app, please periodically review",
        "and refine the keywords in your SOM Faculty Profile.",
    ]
    return "\n".join(lines)


def send_email(config: dict, matched_results: list):
    """
    Send HTML digest via Gmail SMTP.
    Required Railway Variables:
      GMAIL_SENDER        - Gmail address to send from
      GMAIL_APP_PASSWORD  - 16-character Gmail App Password
      ALERT_RECIPIENTS    - Comma-separated recipient list
    Optional Railway Variables:
      DASHBOARD_URL       - Full URL to your Railway deployment
    """
    if not matched_results:
        logger.info("No matches to email.")
        return

    sender       = os.environ.get("GMAIL_SENDER",       config["email"].get("sender", ""))
    app_password = os.environ.get("GMAIL_APP_PASSWORD", config["email"].get("app_password", ""))
    recipients   = config["email"]["recipients"]
    subject_prefix = config["email"].get("subject_prefix", "[Grant Match]")
    dashboard_url  = os.environ.get("DASHBOARD_URL", "")

    if not sender or sender == "set-via-railway-variable":
        raise ValueError("GMAIL_SENDER environment variable is not set in Railway Variables.")
    if not app_password or app_password == "set-via-railway-variable":
        raise ValueError("GMAIL_APP_PASSWORD environment variable is not set in Railway Variables.")

    run_date      = datetime.utcnow().strftime("%B %d, %Y")
    total_grants  = len(matched_results)
    total_matches = sum(len(r["matches"]) for r in matched_results)

    subject = (
        f"{subject_prefix} {total_grants} new grant opportunit{'ies' if total_grants != 1 else 'y'} "
        f"matched to {total_matches:,} SOM faculty - {run_date}"
    )

    html_body = build_html_email(matched_results, run_date, dashboard_url)
    text_body = build_text_body(matched_results, run_date, dashboard_url)

    html_kb = len(html_body.encode("utf-8")) / 1024
    logger.info(f"Email body size: {html_kb:.1f} KB HTML / {len(text_body)/1024:.1f} KB plain text")
    if html_kb > 10240:
        logger.warning(f"Email is {html_kb:.0f} KB — may be near Gmail limits if many grants matched.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, app_password)
            server.sendmail(sender, recipients, msg.as_string())
        logger.info(f"Email sent via Gmail to {len(recipients)} recipient(s): {', '.join(recipients)}")
    except smtplib.SMTPAuthenticationError:
        logger.error(
            "Gmail authentication failed. Make sure GMAIL_APP_PASSWORD in Railway Variables "
            "is the 16-character App Password (no spaces), not your regular Gmail password."
        )
        raise
    except smtplib.SMTPException as e:
        logger.error(f"Failed to send email: {e}")
        raise


# ═══════════════════════════════════════════════════════════════════════════════
# DIAGNOSTIC EMAIL — Separate from the grant match email
# ═══════════════════════════════════════════════════════════════════════════════

def build_diagnostic_html(matcher_diag: dict, scraper_health: dict, run_date: str) -> str:
    """Build HTML body for the daily diagnostic email."""
    summary = matcher_diag.get("summary", {})
    params = matcher_diag.get("params", {})

    # ── Section 1: Run Summary ───────────────────────────────────────────────
    run_summary_html = f"""
    <div style="background:#fff;border:1px solid #ddd;border-radius:6px;padding:16px 20px;margin-bottom:16px;">
      <h2 style="margin:0 0 12px;font-size:16px;color:#1a2e45;">Run Summary</h2>
      <table style="font-size:13px;color:#333;border-collapse:collapse;width:100%;">
        <tr><td style="padding:4px 16px 4px 0;color:#666;">Faculty processed</td><td style="padding:4px 0;font-weight:600;">{summary.get('faculty_count', '?')}</td></tr>
        <tr><td style="padding:4px 16px 4px 0;color:#666;">Grants checked</td><td style="padding:4px 0;font-weight:600;">{summary.get('grants_checked', '?')}</td></tr>
        <tr><td style="padding:4px 16px 4px 0;color:#666;">Grants skipped (not biomedical)</td><td style="padding:4px 0;font-weight:600;">{summary.get('grants_skipped_irrelevant', '?')}</td></tr>
        <tr><td style="padding:4px 16px 4px 0;color:#666;">Grants matched</td><td style="padding:4px 0;font-weight:600;">{summary.get('grants_matched', '?')}</td></tr>
        <tr><td style="padding:4px 16px 4px 0;color:#666;">Raw matches (before filters)</td><td style="padding:4px 0;font-weight:600;">{summary.get('raw_matches', '?')}</td></tr>
        <tr><td style="padding:4px 16px 4px 0;color:#666;">Matches after confidence filter</td><td style="padding:4px 0;font-weight:600;">{summary.get('matches_after_filter', '?')}</td></tr>
        <tr><td style="padding:4px 16px 4px 0;color:#666;">Suppressed by confidence</td><td style="padding:4px 0;font-weight:600;">{summary.get('suppressed_by_confidence', '?')}</td></tr>
        <tr><td style="padding:4px 16px 4px 0;color:#666;">Keyword-only / Semantic-only / Both</td><td style="padding:4px 0;font-weight:600;">{summary.get('keyword_only', 0)} / {summary.get('semantic_only', 0)} / {summary.get('both', 0)}</td></tr>
        <tr><td style="padding:4px 16px 4px 0;color:#666;">Run duration</td><td style="padding:4px 0;font-weight:600;">{summary.get('run_duration_s', '?')}s</td></tr>
      </table>
    </div>"""

    # ── Section 2: Parameters ────────────────────────────────────────────────
    params_html = f"""
    <div style="background:#fff;border:1px solid #ddd;border-radius:6px;padding:16px 20px;margin-bottom:16px;">
      <h2 style="margin:0 0 12px;font-size:16px;color:#1a2e45;">Active Parameters</h2>
      <table style="font-size:13px;color:#333;border-collapse:collapse;width:100%;">
        <tr><td style="padding:4px 16px 4px 0;color:#666;">Semantic threshold</td><td style="padding:4px 0;font-weight:600;">{params.get('semantic_threshold', '?')}</td></tr>
        <tr><td style="padding:4px 16px 4px 0;color:#666;">Min confidence</td><td style="padding:4px 0;font-weight:600;">{params.get('min_confidence', '?')}%</td></tr>
        <tr><td style="padding:4px 16px 4px 0;color:#666;">Max kw prevalence (stop word)</td><td style="padding:4px 0;font-weight:600;">{params.get('max_kw_prevalence_pct', '?')}</td></tr>
        <tr><td style="padding:4px 16px 4px 0;color:#666;">Max matches per grant</td><td style="padding:4px 0;font-weight:600;">{params.get('max_matches_per_grant', '?')}</td></tr>
        <tr><td style="padding:4px 16px 4px 0;color:#666;">Min IDF for match</td><td style="padding:4px 0;font-weight:600;">{params.get('min_idf_for_match', '?')}</td></tr>
        <tr><td style="padding:4px 16px 4px 0;color:#666;">Semantic enabled</td><td style="padding:4px 0;font-weight:600;">{params.get('semantic_enabled', '?')}</td></tr>
      </table>
    </div>"""

    # ── Section 3: Dynamic Stop Words ────────────────────────────────────────
    stop_words = matcher_diag.get("stop_words_suppressed", [])
    stop_pills = "".join(
        f'<span style="display:inline-block;background:#fff3e0;color:#e65100;border:1px solid #ffcc80;'
        f'padding:3px 10px;border-radius:12px;font-size:12px;margin:3px 3px 3px 0;">{w}</span>'
        for w in stop_words
    ) if stop_words else '<span style="color:#999;font-size:13px;">None suppressed</span>'

    stop_words_html = f"""
    <div style="background:#fff;border:1px solid #ddd;border-radius:6px;padding:16px 20px;margin-bottom:16px;">
      <h2 style="margin:0 0 12px;font-size:16px;color:#1a2e45;">Dynamic Stop Words ({len(stop_words)} suppressed)</h2>
      <div>{stop_pills}</div>
    </div>"""

    # ── Section 4: Per-Grant Matching Detail ─────────────────────────────────
    per_grant = matcher_diag.get("per_grant", [])
    grant_rows = ""
    for g in per_grant:
        grant_rows += f"""
        <tr>
          <td style="padding:6px 12px 6px 0;border-bottom:1px solid #eee;font-size:12px;max-width:300px;overflow:hidden;text-overflow:ellipsis;">{g['grant_title'][:80]}</td>
          <td style="padding:6px 8px;border-bottom:1px solid #eee;font-size:12px;text-align:center;">{g['keyword_matches']}</td>
          <td style="padding:6px 8px;border-bottom:1px solid #eee;font-size:12px;text-align:center;">{g['semantic_matches']}</td>
          <td style="padding:6px 8px;border-bottom:1px solid #eee;font-size:12px;text-align:center;font-weight:600;">{g['after_confidence_filter']}</td>
          <td style="padding:6px 8px;border-bottom:1px solid #eee;font-size:12px;text-align:center;">{g['avg_confidence']}%</td>
        </tr>"""

    per_grant_html = f"""
    <div style="background:#fff;border:1px solid #ddd;border-radius:6px;padding:16px 20px;margin-bottom:16px;">
      <h2 style="margin:0 0 12px;font-size:16px;color:#1a2e45;">Per-Grant Matching Detail</h2>
      <table style="width:100%;border-collapse:collapse;">
        <thead>
          <tr style="border-bottom:2px solid #ddd;">
            <th style="padding:6px 12px 6px 0;text-align:left;font-size:11px;color:#666;">Grant</th>
            <th style="padding:6px 8px;text-align:center;font-size:11px;color:#666;">Keyword</th>
            <th style="padding:6px 8px;text-align:center;font-size:11px;color:#666;">Semantic</th>
            <th style="padding:6px 8px;text-align:center;font-size:11px;color:#666;">After Filter</th>
            <th style="padding:6px 8px;text-align:center;font-size:11px;color:#666;">Avg Conf</th>
          </tr>
        </thead>
        <tbody>{grant_rows}</tbody>
      </table>
    </div>""" if per_grant else ""

    # ── Section 5: Confidence Histograms ─────────────────────────────────────
    histograms = matcher_diag.get("confidence_histograms", [])
    hist_html = ""
    if histograms:
        hist_rows = ""
        for h in histograms:
            hg = h.get("histogram", {})
            hist_rows += f"""
            <tr>
              <td style="padding:4px 8px 4px 0;border-bottom:1px solid #eee;font-size:11px;">{h['grant_title'][:60]}</td>
              <td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:11px;text-align:center;">{h['total_before_filter']}</td>
              <td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:11px;text-align:center;">{hg.get('>=60%', 0)}</td>
              <td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:11px;text-align:center;">{hg.get('>=50%', 0)}</td>
              <td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:11px;text-align:center;">{hg.get('>=40%', 0)}</td>
              <td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:11px;text-align:center;">{hg.get('>=35%', 0)}</td>
              <td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:11px;text-align:center;">{hg.get('>=30%', 0)}</td>
              <td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:11px;text-align:center;">{hg.get('>=20%', 0)}</td>
            </tr>"""
        hist_html = f"""
    <div style="background:#fff;border:1px solid #ddd;border-radius:6px;padding:16px 20px;margin-bottom:16px;">
      <h2 style="margin:0 0 12px;font-size:16px;color:#1a2e45;">Confidence Distributions (before filter)</h2>
      <table style="width:100%;border-collapse:collapse;">
        <thead>
          <tr style="border-bottom:2px solid #ddd;">
            <th style="padding:4px 8px 4px 0;text-align:left;font-size:10px;color:#666;">Grant</th>
            <th style="padding:4px 6px;text-align:center;font-size:10px;color:#666;">Total</th>
            <th style="padding:4px 6px;text-align:center;font-size:10px;color:#666;">&ge;60%</th>
            <th style="padding:4px 6px;text-align:center;font-size:10px;color:#666;">&ge;50%</th>
            <th style="padding:4px 6px;text-align:center;font-size:10px;color:#666;">&ge;40%</th>
            <th style="padding:4px 6px;text-align:center;font-size:10px;color:#666;">&ge;35%</th>
            <th style="padding:4px 6px;text-align:center;font-size:10px;color:#666;">&ge;30%</th>
            <th style="padding:4px 6px;text-align:center;font-size:10px;color:#666;">&ge;20%</th>
          </tr>
        </thead>
        <tbody>{hist_rows}</tbody>
      </table>
    </div>"""

    # ── Section 6: Semantic Score Analysis ────────────────────────────────────
    sem_dists = matcher_diag.get("semantic_score_distributions", [])
    sem_html = ""
    if sem_dists:
        sem_rows = ""
        for sd in sem_dists:
            top5 = sd.get("top_5", [])
            top5_str = ", ".join(f"{name} ({score:.3f})" for name, score in top5[:3])
            sem_rows += f"""
            <tr>
              <td style="padding:4px 8px 4px 0;border-bottom:1px solid #eee;font-size:11px;">{sd['grant_title'][:50]}</td>
              <td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:11px;text-align:center;font-weight:600;">{sd['max']}</td>
              <td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:11px;text-align:center;">{sd['p95']}</td>
              <td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:11px;text-align:center;">{sd['p90']}</td>
              <td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:11px;text-align:center;">{sd['median']}</td>
              <td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:11px;text-align:center;font-weight:600;">{sd['above_threshold']}</td>
              <td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:11px;text-align:center;">{sd['above_060']}</td>
              <td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:11px;text-align:center;">{sd['above_065']}</td>
            </tr>"""

        sem_html = f"""
    <div style="background:#fff;border:1px solid #ddd;border-radius:6px;padding:16px 20px;margin-bottom:16px;">
      <h2 style="margin:0 0 12px;font-size:16px;color:#1a2e45;">Semantic Score Analysis (threshold = {params.get('semantic_threshold', '?')})</h2>
      <table style="width:100%;border-collapse:collapse;">
        <thead>
          <tr style="border-bottom:2px solid #ddd;">
            <th style="padding:4px 8px 4px 0;text-align:left;font-size:10px;color:#666;">Grant</th>
            <th style="padding:4px 6px;text-align:center;font-size:10px;color:#666;">Max</th>
            <th style="padding:4px 6px;text-align:center;font-size:10px;color:#666;">p95</th>
            <th style="padding:4px 6px;text-align:center;font-size:10px;color:#666;">p90</th>
            <th style="padding:4px 6px;text-align:center;font-size:10px;color:#666;">Median</th>
            <th style="padding:4px 6px;text-align:center;font-size:10px;color:#666;">Above thresh</th>
            <th style="padding:4px 6px;text-align:center;font-size:10px;color:#666;">&ge;0.60</th>
            <th style="padding:4px 6px;text-align:center;font-size:10px;color:#666;">&ge;0.65</th>
          </tr>
        </thead>
        <tbody>{sem_rows}</tbody>
      </table>
      <div style="margin-top:10px;font-size:11px;color:#666;">
        Top matches per grant: {'; '.join(f"{sd['grant_title'][:30]}: {sd['top_5'][0][0]} ({sd['top_5'][0][1]:.3f})" for sd in sem_dists if sd.get('top_5')) or 'none'}
      </div>
    </div>"""
    else:
        sem_html = """
    <div style="background:#fff;border:1px solid #ddd;border-radius:6px;padding:16px 20px;margin-bottom:16px;">
      <h2 style="margin:0 0 12px;font-size:16px;color:#1a2e45;">Semantic Score Analysis</h2>
      <p style="color:#999;font-size:13px;">No semantic diagnostic data collected this run. Check if semantic matching is enabled and faculty have embeddings.</p>
    </div>"""

    # ── Section 7: Grants Capped ─────────────────────────────────────────────
    capped = matcher_diag.get("grants_capped", [])
    capped_html = ""
    if capped:
        capped_rows = "".join(
            f'<tr><td style="padding:4px 8px 4px 0;border-bottom:1px solid #eee;font-size:12px;">{c["grant_title"][:60]}</td>'
            f'<td style="padding:4px 8px;border-bottom:1px solid #eee;font-size:12px;text-align:center;">{c["original"]}</td>'
            f'<td style="padding:4px 8px;border-bottom:1px solid #eee;font-size:12px;text-align:center;">{c["capped_to"]}</td>'
            f'<td style="padding:4px 8px;border-bottom:1px solid #eee;font-size:12px;text-align:center;">{c["min_conf_kept"]}%</td></tr>'
            for c in capped
        )
        capped_html = f"""
    <div style="background:#fff;border:1px solid #ddd;border-radius:6px;padding:16px 20px;margin-bottom:16px;">
      <h2 style="margin:0 0 12px;font-size:16px;color:#e65100;">Grants Capped ({len(capped)} hit limit of {params.get('max_matches_per_grant', '?')})</h2>
      <table style="width:100%;border-collapse:collapse;">
        <thead><tr style="border-bottom:2px solid #ddd;">
          <th style="text-align:left;font-size:11px;color:#666;padding:4px 8px 4px 0;">Grant</th>
          <th style="text-align:center;font-size:11px;color:#666;padding:4px 8px;">Original</th>
          <th style="text-align:center;font-size:11px;color:#666;padding:4px 8px;">Capped To</th>
          <th style="text-align:center;font-size:11px;color:#666;padding:4px 8px;">Min Conf Kept</th>
        </tr></thead>
        <tbody>{capped_rows}</tbody>
      </table>
    </div>"""

    # ── Section 8: Foundation Scraper Health ──────────────────────────────────
    per_source = scraper_health.get("per_source", {})
    health_alerts = scraper_health.get("health_alerts", [])

    source_rows = ""
    for src, count in sorted(per_source.items()):
        color = "#2e7d32" if count > 0 else ("#c62828" if count < 0 else "#999")
        status = f"{count} new" if count >= 0 else "ERROR"
        source_rows += (
            f'<tr><td style="padding:3px 12px 3px 0;border-bottom:1px solid #eee;font-size:12px;">{src}</td>'
            f'<td style="padding:3px 0;border-bottom:1px solid #eee;font-size:12px;color:{color};font-weight:600;">{status}</td></tr>'
        )

    alert_html = ""
    if health_alerts:
        alert_items = "".join(
            f'<div style="background:#fff3e0;border-left:3px solid #e65100;padding:8px 12px;margin-bottom:6px;font-size:12px;">'
            f'<strong>{a["source"]}</strong>: {a["consecutive_zeros"]} consecutive zero runs '
            f'(last success: {a["last_success"]})</div>'
            for a in health_alerts
        )
        alert_html = f'<div style="margin-bottom:12px;">{alert_items}</div>'

    scraper_html = f"""
    <div style="background:#fff;border:1px solid #ddd;border-radius:6px;padding:16px 20px;margin-bottom:16px;">
      <h2 style="margin:0 0 12px;font-size:16px;color:#1a2e45;">Foundation Scraper Health ({scraper_health.get('sources_succeeded', 0)}/{scraper_health.get('sources_tried', 0)} succeeded)</h2>
      {alert_html}
      <table style="width:100%;border-collapse:collapse;">
        <tbody>{source_rows}</tbody>
      </table>
    </div>"""

    # ── Assemble full email ──────────────────────────────────────────────────
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:Arial,sans-serif;">
  <div style="max-width:860px;margin:0 auto;padding:24px 16px;">

    <div style="background:#1a2e45;border-radius:8px 8px 0 0;padding:20px 24px;">
      <h1 style="margin:0;font-size:18px;color:#ffffff;">Grant Matcher — Daily Diagnostic Report</h1>
      <p style="margin:6px 0 0;color:#a8c4e0;font-size:13px;">{run_date}</p>
    </div>

    <div style="padding:16px 0;">
      {run_summary_html}
      {params_html}
      {stop_words_html}
      {per_grant_html}
      {capped_html}
      {hist_html}
      {sem_html}
      {scraper_html}
    </div>

    <div style="background:#f0f0f0;padding:12px 20px;border-radius:6px;font-size:11px;color:#999;text-align:center;">
      This diagnostic email is sent to the system administrator only.
      Forward to Claude for analysis and tuning recommendations.
    </div>

  </div>
</body>
</html>"""


def build_diagnostic_text(matcher_diag: dict, scraper_health: dict, run_date: str) -> str:
    """Build plain text body for the diagnostic email."""
    summary = matcher_diag.get("summary", {})
    params = matcher_diag.get("params", {})

    lines = [
        "GRANT MATCHER — DAILY DIAGNOSTIC REPORT",
        f"Date: {run_date}",
        "=" * 60,
        "",
        "RUN SUMMARY",
        f"  Faculty processed:         {summary.get('faculty_count', '?')}",
        f"  Grants checked:            {summary.get('grants_checked', '?')}",
        f"  Grants skipped:            {summary.get('grants_skipped_irrelevant', '?')}",
        f"  Grants matched:            {summary.get('grants_matched', '?')}",
        f"  Raw matches:               {summary.get('raw_matches', '?')}",
        f"  After confidence filter:   {summary.get('matches_after_filter', '?')}",
        f"  Suppressed by confidence:  {summary.get('suppressed_by_confidence', '?')}",
        f"  KW-only / Sem-only / Both: {summary.get('keyword_only', 0)} / {summary.get('semantic_only', 0)} / {summary.get('both', 0)}",
        f"  Run duration:              {summary.get('run_duration_s', '?')}s",
        "",
        "PARAMETERS",
        f"  Semantic threshold:        {params.get('semantic_threshold', '?')}",
        f"  Min confidence:            {params.get('min_confidence', '?')}%",
        f"  Max kw prevalence:         {params.get('max_kw_prevalence_pct', '?')}",
        f"  Max matches/grant:         {params.get('max_matches_per_grant', '?')}",
        f"  Min IDF for match:         {params.get('min_idf_for_match', '?')}",
        "",
        f"DYNAMIC STOP WORDS ({len(matcher_diag.get('stop_words_suppressed', []))} suppressed)",
        f"  {', '.join(matcher_diag.get('stop_words_suppressed', [])) or 'None'}",
        "",
    ]

    # Per-grant detail
    per_grant = matcher_diag.get("per_grant", [])
    if per_grant:
        lines.append("PER-GRANT MATCHING DETAIL")
        for g in per_grant:
            lines.append(f"  {g['grant_title'][:70]}")
            lines.append(f"    KW:{g['keyword_matches']} Sem:{g['semantic_matches']} "
                        f"After filter:{g['after_confidence_filter']} Avg conf:{g['avg_confidence']}%")
        lines.append("")

    # Grants capped
    capped = matcher_diag.get("grants_capped", [])
    if capped:
        lines.append("GRANTS CAPPED")
        for c in capped:
            lines.append(f"  {c['grant_title'][:60]}: {c['original']} -> {c['capped_to']} "
                        f"(min conf kept: {c['min_conf_kept']}%)")
        lines.append("")

    # Semantic scores
    sem_dists = matcher_diag.get("semantic_score_distributions", [])
    if sem_dists:
        lines.append("SEMANTIC SCORE ANALYSIS")
        for sd in sem_dists:
            lines.append(f"  {sd['grant_title'][:50]}")
            lines.append(f"    Max={sd['max']} p95={sd['p95']} p90={sd['p90']} Median={sd['median']}")
            lines.append(f"    Above threshold: {sd['above_threshold']}  >=0.60: {sd['above_060']}  >=0.65: {sd['above_065']}")
            if sd.get("top_5"):
                top3 = ", ".join(f"{n} ({s:.3f})" for n, s in sd["top_5"][:3])
                lines.append(f"    Top 3: {top3}")
        lines.append("")

    # Confidence histograms
    histograms = matcher_diag.get("confidence_histograms", [])
    if histograms:
        lines.append("CONFIDENCE HISTOGRAMS (before filter)")
        for h in histograms:
            hg = h.get("histogram", {})
            lines.append(f"  {h['grant_title'][:50]} (total: {h['total_before_filter']})")
            lines.append(f"    >=60%:{hg.get('>=60%',0)}  >=50%:{hg.get('>=50%',0)}  "
                        f">=40%:{hg.get('>=40%',0)}  >=35%:{hg.get('>=35%',0)}  "
                        f">=30%:{hg.get('>=30%',0)}  >=20%:{hg.get('>=20%',0)}")
        lines.append("")

    # Scraper health
    per_source = scraper_health.get("per_source", {})
    lines.append(f"FOUNDATION SCRAPER HEALTH ({scraper_health.get('sources_succeeded', 0)}/{scraper_health.get('sources_tried', 0)} succeeded)")
    for src, count in sorted(per_source.items()):
        status = f"{count} new" if count >= 0 else "ERROR"
        lines.append(f"  {src:<35} {status}")

    alerts = scraper_health.get("health_alerts", [])
    if alerts:
        lines.append("")
        lines.append("HEALTH ALERTS:")
        for a in alerts:
            lines.append(f"  WARNING: {a['source']} — {a['consecutive_zeros']} consecutive zero runs "
                        f"(last success: {a['last_success']})")

    lines.append("")
    lines.append("-" * 60)
    lines.append("Forward this email to Claude for analysis and tuning recommendations.")
    return "\n".join(lines)


def send_diagnostic_email(config: dict, matcher_diag: dict, scraper_health: dict):
    """
    Send a separate diagnostic email to the system admin only.
    Uses DIAGNOSTIC_RECIPIENTS env var if set, otherwise falls back to the
    first address in ALERT_RECIPIENTS.
    """
    import os

    sender       = os.environ.get("GMAIL_SENDER",       config["email"].get("sender", ""))
    app_password = os.environ.get("GMAIL_APP_PASSWORD", config["email"].get("app_password", ""))

    # Diagnostic email goes to admin only — not all grant alert recipients
    diag_recipients_str = os.environ.get("DIAGNOSTIC_RECIPIENTS", "")
    if diag_recipients_str:
        recipients = [r.strip() for r in diag_recipients_str.split(",") if r.strip()]
    else:
        # Fallback: first recipient from the main alert list
        all_recipients = config["email"].get("recipients", [])
        recipients = [all_recipients[0]] if all_recipients else []

    if not recipients:
        logger.warning("No diagnostic email recipients configured. Skipping diagnostic email.")
        return

    if not sender or sender == "set-via-railway-variable":
        logger.warning("GMAIL_SENDER not set — skipping diagnostic email.")
        return
    if not app_password or app_password == "set-via-railway-variable":
        logger.warning("GMAIL_APP_PASSWORD not set — skipping diagnostic email.")
        return

    run_date = datetime.utcnow().strftime("%B %d, %Y")
    summary = matcher_diag.get("summary", {})
    total_matches = summary.get("matches_after_filter", 0)
    sem_only = summary.get("semantic_only", 0)

    subject = (
        f"[Grant Matcher Diagnostic] "
        f"{summary.get('grants_matched', 0)} grants, "
        f"{total_matches} matches "
        f"({sem_only} semantic) — {run_date}"
    )

    html_body = build_diagnostic_html(matcher_diag, scraper_health, run_date)
    text_body = build_diagnostic_text(matcher_diag, scraper_health, run_date)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, app_password)
            server.sendmail(sender, recipients, msg.as_string())
        logger.info(f"Diagnostic email sent to {len(recipients)} recipient(s): {', '.join(recipients)}")
    except Exception as e:
        logger.error(f"Failed to send diagnostic email: {e}")
        # Don't raise — diagnostic email failure shouldn't stop the main flow
