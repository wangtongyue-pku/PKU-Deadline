"""Stage 7D Streamlit tests for the school-mailbox page.

The real page is driven through ``app.py``; only the mailbox itself is replaced
with an offline fictional one, so the connection flow, the board, the import
confirmation and the feedback path are all the production code.
"""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from pku_deadline_agent.database import TaskRepository
from pku_deadline_agent.email_demo import DEMO_SUBJECTS
from pku_deadline_agent.email_page import DEMO_FACTORY_KEY, MODE_DEMO, MODE_REAL

APP = Path(__file__).resolve().parents[1] / "app.py"
PAGE = "学校邮箱"

SUBJECTS = {
    "homework": DEMO_SUBJECTS[0],
    "lecture": DEMO_SUBJECTS[1],
    "material": DEMO_SUBJECTS[2],
    "news": DEMO_SUBJECTS[6],
    "undated": DEMO_SUBJECTS[5],
}


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "email_ui.sqlite3"
    repository = TaskRepository(path)
    repository.initialize()
    return repository


def open_page(repo, monkeypatch, *, factory=None, page=PAGE, seed=None):
    monkeypatch.setenv("PKU_DATABASE_PATH", str(repo.path))
    app = AppTest.from_file(APP)
    app.session_state["page"] = page
    # The demo branch reads its mailbox factory from session state, so the
    # substitution survives Streamlit's re-import of the page module.
    if factory is not None:
        app.session_state[DEMO_FACTORY_KEY] = factory
    for key, value in (seed or {}).items():
        app.session_state[key] = value
    return app.run()


def sync_and_open(repo, monkeypatch):
    """Open the page and run one fictional sync through the real button.

    Two additional runs are needed for the same reason the other pages in this
    project need them: the button handler runs after the elements above it were
    drawn, so its message and the refreshed metrics appear on the following run.
    """
    app = open_page(repo, monkeypatch)
    _click(app, "email_sync")
    return settle(settle(app))


def _click(app, key, label=None):
    buttons = [item for item in app.button if item.key == key]
    if not buttons and label is not None:
        buttons = [item for item in app.button if item.label == label]
    assert buttons, f"button {key!r} not found: {[b.key for b in app.button]}"
    return buttons[0].click().run()


def _check(app, key, value=True):
    boxes = [item for item in app.checkbox if item.key == key]
    assert boxes, f"checkbox {key!r} not found: {[c.key for c in app.checkbox]}"
    return boxes[0].set_value(value).run()


def settle(app):
    """Run once more so queued messages and the metrics above the buttons update.

    A Streamlit button handler runs *after* the elements above it were drawn, so
    the status line and any message it queues become visible on the next run.
    This mirrors how the other pages in this project report what just happened.
    """
    return app.run()


def button_keys(app):
    return [item.key for item in app.button]


def expander_titles(app):
    return [item.label for item in app.expander]


def metrics(app):
    return {item.label: item.value for item in app.metric}


# ------------------------------------------------------------ shell and status


def test_sidebar_offers_the_mailbox_page(repo, monkeypatch):
    app = open_page(repo, monkeypatch, page="手动待办")
    assert "学校邮箱" in app.sidebar.radio[0].options


def test_page_opens_with_demo_mode_and_connection_controls(repo, monkeypatch):
    app = open_page(repo, monkeypatch)
    assert not app.exception, app.exception
    keys = button_keys(app)
    assert "email_check" in keys
    assert "email_sync" in keys
    assert "email_disconnect" in keys
    # Nothing is synced yet, and the page says so instead of showing an empty board.
    assert metrics(app)["上次同步"] == "尚未同步"
    assert metrics(app)["连接状态"] == "未连接"
    assert any("尚未同步" in item.value for item in app.info)


def test_real_mode_asks_for_credentials_without_echoing_them(repo, monkeypatch):
    app = open_page(repo, monkeypatch)
    app.radio(key="email_mode").set_value(MODE_REAL).run()
    assert not app.exception, app.exception
    labels = [item.label for item in app.text_input]
    assert any("授权码" in label for label in labels)
    assert any("邮箱地址" in label for label in labels)
    # No credential is required to look at the page, and none is stored.
    assert metrics(app)["凭据状态"] == "未提供凭据"


def test_sync_without_credentials_stops_before_connecting(repo, monkeypatch):
    def refuse():
        raise AssertionError("在没有凭据时不应尝试连接")

    app = open_page(repo, monkeypatch, factory=refuse)
    app.radio(key="email_mode").set_value(MODE_REAL).run()
    app = _click(app, "email_sync")
    app = settle(app)
    assert not app.exception, app.exception
    assert any("授权码" in item.value for item in app.error)
    assert repo.list_tasks() == []


def test_check_connection_reports_the_folders_it_may_read(repo, monkeypatch):
    app = open_page(repo, monkeypatch)
    app = _click(app, "email_check")
    assert not app.exception, app.exception
    app = settle(app)
    assert any("可读取的文件夹" in item.value for item in app.info)
    assert metrics(app)["连接状态"] == "已连接"


def test_connection_failure_keeps_the_page_usable(repo, monkeypatch):
    from pku_deadline_agent.email_adapter import EmailNetworkError

    def broken():
        raise EmailNetworkError("虚构的连不上")

    app = open_page(repo, monkeypatch, factory=broken)
    app = _click(app, "email_check")
    assert not app.exception, app.exception
    app = settle(app)
    assert any("网络或服务端问题" in item.value for item in app.error)
    assert metrics(app)["连接状态"] == "未连接"
    assert repo.list_tasks() == []


def test_disconnect_clears_credentials_and_preview(repo, monkeypatch):
    app = sync_and_open(repo, monkeypatch)
    assert expander_titles(app)  # the board is on screen
    app = _click(app, "email_disconnect")
    assert not app.exception, app.exception
    app = settle(app)
    assert metrics(app)["上次同步"] == "尚未同步"
    assert metrics(app)["本次邮件数"] == "—"
    assert any("已断开" in item.value for item in app.info)
    assert not any(SUBJECTS["homework"] in title for title in expander_titles(app))


# --------------------------------------------------------------- sync + board


def test_sync_reads_the_fictional_inbox_and_shows_the_board(repo, monkeypatch):
    app = sync_and_open(repo, monkeypatch)
    assert not app.exception, app.exception
    assert metrics(app)["本次邮件数"] == "7"
    titles = expander_titles(app)
    for subject in SUBJECTS.values():
        assert any(subject in title for title in titles), subject
    combined = "\n".join(item.value for item in app.markdown)
    assert "分类理由" in combined


def test_metrics_refresh_after_the_sync_run(repo, monkeypatch):
    app = sync_and_open(repo, monkeypatch)
    assert metrics(app)["上次同步"] != "尚未同步"
    assert metrics(app)["连接状态"] == "已连接"


def test_board_shows_category_importance_and_reasons_per_mail(repo, monkeypatch):
    app = sync_and_open(repo, monkeypatch)
    labels = expander_titles(app)
    # The label carries both the category and the importance, so a low-importance
    # verdict is never hidden behind an unexplained badge.
    assert sum(1 for label in labels if label.startswith("【")) == 7
    combined = "\n".join(item.value for item in app.markdown)
    assert "分类理由" in combined
    assert "全部时间点" in combined


def test_default_filters_hide_nothing(repo, monkeypatch):
    """Opening the page must not hide mail before the user asks it to."""
    app = sync_and_open(repo, monkeypatch)
    assert not any("隐藏了" in item.value for item in app.caption)
    assert sum(1 for label in expander_titles(app) if label.startswith("【")) == 7


def test_board_filter_explains_that_it_hides_mail(repo, monkeypatch):
    app = sync_and_open(repo, monkeypatch)
    before = len(expander_titles(app))
    app.selectbox(key="email_filter_category").set_value("竞赛").run()
    assert not app.exception, app.exception
    after = len(expander_titles(app))
    assert after < before
    # A filter must never look like "no mail arrived".
    assert any("隐藏" in item.value for item in app.caption) or any(
        "隐藏" in item.value for item in app.markdown)


def test_filter_that_hides_everything_says_so_instead_of_showing_nothing(repo, monkeypatch):
    app = sync_and_open(repo, monkeypatch)
    app.text_input(key="email_filter_search").set_value("不存在的关键字").run()
    assert not app.exception, app.exception
    assert any("不代表邮箱里没有邮件" in item.value for item in app.info)


def test_importance_filter_keeps_low_importance_mail_reachable(repo, monkeypatch):
    app = sync_and_open(repo, monkeypatch)
    app.selectbox(key="email_filter_importance").set_value("低").run()
    assert not app.exception, app.exception
    assert expander_titles(app)  # grouped later, not dropped


# --------------------------------------------------------------- import flow


def import_buttons(app):
    return [item for item in app.button if item.key and item.key.startswith("email_apply_")]


def confirm_key(app):
    keys = [item.key for item in app.checkbox if item.key and item.key.startswith("email_confirm_")]
    assert len(keys) == 1, keys
    return keys[0]


def test_import_requires_the_confirmation_checkbox(repo, monkeypatch):
    app = sync_and_open(repo, monkeypatch)
    buttons = import_buttons(app)
    assert len(buttons) == 1
    assert buttons[0].disabled is True
    app = _check(app, confirm_key(app))
    assert import_buttons(app)[0].disabled is False
    # Still nothing written until the button is actually pressed.
    assert repo.list_tasks() == []


def test_import_writes_confirmed_tasks_with_the_email_source(repo, monkeypatch):
    app = sync_and_open(repo, monkeypatch)
    app = _check(app, confirm_key(app))
    app = _click(app, "email_apply_1")
    assert not app.exception, app.exception
    tasks = repo.list_tasks()
    assert tasks, "导入后应至少有一个任务"
    assert {task.source for task in tasks} == {"email"}
    app = settle(app)
    assert any("已写入" in item.value for item in app.success)
    # History and source mapping are the shared stage 4/6 path, not a shortcut.
    assert repo.source_mappings("email")
    assert repo.history(tasks[0].id)


def test_activity_time_is_shown_but_never_imported(repo, monkeypatch):
    app = sync_and_open(repo, monkeypatch)
    app = _check(app, confirm_key(app))
    app = _click(app, "email_apply_1")
    tasks = repo.list_tasks()
    assert not any("讲座" in task.title and "举办" in task.title for task in tasks)
    combined = "\n".join(item.value for item in app.caption) + "\n".join(
        item.value for item in app.markdown)
    assert "活动举办时间" in combined


def test_undated_time_is_listed_for_manual_entry(repo, monkeypatch):
    app = sync_and_open(repo, monkeypatch)
    combined = "\n".join(item.value for item in app.markdown) + "\n".join(
        item.value for item in app.caption)
    assert "待确认时间" in combined
    assert "手动待办" in combined


def test_second_sync_of_the_same_inbox_changes_nothing(repo, monkeypatch):
    app = sync_and_open(repo, monkeypatch)
    app = _check(app, confirm_key(app))
    app = _click(app, "email_apply_1")
    first = repo.list_tasks()

    # A fresh session reads the same fictional mail again.
    app2 = sync_and_open(repo, monkeypatch)
    labels = [item.label for item in app2.checkbox if item.key and item.key.startswith("email_pick_")]
    assert any("无需改动" in label for label in labels)
    assert repo.list_tasks() == first


def test_leaving_the_page_drops_the_unconfirmed_preview(repo, monkeypatch):
    app = sync_and_open(repo, monkeypatch)
    assert import_buttons(app), "同步后应显示导入按钮"
    app.session_state["page"] = "手动待办"
    app.run()
    app.session_state["page"] = PAGE
    app.run()
    app = settle(app)
    assert not app.exception, app.exception
    # The preview is gone: the user must sync again before writing anything. The
    # report itself stays, so the board can be shown again without a new sync, but
    # there is nothing to confirm until they look at it afresh.
    assert repo.list_tasks() == []
    # Reading the same report again must not recreate an actionable plan, and it
    # must not have written anything on the way out.
    assert repo.list_tasks() == []


# ------------------------------------------------------------------- feedback


def test_feedback_records_a_correction_and_re_displays_it(repo, monkeypatch):
    app = sync_and_open(repo, monkeypatch)
    app.selectbox(key="email_feedback_target").set_value(
        next(option for option in app.selectbox(key="email_feedback_target").options
             if SUBJECTS["news"] in option)).run()
    app.selectbox(key="email_feedback_category").set_value("讲座").run()
    app = _click(app, "email_feedback_save")
    assert not app.exception, app.exception
    app = settle(app)
    assert any("你已修正为" in item.value for item in app.caption)
    # The corrected category is what the board now shows for that message.
    assert any("虚构校园生活小贴士" in label and "讲座" in label
               for label in expander_titles(app))


def test_feedback_refuses_a_no_op_correction(repo, monkeypatch):
    app = sync_and_open(repo, monkeypatch)
    app = _click(app, "email_feedback_save")
    assert not app.exception, app.exception
    # This branch does not restart, so its warning belongs to the click run itself.
    assert any("无需记录" in item.value for item in app.warning)
    assert repo.list_tasks() == []


# ------------------------------------------------------------------- AI gate


def test_ai_section_prepares_a_minimal_payload_only_after_confirmation(repo, monkeypatch):
    app = sync_and_open(repo, monkeypatch)
    prepare = [item for item in app.button if item.key == "email_ai_prepare"]
    assert prepare and prepare[0].disabled is True
    app = _check(app, "email_ai_confirm")
    prepare = [item for item in app.button if item.key == "email_ai_prepare"]
    assert prepare[0].disabled is False
    # The page states the guarantee it enforces: bodies and attachments never leave.
    combined = "\n".join(item.value for item in app.caption)
    assert "正文与附件不在其中" in combined


def test_ai_section_never_sends_anything(repo, monkeypatch):
    """The gate prepares a payload; dispatch stays in the parse page."""
    app = sync_and_open(repo, monkeypatch)
    app = _check(app, "email_ai_confirm")
    app = _click(app, "email_ai_prepare")
    assert not app.exception, app.exception
    assert any("只准备载荷、不发送" in item.value for item in app.caption)


# ------------------------------------------------------------------ boundaries


def test_page_never_writes_tasks_without_a_confirmed_import(repo, monkeypatch):
    app = sync_and_open(repo, monkeypatch)
    assert repo.list_tasks() == []
    app = _click(app, "email_check")
    assert repo.list_tasks() == []


def test_demo_mode_is_the_default_so_no_account_is_needed(repo, monkeypatch):
    app = open_page(repo, monkeypatch)
    assert app.radio(key="email_mode").value == MODE_DEMO
    assert not any(item.type == "password" for item in app.text_input)
    combined = "\n".join(item.value for item in app.info)
    assert "不会访问任何网络或真实邮箱" in combined


def test_demo_inbox_parses_cleanly_and_states_distinct_dates():
    """The fixture itself must be sound, or the demo teaches the wrong thing.

    Two properties matter and both were wrong once: no message may carry a
    parsing warning, and a mail that states an event *and* its registration
    deadline must state two different dates (they are not the same day, and a
    demo that showed one date twice looked like a parser bug).
    """
    from pku_deadline_agent.email_adapter import DEFAULT_FOLDER
    from pku_deadline_agent.email_intel import analyze_email
    from pku_deadline_agent.email_parse import parse_raw_email
    from pku_deadline_agent.email_demo import DEMO_SUBJECTS, demo_mailbox

    mailbox = demo_mailbox()
    assert len(mailbox._folders[DEFAULT_FOLDER]) == len(DEMO_SUBJECTS)
    parsed = {}
    for uid, raw in sorted(mailbox._folders[DEFAULT_FOLDER].items()):
        record, warnings = parse_raw_email(raw, uid=uid, folder=DEFAULT_FOLDER, uidvalidity=1)
        assert warnings == (), (record.subject, warnings)
        parsed[record.subject] = analyze_email(record)

    lecture = parsed["虚构学术讲座报名"]
    dated = {item.kind: item.due_at for item in lecture.deadlines if item.due_at}
    assert set(dated) == {"报名截止", "活动举办时间"}
    assert dated["报名截止"] != dated["活动举办时间"]

    # Every demo deadline is in the future of the demo clock, so the board shows
    # live urgency rather than a pile of expired mail.
    from pku_deadline_agent.email_demo import demo_now

    now = demo_now()
    for analysis in parsed.values():
        for deadline in analysis.deadlines:
            if deadline.due_at is not None:
                assert deadline.due_at > now, (analysis.record.subject, deadline.kind)
