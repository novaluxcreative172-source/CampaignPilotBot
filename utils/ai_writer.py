"""Generates an email subject + body from a topic using the Anthropic API."""
import os
import json
from anthropic import Anthropic

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

_client = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = os.environ["ANTHROPIC_API_KEY"]
        _client = Anthropic(api_key=api_key)
    return _client


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


def draft_email(topic: str, tone_hint: str = "") -> dict:
    """Returns {'subject': str, 'body': str} drafted from a topic."""
    client = _get_client()
    user_content = topic if not tone_hint else f"{topic}\n\nTone/style: {tone_hint}"

    response = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )

    text = "".join(block.text for block in response.content if block.type == "text").strip()

    # Defensive parsing in case the model wraps JSON in fences despite instructions
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        data = json.loads(text)
        subject = str(data.get("subject", "")).strip()
        body = str(data.get("body", "")).strip()
        if not subject or not body:
            raise ValueError("empty subject/body")
        return {"subject": subject, "body": body}
    except (json.JSONDecodeError, ValueError):
        # Fallback: use the topic as subject and the raw text as body
        return {"subject": topic[:78], "body": text}


def regenerate(topic: str, previous_body: str, feedback: str = "") -> dict:
    """Ask for a fresh take, optionally steered by feedback on the previous draft."""
    instruction = (
        f"Original topic: {topic}\n\n"
        f"Here was a previous draft you wrote:\n{previous_body}\n\n"
        "Write a NEW, different version of this email."
    )
    if feedback:
        instruction += f"\n\nIncorporate this feedback: {feedback}"
    return draft_email(instruction)
