"""Small domain model shared by the database and UI."""

from dataclasses import dataclass
from datetime import datetime

TASK_TYPES = ("作业", "考试", "活动报名", "其他")
# Every value here is allowed by the tasks.source CHECK constraint; adding one
# means a schema migration (see database.SCHEMA_VERSION), not just an edit.
SOURCES = ("manual", "wechat", "teaching_platform", "programming_grid", "email")


@dataclass(frozen=True)
class Task:
    id: int
    title: str
    category: str
    task_type: str
    due_at: datetime | None
    source: str
    completed: bool
    created_at: datetime
    updated_at: datetime
    revision: int = 0
    deadline_revision: int = 0
    deleted_at: datetime | None = None

    @property
    def needs_confirmation(self) -> bool:
        return self.due_at is None
