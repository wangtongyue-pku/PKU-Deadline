"""Remember the programming-grid courses you synced, so the next sync is a pick.

The course id (32 hex characters from ``/course/<id>``) is the only handle the
platform needs, and retyping it from the address bar is the most annoying step in
the whole app. This module keeps a tiny local list of ids that already worked,
next to the database, and nothing else: no cookies, no credentials, no page text.

Losing or corrupting this file is harmless by design - it only pre-fills a form.
"""

from dataclasses import dataclass
from datetime import datetime
import json
import os
import re
from pathlib import Path

from pku_deadline_agent.config import database_path
from pku_deadline_agent.time_utils import beijing_now

COURSE_STORE_ENV = "PKU_GRID_COURSES"
MAX_COURSES = 20
_COURSE_ID = re.compile(r"[0-9a-fA-F]{32}")


@dataclass(frozen=True)
class KnownCourse:
    course_id: str
    label: str
    seen_at: str = ""

    def display(self) -> str:
        """Short, unambiguous option text: course name plus an id prefix."""
        name = self.label.strip() or "未命名课程"
        return f"{name} · {self.course_id[:8]}…"


def store_path() -> Path:
    override = os.environ.get(COURSE_STORE_ENV, "").strip()
    if override:
        return Path(override)
    return database_path().parent / "grid-courses.json"


def is_course_id(value: str | None) -> bool:
    return isinstance(value, str) and bool(_COURSE_ID.fullmatch(value.strip()))


def known(path: Path | None = None) -> tuple[KnownCourse, ...]:
    """Newest first. A missing or damaged file simply means "nothing remembered"."""
    source = Path(path) if path is not None else store_path()
    try:
        # utf-8-sig: a hand-edited file may carry a BOM, which must not hide the list.
        raw = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ()
    entries = raw.get("courses") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return ()
    courses = []
    for entry in entries:
        if not isinstance(entry, dict) or not is_course_id(entry.get("id")):
            continue
        courses.append(KnownCourse(str(entry["id"]).strip(),
                                   str(entry.get("label") or ""),
                                   str(entry.get("seen") or "")))
    return tuple(courses[:MAX_COURSES])


def remember(course_id: str, label: str = "", path: Path | None = None) -> None:
    """Upsert one successfully synced course. Validation errors raise ValueError."""
    if not is_course_id(course_id):
        raise ValueError("课程编号应为 32 位十六进制。")
    source = Path(path) if path is not None else store_path()
    entry = {"id": course_id.strip(), "label": label.strip()[:80],
             "seen": beijing_now().isoformat(timespec="seconds")}
    others = [course for course in known(source) if course.course_id != entry["id"]]
    payload = {"version": 1, "courses": [
        {"id": entry["id"], "label": entry["label"], "seen": entry["seen"]},
        *({"id": course.course_id, "label": course.label, "seen": course.seen_at}
          for course in others[:MAX_COURSES - 1]),
    ]}
    source.parent.mkdir(parents=True, exist_ok=True)
    temporary = source.with_suffix(source.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(source)


def last_seen(path: Path | None = None) -> str:
    """The most recent successful sync time, or an empty string."""
    courses = known(path)
    stamps = [course.seen_at for course in courses if course.seen_at]
    if not stamps:
        return ""
    try:
        return datetime.fromisoformat(max(stamps)).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ""
