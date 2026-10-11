"""Local, explainable fragment filtering; leave task/time understanding to stage 3.

Names are contextual heuristics. Ambiguous retained privacy contexts must be
edited, never approved away. No model, raw-text fallback, storage, or networking.
"""

import re
from datetime import datetime

from pku_deadline_agent.notices import FilteredBatch, FilteredNotice, _parse_publication
from pku_deadline_agent.privacy import Finding

MASK = "[已移除敏感信息]"
_TASK = re.compile(r"作业|习题|题集|练习|报告|论文|实验|考试|测验|报名|活动|讲座|比赛|竞赛|申请|提交|上交|补交|完成|选课|签到|整理|资料|文档|项目|设计稿|收集|截止|延期|延长|推迟|提前|改为|更改|调整|取消|开放|deadline|ddl", re.I)
_DATE = re.compile(r"\d{1,4}[-/年]\d{1,2}|\d{1,2}[月日号点时]|\d{1,2}:\d{2}|明[天早晚日]|后天|今[天晚日]|[上下本]周|星期|周[一二三四五六日天]|待定|另行通知")
_COURSE = re.compile(r"《[^》\n]{1,80}》|课程|课上|课后|(?:数学|物理|英语|程序设计|数据结构|算法|线性代数|计算概论)(?:课)?")
_CHAT = re.compile(r"吃饭|约饭|奶茶|哈哈|八卦|吐槽|打游戏|室友|吵架|恋爱|感情|家里|我家|生病|病历|诊断|住院|医院|心情很|天气真|私人话题|私人聊天|随便聊|无关聊天")
_PERSONAL_CHAT = re.compile(r"(?:我(?!们)|室友|朋友).{0,12}(?:打游戏|奶茶|吃饭|约饭|心情|吐槽)")
_GREETING = re.compile(r"^(?:大家好|各位(?:同学)?好|同学们好?|谢谢(?:大家)?|收到|好的|晚安|早上好|[!！。.,，\s]+)$")
_CONTACT_LINE = re.compile(r"^(?:联系人|联系方式|姓名|学号|手机号|手机|电话|邮箱|微信号|QQ号|住址|家庭住址|昵称)\s*[:=]", re.I)
_RISK = re.compile(r"姓名|名字|昵称|联系人|(?:负责人|主讲人|报告人)\s*[:=]|联系方式|学号|身份证|手机号|手机号码|电话|邮箱|微信号|微信联系|QQ|私聊|私人|住址|我的地址|我住|家庭(?!作业)|宿舍(?:号|地址|是|在)|室友|吵架|病历|诊断|住院|医院|生病|恋爱|感情|我家|我叫|我是|个人信息", re.I)
_PERSONAL_ONLY = re.compile(r"^(?:姓名|昵称|联系人|负责人|联系方式|学号|身份证|手机号|电话|邮箱|微信号|我叫|我是|我的地址|我住|家庭住址)")
_MASKED_CONTEXT = re.compile(r"(?:姓名|联系人|负责人|主讲人|报告人|授课教师|学号|手机号|手机|电话|邮箱|微信号|QQ号|联系方式|联系|我叫|我是)\s*(?:[:=]|是|为|到)?\s*\[已移除敏感信息\]", re.I)
# Chat export headers are local metadata, not publication timestamps. Only a
# separately supplied publication value is used as a relative-date reference.
_CHAT_HEADER = re.compile(r"(?m)^.{1,30}?[ \t]+(?:\d{4}[-/]\d{2}[-/]\d{2}[ \t]+)?\d{1,2}:\d{2}(?::\d{2})?[ \t]*[:：]?[ \t]*")
_MASKED_SENDER = re.compile(r"(?m)^\[已移除敏感信息\][ \t]*[:：][ \t]*")
_SENDER_PREFIX = re.compile(r"(?m)^([^\n:：]{1,20})[:：][ \t]*")
_PUBLICATION = re.compile(r"^\s*(?:发布时间|发布日期|published_at)\s*[:=]\s*(.*?)\s*$", re.I)


def filter_natural_sections(sections: list[str], findings: tuple[Finding, ...], published_at: datetime | None) -> FilteredBatch:
    notices: list[FilteredNotice] = []
    errors: list[str] = []
    warnings: list[str] = []
    ignored = skipped = 0
    for index, section in enumerate(sections, start=1):
        publication = published_at
        lines: list[str] = []
        publication_seen = False
        for line in section.splitlines():
            match = _PUBLICATION.fullmatch(line)
            if match:
                if publication_seen:
                    errors.append(f"第 {index} 条通知有重复发布时间，请保留一个明确值。")
                publication_seen = True
                publication = _parse_publication(match[1])
                if publication is None:
                    warnings.append(f"第 {index} 条通知发布时间不明确，保留为未知。")
            else:
                lines.append(line)
        text = _CHAT_HEADER.sub(lambda match: match[0] if _TASK.search(match[0]) or _COURSE.search(match[0]) else "", "\n".join(lines))
        text = _MASKED_SENDER.sub("", text)
        # Unknown nicknames in "sender: message" are unnecessary. Preserve
        # task/course labels and personal-risk labels so a risk cannot be hidden.
        text = _SENDER_PREFIX.sub(lambda match: match[0] if _TASK.search(match[1]) or _COURSE.search(match[1]) or _RISK.search(match[1]) else "", text)
        fragments: list[str] = []
        uncertain = False
        for fragment in re.split(r"(?<=[。！？；!?;，,])|\n+", text):
            fragment = fragment.strip()
            meaningful = fragment.replace(MASK, "").strip(" ，,。；;:：!！?？")
            if not meaningful:
                ignored += bool(fragment)
                continue
            task_related = bool(_TASK.search(fragment))
            context = bool(_DATE.search(fragment) or _COURSE.search(fragment))
            if _GREETING.fullmatch(meaningful) or (_CHAT.search(fragment) and not task_related) or (_CONTACT_LINE.search(fragment) and not task_related) or (_PERSONAL_ONLY.search(fragment) and not task_related and not context):
                ignored += 1
                continue
            # Known masked labels are safe to retain; unhandled personal contexts
            # in a task clause cannot be removed reliably without human editing.
            residual = _MASKED_CONTEXT.sub(MASK, fragment)
            if _RISK.search(residual) or _PERSONAL_CHAT.search(residual):
                uncertain = True
            if not task_related and not context:
                warnings.append(f"第 {index} 条通知有相关性不明确的片段，请在预览中确认是否需要保留。")
            fragments.append(fragment)
        candidate = "\n".join(fragments)
        if not candidate:
            skipped += 1
            warnings.append(f"第 {index} 条通知只剩已移除的内容或明显闲聊，没有必要发送的片段。")
            continue
        if uncertain:
            errors.append(f"第 {index} 条通知保留片段仍有无法可靠消除的个人信息，请修改原文或使用结构化输入；不能确认放行。")
        if publication is None and _DATE.search(candidate):
            warnings.append(f"第 {index} 条通知未提供明确发布时间；相对日期或模糊时间保留待确认，不使用粘贴时间推算。")
        notices.append(FilteredNotice(index, "待解析通知", "", "其他", None, publication, candidate))
    if not notices:
        errors.append("筛选后没有需要发送的通知片段。可修改输入或切换到结构化输入；不会上传原始聊天。")
    return FilteredBatch(tuple(notices), findings, ignored, skipped, tuple(errors), tuple(dict.fromkeys(warnings)))
