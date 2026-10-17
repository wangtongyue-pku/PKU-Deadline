"""Finite DOM-only PKU Yuetang homework reader. No clicks, private API or DB.

Observed: course h2, homework tab, Ant List card-local title/actions. No stable
task ID is available; node references below are ephemeral, never source identity.
"""
import re
from datetime import datetime
from urllib.parse import urlsplit
from pku_deadline_agent.sources import SourceRejected
from pku_deadline_agent.time_utils import BEIJING
from pku_deadline_agent.zhihuishu_reader import ReadTask, _text
from pku_deadline_agent.zhihuishu_scroll import ScrollCoverage, ScrollReport

SOURCE = 'pku_yuetang'
ENTRY = 'https://class.pku.edu.cn/'


def safe_list_url(value):
    if value is None: return None
    if not isinstance(value,str) or len(value)>2000: raise SourceRejected('来源链接无效。')
    try:
        p=urlsplit(value)
        if (p.scheme!='https' or p.hostname!='class.pku.edu.cn' or p.port not in (None,443)
            or p.username or p.password or p.fragment or not re.fullmatch(r'/courses/[A-Za-z0-9_-]+/?',p.path)):
            raise ValueError()
    except ValueError:
        raise SourceRejected('仅允许已核验的问学阅堂课程列表链接。') from None
    return 'https://class.pku.edu.cn'+p.path  # Never persist query parameters.


def parse_due(text):
    text=_text(text,100)
    if not text: return None,None
    m=re.fullmatch(r'(?:截止(?:时间|日期)?\s*[:：]?\s*)?(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?',text)
    if not m: return None,None
    y,month,d,h,minute,second=m.groups()
    try: value=datetime(int(y),int(month),int(d),int(h or 0),int(minute or 0),int(second or 0),tzinfo=BEIJING)
    except ValueError: return None,None
    return (value,None) if h is not None else (None,value.date().isoformat())


CONTROLLER=r'''() => {
 const shown=e=>{if(!e)return false;for(let n=e;n;n=n.parentElement){const s=getComputedStyle(n);
  if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||s.opacity==='0'||n.getAttribute('aria-hidden')==='true')return false;}return true;};
 const visible=e=>{if(!shown(e))return false;let r=e.getBoundingClientRect(),t=Math.max(0,r.top),b=Math.min(innerHeight,r.bottom),l=Math.max(0,r.left),q=Math.min(innerWidth,r.right);
  for(let n=e.parentElement;n;n=n.parentElement)if(/auto|scroll|hidden|clip/.test(getComputedStyle(n).overflowY)){const a=n.getBoundingClientRect();t=Math.max(t,a.top);b=Math.min(b,a.bottom);l=Math.max(l,a.left);q=Math.min(q,a.right);}return b>t&&q>l;};
 const tabs=[...document.querySelectorAll('.ant-tabs-tab-active .ant-tabs-tab-btn')].filter(shown);
 const homeworkTab=()=>tabs.length===1&&/作业/.test(tabs[0].innerText)&&!/考试|测试|测验|试卷/.test(tabs[0].innerText)&&tabs[0].innerText.length<80;
 if(!homeworkTab()||document.querySelector('input[type=password]'))throw Error('not-homework-list');
 const panes=[...document.querySelectorAll('.ant-tabs-tabpane-active')].filter(shown);
 if(panes.length!==1)throw Error('ambiguous-pane');
 const pane=panes[0],lists=[...pane.querySelectorAll('.ant-list')].filter(shown);
 if(lists.length!==1)throw Error('list-layout-changed');
 const list=lists[0];let scope=list;while(scope&&!scope.querySelector('h2.ant-typography'))scope=scope.parentElement;
 const names=scope?[...scope.querySelectorAll('h2.ant-typography')].filter(shown):[];
 if(names.length!==1)throw Error('course-not-bound');
 const name=names[0],course=name.innerText.trim(),locationKey=location.href;
 if(!course||/^(课程|课程详情|作业|全部作业)$/.test(course))throw Error('course-not-bound');
 const ancestors=[];for(let n=list;n&&n!==document.body&&n!==document.documentElement;n=n.parentElement)
  if(/^(auto|scroll)$/.test(getComputedStyle(n).overflowY)&&n.scrollHeight>n.clientHeight+2)ancestors.push(n);
 if(ancestors.length>1)throw Error('ambiguous-scroller');
 const scroller=ancestors[0]||document.scrollingElement;let old=[...list.querySelectorAll('.ant-list-item')];
 const check=()=>{const now=[...list.querySelectorAll('.ant-list-item')];
  if(location.href!==locationKey||!name.isConnected||name.innerText.trim()!==course||!pane.classList.contains('ant-tabs-tabpane-active')||!homeworkTab()||!list.isConnected||now.length<old.length||old.some((e,i)=>now[i]!==e))throw Error('unstable-list');old=now;return now;};
 const field=(card,s)=>{const all=[...card.querySelectorAll(s)].filter(e=>e.closest('.ant-list-item')===card&&visible(e));if(all.length>1)throw Error('ambiguous-field');return all[0]?.innerText.trim()||null;};
 return {snapshot:(limit)=>{const all=check(),onscreen=all.map((c,i)=>({c,i})).filter(x=>visible(x.c));
  return {course,loaded:all.length,overflow:onscreen.some(x=>x.i>=limit),rows:onscreen.filter(x=>x.i<limit).map(({c,i})=>({position:i,title:field(c,'.ant-list-item-meta-title a'),due:field(c,'.ant-list-item-action .ant-typography-secondary'),status:field(c,'.ant-list-item-action .ant-tag')})),
   busy:!!pane.querySelector('.ant-spin-spinning,.ant-skeleton'),pagination:!!pane.querySelector('.ant-pagination'),bottom:scroller.scrollTop+scroller.clientHeight>=scroller.scrollHeight-2};},
  move:()=>{check();const before=scroller.scrollTop;scroller.scrollTop+=Math.max(100,scroller.clientHeight*.7);return scroller.scrollTop!==before;}};
}'''


def read_list(page,*,max_tasks=20,max_scrolls=12,allow_real=False):
    if type(max_tasks) is not int or not 1<=max_tasks<=20 or type(max_scrolls) is not int or not 0<=max_scrolls<=12:
        raise ValueError('读取范围须为 1–20 项、0–12 次滚动。')
    real=page.url!='about:blank'
    if real:
        if allow_real is not True: raise PermissionError('真实读取必须先获得本次授权。')
        safe_list_url(page.url)
    handle=page.evaluate_handle(CONTROLLER);fragments={};peak=0;moves=0
    try:
        for turn in range(max_scrolls+1):
            first=handle.evaluate('(h,n)=>h.snapshot(n)',max_tasks);page.wait_for_timeout(150)
            last=handle.evaluate('(h,n)=>h.snapshot(n)',max_tasks)
            if first!=last or last['busy']:raise ValueError('列表尚未稳定或仍在加载，请准备好后重新读取。')
            course=_text(last['course'],100);peak=max(peak,last['loaded'])
            for row in last['rows']:
                prior=fragments.setdefault(row['position'],{})
                for k in ('title','due','status'):
                    if row[k] is not None:
                        if prior.get(k) is not None and prior[k]!=row[k]:raise ValueError('同一作业字段变化，已停止读取。')
                        prior[k]=row[k]
            if last['overflow'] or len(fragments)>=max_tasks or last['bottom'] or turn==max_scrolls:break
            handle.evaluate('(h)=>h.move()');moves+=1;page.wait_for_timeout(200)
    finally:handle.dispose()
    tasks=[]
    for pos,row in sorted(fragments.items()):
        due,day=parse_due(row.get('due'))
        status={'未提交':'未完成','待提交':'未完成','已提交':'已完成','已批改':'已完成'}.get(row.get('status'),'未知')
        tasks.append(ReadTask(_text(row.get('title'),200) or '',course,due,day,source_url=safe_list_url(page.url) if real else None,status=status,scan_position=pos))
    reasons=['boundary_unverified','displayed_total_unknown']
    if last['pagination']:reasons.append('pagination_not_traversed')
    if peak>max_tasks or last['overflow']:reasons.append('task_limit')
    if not last['bottom']:reasons.append('scroll_limit')
    if len(tasks)<peak:reasons.append('unread_positions')
    fields=bool(tasks) and all(t.title and t.course and t.deadline is not None and t.status!='未知' for t in tasks)
    if not fields:reasons.append('fields_incomplete')
    coverage=ScrollCoverage(None,peak,last['loaded'],len(fragments),len(tasks),moves,last['bottom'],False,fields,False,
        '仅核验当前课程列表；未证明全部分页或懒加载边界',max(0,peak-len(tasks)),tuple(reasons))
    return ScrollReport(tuple(tasks),coverage,course,'authorized-real-validation' if real else 'synthetic',source=SOURCE)
