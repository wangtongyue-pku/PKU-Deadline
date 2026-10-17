"""Foreground worker/UI tests: no real browser launch, account, AI or user DB."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from pku_deadline_agent.zhihuishu_browser import BrowserRead, publish_result, worker_environment
from pku_deadline_agent.zhihuishu_validation import encode_report, load_latest
from test_zhihuishu_validation import real_report

ROOT = Path(__file__).resolve().parents[1]


def worker_module():
    spec = importlib.util.spec_from_file_location('fictional_zhs_worker', ROOT / 'scripts/zhihuishu_real_read.py')
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    return worker


def job_at(tmp_path, status='waiting-for-operator', code=None):
    (tmp_path / 'status.txt').write_text(status, encoding='utf-8')
    process = SimpleNamespace(poll=lambda: code)
    return BrowserRead(tmp_path, process)


def test_start_requires_explicit_authorization_and_passes_no_secrets(tmp_path, monkeypatch):
    captured = []
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'fictional-secret')
    monkeypatch.setenv('PKU_EMAIL_PASSWORD', 'fictional-password')
    monkeypatch.setenv('PKU_BROWSER_CHANNEL', 'chrome')
    monkeypatch.setattr('pku_deadline_agent.zhihuishu_browser.subprocess.Popen',
                        lambda *args, **kw: captured.append((args, kw)) or SimpleNamespace())
    with pytest.raises(PermissionError):
        BrowserRead.start(root=tmp_path)
    with pytest.raises(ValueError):
        BrowserRead.start(authorized=True, max_tasks=21, root=tmp_path)
    assert not captured
    first = BrowserRead.start(authorized=True, root=tmp_path)
    second = BrowserRead.start(authorized=True, root=tmp_path)
    assert first.directory != second.directory
    args, options = captured[0]
    assert '--session-dir' in args[0] and '--max-scrolls' in args[0]
    assert options['env'].get('PKU_BROWSER_CHANNEL') == 'chrome'
    assert not {'DEEPSEEK_API_KEY', 'PKU_EMAIL_PASSWORD'} & options['env'].keys()
    assert 'fictional-secret' not in repr(captured)


def test_ready_once_cancel_and_exit_gate(tmp_path):
    job = job_at(tmp_path)
    assert job.status() == 'waiting'
    with pytest.raises(ValueError):
        job.result()
    job.ready()
    assert job.status() == 'reading'
    with pytest.raises(ValueError):
        job.ready()
    job.cancel()
    assert job.status() == 'cancelling'
    assert (tmp_path / 'cancel.request').exists()


def test_result_only_after_exit_and_auth_cleanup(tmp_path):
    report = real_report()
    (tmp_path / 'report.json').write_text(json.dumps(encode_report(report)), encoding='utf-8')
    job = job_at(tmp_path, 'done', 0)
    (tmp_path / 'profile').mkdir()
    assert job.status() == 'cleanup_failed'
    with pytest.raises(ValueError): job.result()
    (tmp_path / 'profile').rmdir()  # This test owns an empty fictional directory.
    assert job.result().tasks[0].title == report.tasks[0].title
    publish_result(job.result())
    assert load_latest().evidence == 'authorized-real-validation'
    payload = encode_report(report)
    payload['tasks'][0]['cookie'] = 'fictional'
    (tmp_path / 'report.json').write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ValueError): job.result()


def test_worker_cleanup_on_launch_failure_never_reuses_old_profile(tmp_path, monkeypatch):
    worker = worker_module()
    worker.OUT, worker.PROFILE = tmp_path / 'new', tmp_path / 'new/profile'
    class BrokenPlaywright:
        def __enter__(self):
            raise RuntimeError('fictional startup failure')
        def __exit__(self, *args): pass
    monkeypatch.setattr('playwright.sync_api.sync_playwright', BrokenPlaywright)
    assert worker.main() == 1
    assert not worker.PROFILE.exists()
    # A pre-existing profile must survive a refused restart.
    worker.OWN_PROFILE = False
    worker.PROFILE.mkdir()
    sentinel = worker.PROFILE / 'fictional-user-data'
    sentinel.write_text('keep', encoding='utf-8')
    assert worker.main() == 1 and sentinel.read_text(encoding='utf-8') == 'keep'


def test_probes_never_inspect_login_exam_or_encoded_routes():
    worker = worker_module()
    touched = []
    class Page:
        frames = ()
        main_frame = None
        def __init__(self, url): self.url = url
        def is_closed(self): return False
        def evaluate(self, *args): touched.append(self.url); return {'homework_items': 1}
    base = 'https://wenda.zhihuishu.com'
    pages = [Page(base + path) for path in (
        '/login', '/exam/paper', '/stu/courseInfo/homeworkExam%2F',
        '/stu/courseInfo/homeworkExam')]
    ctx = SimpleNamespace(pages=pages)
    assert len(worker.probe(ctx)) == 1
    assert worker.first_nodes(ctx)['homework_items'] == 1
    worker.census(ctx, SimpleNamespace(title='.tit', deadline='.date', card_course=None))
    assert touched == [pages[-1].url] * 3


@pytest.mark.parametrize('outcome', ['cancel', 'failure'])
def test_cancel_or_failure_after_read_discards_result_without_second_read(tmp_path, monkeypatch, outcome):
    worker = worker_module()
    worker.OUT, worker.PROFILE = tmp_path, tmp_path / 'profile'
    (tmp_path / 'read.request').touch()
    calls = []
    class Page:
        url = worker.ENTRY
        frames = ()
        main_frame = None
        def close(self): pass
        def goto(self, *args, **kw): pass
        def is_closed(self): return False
    ctx = SimpleNamespace(pages=[Page()], new_page=Page, close=lambda: calls.append('close'))
    pw = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=lambda **kw: ctx))
    class Playwright:
        def __enter__(self): return pw
        def __exit__(self, *args): pass
    monkeypatch.setattr('playwright.sync_api.sync_playwright', Playwright)
    monkeypatch.setattr(worker, 'detect_channel', lambda: 'chrome')
    monkeypatch.setattr(worker, 'wait_for_render', lambda ctx: {})
    monkeypatch.setattr(worker, 'probe', lambda ctx: [])
    monkeypatch.setattr(worker, 'census', lambda *args: {})
    def read(*args, **kw):
        calls.append('read')
        if outcome == 'failure': raise RuntimeError('fictional failure after touching fields')
        (tmp_path / 'cancel.request').touch()
        return real_report()
    monkeypatch.setattr(worker, 'readers', lambda **kw: (read, None, None, encode_report, None))
    assert worker.main() == 1
    assert calls == ['read', 'close']
    assert not (tmp_path / 'report.json').exists()
    assert not worker.PROFILE.exists()


SCRIPT = '''from pku_deadline_agent.zhihuishu_browser_page import render_browser_read
render_browser_read()
'''


def test_ui_never_launches_on_render_and_requires_consent(monkeypatch):
    calls = []
    monkeypatch.setattr(BrowserRead, 'start', lambda **kw: calls.append(kw))
    app = AppTest.from_string(SCRIPT).run()
    assert not app.exception and not calls
    assert app.button(key='zhs_browser_start').disabled
    consent = next(c for c in app.checkbox if c.key.startswith('zhs_browser_consent_'))
    consent.check().run()
    assert not app.button(key='zhs_browser_start').disabled and not calls
    # Changing the scope invalidates the old consent.
    app.number_input(key='zhs_browser_tasks').set_value(6).run()
    assert app.button(key='zhs_browser_start').disabled and not calls


def test_ui_ready_cancel_and_completed_result_reset_old_review(tmp_path, monkeypatch):
    job = job_at(tmp_path)
    monkeypatch.setattr(BrowserRead, 'start', lambda **kw: job)
    app = AppTest.from_string(SCRIPT).run()
    next(c for c in app.checkbox if c.key.startswith('zhs_browser_consent_')).check().run()
    app.button(key='zhs_browser_start').click().run()
    assert not app.exception and app.button(key='zhs_browser_ready')
    app.button(key='zhs_browser_ready').click().run()
    assert (tmp_path / 'read.request').exists()
    app.button(key='zhs_browser_cancel').click().run()
    assert (tmp_path / 'cancel.request').exists()
    # A separate completed fictional job exercises the whitelisted handoff.
    finished = tmp_path / 'finished'
    finished.mkdir()
    done = job_at(finished, 'done', 0)
    (finished / 'report.json').write_text(json.dumps(encode_report(real_report())), encoding='utf-8')
    app.session_state['zhs_browser_job'] = done
    app.session_state['zhs_review_edits'] = {1: 'old'}
    app.session_state['zhs_review_prod_authorize'] = True
    app.run()
    assert not app.exception
    assert 'zhs_browser_job' not in app.session_state
    assert 'zhs_review_edits' not in app.session_state
    assert 'zhs_review_prod_authorize' not in app.session_state
    assert load_latest().evidence == 'authorized-real-validation'
