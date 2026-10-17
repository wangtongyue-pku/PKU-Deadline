"""Finite, user-authorized list scrolling; no navigation, clicks, AI or database.

One ephemeral handle guards the exact list/course DOM while Python reuses the
bounded reader. Ordinary/lazy lists must preserve their existing node prefix;
virtual lists require verified logical positions. Neither is a persistent ID.
"""

from dataclasses import dataclass, field, replace
import time

from pku_deadline_agent.zhihuishu_reader import (
    BoundedTaskListReader, ReadTask, ReaderError, ReaderLimitExceeded, ReaderUnstable, possible_matches,
)
from pku_deadline_agent.grid_session import SessionAuthRequired

# Per-card field gaps are derived from the FINAL surfaced tasks (what the user
# actually sees), never from transient observations: the reader reads each view
# twice, so mid-render misses would otherwise be double counted.
FIELD_GAP_CODES = frozenset({
    'title_missing', 'deadline_missing', 'deadline_time_missing', 'course_binding_missing',
})


@dataclass(frozen=True)
class ScrollPolicy:
    mode: str = 'ordinary'  # ordinary / lazy / virtual; no guessing from names.
    container: str | None = None  # None explicitly means the document scroller.
    end: str | None = None  # An independently verified visible end marker.
    boundary_verified: bool = False
    max_scrolls: int = 12  # Includes initial reset to top, if required.
    settle_rounds: int = 3
    wait_ms: int = 150
    max_seconds: int = 20
    auto_container: bool = False  # Geometry only; never guess a platform selector.

    def __post_init__(self):
        if self.mode not in ('ordinary', 'lazy', 'virtual'):
            raise ValueError('必须明确列表滚动方式。')
        for value, lower, upper in ((self.max_scrolls, 1, 30), (self.settle_rounds, 2, 5),
                                    (self.wait_ms, 50, 500), (self.max_seconds, 1, 60)):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError('滚动次数 / 等待 / 时间上限无效。')
        if type(self.boundary_verified) is not bool:
            raise ValueError('边界证据标记必须为布尔值。')
        if type(self.auto_container) is not bool or (self.auto_container and self.container):
            raise ValueError('自动定位滚动容器不能与指定容器混用。')


@dataclass(frozen=True)
class ScrollCoverage:
    displayed_total: int | None
    loaded_peak: int
    loaded_final: int
    observed_count: int
    read_count: int
    scroll_count: int
    bottom_settled: bool
    inventory_complete: bool
    fields_complete: bool
    complete: bool
    completeness_basis: str
    unread_minimum: int
    reasons: tuple[str, ...]
    # Per-code counts of the reader's own issues (never task text or identities).
    # Without them a partial read says "fields incomplete" but not WHY, and the
    # next authorized read would have to guess which field failed to bind.
    issue_counts: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class ScrollReport:
    tasks: tuple[ReadTask, ...] = field(repr=False)
    coverage: ScrollCoverage
    course: str | None = field(repr=False)
    evidence: str
    requires_identity_review: bool = True

    @property
    def ready_for_sync(self) -> bool:
        return False

    @property
    def preview_ready(self) -> bool:
        # Allows later REVIEW UI development; never authorizes a database write.
        return self.coverage.complete


# No text is read here: only node references, mutation flags and scroll geometry.
# This private observer belongs to our handle, not the website's hidden state.
_CONTROLLER = r"""p => {
 const rendered=e=>{for(let n=e;n;n=n.parentElement){const s=getComputedStyle(n);
   if(s.display==='none'||s.visibility==='hidden'||n.getAttribute('aria-hidden')==='true')return false;}return true;};
 const selectedView=()=>{
   const tabs=[...document.querySelectorAll('.ant-tabs-tab[aria-selected="true"]')].filter(rendered);
   const panes=[...document.querySelectorAll('.ant-tabs-tabpane-active')].filter(rendered);
   const states=panes.length===1?[...panes[0].querySelectorAll('.state-tabs .menu-pane.active')].filter(rendered):[];
   return tabs.length===1&&tabs[0].innerText.trim()==='作业'&&panes.length===1&&
     states.length===1&&states[0].innerText.trim()==='未完成'&&!document.querySelector('input[type=password]');
 };
 if(!selectedView())return null;
 const list=document.querySelector('.ant-tabs-tabpane-active .homework-content');
 const scope=list&&list.closest(p.course_scope);
 const names=scope?[...scope.querySelectorAll(p.course)]:[];
 // Inspect only ancestors of this exact list. Unrelated sidebars are irrelevant;
 // multiple independently scrollable ancestors are ambiguous, so fail closed.
 const ancestors=[];
 if(p.auto_container&&list)for(let n=list;n&&n!==document.body&&n!==document.documentElement;n=n.parentElement){
   const s=getComputedStyle(n);
   if(/^(auto|scroll)$/.test(s.overflowY)&&n.scrollHeight>n.clientHeight+2&&n.clientHeight>0)ancestors.push(n);
 }
 if(ancestors.length>1)return null;
 const scroller=p.container?document.querySelector(p.container):
   (ancestors[0]||document.scrollingElement);
 if(!list||!scope||names.length!==1||!scroller||!scroller.contains(list)||
    (p.container&&document.querySelectorAll(p.container).length!==1)) return null;
 const name=names[0];
 let old=[...list.querySelectorAll('.homework-item')], dirty=false;
 const observer=new MutationObserver(records=>{
   for(const record of records) {
     if(name.contains(record.target))dirty=true;
     if(p.mode!=='virtual' && old.some(card=>card.contains(record.target)) &&
       (record.type==='childList'||(record.target.nodeType===3?record.target.parentElement:record.target)
         ?.closest(p.title+','+p.deadline)))dirty=true;
   }
 });
 observer.observe(name,{subtree:true,characterData:true,childList:true});
 observer.observe(list,{subtree:true,characterData:true,childList:true});
 const valid=()=>{
   const all=[...list.querySelectorAll('.homework-item')];
   if(dirty||!selectedView()||!list.isConnected||!scope.isConnected||!name.isConnected||
      !scroller.isConnected||!scroller.contains(list)||
      list!==document.querySelector('.ant-tabs-tabpane-active .homework-content')||
      list.closest(p.course_scope)!==scope||scope.querySelectorAll(p.course).length!==1||
      scope.querySelector(p.course)!==name||all.length>1000)return false;
   if(p.mode!=='virtual' && !old.every((node,i)=>all[i]===node))return false;
   old=all;
   return true;
 };
 const endVisible=()=>{
   if(!p.end)return false;
   const nodes=[...scope.querySelectorAll(p.end)];
   if(nodes.length!==1)return false;
   const e=nodes[0],r=e.getBoundingClientRect(),s=getComputedStyle(e);
   let left=Math.max(0,r.left),right=Math.min(innerWidth,r.right),top=Math.max(0,r.top),bottom=Math.min(innerHeight,r.bottom);
   for(let n=e;n;n=n.parentElement){
     const style=getComputedStyle(n);
     if(style.display==='none'||style.visibility==='hidden'||n.getAttribute('aria-hidden')==='true')return false;
     if(n!==e&&/(auto|scroll|hidden|clip)/.test(style.overflow+style.overflowX+style.overflowY)){
       const b=n.getBoundingClientRect();left=Math.max(left,b.left);right=Math.min(right,b.right);
       top=Math.max(top,b.top);bottom=Math.min(bottom,b.bottom);
     }
   }
   return right>left&&bottom>top;
 };
 return {probe(action){
   if(!valid())return {valid:false};
   const before=scroller.scrollTop;
   if(action==='top')scroller.scrollTo({top:0,behavior:'instant'});
   if(action==='next')scroller.scrollTo({top:before+Math.max(1,Math.floor(scroller.clientHeight/2)),behavior:'instant'});
   const top=scroller.scrollTop,height=scroller.scrollHeight,viewport=scroller.clientHeight;
   return {valid:true,top,height,viewport,bottom:top+viewport>=height-2,
           end_visible:endVisible(),moved:top!==before};
 },close(){observer.disconnect();}};
}"""


class BoundedScrollReader:
    def __init__(self, reader: BoundedTaskListReader, policy: ScrollPolicy = ScrollPolicy()):
        self.reader, self.policy = reader, policy

    def read(self, page, *, allow_real: bool = False, validation_only: bool = False) -> ScrollReport:
        url, evidence, _ = self.reader.check_access(page, allow_real=allow_real, validation_only=validation_only)
        policy, profile = self.policy, self.reader.profile
        if policy.mode == 'virtual' and profile.scan_position_attribute is None:
            raise ReaderError('虚拟列表缺少已核验的逻辑位置；不能凭标题或复用节点合并。')
        if policy.mode != 'virtual' and profile.scan_position_attribute is not None:
            raise ReaderError('普通 / 懒加载列表使用受保护的本次 DOM 顺序，不混用虚拟位置。')
        params = dict(course_scope=profile.course_scope, course=profile.course, title=profile.title,
                      deadline=profile.deadline, container=policy.container, end=policy.end, mode=policy.mode,
                      auto_container=policy.auto_container)
        collected: dict[int, ReadTask] = {}
        seen: set[int] = set()
        reasons: list[str] = []
        issue_tally: dict[str, set] = {}
        course = None
        total = None
        loaded_peak = loaded_final = scrolls = stable_bottom = 0
        bottom_settled = False
        last_bottom_signature = None
        expires = time.monotonic() + policy.max_seconds
        handle = None
        empty_proven = False
        has_next_observed = end_visible = False
        unstable_attempts = 0
        try:
            handle = page.evaluate_handle(_CONTROLLER, params)

            def probe(action='observe'):
                if page.url != url:
                    raise ReaderError('读取期间发生跳转。')
                result = handle.evaluate('(r,a)=>r?r.probe(a):{valid:false}', action)
                if not result.get('valid'):
                    raise ReaderError('课程 / 列表节点或既有任务发生变化。')
                return result

            geometry = probe()
            if geometry['top'] > 0:
                geometry = probe('top')
                scrolls += 1
                page.wait_for_timeout(policy.wait_ms)
            # Bound BOTH movements and observations; a stalled/lazy page cannot loop.
            for _ in range((policy.max_scrolls + 1) * (policy.settle_rounds + 1)):
                if time.monotonic() >= expires:
                    reasons.append('time_limit')
                    break
                probe()
                try:
                    report = self.reader.read(page, allow_real=allow_real, validation_only=validation_only,
                                              capture_fragments=True, seen_positions=tuple(sorted(seen)))
                except ReaderLimitExceeded as error:
                    seen.update(error.observed_positions)
                    loaded_peak = max(loaded_peak, error.loaded)
                    reasons.append('task_limit')
                    break
                except ReaderUnstable as error:
                    seen.update(error.observed_positions)
                    unstable_attempts += 1
                    if unstable_attempts >= policy.settle_rounds:
                        reasons.append('unstable_list')
                        break
                    probe()
                    page.wait_for_timeout(policy.wait_ms)
                    continue
                unstable_attempts = 0
                geometry = probe()
                for issue in report.issues:
                    # Structural codes only; field gaps are derived from the final
                    # tasks below, so a twice-read view is not counted twice.
                    if issue.code in FIELD_GAP_CODES:
                        continue
                    issue_tally.setdefault(issue.code, set()).add(issue.position)
                loaded_final = report.coverage.loaded_count
                has_next_observed = has_next_observed or report.coverage.has_next is True
                end_visible = geometry['end_visible']
                loaded_peak = max(loaded_peak, loaded_final)
                seen.update(report.observed_positions)
                if len(seen) > self.reader.limit:
                    raise ReaderError('累计读取上限校验失败。')
                if report.course_context:
                    if course and course != report.course_context:
                        raise ReaderError('滚动期间课程发生变化。')
                    course = report.course_context
                if report.coverage.displayed_total is not None:
                    if total is not None and total != report.coverage.displayed_total:
                        raise ReaderError('滚动期间显示总数发生变化。')
                    total = report.coverage.displayed_total
                fatal = {i.code for i in report.issues} - {
                    'course_binding_missing', 'title_missing', 'deadline_missing',
                    'deadline_time_missing', 'empty_not_proven',
                }
                if fatal:
                    reasons.append('invalid_or_ambiguous_fields')
                    break
                empty_proven = empty_proven or report.confirmed_empty
                for fragment in report.fragments:
                    position = fragment.scan_position
                    if position is None:
                        raise ReaderError('缺少本次列表位置，不能合并字段片段。')
                    old = collected.get(position)
                    if old:
                        if (old.title and fragment.title and old.title != fragment.title or
                            old.deadline and fragment.deadline and old.deadline != fragment.deadline or
                            old.deadline_date and fragment.deadline_date and old.deadline_date != fragment.deadline_date):
                            raise ReaderError('同一列表位置字段变化，不能拼接新旧任务。')
                        for key in ('observed_task_id','observed_course_id','observed_enrollment_id','source_url'):
                            if getattr(old,key) and getattr(fragment,key) and getattr(old,key)!=getattr(fragment,key):
                                raise ReaderError('同一位置来源信息变化，不能继续绑定。')
                        fragment = replace(fragment, title=fragment.title or old.title,
                                           deadline=fragment.deadline or old.deadline,
                                           deadline_date=fragment.deadline_date or old.deadline_date,
                                           source_url=fragment.source_url or old.source_url,
                                           observed_task_id=fragment.observed_task_id or old.observed_task_id,
                                           observed_course_id=fragment.observed_course_id or old.observed_course_id,
                                           observed_enrollment_id=fragment.observed_enrollment_id or old.observed_enrollment_id)
                    collected[position] = replace(fragment, course=course or fragment.course)
                if course:
                    collected = {p: replace(t, course=course) for p, t in collected.items()}
                signature = (geometry['height'], geometry['top'], loaded_final, tuple(sorted(seen)))
                if geometry['bottom'] and not report.coverage.loading:
                    stable_bottom = stable_bottom + 1 if signature == last_bottom_signature else 1
                    last_bottom_signature = signature
                    if stable_bottom >= policy.settle_rounds:
                        bottom_settled = True
                        break
                    page.wait_for_timeout(policy.wait_ms)
                    continue
                stable_bottom = 0
                last_bottom_signature = None
                if report.coverage.loading:
                    page.wait_for_timeout(policy.wait_ms)
                    continue
                if scrolls >= policy.max_scrolls:
                    reasons.append('scroll_limit')
                    break
                geometry = probe('next')
                scrolls += 1
                page.wait_for_timeout(policy.wait_ms)
            else:
                reasons.append('observation_limit')
        except SessionAuthRequired:
            reasons.append('session_auth_required')
        except ReaderError:
            reasons.append('source_or_structure_changed')
        except Exception:
            # No raw browser exceptions or task text in diagnostics.
            reasons.append('browser_read_failed')
        finally:
            if handle is not None:
                try:
                    handle.evaluate('r=>{if(r)r.close()}')
                except Exception:
                    pass
                try:
                    handle.dispose()
                except Exception:
                    pass
        # Surface EVERY position that carries any field, not only the fully bound
        # ones: a card whose title leaf did not resolve is still a real task the
        # user can complete by hand in the review page. Dropping it silently is
        # what made a 5-card list look like a 1-task list.
        tasks = tuple(collected[p] for p in sorted(collected)
                      if collected[p].title or collected[p].deadline or collected[p].deadline_date)
        # Field gaps of the FINAL result: the honest answer to "what is missing".
        if not course:
            issue_tally.setdefault('course_binding_missing', set()).add(None)
        for position, task in collected.items():
            if not task.title:
                issue_tally.setdefault('title_missing', set()).add(position)
            elif task.deadline is None:
                code = 'deadline_time_missing' if task.deadline_date else 'deadline_missing'
                issue_tally.setdefault(code, set()).add(position)
        if not course:
            reasons.append('course_not_bound')
        if not bottom_settled:
            reasons.append('end_not_settled')
        if not policy.boundary_verified:
            reasons.append('boundary_unverified')
        if has_next_observed:
            reasons.append('pagination_not_traversed')
        if policy.end and not end_visible:
            reasons.append('end_marker_not_visible')
        # A stable scroll height alone is never proof of complete loading.
        end_proven = bool(policy.boundary_verified and policy.end and end_visible and bottom_settled)
        expected = total if total is not None else (max(seen,default=0) if end_proven else None)
        contiguous = (seen == set(range(1,expected+1))) if expected is not None and expected <= self.reader.limit else False
        basis = 'displayed_total' if total is not None else ('verified_end_marker' if end_proven else 'unverified')
        if total is None and not end_proven:
            reasons.append('displayed_total_unknown')
        elif not contiguous:
            reasons.append('unread_positions')
        inventory = bool(bottom_settled and policy.boundary_verified and contiguous and not reasons)
        if expected == 0 and not empty_proven:
            inventory = False
            reasons.append('empty_not_proven')
        fields = bool(len(tasks) == len(seen) and course
                      and all(t.title and t.deadline is not None for t in tasks))
        if not fields:
            reasons.append('fields_incomplete')
        lower_bound = total if total is not None else (loaded_peak if policy.mode != 'virtual' else len(seen))
        coverage = ScrollCoverage(total, loaded_peak, loaded_final, len(seen), len(tasks), scrolls,
                                  bottom_settled, inventory, fields, inventory and fields, basis,
                                  max(0, lower_bound - len(seen)), tuple(dict.fromkeys(reasons)),
                                  tuple(sorted((code, len(where)) for code, where in issue_tally.items())))
        return ScrollReport(tasks, coverage, course, evidence)


@dataclass(frozen=True)
class MatchAssessment:
    candidates: tuple[int, ...]
    outcome: str
    requires_confirmation: bool = True


def assess_match(task: ReadTask, previous: tuple[ReadTask, ...]) -> MatchAssessment:
    """Review suggestions only, reusing the existing conservative matching rule."""
    if not task.course:
        return MatchAssessment((), 'course_unknown')
    matches = possible_matches(task, previous)
    if not matches:
        return MatchAssessment((), 'new_candidate')
    if len(matches) > 1:
        return MatchAssessment(matches, 'ambiguous_namesakes')
    old = previous[matches[0]]
    changed=(old.deadline,old.deadline_date)!=(task.deadline,task.deadline_date)
    return MatchAssessment(matches, 'possible_deadline_change' if changed else 'possible_duplicate')
