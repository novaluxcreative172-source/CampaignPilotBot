"""Simple JSON-backed storage for named, reusable recipient lists.

Good enough to start with — persists across bot restarts as long as the
container's filesystem isn't wiped. If you outgrow it, swap this module for
a real database (Railway's Postgres add-on is an easy upgrade) without
touching bot.py, since everything goes through the functions below.
"""
import json
import os
import threading

DATA_PATH = os.environ.get(
    "LISTS_DATA_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "lists.json"),
)
_lock = threading.Lock()


def _ensure_file():
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    if not os.path.exists(DATA_PATH):
        with open(DATA_PATH, "w") as f:
            json.dump({}, f)


def _load() -> dict:
    _ensure_file()
    with open(DATA_PATH, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def _save(data: dict):
    _ensure_file()
    with open(DATA_PATH, "w") as f:
        json.dump(data, f, indent=2)


def get_lists(user_id: int) -> dict:
    """Returns {list_name: [emails]} for this user."""
    data = _load()
    return data.get(str(user_id), {})


def get_list(user_id: int, name: str) -> list:
    return get_lists(user_id).get(name, [])


def save_list(user_id: int, name: str, emails: list) -> int:
    """Creates or overwrites a named list. Returns the number of emails saved."""
    with _lock:
        data = _load()
        user_lists = data.setdefault(str(user_id), {})
        deduped = sorted(set(e.lower() for e in emails))
        user_lists[name] = deduped
        _save(data)
        return len(deduped)


def delete_list(user_id: int, name: str) -> bool:
    with _lock:
        data = _load()
        user_lists = data.get(str(user_id), {})
        if name in user_lists:
            del user_lists[name]
            _save(data)
            return True
        return False
