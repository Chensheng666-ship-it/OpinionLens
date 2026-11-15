# 变更记录

本项目自开源基线 0.1.0 起按版本记录变更。

## 0.6.0 · 2026-11-15（工程化重构：模块拆分 · 增量轮询 · 类型全绿 · 安装包瘦身）

本轮没有堆新功能，而是把 0.5.0 之后快速长出来的代码重新整形——目标只有一个：**让它能被长期维护**。
重构后功能行为保持不变（离线自测 207 项、账号池 256 项、导出 233 项、报告 124 项、通知 248 项、
模型 API 85 项、引擎 212 项、RunManager 38 项全绿，Electron 端到端 9 项断言全绿）。

**引擎层：从 monkeypatch 大杂烩到可探测的适配器**
- `patches.py` 2617 → **703 行门面**，实现拆到 `app/engine/adapters/`（base/session/browser/content/qr/manual/network/login + 7 个平台适配器）
- 新增**上游契约探测**：74 项符号 / 10 组，启动时真实 import + getattr 链校验；缺失即
  `ENGINE_INCOMPATIBLE`（明确报缺失项 + 重装指引），进程退出码 2——不再出现「补丁打空枪、界面显示采集完成 0 条」
- `except: pass` 从 23 处归零：必需补丁失败抛 `EnginePatchError`，可选补丁发 `warning` 事件并计数
- 引入 `PatchSession`（每运行一个会话）：修掉 `instrument_login_class` 重复包装导致事件翻倍、
  `_WARNED_ONCE` 不随重置清理两处缺陷；登录类契约按 `LOGIN_CLASSES` 自动生成并补齐 ks/wb
- 探测结果缓存：冷启动 2910ms → **0.47ms**

**Sidecar：拆开上帝模块**
- `engine_runner.py` 3187 → **21 行门面**，拆为 `runner/{config,process,guard,login,run,service}.py`
- `RunManager` 改为 `CrawlBackend` 构造注入，不再自己 spawn 进程；新增 `FakeBackend` + 38 条单测（含换号/重试/续采/停止/事件契约）
- `report_data.py` 1555 → **235 行门面**，拆出 `report_dataset / report_package / report_sentiment / report_common`；
  数据包用 TypedDict + `REPORT_PACKAGE_INVALID` 校验
- `exporter.py` 改声明式列规格（`export_specs.py`，14 张表）+ `EXPORT_SCHEMA_MISMATCH` 导出前自检；
  新旧导出器对同一 fixture 的工作簿 **sha256 完全一致**

**前端：不再整块重绘**
- 采集动态 `/feed?since_seq=` 增量（item `seq` + `next_seq`，counts 恒全量）、日志 `/logs?after_line=` 增量（环形缓冲）
- 运行详情改原地补丁：新帖顶部追加、日志底部追加，贴顶/贴底才自动滚动，翻历史不被打断（实测 scrollTop 补偿正确）
- 抽出 `renderer/ui-kit.js`（9 个工具函数，删掉 11 份重复实现）；`preview.js`（112 KB mock）退出主包，正式运行零加载；
  9 个 `setInterval` 统一走 `FS.timers`，切页/终态/destroy 全清

**类型与测试护栏**
- `mypy` 从 **303 → 0 errors / 64 files**（`mypy.ini` + `typing_utils.py`），顺带修 7 个真实缺陷：
  `report_sentiment.py` 的 `Sequence` 未导入、`platform_douyin.py` 坏签名、`api_pool.py` 传 `{}` 时潜在 500 等
- 新增 `app/scripts/e2e/`（CDP，零依赖）：9 项断言覆盖就绪状态、IPC、设置页渲染、数据目录校验、增量参数校验、
  preview 懒加载、ui-kit、timer 泄漏、未捕获 JS 错误；`run_e2e.ps1` 保证 finally 杀干净进程
- CI 新增 e2e job（no-sidecar），offline 列表补 `test_run_manager.py`

**安装包瘦身**
- staging 632.78 → **369.7 MB**，安装包 236.9 → **169.8 MB**（−28%），许可证文件 95 个零改动
- 主要手段：Playwright 自带 node.exe 与 runtime/node 去重（88 MB）、各包 tests / 构建残留 C 源码 / cv2 ffmpeg 后端 /
  jieba 未用模型 / pip+virtualenv+tcl 等；`__pycache__` 刻意保留（25s 首启超时约束）


## 0.5.0 · 2026-11-14（默认账号改用 v2 独立目录 · 移除「迁移旧目录」）

**产品口径：不再有「旧版目录」这个概念出现在界面上**
- 新装/新平台的默认账号改为 **v2 独立目录**（`acc_default_<platform>`，目录
  `browser_data/<platform>/<account_id>/user_data_dir`）：同一平台可并存多个账号、Cookie 互不覆盖，
  开箱即是多账号结构，不需要任何「迁移」动作。
- 移除「迁移旧目录」整套 UI：设置 → 账号池的「旧版登录目录迁移」卡片、账号卡片上的按钮与「· 旧版目录」角标、
  前端的 `adopt` 调用与提示文案一并删除（后端 `POST /accounts/pool/{id}/adopt` 保留，仅作旧数据修理端点，不再有入口）。
- legacy 布局只作为**旧数据兼容**：检测到旧版共用目录非空时仍按 legacy 建行、原样沿用旧 Cookie（老用户不被迫重扫）；
  「从未登录过」的空 legacy 行在启动时原地升级为 v2（不丢任何数据）。

**调度口径随之变化（行为变更）**
- v2 账号未登录 / 冷却中**不会**被调度，平台直接 `failed` 并给出可解释原因（如「无可用账号：账号需要重新登录」）；
  `legacy_fallback`（无视登录态/冷却的旧行为）只对 legacy 账号生效。首次采集前请先在「平台账号」扫码登录。

**测试**
- 账号池用例改写：新增「新装默认账号=v2」「空 legacy 自动升级」「有登录态的 legacy 不动」三组断言；
  adopt 用例改为显式构造 legacy 数据（继续覆盖旧数据修理路径）；老安装升级用 `ServiceCtx(legacy_install=True)` 复现。
- sidecar 自测适配新口径：`read_fake_engine_args` 兼容账号级参数文件名；采集/续采用例在启动前用「测试连接」
  建立登录态并清冷却（不再依赖 legacy 兜底）；`resume` 并发断言区分「真并发 409」与「首次已完成 200」两种情况。
- `selftest.ps1` 全绿（sidecar 200 项、账号池 256 项、更新逻辑 7 项、engine/exporter/Electron 截图）。


## 0.4.0 · 2026-11-13（软件更新 · 数据目录可迁移 · 界面过渡动效）

**软件更新（设置 → 关于 → 软件更新）**
- 「检查更新」对比 GitHub Release 的 tag 与本地 `package.json` 版本（`hasUpdate`），显示当前/最新版本、发布日期、
  发布说明（截断 4000 字）；两个出口：**打开发布页**（系统浏览器）与**下载安装包**（主进程流式下载、进度条、
  校验 PE `MZ` 头后由「运行安装包」启动）。未签名构建同样可用，不依赖 electron-updater / `latest.yml`。
- 新增 `app/desktop/update.js`（纯 Node）：`compareVersions / isNewer / pickInstallerAsset`，语义化比较
  （`1.0.0 > 1.0.0-beta.1`，数值比较）与资产挑选（排除 `.blockmap`/`.zip`/`.yml`，优先 Setup+版本号+x64）。
- GitHub 匿名 API 限流/不可用时自动退化：解析 `releases/latest` 的 302 Location 拿 tag（`source:'redirect'`），
  此时只能引导到发布页；UI 明确提示降级来源。
- 下载域名白名单（`github.com` / `objects.githubusercontent.com` / `*githubusercontent.com`），
  安装包只允许运行下载目录 `<tmp>/OpinionLens-update/` 内的文件。

**数据目录可迁移（设置 → 关于 → 数据存储）**
- 数据目录不再死绑 `%APPDATA%\OpinionLens`：可在应用内选新目录，主进程「停 sidecar → 复制（跳过 `electron/`
  Chromium 缓存与兼容标记）→ 写配置 → 自动重启」；**旧目录保留不删**，确认后再手动清理。
- 位置记录固定写 `%APPDATA%\OpinionLens\user-config.json` 的 `dataDir`（不跟随数据目录，换盘后仍能找到），
  「恢复默认目录」直接删该字段并重启。
- 迁移前校验：绝对路径、与当前目录不互为上下级、目标已有 `app.db` 时拒绝（`DATA_TARGET_OCCUPIED`）、
  写探针校验可写（`DATA_TARGET_NOT_WRITABLE`）；失败不写配置、回滚并恢复 sidecar（`DATA_MIGRATE_FAILED`）。

**版本号与发版**
- 新增 `scripts/bump_version.ps1`：一次同步 `package.json` / `package-lock.json` / `app/sidecar/version.py`
  `FALLBACK_VERSION` / README 徽章 / CHANGELOG 小节骨架；本版本即由它升到 0.4.0。
- 新增 `docs/发布与自动更新.md`：版本号单一来源链路、发版顺序（自测→升版→tag→出包→发 Release）、
  检查更新机制与限流降级、数据目录迁移说明。

**界面与动效**
- 页面切换：`#view` 入场动画（`app.js` 的 `playViewEnter()` 覆盖 offline/页面不存在/渲染成功/渲染失败四条分支，
  强制重排以重放）；设置页 tab 面板切换用 `.panel-enter`，数据刷新重渲染不重播，避免进度刷新闪烁。
- 左侧导航/设置 tab/chip/列表行/按钮补过渡（`styles.css` 新增动效层，只动 `opacity`/`transform`/颜色，
  不碰 `height`/`margin`，无 CLS）；全部包在 `@media (prefers-reduced-motion: no-preference)` 内，
  reduce 时直接呈现最终态。

**修复**
- **未登录平台也吃「日采集次数」配额**：`runs` 原本在尝试开始时 +1，未登录直接失败（`login_invalid`）也会白扣一次。
  现在 `finish_attempt()` 对 `running → login_invalid` 的终态做配额回滚（`_refund_quota`，SQL 层夹到 0、幂等不重复退），
  自动重试/续采照常计数、`risk_blocked` 不退；账号卡片配额 tooltip 补口径说明
  （「未登录直接失败的不计入，自动重试/续采各算一次」），契约 §7.4 同步。
- **「迁移旧目录」看不懂**：账号池里该按钮补 hover 提示，说明文案改为先讲「旧目录是什么（0.3 前每平台一个共用
  浏览器目录，只能存一个账号登录态）」再讲「点它做什么（搬到该账号独立目录、空闲时执行、幂等、可重复点）」。

**验证**
- `node --check` 全部通过；`python -m compileall app` 通过；离线自测 `selftest.ps1` 全绿（新增 node 更新逻辑单测 7 项；
  sidecar 自测的「版本号一致性」断言随版本升到 0.4.0）。
- CDP 端到端（真实 Electron + 真实 IPC）：`getInfo` 新字段、`checkUpdate` 真实请求与降级、`migrateDataDir` 三条校验分支、
  设置-关于两张卡渲染、`panel-enter`/`view-enter` 挂载、点击「检查更新」后的 UI 反馈 —— 10/10 通过；
  迁移往返实测：`app\data` → 临时目录（复制 278 MB、sidecar 在线）→ `resetDataDir` 回默认，配置与数据均正确。


## 0.3.0 · 2026-11-13（版本号统一 · 安装链路加固）

**版本号唯一来源（修复界面/报告/包版本三处不一致）**
- 新增 `app/sidecar/version.py`：`FALLBACK_VERSION = "0.3.0"` + `app_version()`，优先读主进程注入的
  `FS_APP_VERSION`（= `app/desktop/package.json` 的 `version`），缺失时回退。
- `app/sidecar/engine_runner.py`：`SIDECAR_VERSION` 由硬编码 `"0.6.0"` 改为引用 `version.APP_VERSION`
  （`/api/system`、`/health` 回报的版本随之统一）。
- `app/sidecar/report_data.py`：`APP_VERSION` 由硬编码 `"0.3.0"` 改为引用同一模块。
- `app/desktop/package.json` / `package-lock.json`：`0.2.2 → 0.3.0`。
- `app/desktop/renderer/app.js`：静态预览兜底 `FS.APP_VERSION` `0.1.0 → 0.3.0`。
- 自测新增断言「sidecar 回退版本 == package.json version」，防止再次漂移。

**安装链路**
- `scripts/setup_mediacrawler.ps1`：`uv sync` 失败（上游默认清华源对部分 wheel 返回 403）时，
  自动按 `-FallbackIndexes`（阿里云 → PyPI 官方）重试，并输出最终使用的索引；
  全部失败时给出可复制的修复命令。脚本头补充「MediaCrawler 目录固定位置」说明。
- `app/sidecar/selftest.py`：`_craft_interrupted_run()` 对 WAL 库建连增加有限次退避重试，
  消除 `taskkill /F` 后 `-wal/-shm` 未释放导致的 `disk I/O error` 假红（生产逻辑不变）。

**文档**
- `README.md`：快速开始补「MediaCrawler 装在哪、为何不能改名/挪位」、三条实测安装坑与处理方式；
  FAQ 同步；文档索引新增安装文档。
- 新增 `docs/安装与启动_从零开始.md`：从依赖检查到「服务在线」的手把手步骤、验证命令、排查清单。
- `docs/常用命令_构建运行自测.md`：补充 `uv sync` 备用索引用法。

## 0.2.2 · 2026-11-13（界面体验与报告口径修复）

**界面**
- **报告中心日期选择器**：开始/结束日期由浏览器原生 `<input type="date">` 改为 **Ant Design 风格日历浮层**
  （新增 `app/desktop/renderer/datepicker.js`，纯原生 JS、零依赖/零外部请求）：点击日期框弹出日历，支持年/月面板切换、
  上月/下月、「今天」「清空」、键盘操作（方向键/Enter/Esc/PageUp/PageDown）与视口自动翻转；选择后派发 input/change，
  现有表单校验（开始不能晚于结束、必填）保持不变；浮层打开时轮询不会重建表单。
- **采集动态时间显示完整日期**：帖子与评论时间从 `MM-DD HH:mm` 改为 **`YYYY-MM-DD HH:mm`**（本地时区，title 含秒），
  跨年数据不再混淆。
- **采集动态原文链接直接打开**：右下角原文链接由「点击复制」改为 **系统默认浏览器打开**（新增主进程
  `app:openExternal` IPC + preload `openExternal`，仅允许 http/https、长度 ≤2048，日志只记 host 不落完整 URL）；
  「复制摘要」等其它复制功能保留。

**报告中心修复**
- **风险等级与「重点负面事件」口径矛盾**：风险等级按全量（内容帖 + 评论）负面占比判定，而第四章只列负面内容帖；
  当无负面内容帖、评论负面占比高时，旧报告会出现「风险等级高但未识别到负面内容、舆情面平稳」的自相矛盾。
  - `app/report_pipeline/compute_stats.py`：`risk` 新增 `comment_negative_ratio / negative_comments / risk_driver`，
    `basis` 同时给出内容/评论占比与条数；`samples` 新增 `negative_comments` 高赞负面评论样例。
  - `app/report_pipeline/narrator.py`：无负面内容帖但风险中/高、负面评论 > 0 时，第四章导语改为「风险主要由评论区
    负面情绪驱动」，状态说明补充评论区口径；`render_html.py` 风险卡副标题显示「重点负面事件 N 起 · 负面评论 M 条」，
    第四章空表显示评论区负面说明；`stats.schema.json` 同步新增字段。
  - `app/sidecar/local_report.py`：新增 `reconcile_narrative()`，对模型 API 返回的叙述做同一口径纠偏（即使模型写出
    「舆情面平稳」，本地渲染前也会被纠正）并接入 `render_local_report`；`report_data.py` 数据包 samples 同样增加负面
    评论样例，供模型生成叙述时参考。
- **修订语义澄清（AI 只能改文字，不能改 HTML 格式）**：报告详情按来源区分修订入口并更新文案：
  - `source=local-ai`：按钮「修订叙述」，弹窗标题「修订 AI 叙述（只改文字）」，明确「只重写文字叙述后本地重渲染，
    HTML 版式/图表/章节不变，版本号 +1」，示例指令只写文字类诉求；
  - `source=local-template`（无 AI 叙述）：**隐藏修订按钮**，详情提示「本地模板报告没有 AI 文字叙述，无法修订；
    如需 AI 叙述，请先在「设置 → 模型 API」配置并测试连接后重新生成报告」（后端兼容重渲染能力保留）。

**验证**
- `node --check` 全部通过；Electron CDP E2E 覆盖：日期选择器（浮层/年月切换/今天/清空/校验联动）、采集动态（完整时间、
  `open-url`、`fsapi.openExternal` 可用、复制摘要保留）、修订入口三种场景（本地模板隐藏+提示、AI 报告「修订叙述」弹窗）。
- 报告口径单测：合成「风险高 + 0 条负面内容帖 + 12 条负面评论」数据，`narrator._build_negative` / `reconcile_narrative` /
  `render_html` 均输出「风险由评论区驱动」文案，无「舆情面平稳」矛盾；开源版离线自测全绿。

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
