"""Real DOM engine on fictional HTML, ALL network requests blocked.

Optional browser dependency as with grid sync; no platform/account/AI requests.
"""

import copy
from dataclasses import replace
from pathlib import Path

import pytest

from pku_deadline_agent.grid_session import SessionAuthRequired, detect_channel
from pku_deadline_agent.sources import SourceUnverified
from pku_deadline_agent.time_utils import BEIJING
from pku_deadline_agent.zhihuishu_reader import (
    BoundedTaskListReader, ListProfile, ReaderError, ReaderLimitExceeded,
    FLIPPED_HOMEWORK_LIST_PATH, FLIPPED_HOMEWORK_VALIDATION_PROFILE, possible_matches, summarize_validation,
)

HTML = Path(__file__).with_name('fixtures').joinpath('zhihuishu_task_list.html').read_text(encoding='utf-8')
PROFILE = ListProfile(
    title='.fixture-title', course_scope='.fixture-course-scope', course='.fixture-course-name',
    deadline='.homework-item-time .fixture-deadline',
    total='.fixture-total', next='.fixture-next', empty='.fixture-empty',
    loading='.fixture-loading', lazy='.fixture-lazy', virtual='.fixture-virtual',
    card_course='.fixture-card-course', link='.fixture-source',
    task_id_attribute='data-task-id', course_id_attribute='data-course-id',
    enrollment_id_attribute='data-enrollment-id',
)


@pytest.fixture(scope='module')
def browser():
    api = pytest.importorskip('playwright.sync_api')
    with api.sync_playwright() as pw:
        # Use the same installed-browser detection as grid sync, no download.
        browser = pw.chromium.launch(channel=detect_channel(), headless=True)
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    with browser.new_context(locale='zh-CN', timezone_id='Asia/Shanghai') as context:
        requests = []
        def block(route):
            requests.append(route.request.url)
            route.abort()
        context.route('**/*', block)
        page = context.new_page()
        page.set_viewport_size({'width': 1280, 'height': 960})
        page.set_content(HTML)
        yield page
        assert requests == [], 'fictional page must not attempt network access'


def read(page, profile=PROFILE):
    return BoundedTaskListReader(profile).read(page)


def test_pairing_is_per_card_and_midnight_preserved(page):
    report = read(page)
    assert [(x.title, x.deadline.isoformat()) for x in report.tasks] == [
        ('虚构作业甲', '2031-04-18T20:30:00+08:00'),
        ('虚构作业乙', '2031-05-02T00:00:00+08:00')]
    assert all(x.course == '虚构课程甲' and x.status == '未完成' and
               x.kind == 'homework' and x.opens_at is None for x in report.tasks)
    assert report.tasks[0].deadline.tzinfo == BEIJING
    assert report.tasks[0].observed_task_id == '1' * 32
    assert report.tasks[0].observed_course_id == 'a' * 32
    assert report.tasks[0].source_url.endswith('/homework/1111')
    assert report.tasks[1].source_url is None
    assert report.fields_valid and report.coverage.view_complete
    assert report.coverage.read_count == report.coverage.displayed_total == 2
    assert not report.ready_for_sync and report.verification == 'synthetic'
    assert not any(x.identity_verified for x in report.tasks)
    assert page.evaluate('window.sideEffects') == 0
    assert page.evaluate('scrollY') == 0


def test_no_id_is_not_fabricated_and_namesakes_stay_separate(page):
    page.eval_on_selector_all('.homework-item', "es=>es.forEach(e=>e.removeAttribute('data-task-id'))")
    page.eval_on_selector_all('.fixture-title', "es=>es.forEach(e=>e.textContent='虚构同名任务')")
    report = read(page)
    assert len(report.tasks) == 2
    assert all(x.observed_task_id is None for x in report.tasks)
    assert possible_matches(report.tasks[1], report.tasks) == (0, 1)
    with pytest.raises(SourceUnverified):
        report.require_sync_ready()


def test_conservative_matches_keep_course_and_kind_boundaries(page):
    a = read(page).tasks[0]
    changed = replace(a, deadline=None)
    assert possible_matches(changed, (a, replace(a, course='虚构课程乙'), replace(a, kind='exam'))) == (0,)
    assert possible_matches(replace(a, course=None), (a,)) == ()
    assert possible_matches(replace(a, title='不同虚构任务'), (a,)) == ()


def test_duplicate_observed_ids_block_trusted_result(page):
    page.eval_on_selector_all('.homework-item', "es=>es.forEach(e=>e.dataset.taskId='1111')")
    result = read(page)
    assert len(result.tasks) == 2
    assert 'duplicate_observed_id' in [i.code for i in result.issues]
    assert not result.coverage.view_complete


@pytest.mark.parametrize('change,code', [
    ("document.querySelector('.fixture-title').remove()", 'title_missing'),
    ("document.querySelector('.fixture-title').insertAdjacentHTML('afterend','<h2 class=fixture-title>另一虚构标题</h2>')", 'ambiguous_title'),
    ("document.querySelector('.fixture-deadline').insertAdjacentHTML('afterend','<span class=fixture-deadline>2032.01.01 08:00</span>')", 'ambiguous_deadline'),
    ("document.querySelector('.fixture-course-name').remove()", 'course_binding_missing'),
    ("document.querySelector('.homework-item').insertAdjacentHTML('beforeend','<span class=fixture-card-course>另一虚构课程</span>')", 'course_binding_conflict'),
    ("document.querySelector('.fixture-total').textContent='未知数量'", 'total_format_changed'),
    ("document.querySelector('.fixture-total').textContent='共 1 条'", 'total_count_conflict'),
    ("document.querySelector('.fixture-deadline').textContent='考试开放时间：2031.04.18 20:30'", 'invalid_task_field'),
    ("document.querySelector('.fixture-title').textContent='Token: fictional-token-for-test-123456789'", 'invalid_task_field'),
])
def test_invalid_fields_never_claim_complete(page, change, code):
    page.evaluate(change)
    result = read(page)
    assert code in [i.code for i in result.issues]
    assert not result.fields_valid and not result.coverage.view_complete


@pytest.mark.parametrize('value,date_only', [('', None), ('截止时间：2031.04.18', '2031-04-18')])
def test_missing_or_date_only_deadline_not_filled(page, value, date_only):
    page.eval_on_selector('.fixture-deadline', '(e,v)=>e.textContent=v', value)
    result = read(page)
    assert result.tasks[0].deadline is None
    assert result.tasks[0].deadline_date == date_only
    assert result.tasks[1].deadline is not None


def test_absent_deadline_does_not_steal_neighbor_time(page):
    page.eval_on_selector('.homework-item-time', 'e=>e.remove()')
    result = read(page)
    assert result.tasks[0].deadline is None
    assert result.tasks[1].deadline.month == 5


@pytest.mark.parametrize('selector', ['.homework-item-time', '.homework-item', 'div'])
def test_broad_or_overlapping_title_selector_refused(page, selector):
    result = read(page, replace(PROFILE, title=selector))
    assert not result.coverage.view_complete
    assert not result.tasks


def test_missing_title_cannot_zip_with_adjacent_task(page):
    page.eval_on_selector('.fixture-title', 'e=>e.remove()')
    result = read(page)
    assert [(x.title, x.deadline.month) for x in result.tasks] == [('虚构作业乙', 5)]
    assert result.coverage.rejected_count == 1


@pytest.mark.parametrize('selector', ['.fixture-loading', '.fixture-lazy', '.fixture-virtual'])
def test_loading_lazy_virtual_are_explicit_unread_scopes(page, selector):
    page.eval_on_selector('.fixture-total', '(e,s)=>e.insertAdjacentHTML("afterend",`<div class="${s.slice(1)}">未读范围</div>`)', selector)
    result = read(page)
    assert result.coverage.unread_scope
    assert not result.coverage.view_complete


def test_pagination_reports_total_read_and_unread_without_click(page):
    page.eval_on_selector('.fixture-total', "e=>e.textContent='共 7 条'")
    page.eval_on_selector('.fixture-next', 'e=>e.disabled=false')
    result = read(page)
    assert result.coverage.displayed_total == 7
    assert result.coverage.read_count == 2
    assert result.coverage.unread_minimum == 5
    assert 'later_pages' in result.coverage.unread_scope
    assert not result.coverage.view_complete
    assert page.evaluate('window.sideEffects') == 0


def test_no_total_is_unknown_not_complete(page):
    page.eval_on_selector('.fixture-total', 'e=>e.remove()')
    result = read(page)
    assert result.coverage.displayed_total is None
    assert 'total_unknown' in result.coverage.unread_scope
    assert not result.coverage.view_complete


def test_offscreen_and_hidden_cards_count_but_are_not_read(page):
    page.eval_on_selector_all('.homework-item', "es=>{es[0].style.display='none';es[1].style.marginTop='2000px'}")
    result = read(page)
    assert not result.tasks
    assert result.coverage.loaded_count == result.coverage.outside_view_count == 2
    assert result.coverage.visible_count == result.coverage.read_count == 0
    assert not result.confirmed_empty


@pytest.mark.parametrize('total,empty,confirmed', [(0, True, True), (0, False, False), (None, True, False), (3, True, False)])
def test_empty_requires_explicit_zero_and_empty_marker(page, total, empty, confirmed):
    page.eval_on_selector('.homework-content', 'e=>e.replaceChildren()')
    if empty:
        page.eval_on_selector('.homework-content', "e=>e.innerHTML='<div class=fixture-empty>暂无作业</div>'")
    page.eval_on_selector('.fixture-total', '(e,t)=>{if(t===null)e.remove();else e.textContent=`共 ${t} 条`}', total)
    result = read(page)
    assert result.confirmed_empty is confirmed
    assert result.coverage.view_complete is confirmed


def test_more_than_twenty_reports_before_reading_task_fields(page):
    page.eval_on_selector('.homework-content', "e=>e.innerHTML=Array.from({length:21},()=>'<div class=homework-item style=height:8px;padding:0;margin:0><input type=text value=must-not-read></div>').join('')")
    with pytest.raises(ReaderLimitExceeded) as exc:
        read(page)
    assert exc.value.visible == exc.value.loaded == 21


def test_exact_twenty_is_allowed_without_implicit_truncation(page):
    page.eval_on_selector('.homework-content', """e=>e.innerHTML=Array.from({length:20},(_,i)=>
      `<div class=homework-item style="height:22px;padding:0;margin:0;display:flex;gap:12px">
       <span class=fixture-title>虚构任务${i}</span>
       <div class=homework-item-time><span class=fixture-deadline>2031.04.18 20:30</span></div></div>`).join('')""")
    page.eval_on_selector('.fixture-total', "e=>e.textContent='共 20 条'")
    result = read(page)
    assert len(result.tasks) == result.coverage.visible_count == 20
    assert result.tasks[-1].title == '虚构任务19'
    assert result.coverage.view_complete


def test_css_clipped_task_fields_are_not_read(page):
    page.eval_on_selector('.homework-content', 'e=>{e.style.height="1px";e.style.overflow="hidden"}')
    result = read(page)
    assert not result.tasks
    assert not result.confirmed_empty


def test_wrong_deadline_selector_cannot_be_successful_empty_time(page):
    result = read(page, replace(PROFILE, deadline='.changed-deadline-selector'))
    assert len(result.tasks) == 2 and all(x.deadline is None for x in result.tasks)
    assert not result.fields_valid and not result.coverage.view_complete


def test_hidden_authentication_text_does_not_enter_title_or_date(page):
    page.eval_on_selector('.fixture-title', "e=>e.insertAdjacentHTML('beforeend','<span style=display:none>Token: fictional-secret-hidden</span>')")
    result = read(page)
    assert result.fields_valid
    assert result.tasks[0].title == '虚构作业甲'


@pytest.mark.parametrize('change', [
    "document.querySelector('.homework-content').className='changed'",
    "document.querySelector('.menu-pane.active').classList.remove('active')",
    "document.querySelector('[aria-selected=true]').textContent='考试'",
    "document.querySelector('.fixture-course-scope').className='changed'",
    "document.querySelector('.homework-item').insertAdjacentHTML('beforeend','<div class=homework-item></div>')",
    "document.querySelector('.homework-content').insertAdjacentHTML('afterend','<div class=homework-content></div>')",
])
def test_layout_changes_stop_instead_of_empty_success(page, change):
    page.evaluate(change)
    with pytest.raises(ReaderError):
        read(page)


def test_password_page_stops_before_task_read(page):
    page.evaluate("document.body.insertAdjacentHTML('beforeend','<input type=password value=fictional>')")
    with pytest.raises(SessionAuthRequired):
        read(page)


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'https://evil.invalid/task',
                                'https://wenda.zhihuishu.com/exam/1111',
                                'https://wenda.zhihuishu.com/task?token=fictional',
                                '//evil.invalid/task'])
def test_unsafe_links_are_never_followed_or_retained(page, url):
    page.eval_on_selector('.fixture-source', '(e,v)=>e.setAttribute("href",v)', url)
    result = read(page)
    assert result.tasks[0].source_url is None
    assert 'unsafe_source_link' in [i.code for i in result.issues]
    assert page.url == 'about:blank'


def test_repr_does_not_disclose_task_content_identifiers_or_dates(page):
    result = read(page)
    diagnostic = repr(result) + repr(result.tasks[0])
    for private in ('虚构作业甲', '虚构课程甲', '2031', '1'*32, 'a'*32, '/homework/1111'):
        assert private not in diagnostic


class StubPage:
    """An already-open page double; has no goto/click/storage/network APIs."""
    def __init__(self, snapshots=(), url='about:blank'):
        self.url, self.snapshots, self.calls = url, iter(snapshots), 0

    def evaluate(self, expression, arg):
        self.calls += 1
        return next(self.snapshots)


def test_blank_nested_frame_is_not_a_fictional_page():
    """A blank iframe inside a real session must never be labeled synthetic.

    Playwright exposes ``.page`` on a Frame and not on a Page; the reader uses that
    to tell the offline about:blank fixture apart from an auxiliary blank frame.
    """
    frame = StubPage(url='about:blank')
    frame.page = object()
    with pytest.raises(PermissionError):
        read(frame)
    assert frame.calls == 0


@pytest.mark.parametrize('url', ['https://wenda.zhihuishu.com/course/aaaa', 'file:///private',
                                'https://evil.invalid/', 'https://wenda.zhihuishu.com/exam/aaaa'])
def test_real_page_gate_precedes_dom_access(url):
    page = StubPage(url=url)
    with pytest.raises(PermissionError):
        read(page)
    assert page.calls == 0


def test_fake_profile_cannot_be_used_on_real_page_even_with_flag():
    page = StubPage(url='https://wenda.zhihuishu.com/course/aaaa')
    with pytest.raises(PermissionError):
        BoundedTaskListReader(PROFILE).read(page, allow_real=True)
    assert page.calls == 0


def test_two_observations_must_agree(page):
    from pku_deadline_agent.zhihuishu_dom import SNAPSHOT_SCRIPT
    a = page.evaluate(SNAPSHOT_SCRIPT, PROFILE.__dict__ | {'limit': 20})
    b = copy.deepcopy(a)
    b['tasks'][0]['deadline'] = '2032.01.01 08:00'
    with pytest.raises(ReaderError):
        read(StubPage([a, b]))


def test_unexpected_browser_error_is_sanitized():
    class BrokenPage(StubPage):
        def evaluate(self, *args):
            raise RuntimeError('Token: fictional-sensitive-error')
    with pytest.raises(ReaderError) as exc:
        read(BrokenPage())
    assert 'fictional-sensitive-error' not in str(exc.value)


@pytest.mark.parametrize('limit', [0, 21, -1, True, 2.5])
def test_limit_cannot_be_bypassed(limit):
    with pytest.raises(ValueError):
        BoundedTaskListReader(PROFILE, limit=limit)


@pytest.mark.parametrize('attribute', ['data-v-deadbeef', 'data-token', 'onclick', 'href', 'data-user-id', 'data-api-key', 'data-session-id'])
def test_identity_selector_cannot_read_credentials_or_vue_ids(attribute):
    with pytest.raises(ValueError):
        replace(PROFILE, task_id_attribute=attribute)


def test_two_visible_course_headings_do_not_choose_the_first(page):
    page.eval_on_selector('.fixture-course-name', "e=>e.insertAdjacentHTML('afterend','<h1 class=fixture-course-name>另一虚构课程</h1>')")
    result = read(page)
    assert all(task.course is None for task in result.tasks)
    assert not result.coverage.view_complete


def test_empty_marker_with_existing_tasks_is_a_conflict(page):
    page.eval_on_selector('.homework-content', "e=>e.insertAdjacentHTML('beforeend','<div class=fixture-empty>暂无作业</div>')")
    result = read(page)
    assert not result.confirmed_empty and not result.coverage.view_complete
    assert 'empty_state_conflict' in [i.code for i in result.issues]


def test_page_navigation_between_observations_stops(page):
    from pku_deadline_agent.zhihuishu_dom import SNAPSHOT_SCRIPT
    snapshot = page.evaluate(SNAPSHOT_SCRIPT, PROFILE.__dict__ | {'limit': 20})
    class NavigatingPage(StubPage):
        def evaluate(self, expression, arg):
            self.url = 'https://wenda.zhihuishu.com/login'
            return snapshot
    with pytest.raises(ReaderError):
        read(NavigatingPage())


def test_non_object_snapshot_stops_before_normalization():
    with pytest.raises(ReaderError):
        read(StubPage([[]]))


def test_course_selector_cannot_use_a_task_title_as_course(page):
    page.eval_on_selector_all('.fixture-title', 'es=>es[1].remove()')
    with pytest.raises(ReaderError):
        read(page, replace(PROFILE, course='.fixture-title'))


def test_opening_time_outside_deadline_container_not_used_as_deadline(page):
    page.eval_on_selector_all('.homework-item', "es=>es.forEach(e=>e.insertAdjacentHTML('beforeend','<div class=fixture-opens>2031.01.01 08:00</div>'))")
    result = read(page, replace(PROFILE, deadline='.fixture-opens'))
    assert not result.tasks and not result.coverage.view_complete
    assert all(i.code == 'deadline_outside_verified_container' for i in result.issues if i.position is not None)


def test_aria_busy_means_loading_even_without_visible_spinner(page):
    page.eval_on_selector('.homework-content', "e=>e.setAttribute('aria-busy','true')")
    result = read(page)
    assert result.coverage.loading and not result.coverage.view_complete
    assert 'loading' in result.coverage.unread_scope


def test_inconsistent_total_cannot_hide_known_unread_cards(page):
    page.eval_on_selector('.fixture-total', "e=>e.textContent='共 1 条'")
    page.eval_on_selector('.homework-item', "e=>e.style.display='none'")
    result = read(page)
    assert result.coverage.read_count == 1
    assert result.coverage.unread_minimum == 1
    assert 'total_count_conflict' in [i.code for i in result.issues]
    assert not result.coverage.view_complete


class SimulatedPlatformPage:
    """Pretend URL for guard tests; evaluate ONLY on our offline about:blank page."""
    url = 'https://wenda.zhihuishu.com/course/aaaa'

    def __init__(self, page):
        self.page = page

    def evaluate(self, expression, arg):
        return self.page.evaluate(expression, arg)


@pytest.fixture
def observed_layout(page):
    fictional = Path(__file__).with_name('fixtures').joinpath('zhihuishu_observed_layout.html')
    page.set_content(fictional.read_text(encoding='utf-8'))
    return SimulatedPlatformPage(page)


def test_observed_layout_partial_fields_are_not_promoted_to_complete(observed_layout):
    reader = BoundedTaskListReader(FLIPPED_HOMEWORK_VALIDATION_PROFILE)
    reports = tuple(reader.read(observed_layout, allow_real=True, validation_only=True) for _ in range(3))
    report = reports[0]
    assert [t.title for t in report.tasks] == ['虚构任务甲', '虚构任务乙', '虚构任务丙']
    assert [t.deadline.month if t.deadline else None for t in report.tasks] == [6, 7, None]
    assert all(t.course is None and t.observed_task_id is None and t.source_url is None for t in report.tasks)
    summary = summarize_validation(reports)
    assert summary['loaded_count'] == 5 and summary['visible_count'] == 3
    assert summary['known_deadline_count'] == 2 and summary['unknown_deadline_count'] == 1
    assert summary['course_bound_count'] == 0 and summary['pending_status_count'] == 3
    assert summary['same_view_fields_equal'] and summary['coverage_equal']
    assert summary['id_stability'] == 'not_evaluable_no_id'
    assert summary['unread_minimum'] == summary['outside_view_count'] == 2
    assert summary['displayed_total'] is None
    assert summary['pagination_has_next'] is None
    assert summary['lazy_loading_assessment'] == summary['virtual_scroll_assessment'] == 'unverified'
    assert not summary['view_complete'] and not summary['ready_for_sync']


def test_validation_profile_still_refuses_default_production_read(observed_layout):
    with pytest.raises(PermissionError):
        BoundedTaskListReader(FLIPPED_HOMEWORK_VALIDATION_PROFILE).read(observed_layout, allow_real=True)
    assert not FLIPPED_HOMEWORK_VALIDATION_PROFILE.real_verified


def test_validation_mode_still_requires_separate_real_authorization(observed_layout):
    with pytest.raises(PermissionError):
        BoundedTaskListReader(FLIPPED_HOMEWORK_VALIDATION_PROFILE).read(observed_layout, validation_only=True)


def test_validation_mode_never_claims_complete_even_when_counter_matches(page):
    result = BoundedTaskListReader(PROFILE).read(SimulatedPlatformPage(page), allow_real=True, validation_only=True)
    assert result.fields_valid
    assert not result.coverage.view_complete and 'layout_validation_only' in result.coverage.unread_scope
    assert not result.confirmed_empty and not result.ready_for_sync


def test_visible_course_name_is_used_without_course_code_fallback(observed_layout):
    observed_layout.page.eval_on_selector('.course-name > .name', "e=>{e.style.position='static'}")
    report = BoundedTaskListReader(FLIPPED_HOMEWORK_VALIDATION_PROFILE).read(observed_layout, allow_real=True, validation_only=True)
    assert all(t.course == '虚构课程丙' for t in report.tasks)
    assert not report.coverage.view_complete


def test_first_date_span_does_not_consume_late_submission_hint(observed_layout):
    observed_layout.page.eval_on_selector_all('.homework-item', 'es=>{for(const e of es)e.style.position="static"}')
    report = BoundedTaskListReader(FLIPPED_HOMEWORK_VALIDATION_PROFILE).read(observed_layout, allow_real=True, validation_only=True)
    assert all(t.deadline is not None for t in report.tasks)
    assert not any(i.code == 'invalid_task_field' for i in report.issues)


def test_validation_summary_contains_no_private_values(page):
    report = read(page)
    import json
    text = json.dumps(summarize_validation((report, report)), ensure_ascii=False)
    for value in ('虚构作业甲', '虚构课程甲', '2031', 'a'*32, '1'*32, '/homework/1111'):
        assert value not in text
    assert 'unverified_same_view_only' in text


@pytest.mark.parametrize('value', ['yes', 1, None])
def test_validation_mode_is_explicit_boolean(value):
    with pytest.raises(ValueError):
        BoundedTaskListReader(PROFILE).read(StubPage(), validation_only=value)


@pytest.mark.parametrize('reports', [(), (None,)*4])
def test_validation_summary_rejects_invalid_number_of_reads(reports):
    with pytest.raises(ValueError):
        summarize_validation(reports)


def test_observed_list_route_is_allowed_only_in_explicit_validation_mode(observed_layout):
    observed_layout.url = 'https://wenda.zhihuishu.com' + FLIPPED_HOMEWORK_LIST_PATH
    reader = BoundedTaskListReader(FLIPPED_HOMEWORK_VALIDATION_PROFILE)
    with pytest.raises(PermissionError):
        reader.read(observed_layout, allow_real=True)
    result = reader.read(observed_layout, allow_real=True, validation_only=True)
    assert len(result.tasks) == 3 and not result.coverage.view_complete


def test_same_list_route_without_the_trailing_slash_or_with_a_course_query(observed_layout):
    """The real page keeps the path slash-free once a course query is present.

    Same page, same route: only the trailing slash is normalised away. The mode
    requirement is unchanged, and a query string never widens what is allowed.
    """
    observed_layout.url = ('https://wenda.zhihuishu.com/stu/courseInfo/homeworkExam'
                           '?courseId=11111111')
    reader = BoundedTaskListReader(FLIPPED_HOMEWORK_VALIDATION_PROFILE)
    with pytest.raises(PermissionError):
        reader.read(observed_layout, allow_real=True)
    result = reader.read(observed_layout, allow_real=True, validation_only=True)
    assert len(result.tasks) == 3 and not result.coverage.view_complete


@pytest.mark.parametrize('path', [
    '/stu/courseInfo/homeworkExam/start', '/stu/courseInfo/homeworkExam/answer',
    '/stu/courseInfo/exam/', '/stu/other/homeworkExam/',
    '/stu/courseInfo/HomeworkExam/', '/stu/courseInfo/homeworkExam%2F',
])
def test_list_route_exception_does_not_allow_other_exam_routes(path):
    page = StubPage(url='https://wenda.zhihuishu.com' + path)
    with pytest.raises(PermissionError):
        BoundedTaskListReader(FLIPPED_HOMEWORK_VALIDATION_PROFILE).read(page, allow_real=True, validation_only=True)
    assert page.calls == 0


def test_synthetic_validation_mode_also_cannot_claim_complete(page):
    result = BoundedTaskListReader(PROFILE).read(page, validation_only=True)
    assert result.verification == 'synthetic-validation'
    assert result.fields_valid and not result.coverage.view_complete
    assert not result.confirmed_empty and not result.ready_for_sync
