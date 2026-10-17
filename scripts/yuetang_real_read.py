"""Foreground Yuetang worker, launched only by the user's consented read button.

Same isolated installed-browser architecture as Zhihuishu. No login automation,
DB, model client, private API or click on a platform task. Keep diagnostics.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from pku_deadline_agent.grid_session import detect_channel
from pku_deadline_agent.yuetang_reader import ENTRY, read_list, safe_list_url
from pku_deadline_agent.yuetang_validation import encode_report


def run(directory, *, max_tasks=5, max_scrolls=12):
    from playwright.sync_api import sync_playwright
    directory=Path(directory).resolve()
    allowed=(ROOT/'.local-tmp/yue-sessions').resolve()
    if not allowed.is_relative_to(ROOT.resolve()) or directory.parent!=allowed or str(uuid.UUID(directory.name))!=directory.name:
        raise ValueError('临时目录不在本次会话范围内。')
    profile=directory/'profile'
    if profile.exists():raise ValueError('不复用旧登录目录。')
    directory.mkdir(parents=True,exist_ok=True)
    def state(value): (directory/'status.txt').write_text(value,encoding='utf-8')
    success=False
    try:
        with sync_playwright() as pw:
            context=pw.chromium.launch_persistent_context(str(profile),channel=detect_channel(),headless=False,
                no_viewport=True,args=['--no-first-run','--no-default-browser-check','--start-maximized'])
            try:
                for page in list(context.pages):page.close()
                page=context.new_page();page.goto(ENTRY,wait_until='domcontentloaded',timeout=45000)
                state('waiting-for-operator')
                expires=time.monotonic()+900
                for attempt in range(2):
                    pages=[]
                    trigger=directory/('read.request' if attempt==0 else 'retry.request')
                    while not trigger.exists() and time.monotonic()<expires and not (directory/'cancel.request').exists():
                        if not context.pages:return 1
                        context.pages[0].wait_for_timeout(300)
                    if not trigger.exists() or (directory/'cancel.request').exists():return 1
                    state('attempt-reading')
                    try:
                        pages=[]
                        for candidate in context.pages:
                            try:
                                safe_list_url(candidate.url)
                                pages.append(candidate)
                            except Exception:continue
                        if len(pages)!=1:raise ValueError('需要一个目标课程列表。')
                        report=read_list(pages[0],max_tasks=max_tasks,max_scrolls=max_scrolls,allow_real=True)
                        # Cancellation after collection still prevents publication.
                        if (directory/'cancel.request').exists():return 1
                        temporary=directory/'report.tmp'
                        temporary.write_text(json.dumps(encode_report(report),ensure_ascii=False),encoding='utf-8')
                        temporary.replace(directory/'report.json')
                        success=True
                        break
                    except Exception:
                        # Never log an external error, URL, title or response body.
                        if len(pages)==1:
                            try:
                                safe_list_url(pages[0].url)
                                probe=pages[0].evaluate('''() => ({
                                  activeTabs:[...document.querySelectorAll('.ant-tabs-tab-active .ant-tabs-tab-btn')].map(e=>({
                                    homework:/作业/.test(e.innerText),forbidden:/考试|测试|测验|试卷/.test(e.innerText),length:e.innerText.length})),
                                  panes:document.querySelectorAll('.ant-tabs-tabpane-active').length,
                                  lists:document.querySelectorAll('.ant-tabs-tabpane-active .ant-list').length,
                                  headings:document.querySelectorAll('h2.ant-typography').length,
                                  passwordFields:document.querySelectorAll('input[type=password]').length})''')
                                (directory/'diagnostic.json').write_text(json.dumps(probe),encoding='utf-8')
                            except Exception:pass
                        state('waiting for retry.request' if attempt==0 else 'stopped')
            finally:context.close()
    finally:
        try:
            if profile.exists():shutil.rmtree(profile)
        except OSError:
            state('cleanup-failed');return 2
        state('done' if success else 'stopped')
    return 0 if success else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--session-dir',type=Path,required=True)
    parser.add_argument('--max-tasks',type=int,default=5,choices=range(1,21))
    parser.add_argument('--max-scrolls',type=int,default=12,choices=range(1,13))
    args=parser.parse_args()
    try: code=run(args.session_dir,max_tasks=args.max_tasks,max_scrolls=args.max_scrolls)
    except Exception:code=1
    sys.exit(code)
