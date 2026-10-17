"""Foreground worker for the app's explicitly authorized bounded real read.

The app launches this existing Playwright route in a fresh per-run directory,
signals read.request only after the user logs in and opens ONE homework list,
and can send cancel.request. Wait expires after 15 minutes. One explicit retry
is allowed. All outcomes close the owned browser and clean its fresh profile;
old diagnostics and previous reports are never deleted. Whitelisted reports
reach the app only after process exit. No database or AI code lives here.

The CLI remains a diagnostic entry; ordinary users use the app buttons.
"""

import importlib
import argparse
import json
import re
import shutil
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from pku_deadline_agent.grid_session import detect_channel  # noqa: E402
from pku_deadline_agent.zhihuishu_reader import FLIPPED_HOMEWORK_LIST_PATH, REAL_HOST  # noqa: E402

OUT = ROOT / '.local-tmp' / 'zhs-sessions' / str(uuid.uuid4())
PROFILE = OUT / 'profile'
# Start at the public portal, NOT a course-bound child route without course ID.
# The operator logs in and selects their course. The reader's separate URL gate
# still accepts only the observed homework list; the portal itself is not read.
ENTRY = 'https://onlineweb.zhihuishu.com/'
TRIGGERS = ('read.request', 'retry.request')
MAX_TASKS, MAX_SCROLLS = 5, 1
SESSION_SECONDS = 15 * 60
OWN_PROFILE = False
FORBIDDEN = re.compile(r'exam|paper|answer|submit|video|learn|login|auth', re.I)
STRUCTURAL = {'stu', 'courseInfo', 'homeworkExam', 'index', 'homework', 'exam', 'html'}

# Node counts and booleans only: this never extracts a title, course or date.
PROBE = """() => {
  const count = s => document.querySelectorAll(s).length;
  const visible = s => [...document.querySelectorAll(s)].filter(e => {
    for (let n = e; n; n = n.parentElement) { const st = getComputedStyle(n);
      if (st.display === 'none' || st.visibility === 'hidden') return false; }
    return true; }).length;
  return {
    homework_content: count('.homework-content'),
    homework_items: count('.homework-item'),
    visible_items: visible('.homework-item'),
    active_tab_is_homework: [...document.querySelectorAll('.ant-tabs-tab[aria-selected="true"]')]
      .some(e => e.innerText.trim() === '作业'),
    active_state_is_unfinished: [...document.querySelectorAll('.state-tabs .menu-pane.active')]
      .some(e => e.innerText.trim() === '未完成'),
    password_fields: count('input[type=password]'),
    ant_spin: count('.ant-spin-spinning'),
    loading_class: count('[class*="loading"]'),
    skeleton: count('.ant-skeleton'),
    empty_marker: count('.ant-empty'),
  };
}"""
RENDER_WAIT_SECONDS = 45

# Per-card field-binding census. Counts, booleans and element SHAPE only - never a
# title, course or date. This is what answers "which selector failed on which
# card" for a partial read, so the next profile revision is evidence-based.
CENSUS = r"""(p) => {
  const cards=[...document.querySelectorAll('.homework-item')];
  const onScreen = e => {
    if(!e || !e.isConnected) return false;
    let r=e.getBoundingClientRect();
    let left=Math.max(0,r.left), right=Math.min(innerWidth,r.right);
    let top=Math.max(0,r.top), bottom=Math.min(innerHeight,r.bottom);
    for(let n=e;n;n=n.parentElement) {
      const s=getComputedStyle(n);
      if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||
         n.getAttribute('aria-hidden')==='true'||Number(s.opacity)===0) return false;
      if(n!==e && /(auto|scroll|hidden|clip)/.test(s.overflow+s.overflowX+s.overflowY)) {
        const b=n.getBoundingClientRect();
        left=Math.max(left,b.left);right=Math.min(right,b.right);
        top=Math.max(top,b.top);bottom=Math.min(bottom,b.bottom);
      }
    }
    return right>left && bottom>top;
  };
  const count = (card,sel) => { try { return sel
      ? [...card.querySelectorAll(sel)].filter(n=>n.closest('.homework-item')===card).length
      : -1; } catch(_) { return -2; } };
  const shape = e => e ? {tag:e.tagName.toLowerCase(), classes:e.classList.length,
      depth:(()=>{let d=0;for(let n=e;n;n=n.parentElement)d++;return d;})()} : null;
  const scrollers=[...document.querySelectorAll('*')].filter(e=>{
      const s=getComputedStyle(e);
      return /(auto|scroll)/.test(s.overflow+s.overflowY) && e.scrollHeight>e.clientHeight+2;});
  const label = e => (e && typeof e.className==='string' && e.className)
      ? e.className.trim().split(/\s+/).slice(0,3).join('.') : '';
  return {
    cards: cards.length,
    cards_on_screen: cards.filter(onScreen).length,
    document_scrollable: document.documentElement.scrollHeight > innerHeight + 2,
    inner_scroller_count: scrollers.length,
    inner_scroller_labels: scrollers.slice(0,3).map(label),
    per_card: cards.map((card,i)=>({
      index: i+1,
      on_screen: onScreen(card),
      title_matches: count(card,p.title),
      deadline_matches: count(card,p.deadline),
      card_course_matches: count(card,p.card_course),
      title_shape: shape(p.title?card.querySelector(p.title):null),
      deadline_shape: shape(p.deadline?card.querySelector(p.deadline):null),
      title_wrapper_nodes: card.querySelectorAll('.title-wrapper').length,
      tit_class_nodes: card.querySelectorAll('.tit').length,
      item_time_nodes: card.querySelectorAll('.homework-item-time').length,
    })),
  };
}"""


def status(text: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'status.txt').write_text(text, encoding='utf-8')
    print(f'[zhihuishu-real-read] {text}', flush=True)


def url_shape(url: str) -> dict:
    """Structural description only; identifiers never leave the browser."""
    parts = urlsplit(url)
    segments = [s for s in parts.path.split('/') if s]
    return {
        'hostname': parts.hostname,
        'host_is_observed_list_host': parts.hostname == REAL_HOST,
        'path_shape': '/' + '/'.join(s if s in STRUCTURAL else '<seg>' for s in segments)
                      + ('/' if parts.path.endswith('/') else ''),
        'segment_count': len(segments),
        'has_query': bool(parts.query),
        'has_fragment': bool(parts.fragment),
        'forbidden_words_in_path': sorted({w.lower() for w in FORBIDDEN.findall(unquote(parts.path))}),
        'path_equals_observed': unquote(parts.path) == FLIPPED_HOMEWORK_LIST_PATH,
        'has_credentials_in_url': bool(parts.username or parts.password),
    }


def targets(context):
    """Real http(s) pages and iframes only, best candidate first.

    Auxiliary frames (about:blank, blob:, data:) are excluded outright: the reader
    treats a top-level about:blank page as the offline fictional fixture, and an
    empty auxiliary frame must never masquerade as one.
    """
    found = []
    for page in context.pages:
        if page.is_closed():
            continue
        for kind, target in [('page', page)] + [('frame', f) for f in page.frames
                                                if f is not page.main_frame]:
            try:
                parts = urlsplit(target.url)
            except Exception:                    # noqa: BLE001 - target may be gone
                continue
            if parts.scheme not in ('http', 'https') or not parts.hostname:
                continue
            found.append((kind, target))

    def rank(item):
        kind, target = item
        shape = url_shape(target.url)
        return (not shape['path_equals_observed'], not shape['host_is_observed_list_host'],
                'homeworkExam' not in shape['path_shape'], kind != 'page')

    return sorted(found, key=rank)


def list_targets(context):
    """Only the observed list route; never inspect login/learning/exam pages."""
    found = []
    for kind, target in targets(context):
        p = urlsplit(target.url)
        if (p.scheme == 'https' and p.hostname == REAL_HOST and
                p.port in (None, 443) and not p.username and not p.password and
                p.path in (FLIPPED_HOMEWORK_LIST_PATH, FLIPPED_HOMEWORK_LIST_PATH.rstrip('/'))):
            found.append((kind, target))
    return found


def watch(context) -> None:
    snapshot = []
    # URL SHAPES only, including non-list tabs, so missing/wrong windows can be
    # diagnosed without probing their DOM, queries, identifiers or credentials.
    for kind, target in targets(context):
        try:
            snapshot.append(url_shape(target.url) | {'kind': kind})
        except Exception:                        # noqa: BLE001
            continue
    with (OUT / 'watch.log').open('a', encoding='utf-8') as handle:
        handle.write(f'{time.strftime("%H:%M:%S")} targets={len(snapshot)} '
                     f'{json.dumps(snapshot, ensure_ascii=False)}\n')


def pump_events(context, milliseconds=500):
    """Dispatch navigation/new-tab events without reading DOM or clicking.

    Playwright's synchronous API dispatches events during API calls. Python
    time.sleep leaves cached Page.url/context.pages stale after manual login.
    """
    for page in context.pages:
        if not page.is_closed():
            page.wait_for_timeout(milliseconds)
            return
    raise RuntimeError('temporary browser closed')


def probe(context) -> list:
    """Where is the 作业/未完成 list? Counts and booleans only."""
    results = []
    for kind, target in list_targets(context):
        try:
            results.append(url_shape(target.url) | {'kind': kind,
                                                    'nodes': target.evaluate(PROBE)})
        except Exception as error:               # noqa: BLE001
            results.append({'kind': kind, 'probe_failed': True})
    return results


def first_nodes(context):
    """Node counts of the best candidate, or None when nothing can be probed."""
    for kind, target in list_targets(context):
        try:
            return target.evaluate(PROBE)
        except Exception:                        # noqa: BLE001 - try the next one
            continue
    return None


def wait_for_render(context, seconds=RENDER_WAIT_SECONDS):
    """Poll until the list actually renders; never invents a result."""
    deadline = time.monotonic() + seconds
    nodes = None
    while time.monotonic() < deadline:
        if (OUT / 'cancel.request').exists():
            return None
        nodes = first_nodes(context)
        if nodes and nodes.get('homework_items', 0) > 0:
            return nodes
        pump_events(context)
    return nodes


def census(context, profile) -> dict:
    """Structural field-binding census of the best candidate (no text at all)."""
    params = {'title': profile.title, 'deadline': profile.deadline,
              'card_course': profile.card_course}
    for kind, target in list_targets(context):
        try:
            return {'kind': kind, 'url': url_shape(target.url),
                    'nodes': target.evaluate(CENSUS, params)}
        except Exception:                        # noqa: BLE001 - try the next one
            continue
    return {'error': 'census-unavailable'}


def readers(reload=False):
    """Resolve the read path from the (optionally reloaded) modules."""
    import pku_deadline_agent.zhihuishu_reader as reader
    import pku_deadline_agent.zhihuishu_scroll as scroll
    import pku_deadline_agent.zhihuishu_session as session
    import pku_deadline_agent.zhihuishu_validation as validation
    if reload:
        for module in (reader, scroll, session, validation):
            importlib.reload(module)
    return (session.read_list_for_review, reader.FLIPPED_HOMEWORK_VALIDATION_PROFILE,
            session.ReviewReadScope(max_tasks=MAX_TASKS, max_scrolls=MAX_SCROLLS, auto_container=True),
            validation.encode_report, validation.summarize)


def _run() -> int:
    global OWN_PROFILE
    # Each run owns a NEW directory. Never delete earlier diagnostic data.
    OUT.mkdir(parents=True, exist_ok=True)
    if PROFILE.exists() or (OUT / 'status.txt').exists():
        raise ValueError('会话目录已使用，拒绝复用登录状态。')

    from playwright.sync_api import sync_playwright

    status('launching-browser')
    PROFILE.mkdir(parents=True, exist_ok=False)
    OWN_PROFILE = True
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel=detect_channel(), headless=False,
            no_viewport=True,
            args=['--no-first-run', '--no-default-browser-check', '--start-maximized'])
        entry = None
        try:
            # A kept profile makes Chrome restore the previous session, which can
            # put the interesting tab in a window Playwright does not report.
            # Close everything first so exactly ONE tracked tab remains.
            restored = list(context.pages)
            for page in restored:
                page.close()
            entry = context.new_page()
            print(f'[zhihuishu-real-read] closed {len(restored)} restored page(s); '
                  f'now {len(context.pages)} tracked page(s)', flush=True)
            entry.goto(ENTRY, wait_until='domcontentloaded')
            status('waiting-for-operator: log in and open the 作业/未完成 list')
            watch(context)
            expires = time.monotonic() + SESSION_SECONDS
            for attempt, trigger in enumerate(TRIGGERS, 1):
                request = OUT / trigger
                while not request.exists():
                    if (OUT / 'cancel.request').exists() or time.monotonic() > expires:
                        status('cancelled-or-expired')
                        return 1
                    if all(p.is_closed() for p in context.pages):
                        status('browser-closed-by-operator')
                        return 1
                    pump_events(context)
                    watch(context)

                read, profile, scope, encode, summarize = readers(reload=attempt > 1)
                pump_events(context, 50)
                if len(list_targets(context)) != 1:
                    status('multiple-or-missing-lists: keep exactly one target list open')
                    continue
                status(f'attempt-{attempt}: waiting for the list to render')
                nodes = wait_for_render(context)
                if (OUT / 'cancel.request').exists():
                    status('cancelled-or-expired')
                    return 1
                print(f'[zhihuishu-real-read] rendered nodes: '
                      f'{json.dumps(nodes, ensure_ascii=False)}', flush=True)
                (OUT / 'probe.json').write_text(
                    json.dumps(probe(context), ensure_ascii=False, indent=2), encoding='utf-8')
                (OUT / 'census.json').write_text(
                    json.dumps(census(context, profile), ensure_ascii=False, indent=2),
                    encoding='utf-8')

                shapes, failure = [], '没有可读取的页面或 iframe'
                status(f'attempt-{attempt}: reading')
                candidates = list_targets(context)
                # Two open course views cannot silently become a multi-course read.
                if len(candidates) != 1:
                    status('multiple-or-missing-lists: keep exactly one target list open')
                    continue
                for kind, target in candidates:
                    shapes.append(url_shape(target.url) | {'kind': kind})
                    print(json.dumps(shapes[-1], ensure_ascii=False), flush=True)
                    try:
                        report = read(target, profile, scope=scope,
                                      allow_real=True, validation_only=True)
                    except Exception as error:   # noqa: BLE001 - try the next target
                        # It may already have touched some fields. A second read
                        # would reset the round's cumulative task budget.
                        status('error: bounded read failed; start a newly authorized round')
                        return 1

                    if (OUT / 'cancel.request').exists():
                        status('cancelled-or-expired')
                        return 1

                    payload = encode(report)
                    (OUT / 'report.json').write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
                    summary = summarize(report)
                    (OUT / 'summary.json').write_text(
                        json.dumps(summary.as_dict(), ensure_ascii=False, indent=2), encoding='utf-8')
                    status('done')
                    print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2), flush=True)
                    return 0

                (OUT / 'url-shapes.json').write_text(
                    json.dumps(shapes, ensure_ascii=False, indent=2), encoding='utf-8')
                if attempt < len(TRIGGERS):
                    status(f'attempt-{attempt}: no readable target ({failure}); '
                           'window kept open — waiting for retry.request')
                    continue
                status(f'attempt-{attempt}: no readable target ({failure}); stopping')
                return 1
            return 1
        except Exception as error:               # noqa: BLE001 - report, never guess
            status('error: browser session failed; no database was written')
            return 1
        finally:
            context.close()


def main() -> int:
    try:
        return _run()
    except Exception:
        status('error: browser session failed; no database was written')
        return 1
    finally:
        # Also covers launch/import failures; never clean a pre-existing profile.
        if OWN_PROFILE and PROFILE.resolve().parent == OUT.resolve():
            try:
                shutil.rmtree(PROFILE)
            except OSError:
                status('cleanup-failed: close the temporary browser before retrying')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='智慧树受限只读会话（需本人授权与登录）')
    parser.add_argument('--session-dir', type=Path)
    parser.add_argument('--max-tasks', type=int, default=5, choices=range(1, 21))
    parser.add_argument('--max-scrolls', type=int, default=1, choices=range(1, 13))
    args = parser.parse_args()
    if args.session_dir is not None:
        OUT = args.session_dir.resolve()
    PROFILE = OUT / 'profile'
    MAX_TASKS, MAX_SCROLLS = args.max_tasks, args.max_scrolls
    sys.exit(main())
