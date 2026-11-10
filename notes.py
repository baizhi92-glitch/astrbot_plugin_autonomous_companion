"""关于用户的统一记录：偏好、事实与爱好共用一份清单。

偏好（preference）约束她怎么对待用户；事实（fact）与爱好（interest）描述
用户这个人。三者放在同一个映射里，用 kind 区分，避免两套结构重复维护。
"""

import time

NOTE_KINDS = ("preference", "fact", "interest")

NOTE_KIND_LABELS = {"preference": "偏好", "fact": "事实", "interest": "爱好"}


def note_kind(item) -> str:
    """Normalize a note's kind to one of the supported labels.

    Args:
        item: Stored note mapping.

    Returns:
        ``preference``, ``fact`` or ``interest``; unknown values fall back to fact.
    """
    kind = item.get("kind") if isinstance(item, dict) else None
    return kind if kind in NOTE_KINDS else "fact"


def free_note_key(notes: dict, key: str) -> str:
    """Find a key that is not used yet, appending a counter when needed.

    Args:
        notes: Target note mapping.
        key: Preferred key.

    Returns:
        A key that does not collide with existing rows.
    """
    candidate = key
    index = 0
    while candidate in notes:
        index += 1
        candidate = f"{key[:76]}（{index}）"
    return candidate


def migrate_user_notes(state: dict) -> bool:
    """Fold the old separate preference and impression stores into one list.

    Preferences become ``kind=preference`` notes and stay confirmed, since they
    may only come from the user's own wording; impressions keep their kind and
    confirmation state.

    Args:
        state: Per-conversation state, updated in place.

    Returns:
        Whether anything was migrated.
    """
    if not isinstance(state, dict):
        return False
    notes = state.get("notes")
    if not isinstance(notes, dict):
        notes = {}
    migrated = False
    legacy_prefs = state.pop("preferences", None)
    if isinstance(legacy_prefs, dict) and legacy_prefs:
        migrated = True
        for key, pref in legacy_prefs.items():
            if not isinstance(pref, dict):
                continue
            value = str(pref.get("value") or "").strip()
            if not value:
                continue
            entry = {
                "value": value[:600],
                "kind": "preference",
                "confirmed": True,
                "evidence": str(pref.get("evidence") or "").strip()[:300]
                or "用户手动添加",
                "source": str(pref.get("source") or "explicit_user"),
                "created_at": pref.get("created_at")
                or pref.get("updated_at")
                or time.time(),
                "updated_at": pref.get("updated_at") or time.time(),
                "pinned": bool(pref.get("pinned", False)),
            }
            if pref.get("previous"):
                entry["previous"] = str(pref["previous"])[:600]
            if pref.get("changed_at"):
                entry["changed_at"] = pref["changed_at"]
            notes[free_note_key(notes, str(key).strip()[:80] or "偏好")] = entry
    legacy_notes = state.pop("impressions", None)
    if isinstance(legacy_notes, dict) and legacy_notes:
        migrated = True
        for key, item in legacy_notes.items():
            if not isinstance(item, dict):
                continue
            value = str(item.get("value") or "").strip()
            if not value:
                continue
            entry = {
                "value": value[:600],
                "kind": note_kind(item),
                "confirmed": bool(item.get("confirmed")),
                "evidence": str(item.get("evidence") or "").strip()[:300]
                or "用户手动添加",
                "source": str(item.get("source") or "chat"),
                "created_at": item.get("created_at")
                or item.get("updated_at")
                or time.time(),
                "updated_at": item.get("updated_at") or time.time(),
                "pinned": bool(item.get("pinned", False)),
            }
            if item.get("confirmed_at"):
                entry["confirmed_at"] = item["confirmed_at"]
            if item.get("previous"):
                entry["previous"] = str(item["previous"])[:600]
            if item.get("changed_at"):
                entry["changed_at"] = item["changed_at"]
            notes[free_note_key(notes, str(key).strip()[:80] or "印象")] = entry
    state["notes"] = notes
    return migrated
