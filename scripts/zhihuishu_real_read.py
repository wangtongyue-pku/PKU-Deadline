"""One authorized, scope-capped REAL read of the list currently on screen.

Flow (nothing is read until the operator confirms):

1. Open a temporary VISIBLE, maximized browser window at the Zhihuishu entry
   page. It uses a throwaway profile directory that is deleted when the session
   ends (``--keep-profile`` keeps it across a diagnostic restart only, so a login
   does not have to be repeated; it is still deleted before the round is over).
2. While waiting, log which pages AND iframes this browser can actually see
   (sanitized address shapes only) — so "you are in the wrong window" or "the
   list lives inside an iframe" is visible before any read is attempted.
3. Wait for ``.local-tmp/zhs-validation/read.request``, which the assistant
   creates only after the operator says the target list is open.
4. Try the bounded read on every page and frame this browser sees, most likely
   target first: 作业 → 未完成, at most 5 tasks, at most one positioning scroll,
   validation-only mode. A target that is not the authorized list is refused by
   the reader's own URL gate before any field is read.
5. If nothing can be read, run a STRUCTURAL probe (node counts and booleans only
   — never task text) and record it, so the reason is diagnosable.
6. On success write the whitelisted display payload (title/course/deadline/status
   and coverage counters — never observed IDs, links or page text) plus a
   counts-only summary, then close the browser.

If no target can be read, the window stays open and the script waits for
``retry.request``. Retrying re-imports the reader modules in place.

Run: .\\.venv\\Scripts\\python.exe scripts/zhihuishu_real_read.py
"""

import importlib
import json
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from pku_deadline_agent.grid_session import detect_channel  # noqa: E402
from pku_deadline_agent.zhihuishu_reader import FLIPPED_HOMEWORK_LIST_PATH, REAL_HOST  # noqa: E402

OUT = ROOT / '.local-tmp' / 'zhs-validation'
PROFILE = OUT / 'profile'
# The one address the reader's validation mode is allowed to read. The script
# opens it directly so the operator and the script are looking at the same page.
ENTRY = 'https://wenda.zhihuishu.com/stu/courseInfo/homeworkExam/'
TRIGGERS = ('read.request', 'retry.request')
KEEP_PROFILE = '--keep-profile' in sys.argv
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
    title: document.title,
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


def watch(context) -> None:
    snapshot = []
    for kind, target in targets(context):
        try:
            snapshot.append(url_shape(target.url) | {'kind': kind})
        except Exception:                        # noqa: BLE001
            continue
    with (OUT / 'watch.log').open('a', encoding='utf-8') as handle:
        handle.write(f'{time.strftime("%H:%M:%S")} targets={len(snapshot)} '
                     f'{json.dumps(snapshot, ensure_ascii=False)}\n')


def probe(context) -> list:
    """Where is the 作业/未完成 list? Counts and booleans only."""
    results = []
    for kind, target in targets(context):
        try:
            results.append(url_shape(target.url) | {'kind': kind,
                                                    'nodes': target.evaluate(PROBE)})
        except Exception as error:               # noqa: BLE001
            results.append({'kind': kind, 'probe_failed': f'{type(error).__name__}: {str(error)[:160]}'})
    return results


def first_nodes(context):
    """Node counts of the best candidate, or None when nothing can be probed."""
    for kind, target in targets(context):
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
        nodes = first_nodes(context)
        if nodes and nodes.get('homework_items', 0) > 0:
            return nodes
        time.sleep(2)
    return nodes


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
            session.ReviewReadScope(max_tasks=5, max_scrolls=1),
            validation.encode_report, validation.summarize)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if not KEEP_PROFILE:
        shutil.rmtree(PROFILE, ignore_errors=True)
    for stale in TRIGGERS + ('report.json', 'summary.json', 'watch.log', 'probe.json'):
        (OUT / stale).unlink(missing_ok=True)
    print(f'[zhihuishu-real-read] profile={"kept" if KEEP_PROFILE else "fresh"}', flush=True)

    from playwright.sync_api import sync_playwright

    status('launching-browser')
    PROFILE.mkdir(parents=True, exist_ok=True)
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
            for attempt, trigger in enumerate(TRIGGERS, 1):
                request = OUT / trigger
                while not request.exists():
                    if all(p.is_closed() for p in context.pages):
                        status('browser-closed-by-operator')
                        return 1
                    watch(context)
                    time.sleep(2)

                read, profile, scope, encode, summarize = readers(reload=attempt > 1)
                status(f'attempt-{attempt}: waiting for the list to render')
                nodes = wait_for_render(context)
                print(f'[zhihuishu-real-read] rendered nodes: '
                      f'{json.dumps(nodes, ensure_ascii=False)}', flush=True)
                (OUT / 'probe.json').write_text(
                    json.dumps(probe(context), ensure_ascii=False, indent=2), encoding='utf-8')

                shapes, failure = [], '没有可读取的页面或 iframe'
                status(f'attempt-{attempt}: reading')
                for kind, target in targets(context):
                    shapes.append(url_shape(target.url) | {'kind': kind})
                    print(json.dumps(shapes[-1], ensure_ascii=False), flush=True)
                    try:
                        report = read(target, profile, scope=scope,
                                      allow_real=True, validation_only=True)
                    except Exception as error:   # noqa: BLE001 - try the next target
                        failure = f'{type(error).__name__}: {str(error)[:200]}'
                        continue

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
            status(f'error: {type(error).__name__}: {str(error)[:300]}')
            return 1
        finally:
            context.close()
            if not KEEP_PROFILE:
                shutil.rmtree(PROFILE, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
