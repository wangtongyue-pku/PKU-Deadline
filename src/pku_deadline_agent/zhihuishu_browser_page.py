"""Small foreground read panel; opening the page never opens a browser."""

import streamlit as st

from pku_deadline_agent.zhihuishu_browser import BrowserRead, publish_result
from pku_deadline_agent.sources import SourceRejected

JOB = 'zhs_browser_job'


@st.fragment(run_every='2s')
def _progress(source="zhihuishu"):
    prefix = "yue_browser_" if source=="pku_yuetang" else "zhs_browser_"
    job_key = prefix+"job"
    job = st.session_state.get(job_key)
    if job is None:
        return
    state = job.status()
    if state == 'done':
        try:
            report = job.result()
            publish_result(report)
        except (OSError, ValueError, TypeError, SourceRejected):
            st.error('读取结果未通过校验，未替换原预览；本地诊断数据已保留。')
            if st.button('结束本次会话', key=prefix+'dismiss'):
                st.session_state.pop(job_key, None)
                st.rerun(scope='app')
            return
        from pku_deadline_agent.zhihuishu_review_page import invalidate_review
        # Old manual edits and write authorization belong to the previous read.
        invalidate_review()
        for key in list(st.session_state):
            if key.startswith('zhs_review_edit_') or key in (
                    'zhs_review_edits', 'zhs_review_prod_authorize', 'zhs_review_source'):
                del st.session_state[key]
        st.session_state.pop(job_key, None)
        st.session_state[prefix+'message'] = (
            f'已读取 {len(report.tasks)} 项，临时浏览器已退出；请在下方核对和补充。')
        st.rerun(scope='app')
    elif state in ('waiting', 'retry'):
        st.info(('请在新开的独立 Chrome / Edge 中自行登录问学阅堂，打开一门课的作业列表。不要进入作业详情、测试或考试。' if source=='pku_yuetang' else '请在新开的独立 Chrome / Edge 中自行登录，打开一门翻转课的'
                '「作业 → 未完成」列表；不要点击“开始做题”。只保留一个目标列表。'
                '从网页端首页登录并选择课程；如需复制完整列表地址，只粘到这个临时窗口的地址栏。'))
        if state == 'retry':
            st.warning('未能核验目标列表。请调整页面后重试一次；原读取结果仍保留。')
        if st.button('列表已打开，开始读取' if state == 'waiting' else '列表已调整，重试读取',
                     key=prefix+'ready'):
            try:
                job.ready()
            except (OSError, ValueError):
                st.warning('会话状态已变化，请稍后再查看。')
            st.rerun()
    elif state in ('stopped', 'cleanup_failed'):
        st.warning('本次读取已结束，未替换原结果。可重新打开临时浏览器再试。'
                   if state == 'stopped' else
                   '临时浏览器未能完成登录状态清理，请先关闭本次临时窗口并联系助手检查；诊断文件保留。')
        if state == 'stopped' and st.button('结束本次会话', key=prefix+'dismiss'):
            st.session_state.pop(job_key, None)
            st.rerun(scope='app')
    else:
        st.info({'launching': '正在打开临时浏览器…', 'reading': '正在有限读取列表，完成后将自动显示预览…',
                 'cancelling': '正在退出临时浏览器并清理本次登录状态…'}[state])
    if state not in ('done', 'stopped', 'cleanup_failed', 'cancelling'):
        if st.button('取消读取并关闭本次临时浏览器', key=prefix+'cancel'):
            job.cancel()
            st.rerun()


def render_browser_read(source="zhihuishu"):
    from pku_deadline_agent.review_platforms import platform
    label, _ = platform(source)
    prefix = "yue_browser_" if source=="pku_yuetang" else "zhs_browser_"
    job_key = prefix+"job"
    if message := st.session_state.pop(prefix+'message', None):
        st.success(message)
    if job_key not in st.session_state:
        # One card holds the whole start flow: what it does, the range, the consent
        # and the button - instead of a paragraph, a checkbox and a dead button.
        with st.container(border=True):
            st.markdown(f"#### 从{label}读取作业")
            st.caption('由你本人登录临时浏览器；只读列表里的标题 / 课程 / 截止 / 状态，'
                       '不进入任务、考试或视频，不写任务库，不调用 AI。')
            with st.expander('读取范围与安全说明'):
                tasks = st.number_input('本次最多读取任务数', min_value=1, max_value=20, value=20 if source=='pku_yuetang' else 5,
                                        key=prefix+'tasks')
                scrolls = st.number_input('本次最多滚动次数（含回到顶部）', min_value=1, max_value=12, value=12,
                                          key=prefix+'scrolls')
                st.caption('读取结果只在本机保存用于审核；退出会清理本次临时登录状态，'
                           '等待超过 15 分钟自动退出。')
            generation = st.session_state.get(prefix+'generation', 0)
            consent = st.checkbox(f'我同意本次只读一门课的作业列表，优先未完成，最多 {tasks} 项 / {scrolls} 次滚动',
                                  key=f'{prefix}consent_{tasks}_{scrolls}_{generation}')
            if st.button(f'读取{label}', disabled=not consent, key=prefix+'start', type='primary'):
                try:
                    st.session_state[job_key] = BrowserRead.start(
                        authorized=consent, max_tasks=int(tasks), max_scrolls=int(scrolls), source=source)
                    st.session_state[prefix+'generation'] = generation + 1
                except (OSError, ValueError, PermissionError):
                    st.error('无法启动临时浏览器，请检查本地浏览器和 Playwright 是否已安装。')
                st.rerun()
    _progress(source)
