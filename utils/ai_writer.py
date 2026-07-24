"""Generates an email subject + body from a topic using the Google Gemini API
(free tier — no billing required to get started)."""
import os
import json
import google.generativeai as genai

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

_model = None


def _get_model():
    global _model
    if _model is None:
        api_key = os.environ["GEMINI_API_KEY"]
        genai.configure(api_key=api_key)
        _model = genai.GenerativeModel(MODEL)
    return _model


SYSTEM_PROMPT = (
    "You write short, clear marketing/outreach emails. "
    "Given a topic or instruction from the user, produce ONE email. "
    "Respond with ONLY valid JSON, no markdown fences, no commentary, "
    "in exactly this shape: "
    '{"subject": "...", "body": "..."}. '
    "The body should be plain text (no HTML), friendly but professional, "
    "and end with a natural sign-off. Keep it concise (under 200 words) "
    "unless the user's topic clearly calls for more detail."
)


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


def draft_email(topic: str, tone_hint: str = "") -> dict:
    model = _get_model()
    user_content = topic if not tone_hint else f"{topic}\n\nTone/style: {tone_hint}"
    full_prompt = f"{SYSTEM_PROMPT}\n\nTopic/instruction from user:\n{user_content}"

    response = model.generate_content(full_prompt)
    text = (response.text or "").strip()

    try:
        data = _extract_json(text)
        subject = str(data.get("subject", "")).strip()
        body = str(data.get("body", "")).strip()
        if not subject or not body:
            raise ValueError("empty subject/body")
        return {"subject": subject, "body": body}
    except (json.JSONDecodeError, ValueError):
        return {"subject": topic[:78], "body": text}


def regenerate(topic: str, previous_body: str, feedback: str = "") -> dict:
    instruction = (
        f"Original topic: {topic}\n\n"
        f"Here was a previous draft you wrote:\n{previous_body}\n\n"
        "Write a NEW, different version of this email."
    )
    if feedback:
        instruction += f"\n\nIncorporate this feedback: {feedback}"
    return draft_email(instruction)
