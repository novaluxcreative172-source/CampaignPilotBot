"""Sends transactional emails through Brevo (formerly Sendinblue)."""
import os
import re
import httpx

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def extract_emails(raw_text: str) -> list[str]:
    """Pulls valid-looking email addresses out of pasted text or a file's contents.
    Accepts comma, semicolon, newline, or whitespace separated input, and CSV-ish
    lines (takes the first email-shaped token per line)."""
    candidates = re.split(r"[,\;\n\r\t ]+", raw_text)
    seen = set()
    emails = []
    for c in candidates:
        c = c.strip().strip(",;")
        if EMAIL_RE.match(c) and c.lower() not in seen:
            seen.add(c.lower())
            emails.append(c)
    return emails


def send_single_email(to_email: str, subject: str, body: str) -> tuple[bool, str]:
    """Sends one email. Returns (success, error_message_if_any)."""
    api_key = os.environ["BREVO_API_KEY"]
    sender_email = os.environ["SENDER_EMAIL"]
    sender_name = os.environ.get("SENDER_NAME", "Campaign Pilot")

    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": to_email}],
        "subject": subject,
        "textContent": body,
    }
    headers = {
        "accept": "application/json",
        "api-key": api_key,
        "content-type": "application/json",
    }

    try:
        resp = httpx.post(BREVO_API_URL, json=payload, headers=headers, timeout=20)
        if resp.status_code in (200, 201):
            return True, ""
        return False, f"{resp.status_code}: {resp.text[:200]}"
    except httpx.HTTPError as e:
        return False, str(e)


def send_bulk(recipients: list[str], subject: str, body: str) -> dict:
    """Sends the same email individually to each recipient (so no one sees the
    full recipient list). Returns a summary dict with counts and failures."""
    sent, failed = [], []
    for email in recipients:
        ok, err = send_single_email(email, subject, body)
        if ok:
            sent.append(email)
        else:
            failed.append((email, err))
    return {"sent": sent, "failed": failed}
