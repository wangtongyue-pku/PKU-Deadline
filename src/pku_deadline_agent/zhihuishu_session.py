"""One scope-capped read whose standardized output feeds the review preview.

The reader and scroll modules deliberately never navigate, log in or persist a
session; the caller hands over a Page already showing the agreed list. This
module exists so an authorized read has exactly one composition point: the task
cap, the scroll cap and the list mode are stated once, validated here, and the
result is a plain :class:`ScrollReport` the existing review page already
understands.

Nothing here writes to any database, imports a model client, or touches the
network beyond the page the caller provided. A real read still requires the
reader's own ``allow_real`` / ``validation_only`` gates plus a separately
granted, per-round user authorization — this module does not grant anything.
"""

from dataclasses import dataclass

from pku_deadline_agent.zhihuishu_reader import BoundedTaskListReader, ListProfile
from pku_deadline_agent.zhihuishu_scroll import BoundedScrollReader, ScrollPolicy, ScrollReport

# Hard bounds, wider than any authorized round so far: the reader itself refuses
# more than 20 visible tasks, and the scroll policy refuses more than 30 moves.
MAX_TASKS_HARD_LIMIT = 20
MAX_SCROLLS_HARD_LIMIT = 30


@dataclass(frozen=True)
class ReviewReadScope:
    """The scope one review read may cover.

    Defaults encode the proposed first real validation (first screen of the
    homework → unfinished list, at most 5 tasks, one positioning move, no
    verified end marker — therefore explicitly a **partial** read). A wider scope
    is a per-round user decision, expressed by passing different values, never by
    relaxing these bounds.
    """

    max_tasks: int = 5
    max_scrolls: int = 1
    mode: str = 'ordinary'
    container: str | None = None
    end: str | None = None
    boundary_verified: bool = False
    auto_container: bool = False

    def __post_init__(self):
        if self.mode not in ('ordinary', 'lazy', 'virtual'):
            raise ValueError('必须明确列表滚动方式。')
        for name, value, upper in (('max_tasks', self.max_tasks, MAX_TASKS_HARD_LIMIT),
                                   ('max_scrolls', self.max_scrolls, MAX_SCROLLS_HARD_LIMIT)):
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError(f'读取范围无效：{name} 必须为 1–{upper}。')
        if type(self.boundary_verified) is not bool:
            raise ValueError('边界证据标记必须为布尔值。')
        if type(self.auto_container) is not bool or (self.auto_container and self.container):
            raise ValueError('自动定位与指定滚动容器不能混用。')


def read_list_for_review(page, profile: ListProfile, *, scope: ReviewReadScope = ReviewReadScope(),
                         allow_real: bool = False,
                         validation_only: bool = False) -> ScrollReport:
    """Read the list currently on ``page`` within ``scope`` and return the report.

    The result is a standardized :class:`ScrollReport` — the same shape the
    review page renders and the sandbox import validates. Its ``evidence`` field
    records where the data came from: fictional pages read as ``synthetic*``,
    authorized real pages as ``authorized-real-*``. The separate reviewed import
    contract pairs fictional evidence with sandboxes, real evidence with an
    explicitly authorized production target; this function never writes.
    """
    reader = BoundedTaskListReader(profile, limit=scope.max_tasks)
    policy = ScrollPolicy(mode=scope.mode, container=scope.container, end=scope.end,
                          boundary_verified=scope.boundary_verified,
                          max_scrolls=scope.max_scrolls, auto_container=scope.auto_container)
    return BoundedScrollReader(reader, policy).read(
        page, allow_real=allow_real, validation_only=validation_only)
