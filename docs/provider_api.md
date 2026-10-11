# 第三阶段提供方接口核对

核对日期：2026-10-11。只读取公开官方文档；未登录账号、未获取密钥、未进行真实调用。价格和模型可变，首次真实测试前必须再核对控制台和官方页面。

## 方案

采用 Python 标准库 `http.client` 的 HTTPS 文本请求，避免为一次短通知引入 SDK 及默认重试。客户端与任务、数据库解耦，共用同一个隐私 / 确认网关。
SDK 使用方便，但还需配置重试、日志及额外依赖；当前两家兼容协议所需字段较少，标准库更简单。

仅支持下列官方付费 API 地址，不支持第三方代理、任意网关或 MiMo Token Plan。只使用 JSON 文本输出；关闭思考与流式，不发送工具、联网搜索、图片或文件。
请求仅由已通过隐私网关的必要通知构造；本地规则能处理的通知不进入请求。模型、地址、单价、密钥或内容变化必须重新预览。

| 项目 | DeepSeek | 小米 MiMo |
| --- | --- | --- |
| Base URL | `https://api.deepseek.com`（也允许官方 `/v1` 兼容路径） | `https://api.xiaomimimo.com/v1` |
| POST 路径 | `/chat/completions` | `/v1/chat/completions` |
| 认证头 | `Authorization: Bearer ...` | `api-key: ...` |
| 内置模型选择 | `deepseek-flash`、`deepseek-v4-pro` | `mimo-v2.6-flash`、`mimo-v2.6-pro` |
| JSON 模式 | `response_format: {"type":"json_object"}` | 相同 |
| 输出上限字段 | `max_tokens` | `max_completion_tokens`（包含推理 tokens） |
| 思考 | `thinking: {"type":"disabled"}` | 相同 |
| Token 记录 | `usage.prompt_tokens` / `completion_tokens` | 相同 |

JSON 模式只保证语法倾向，不代表字段 / 日期正确。程序还校验枚举、长度、数量、布尔值、通知编号、引用原句、隐私及完整北京时间；工具调用拒绝处理，任何模型文本都不能执行为代码。

## 计费核对与实现边界

内置估算统一使用人民币 / 百万 tokens，并采用缓存未命中、较高时段的保守单价：

| 模型 | 输入 | 输出 |
| --- | ---: | ---: |
| deepseek-flash | 2 | 8 |
| deepseek-v4-pro | 9 | 27 |
| mimo-v2.6-flash | 1 | 2 |
| mimo-v2.6-pro | 3 | 6 |

DeepSeek 官方区分空闲 / 高峰及缓存价格；这里不尝试判断节假日或缓存优惠。MiMo 的普通 API 按 tokens 扣账户余额，与 Token Plan 配额独立；当前仅支持国内人民币计费配置，不自动混用海外美元价格。
MiMo 官方已列出 V2.6 模型；旧 V2.5 系列有退役安排，因此不作为默认模型。

调用前，以请求 UTF-8 字节数加协议余量作为保守输入 token 边界，加上显式输出上限，原子预留预算。返回有效用量后按配置单价计算保守费用；缓存折扣可能使实际账单更低。用量过大、无效 JSON 等不导致自动重试。
超时、断线、缺少有效用量或进程退出时，保留未决预留；未决请求跨北京时间日期继续占用预算。不要删除账本来解除上限；应先查提供方账单并人工核对。

这是本应用在正确人民币单价和文本 token 边界下的预算限制，不是提供方账户级扣费保证：其他应用的调用、价格调整、错误单价或提供方行为超出本地控制。建议同时在提供方控制台设置可用的账户限额 / 告警。用量账本不保存密钥、通知、请求或响应正文。

## 官方来源

- [DeepSeek 首次调用](https://api-docs.deepseek.com/)
- [DeepSeek 模型与价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)
- [DeepSeek Chat Completions 参数](https://api-docs.deepseek.com/api/create-chat-completion/)
- [MiMo Chat Completions 参数](https://mimo.mi.com/docs/en-US/api/chat)
- [MiMo JSON 输出](https://mimo.mi.com/docs/en-US/quick-start/usage-guide/text-generation/structured-output)
- [MiMo 计费说明](https://mimo.mi.com/docs/price)
- [MiMo 批量 API 文档中的实时 / 批量国内价格对照](https://mimo.mi.com/docs/en-US/quick-start/usage-guide/text-generation/batch-api)
- [MiMo API 接入、普通 Key 与 Token Plan 区别](https://mimo.mi.com/docs/en-US/quick-start/faq/api-integration)
