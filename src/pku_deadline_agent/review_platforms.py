"""Fixed platform identities for the existing human-reviewed import pipeline.

SQL identifiers below come only from this allowlist, never from page content.
"""
PLATFORMS = {
    'zhihuishu': ('智慧树', 'zhs'),
    'pku_yuetang': ('问学阅堂', 'yue'),
}

def platform(source):
    try: return PLATFORMS[source]
    except (KeyError,TypeError): raise ValueError('未知的审核来源。') from None

def compatible(source, target_source):
    return not (source in PLATFORMS and target_source in PLATFORMS and source!=target_source)

def source_link(source,value):
    platform(source)
    if source=='pku_yuetang':
        from pku_deadline_agent.yuetang_reader import safe_list_url
        return safe_list_url(value)
    from pku_deadline_agent.zhihuishu_reader import _source_link
    return _source_link(value,'https://wenda.zhihuishu.com/')
