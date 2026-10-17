"""Foreground browser worker bridge. No login, database or AI implementation here.

The existing bounded-read script owns Playwright on its own thread/process.
Streamlit only starts that script after consent, signals readiness/cancellation,
and accepts its whitelisted result after the browser has exited and cleaned up.
"""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from pku_deadline_agent.zhihuishu_session import ReviewReadScope
from pku_deadline_agent.zhihuishu_validation import decode_report, encode_report, payload_path

ROOT = Path(__file__).resolve().parents[2]
CURRENT_PAYLOAD = ROOT / '.local-tmp' / 'zhs-current.json'


def worker_environment():
    # An allowlist, rather than trying to guess every provider's secret name.
    names = {'systemroot', 'windir', 'path', 'pathext', 'temp', 'tmp', 'comspec',
             'userprofile', 'localappdata', 'appdata', 'programfiles', 'programfiles(x86)',
             'programdata', 'pku_browser_channel'}
    return {key: value for key, value in os.environ.items() if key.lower() in names}


@dataclass
class BrowserRead:
    directory: Path = field(repr=False)
    process: object = field(repr=False)
    source: str = "zhihuishu"

    @classmethod
    def start(cls, *, authorized=False, max_tasks=5, max_scrolls=12, root=None, source="zhihuishu"):
        from pku_deadline_agent.review_platforms import platform
        _, prefix = platform(source)
        if authorized is not True:
            raise PermissionError('请先确认本次读取范围。')
        if source == 'pku_yuetang' and (type(max_scrolls) is not int or not 1 <= max_scrolls <= 12):
            raise ValueError('问学阅堂最多滚动 12 次。')
        scope = ReviewReadScope(max_tasks=max_tasks, max_scrolls=max_scrolls, auto_container=True)
        base = Path(root) if root is not None else ROOT / '.local-tmp' / (prefix+'-sessions')
        directory = base / str(uuid.uuid4())
        directory.mkdir(parents=True, exist_ok=False)
        process = subprocess.Popen(
            [sys.executable, str(ROOT / 'scripts' / ('yuetang_real_read.py' if source=='pku_yuetang' else 'zhihuishu_real_read.py')),
             '--session-dir', str(directory), '--max-tasks', str(scope.max_tasks),
             '--max-scrolls', str(scope.max_scrolls)],
            cwd=ROOT, env=worker_environment(), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return cls(directory, process, source)

    def status(self):
        try:
            text = (self.directory / 'status.txt').read_text(encoding='utf-8')
        except OSError:
            text = ''
        code = self.process.poll()
        if code is not None:
            if (self.directory / 'profile').exists():
                return 'cleanup_failed'
            return 'done' if code == 0 and text == 'done' else 'stopped'
        if (self.directory / 'cancel.request').exists():
            return 'cancelling'
        if text.startswith('cleanup-failed'):
            return 'cleanup_failed'
        if 'waiting-for-operator' in text:
            return 'reading' if (self.directory / 'read.request').exists() else 'waiting'
        if 'waiting for retry.request' in text or 'multiple-or-missing-lists' in text:
            return 'retry'
        return 'reading' if 'attempt-' in text else 'launching'

    def ready(self):
        state = self.status()
        if state not in ('waiting', 'retry'):
            raise ValueError('浏览器尚未就绪，或本次读取已经结束。')
        request = 'read.request' if state == 'waiting' else 'retry.request'
        (self.directory / request).touch(exist_ok=False)

    def cancel(self):
        # Cooperative close: never terminate an unrelated browser or server.
        (self.directory / 'cancel.request').touch(exist_ok=True)

    def result(self):
        if self.status() != 'done':
            raise ValueError('读取尚未完成或临时登录状态尚未清理。')
        path = self.directory / 'report.json'
        if path.stat().st_size > 256_000:
            raise ValueError('读取结果过大，拒绝加载。')
        decoder = decode_report
        if self.source == 'pku_yuetang':
            from pku_deadline_agent.yuetang_validation import decode_report as decoder
        report = decoder(json.loads(path.read_text(encoding='utf-8')))
        if report.source != self.source: raise ValueError('读取来源不匹配。')
        if report.evidence not in ('authorized-real-validation', 'authorized-real-profile'):
            raise ValueError('浏览器读取结果缺少真实来源证据。')
        return report


def publish_result(report):
    """Retain the latest minimized report without deleting older real reads."""
    if not report.evidence.startswith('authorized-real-'):
        raise ValueError('只有已授权真实读取可成为最近读取。')
    if report.source == "pku_yuetang":
        from pku_deadline_agent.yuetang_validation import payload_path as path_for_yuetang, encode_report as encode_yuetang
        target = path_for_yuetang()
        encoder = encode_yuetang
    elif report.source == "zhihuishu":
        target = payload_path() if os.environ.get("PKU_ZHIHUISHU_PAYLOAD") else CURRENT_PAYLOAD
        encoder = encode_report
    else:
        raise ValueError("未知来源。")
    # Respect an explicit payload override (also isolates all automated tests).
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.' + str(uuid.uuid4()) + '.tmp')
    temporary.write_text(json.dumps(encoder(report), ensure_ascii=False), encoding='utf-8')
    temporary.replace(target)
