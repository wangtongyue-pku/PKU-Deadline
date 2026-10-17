"""Finite scrolling on fictional offline pages; never connect a real account."""

from dataclasses import replace
from pathlib import Path

import pytest

from test_zhihuishu_reader import browser, page  # shared, network-blocking fixtures
from pku_deadline_agent.zhihuishu_reader import BoundedTaskListReader, ListProfile, ReadTask, ReaderError
from pku_deadline_agent.zhihuishu_reader import ReaderLimitExceeded, ReaderUnstable
from pku_deadline_agent.zhihuishu_scroll import BoundedScrollReader, ScrollPolicy, assess_match

HTML=Path(__file__).with_name('fixtures').joinpath('zhihuishu_scrolling.html').read_text(encoding='utf-8')
PROFILE=ListProfile(title='.title-wrapper > .tit', course_scope='.fixture-scope', course='.fixture-course',
                    deadline='.homework-item-time > span:first-child',total='.fixture-total')


def prepare(page, *, mode='ordinary', count=6, **options):
    page.set_content(HTML)
    page.evaluate('o=>setupList(o)', {'mode':mode,'count':count,**options})


def collect(page, *, mode='ordinary', document=False, **options):
    profile=replace(PROFILE,scan_position_attribute='data-index',scan_position_base=0) if mode=='virtual' else PROFILE
    policy=ScrollPolicy(mode=mode,container=None if document else '.fixture-scroll',
                        boundary_verified=True,wait_ms=100,max_scrolls=30,max_seconds=15,**options)
    return BoundedScrollReader(BoundedTaskListReader(profile),policy).read(page)


@pytest.mark.parametrize('mode', ['ordinary','lazy','virtual'])
def test_complete_list_with_same_task_field_binding(page,mode):
    prepare(page,mode=mode,count=6)
    result=collect(page,mode=mode)
    assert result.coverage.complete, result.coverage
    assert result.coverage.displayed_total==result.coverage.observed_count==result.coverage.read_count==6
    assert [t.title for t in result.tasks]==[f'虚构滚动任务{i}' for i in range(1,7)]
    assert [t.deadline.day for t in result.tasks]==list(range(1,7))
    assert all(t.course=='虚构滚动课程' and t.status=='未完成' for t in result.tasks)
    assert not result.ready_for_sync and result.preview_ready and result.requires_identity_review
    assert page.evaluate('sideEffects')==0
    assert page.url=='about:blank'


def test_document_scroll_keeps_verified_visible_course_binding(page):
    prepare(page,count=8,document=True)
    result=collect(page,document=True)
    assert result.coverage.complete,result.coverage
    assert page.evaluate('scrollY')>0
    assert all(t.course=='虚构滚动课程' for t in result.tasks)


@pytest.mark.parametrize('document', [False, True])
def test_auto_container_uses_list_ancestors_and_preserves_pairing(page, document):
    prepare(page, count=5, document=document)
    # An unrelated scrolling sidebar must never be selected or moved.
    page.evaluate("""()=>{const e=document.createElement('aside');
        e.id='unrelated'; e.style.cssText='position:fixed;right:0;top:0;height:80px;overflow:auto';
        e.innerHTML='<div style="height:900px">虚构侧栏</div>';document.body.append(e);} """)
    result = BoundedScrollReader(BoundedTaskListReader(PROFILE), ScrollPolicy(
        auto_container=True, boundary_verified=True, max_scrolls=12, wait_ms=50)).read(page)
    assert result.coverage.complete, result.coverage
    assert [(t.title, t.deadline.day) for t in result.tasks] == [
        (f'虚构滚动任务{i}', i) for i in range(1, 6)]
    assert page.eval_on_selector('#unrelated', 'e=>e.scrollTop') == 0
    assert page.evaluate('sideEffects') == 0


def test_auto_container_ambiguous_nested_scroll_stops(page):
    prepare(page, count=5)
    page.eval_on_selector('.ant-tabs-tabpane-active',
                         "e=>e.style.cssText='height:180px;overflow:auto'")
    result = BoundedScrollReader(BoundedTaskListReader(PROFILE),
                                 ScrollPolicy(auto_container=True)).read(page)
    assert not result.tasks
    assert 'source_or_structure_changed' in result.coverage.reasons


def test_auto_container_retains_round_task_and_move_caps(page):
    prepare(page, count=9)
    result = BoundedScrollReader(BoundedTaskListReader(PROFILE, limit=3), ScrollPolicy(
        auto_container=True, max_scrolls=12, wait_ms=50)).read(page)
    assert result.coverage.observed_count <= 3 and len(result.tasks) <= 3
    assert 'task_limit' in result.coverage.reasons
    prepare(page, count=5)
    limited = BoundedScrollReader(BoundedTaskListReader(PROFILE), ScrollPolicy(
        auto_container=True, max_scrolls=1, wait_ms=50)).read(page)
    assert limited.coverage.scroll_count == 1 and not limited.coverage.complete
    assert 'scroll_limit' in limited.coverage.reasons


def test_tall_cards_merge_only_fragments_from_same_position(page):
    prepare(page,count=3,tall=True)
    result=collect(page)
    assert result.coverage.complete,result.coverage
    assert [(t.title,t.deadline.day) for t in result.tasks]==[(f'虚构滚动任务{i}',i) for i in range(1,4)]


@pytest.mark.parametrize('mode',['ordinary','lazy','virtual'])
def test_twenty_task_limit_applies_to_entire_scroll_session(page,mode):
    prepare(page,mode=mode,count=23)
    if mode=='lazy':
        page.add_style_tag(content='.homework-item{height:120px}.homework-item-time{top:80px}')
        page.eval_on_selector('.fixture-scroll',"e=>e.style.height='200px'")
    result=collect(page,mode=mode)
    assert result.coverage.observed_count<=20 and len(result.tasks)<=20
    assert not result.coverage.complete
    assert 'task_limit' in result.coverage.reasons
    assert result.coverage.unread_minimum>=3


def test_card_without_a_bound_title_is_surfaced_for_manual_completion(page):
    """A card whose title leaf did not resolve must not be silently dropped.

    It stays in the report with an empty title, the coverage says the fields are
    incomplete, and the issue tally names the reason - so the review page can ask
    the user to fill it in instead of the list looking short.
    """
    prepare(page,count=3)
    page.eval_on_selector_all('.homework-item .tit','es=>es[1].remove()')
    result=collect(page)
    assert len(result.tasks)==3 and result.coverage.read_count==3
    assert result.tasks[1].title=='' and result.tasks[1].deadline is not None
    assert not result.coverage.fields_complete
    assert 'fields_incomplete' in result.coverage.reasons
    assert dict(result.coverage.issue_counts).get('title_missing')==1


def test_namesakes_are_distinct_tasks_not_merged(page):
    prepare(page,count=5,namesake=True)
    result=collect(page)
    assert result.coverage.complete and len(result.tasks)==5
    assert len({t.title for t in result.tasks})==1
    match=assess_match(result.tasks[0],result.tasks)
    assert match.outcome=='ambiguous_namesakes' and len(match.candidates)==5
    assert match.requires_confirmation


@pytest.mark.parametrize('change,reason',[('changeCourse','source_or_structure_changed'),('reorder','source_or_structure_changed')])
def test_course_change_and_row_reorder_stop_without_wrong_binding(page,change,reason):
    prepare(page,count=6,**{change:True})
    result=collect(page)
    assert not result.coverage.complete and reason in result.coverage.reasons
    assert not result.preview_ready


@pytest.mark.parametrize('option',['missingTitle','missingDeadline'])
def test_missing_field_not_borrowed_from_neighbor(page,option):
    prepare(page,count=4,**{option:1})
    result=collect(page)
    assert not result.coverage.complete and 'fields_incomplete' in result.coverage.reasons
    if option=='missingDeadline':
        assert result.tasks[1].deadline is None
    else:
        assert '虚构滚动任务2' not in [t.title for t in result.tasks]


def test_unknown_total_or_boundary_cannot_claim_complete(page):
    prepare(page,count=4)
    page.eval_on_selector('.fixture-total','e=>e.remove()')
    result=collect(page)
    assert len(result.tasks)==4 and not result.coverage.inventory_complete
    assert 'displayed_total_unknown' in result.coverage.reasons


@pytest.mark.parametrize('mode',['ordinary','virtual'])
def test_verified_visible_end_can_prove_range_without_inventing_total(page,mode):
    prepare(page,mode=mode,count=6)
    page.eval_on_selector('.fixture-total','e=>e.remove()')
    page.eval_on_selector('.homework-content',"e=>e.insertAdjacentHTML('beforeend','<div class=fixture-end style=position:absolute;bottom:0;height:20px>虚构已全部加载</div>')")
    result=collect(page,mode=mode,end='.fixture-end')
    assert result.coverage.complete,result.coverage
    assert result.coverage.completeness_basis=='verified_end_marker'
    assert result.coverage.displayed_total is None
    assert len(result.tasks)==6


def test_hidden_end_marker_never_proves_unknown_total(page):
    prepare(page,count=2)
    page.eval_on_selector('.fixture-total','e=>e.remove()')
    page.eval_on_selector('.homework-content',"e=>e.insertAdjacentHTML('beforeend','<div class=fixture-end style=display:none>虚构已全部加载</div>')")
    result=collect(page,end='.fixture-end')
    assert not result.coverage.complete and result.coverage.displayed_total is None


def test_unverified_boundary_keeps_result_incomplete(page):
    prepare(page,count=2)
    policy=ScrollPolicy(container='.fixture-scroll',wait_ms=50)
    result=BoundedScrollReader(BoundedTaskListReader(PROFILE),policy).read(page)
    assert 'boundary_unverified' in result.coverage.reasons and not result.coverage.complete


def test_scroll_budget_is_finite(page):
    prepare(page,count=9)
    policy=ScrollPolicy(container='.fixture-scroll',max_scrolls=1,wait_ms=50,boundary_verified=True)
    result=BoundedScrollReader(BoundedTaskListReader(PROFILE),policy).read(page)
    assert result.coverage.scroll_count==1 and not result.coverage.complete
    assert 'scroll_limit' in result.coverage.reasons


def test_virtual_without_verified_positions_refuses_before_scroll(page):
    prepare(page,mode='virtual')
    policy=ScrollPolicy(mode='virtual',container='.fixture-scroll')
    with pytest.raises(ReaderError):
        BoundedScrollReader(BoundedTaskListReader(PROFILE),policy).read(page)
    assert page.eval_on_selector('.fixture-scroll','e=>e.scrollTop')==0


def test_virtual_position_gaps_are_not_complete(page):
    prepare(page,mode='virtual',count=6,positionOffset=1)
    result=collect(page,mode='virtual')
    assert not result.coverage.complete and 'unread_positions' in result.coverage.reasons


def test_obvious_pagination_is_not_silently_ignored(page):
    prepare(page,count=2)
    page.eval_on_selector('.fixture-total',"e=>e.insertAdjacentHTML('afterend','<button class=fixture-next>下一页</button>')")
    profile=replace(PROFILE,next='.fixture-next')
    result=BoundedScrollReader(BoundedTaskListReader(profile),ScrollPolicy(container='.fixture-scroll',boundary_verified=True,wait_ms=50)).read(page)
    assert not result.coverage.complete and 'pagination_not_traversed' in result.coverage.reasons


def test_real_access_is_checked_before_handle_or_scroll():
    class RefusePage:
        url='https://wenda.zhihuishu.com/stu/courseInfo/homeworkExam/'
        def evaluate_handle(self,*args):
            raise AssertionError('must not inspect DOM without authorization')
    with pytest.raises(PermissionError):
        BoundedScrollReader(BoundedTaskListReader(PROFILE)).read(RefusePage())


def test_unknown_course_is_not_filled_from_task_or_last_run(page):
    prepare(page,count=3)
    page.eval_on_selector('.fixture-course',"e=>e.style.display='none'")
    result=collect(page)
    assert result.course is None and not result.coverage.complete
    assert all(t.course is None for t in result.tasks)
    assert 'course_not_bound' in result.coverage.reasons


def test_lazy_short_initial_page_does_not_imply_complete(page):
    prepare(page,mode='lazy',count=7)
    page.eval_on_selector('.fixture-scroll',"e=>e.style.height='600px'")
    result=collect(page,mode='lazy')
    assert result.coverage.observed_count==2 and result.coverage.displayed_total==7
    assert not result.coverage.complete and 'unread_positions' in result.coverage.reasons


def test_explicit_empty_needs_zero_count_and_empty_state(page):
    prepare(page,count=0)
    page.eval_on_selector('.homework-content',"e=>e.innerHTML='<div class=fixture-empty>暂无任务</div>'")
    profile=replace(PROFILE,empty='.fixture-empty')
    result=BoundedScrollReader(BoundedTaskListReader(profile),ScrollPolicy(container='.fixture-scroll',boundary_verified=True,wait_ms=50)).read(page)
    assert result.coverage.complete and not result.tasks and result.coverage.displayed_total==0


def test_budget_is_shared_by_both_atomic_observations(page):
    prepare(page,mode='virtual',count=6)
    profile=replace(PROFILE,scan_position_attribute='data-index',scan_position_base=0)
    class MovingPage:
        url='about:blank'
        count=0
        def evaluate(self, expression, arg):
            self.count+=1
            snapshot=page.evaluate(expression,arg)
            if self.count==1:
                page.eval_on_selector_all('.homework-item','es=>es.forEach((e,i)=>e.dataset.index=String(19+i))')
            else:
                assert snapshot['error']=='cumulative_task_limit' and 'tasks' not in snapshot
            return snapshot
    with pytest.raises(ReaderLimitExceeded):
        BoundedTaskListReader(profile).read(MovingPage(),capture_fragments=True,seen_positions=tuple(range(1,20)))


def test_unstable_observation_keeps_consumed_budget(page):
    prepare(page,count=2)
    class MovingPage:
        url='about:blank'
        count=0
        def evaluate(self, expression, arg):
            self.count+=1
            snapshot=page.evaluate(expression,arg)
            if self.count==1:
                page.eval_on_selector('.tit',"e=>e.textContent='另一虚构标题'")
            return snapshot
    with pytest.raises(ReaderUnstable) as exc:
        BoundedTaskListReader(PROFILE).read(MovingPage(),capture_fragments=True)
    assert exc.value.observed_positions==(1,2)


def test_course_element_replacement_stops_cached_binding(page):
    prepare(page,count=6)
    page.evaluate("document.querySelector('.fixture-scroll').addEventListener('scroll',()=>{const n=document.querySelector('.fixture-course');n.replaceWith(n.cloneNode(true))},{once:true})")
    result=collect(page)
    assert not result.coverage.complete and 'source_or_structure_changed' in result.coverage.reasons


def test_missing_virtual_position_stops_before_fields(page):
    prepare(page,mode='virtual',count=6)
    page.eval_on_selector_all('.homework-item','es=>es.forEach(e=>e.removeAttribute("data-index"))')
    result=collect(page,mode='virtual')
    assert not result.tasks and not result.coverage.complete


def test_end_marker_must_be_visible_when_requested(page):
    prepare(page,count=2)
    result=collect(page,end='.not-present-end')
    assert not result.coverage.complete and 'end_marker_not_visible' in result.coverage.reasons


def test_report_repr_contains_no_task_or_course_text(page):
    prepare(page,count=3)
    result=collect(page)
    assert '虚构滚动任务' not in repr(result) and '虚构滚动课程' not in repr(result)


def test_conservative_matching_always_requires_confirmation():
    t=ReadTask('虚构作业','虚构课程')
    assert assess_match(t,()).outcome=='new_candidate'
    assert assess_match(t,(t,)).outcome=='possible_duplicate'
    assert assess_match(replace(t,course=None),(t,)).outcome=='course_unknown'
    for result in (assess_match(t,()),assess_match(t,(t,)),assess_match(t,(t,t))):
        assert result.requires_confirmation
    assert assess_match(replace(t,deadline_date='2034-06-02'),(replace(t,deadline_date='2034-06-01'),)).outcome=='possible_deadline_change'


def test_wrong_view_does_not_scroll_even_to_top(page):
    prepare(page,count=6)
    page.eval_on_selector('.fixture-scroll','e=>e.scrollTop=200')
    page.eval_on_selector('[aria-selected=true]',"e=>e.textContent='考试'")
    result=collect(page)
    assert not result.coverage.complete and result.coverage.scroll_count==0
    assert page.eval_on_selector('.fixture-scroll','e=>e.scrollTop')==200


@pytest.mark.parametrize('options',[{'max_scrolls':0},{'max_scrolls':31},{'wait_ms':0},{'settle_rounds':1},{'max_seconds':61}])
def test_invalid_policy_is_rejected(options):
    with pytest.raises(ValueError):ScrollPolicy(**options)
