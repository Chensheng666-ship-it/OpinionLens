# 变更记录

本项目自开源基线 0.1.0 起按版本记录变更。

## 0.2.1 · 2026-11-13（品牌视觉更新：深海青主题与全新图标）

**视觉**
- 全新「深海青」品牌配色：强调色由赤陶橙改为深青 `#0f766e`，侧栏改为深青墨色 `#08191b`，
  界面底色与中性色同步转为冷调纸感；语义色（正面/负面/警告/信息）重新校准对比度（正文 ≥4.5:1）。
- 全新应用图标与侧边栏 Logo：以「透镜 + 观察波纹」图形替换旧标记（`app-desktop/renderer/assets/`），
  同步生成 NSIS 安装包图标与窗口图标。
- AI 报告 HTML 模板品牌色同步为深海青（页头渐变、章节标记、表头、图表调色板、热词/链接色）。

**工程**
- 新增 `app/scripts/make_app_icons.py`：从设计参数一键重新生成 `app-icon-1024.png` / `app-icon.ico`（可复现）。
- Electron 截图辅助新增 `--demo-tab <model|notify|pool|about>`，便于自动化截取设置页各 tab。
- 重新生成 `docs/preview/` 全部界面截图，移除旧主题历史截图。
## 0.2.0 · 2026-11-12（设置中心：通知 / 多账号号池 / 模型 API）

**新增**
- **设置页**：侧边栏新增第 4 项「设置」，含模型 API、通知推送、账号池、关于 4 个 tab；
- **通知提醒**：采集完成/失败/部分完成、报告完成/失败/降级、账号登录失效、需要人工验证等事件触发通知；
  支持 SMTP 邮件、企业微信机器人、Server酱、Bark、通用 Webhook；outbox 落库、去重、重试退避、
  静默时段（默认 22:00–08:00）、隐私模式（默认最小化）、发送记录与人工重试；
- **多账号号池**：同平台多账号、每账号独立 Chromium profile（一进程一账号）；账号级扫码登录/检测/启停/
  设为默认/软删/旧目录迁移、健康度/冷却/日配额、选号策略（LRU/健康度/轮询/手动）、采集失败自动切号；
  旧单账号数据与旧 API 完全兼容；
- **多 LLM 模型 API**：应用内配置 OpenAI 兼容（含 DeepSeek/Ollama/vLLM/中转站）、DeepSeek、Anthropic；
  多套配置/指定默认/测试连接；API Key 使用 Windows DPAPI 加密，界面与 API 只显示脱敏值；
  AI 报告改为直连用户配置的模型生成叙述（本地统计 + 本地渲染 HTML），未配置/失败自动本地模板降级；
- **工程**：sidecar 新增进程内事件总线；`app.db` 新增账号池/通知/设置/模型 API 表并自动迁移；
  新增离线测试 `test_notify.py`（248）、`test_llm.py`（85）、`test_account_pool.py`（248），
  `selftest.py` 195 项全绿；Windows 数据目录 `%APPDATA%\OpinionLens`；
- **界面截图**：`docs/preview/77~95` 设置页/多账号/任务账号策略等截图。

**移除**
- 原内置 AI 报告服务与遥测（telemetry）相关代码、环境变量、文档与假服务：报告改由用户配置的模型直连；
- 旧 POC 报告工具（`ai_client.py` / `build_report.py` / `validate_report.py` / `selftest.py`）与相关文档；
- `app/desktop/config/telemetry.example.json` 打包配置。

## 0.1.0 · 2026-11-12（开源基线）

首个开源版本（MIT 许可），品牌名 **舆见 OpinionLens**。

**产品能力**
- 七平台扫码登录（小红书 / 抖音 / 快手 / B站 / 微博 / 百度贴吧 / 知乎）：应用内真实二维码、
  测试连接、一键批量检测、登录态本地持久化与清除；
- 安全验证人工接管：短信/滑块/拼图验证时打开可见浏览器窗口，完成后自动继续；
- 关键词采集（内容 + 评论）：多关键词多平台、分平台进度、采集动态内容流、技术日志、停止任务；
- 断点续采：任务中断后按任务/平台续跑，复用任务与时间窗口，内容按 ID 去重；
- Excel 导出：概览 + 分平台内容/评论表，导出条数与界面口径一致；非法字符清洗、防公式注入；
- AI 报告中心：时间窗口数据包统计、AI 叙述 + 本地渲染 HTML、本地模板兜底、自然语言版本修订、
  历史报告管理、声量趋势交互；
- 工程化：Electron + FastAPI sidecar、NSIS 安装包（per-user、内置便携 Python 运行时）、
  离线自测体系、受限环境兼容启动。

**开源化改造（本版本）**
- 全仓去品牌：窗口标题/侧边栏/安装包/数据目录统一为 OpinionLens；包名 `opinionlens-desktop`、
  appId `com.opinionlens.desktop`、可执行名 `OpinionLens`、数据目录 `%APPDATA%\OpinionLens`；
- 协议变更为 MIT（Copyright (c) 2026 OpinionLens Contributors）；
- 仓库整理：移除 POC、交互原型、早期 Skill 方案、样例数据、历史设计文档与旧截图；
- 文档重写：开源 README、贡献与合规说明、第三方许可证声明、构建与常用命令文档；
- 新增 `THIRD_PARTY_NOTICES.md` 与 MediaCrawler 非商业许可免责声明（仓库不随附其源码）。
