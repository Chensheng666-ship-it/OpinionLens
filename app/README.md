# 舆见 OpinionLens（采集 + AI 报告）

产品形态：**扫码登录 → 测试连接 → 关键词采集（内容 + 评论）→ 导出 Excel → 报告中心生成 AI 舆情报告（含情感分析）**。
数据默认不脱敏不打码；AI 报告为可选能力，未配置模型 API 或调用失败时自动使用本地模板生成。

技术形态：Electron 桌面壳 + Python sidecar（FastAPI）+ MediaCrawler 采集引擎（浏览器用系统 Edge，无需额外安装 Chromium）。
AI 报告的**叙述**由用户在「设置 → 模型 API」中配置的模型生成，HTML 在桌面端本地渲染并落盘；报告统计与模板管线位于 `app/report_pipeline/`。

> 环境变量说明：AI 报告使用「设置 → 模型 API」中用户自行配置的模型服务（OpenAI 兼容 / DeepSeek /
> Anthropic），不再依赖内置第三方报告服务；高级调试可用 `FS_REPORT_PIPELINE_DIR` 覆盖管线目录。

## 一、快速启动

前置：本机已有 Chrome/Edge（Windows 自带 Edge 即可）、Node 20+、已按 `scripts/setup_mediacrawler.ps1` 安装采集引擎。

```powershell
cd <仓库目录>
.\app\scripts\dev.ps1
```

Electron 会自动拉起 Python sidecar，窗口右上角显示「服务在线」即就绪（首次启动约 2–4 秒）。

- 若窗口一闪而过：启动脚本会在 App 快速退出/启动失败时**停留并显示原因**，详细日志在 `app\data\logs\main.log`；
  常见原因是**已经有一个 App 实例在运行**（Electron 单实例锁），此时请切到任务栏已有窗口，
  或先结束所有 Electron 进程再重试。
- **单独跑 sidecar（调试，不经过桌面端）**：`.\app\scripts\run_sidecar.ps1`（固定 token=devtoken，便于 curl 调试）。
- **仅看 UI**：`cd app\desktop; npm run start:no-sidecar`。

### 安装包 / 便携版

```text
build\installer\output\OpinionLens-Setup-<version>.exe
build\installer\output\OpinionLens-portable-<version>.zip
```

安装包为 per-user 安装（默认 `%LOCALAPPDATA%\Programs\OpinionLens`，无需管理员），自带便携 Python 运行时；
用户数据与配置在 `%APPDATA%\OpinionLens\`。完整构建/验证/已知限制见
[`docs/打包与安装包_构建说明.md`](../docs/打包与安装包_构建说明.md)。

## 二、使用流程

1. **平台账号**：找到平台（小红书/抖音/快手/B站/微博/贴吧/知乎）→ 点「测试连接」确认状态 → 点「扫码登录」：
   - App 弹窗显示**真实二维码**，同时会打开一个**可见的 Edge 登录窗口**（这是正常的，用于人工完成安全验证）；
   - 手机 App 扫码并在手机上确认；
   - 若平台要求**滑块/拼图/短信验证码**：App 弹窗会变成橙色「平台需要安全验证」并提示到 Edge 窗口完成
     （短信码就输入手机收到的验证码）。完成后 App 自动继续，**无需重复扫码**（重复扫码会重复发短信）；
   - 登录成功后 Edge 窗口自动关闭，账号状态变为「已登录」。
   - 登录态保存在 `app\data\browser_data\<平台>_user_data_dir`，下次启动无需重复扫码（平台侧过期才需重扫）。
2. **批量检测**：平台账号页右上角「一键全部检测」——7 个平台并发检测（客户端并发上限 3，避免同时开 7 个浏览器），
   卡片显示「检测中/排队中」，结束后 Toast 汇总「x 已登录 / y 未登录 / z 失败」。
   - 不同平台的「测试连接」与「扫码登录」**互不阻塞**（各自浏览器 profile）；只有**同一平台**正在检测/登录时才会提示
     「该平台正在检测/登录，请稍候」。
   - 检测走无头引擎，不会弹出浏览器窗口。
3. **采集任务**：填写关键词（多个用英文逗号分隔，例如 `示例科技,示例品牌`）→ 勾选平台 → 开始采集。
   - 采集量使用产品默认值，界面不提供「每平台条数 / 是否抓评论」设置（避免误配置）：
     **每个平台每个关键词采集 15 条内容，并抓取每条内容最多 20 条评论**；如需调整属于高级场景，由研发通过任务参数处理。
   - 运行详情显示各平台进度与「采集动态」：实时展示抓到的帖子（平台/作者/发布时间/标题正文/点赞评论收藏分享）
     与评论内容，数据口径与 Excel 导出一致；技术日志折叠在页面底部，排查问题时展开。
     - 帖子/评论时间显示**完整日期时间**（`YYYY-MM-DD HH:mm`，含年份，跨年数据可区分；悬停显示含秒）；
     - 每条帖子右下角**原文链接点击后用系统默认浏览器打开**（主进程 `shell.openExternal`，仅允许 http/https），不再复制链接；
       「复制摘要」等功能保留。
   - 单平台失败不影响其它平台；可「停止」，失败/跳过的平台可「继续采集」。
4. **断点续采**：采集任务中途异常退出/程序被关后，任务会在重启后标记为「失败」，打开该任务详情：
   - 点「**继续采集未完成平台（N）**」：只续跑 failed / skipped / queued 的平台，已完成的平台数据保留；
   - 或点某个失败/跳过平台行的「**继续此平台**」：只续跑该平台；
   - 续采**复用同一任务（run_id）与原始开始时间**，导出 Excel 会同时包含断掉前已完成的平台 + 续采平台的数据；
     引擎按内容 ID upsert 去重，不会重复入库；
   - 续采前请先修复对应平台的登录态（如微博重新扫码），否则会再次失败。
5. **导出 Excel**：运行结束后点「导出 Excel」→ 生成 `app\data\exports\<关键词>_舆情数据_<时间>.xlsx` →
   可「打开文件」「打开所在文件夹」。
   - Excel 结构：`概览` + 各平台内容表 + 各平台评论表（字段来自 MediaCrawler SQLite，不脱敏：`user_id`/`nickname` 原值）。
6. 换平台/换账号：平台卡片「清除登录态」删除本地 profile 后重新扫码。

### 采集稳定性

- **网络类失败自动重试**：采集时遇到 `Page.goto: net::ERR_CONNECTION_CLOSED`、`RetryError/DataFetchError`、超时等
  **瞬时网络/浏览器错误**，sidecar 会自动重试该平台（默认首次失败后再试 2 次，间隔 5s/10s），日志和平台行会显示
  「网络波动，x 秒后自动重试」；成功后继续，不需要手动点「继续采集」。
  - 未登录、登录失效、扫码/验证码/滑块等**需要人工处理**的错误**不会**自动重试，避免反复弹浏览器。
  - 可用环境变量调整：`SIDECAR_AUTO_RETRY_MAX`（默认 2，0=关闭）、`SIDECAR_AUTO_RETRY_DELAY`（默认 5 秒）。
- **微博「还没有人评论」不再当错误**：微博无评论微博的接口返回 `{"ok":0,"msg":"还没有人评论哦~快来抢沙发！"}`，
  之前会被 MediaCrawler 当 ERROR 并重试 5 次；现在引擎补丁直接按 0 条评论处理，日志不再刷 ERROR，也不会拖慢采集。
- **登录只保留一个浏览器页面**：持久化浏览器上下文自带一个 `about:blank` 初始页，MediaCrawler 随后又会 `new_page()`，
  导致登录时出现两个页面/标签。引擎现在复用初始空白页，只保留登录页；可用 `ENGINE_REUSE_INITIAL_PAGE=0` 关闭（默认 1）。
- **登录/测试启动加速**：登录和测试连接只导入**目标平台** crawler，不再把所有平台（含较重的 bilibili 依赖）全部 import，
  启动约快 2–4 秒；采集仍导入完整 main。可用 `ENGINE_LIGHT_IMPORT=0` 回退。
- 平台登录态位置：代码版 `app\data\browser_data\<平台>_user_data_dir`；
  安装版 `%APPDATA%\OpinionLens\browser_data\<平台>_user_data_dir`。

## 三、目录说明

```text
app/
├─ desktop/            Electron 壳（main.js / preload.js / renderer/）
├─ sidecar/            FastAPI 本地服务（引擎编排、日志、导出、报告中心、通知/模型接入）
├─ engine/             MediaCrawler 包装入口（二维码事件、不脱敏 patch、采集）
├─ report_pipeline/    报告统计与 HTML 模板管线（数据包统计 / 本地模板渲染）
├─ data/               运行期数据（登录态、SQLite、Excel、日志、二维码图片；已 gitignore）
├─ scripts/            启动与自测脚本
├─ CONTRACT.md         开发契约（事件/API/字段）
└─ README.md           本文件
```

## 四、自测（离线，不联网）

```powershell
.\app\scripts\selftest.ps1
```

包含：Engine patch/事件自测、Sidecar API 自测、Excel 导出器自测、报告数据包/本地模板自测、Electron 截图自测。

## 五、已验证能力（真实网络/真实平台）

### 5.1 七平台扫码登录

| 平台 | 二维码 | 用户操作要点 |
|---|---|---|
| 小红书 | ✅ 应用内真实二维码 | 扫码确认；若弹出短信验证，到 Edge 窗口输入手机验证码（App 弹橙色提示） |
| 抖音 | ✅ 应用内真实二维码 | 有头窗口下直接出二维码；若先出滑块，在 Edge 窗口完成滑块 |
| 快手 | ✅ 应用内真实二维码 | 手机扫码确认 |
| B站 | ✅ 应用内真实二维码 | 手机扫码确认 |
| 微博 | ✅ 应用内真实二维码 | 手机扫码确认（**不要**去点登录页里的手机号验证码登录） |
| 贴吧 | ✅ 应用内真实二维码 | 如 App 提示安全验证，在 Edge 窗口完成拼图，自动继续 |
| 知乎 | ✅ 应用内真实二维码 | 手机扫码确认 |

> 二维码均来自真实平台登录页；同一平台若多次失败/触发风控，先「清除登录态」再重试；
> 登录时自动打开可见 Edge 窗口是**正常现象**，仅用于扫码后的安全验证（短信/滑块/拼图），登录成功后自动关闭。

### 5.2 采集与导出

| 验证项 | 结果 |
|---|---|
| 应用内显示真实二维码 | ✅ Electron 窗口内扫码弹窗显示真实二维码，数据链路 UI→sidecar→engine→平台页面 |
| 测试连接 | ✅ `POST /api/accounts/xhs/test` → `{ok:true,message:"已登录",duration_ms:8403}` |
| 真实采集 | ✅ 小红书关键词：内容 20 条 + 评论 27 条，界面显示分平台进度/日志 |
| 导出 Excel | ✅ 概览 + 14 个分平台内容/评论 sheet；导出条数与界面「已采集 N 条」一致 |
| 数据不脱敏 | ✅ Excel/DB 中 `user_id` 为原始 ID、`nickname` 为原昵称（无 `***`） |
| 离线自测 | ✅ engine / sidecar / exporter 多组断言；`.\app\scripts\selftest.ps1` 可复跑 |
| 已登录态重复扫码 | ✅ 已登录时点「扫码登录」返回「已登录，无需重新扫码；如需换号请先清除登录态」，不再误报失败 |
| 人工接管提示 | ✅ 短信/滑块验证时登录弹窗显示橙色「平台需要安全验证」并提示到 Edge 窗口完成 |

### 5.3 已知修复

| 问题 | 根因 | 修复 |
|---|---|---|
| 采集 UI 显示条数与导出 Excel 不一致 | 导出按 `add_ts`（首次入库时间）过滤，重采数据被剔除 | 改为按 `last_modify_ts`（本次更新时间）过滤，缺失时回退 `add_ts`；内容/评论同口径；预览同步修复 |
| 微博登录窗口二维码区空白 | 移动版登录页元素截图为纯白；httpx 直连图片被代理拦截 | 改用桌面 UA + popup 登录 URL；优先 Playwright 元素截图并做 PIL 有效性校验；无效自动重载重试 |
| 快手返回推荐流无关内容 | 首屏游标参数不对，服务端用推荐流补足 | 改为网页真实请求格式，并按关键词/品牌相关词过滤后才入库（`filtered_reason=off_brand` 只标记不删除） |

## 六、AI 报告与模型 API（可选）

AI 报告的**统计**完全由本地完成（`app/report_pipeline` + `report_data`），不依赖外部服务；
只有「AI 叙述」需要外部模型能力。支持 OpenAI 兼容（含 DeepSeek/Ollama/vLLM/中转站）、DeepSeek、Anthropic，
多套配置/默认模型/测试连接均在「设置 → 模型 API」中完成（见根目录 `README.md` 的「设置中心」）。

- **未配置/调用失败时**：自动使用本地模板渲染 HTML（状态 `degraded`），报告不失败、仍可打开；本地模板报告没有 AI 文字叙述，
  UI 不提供修订入口（如需 AI 叙述，先在「设置 → 模型 API」配置并测试连接后重新生成）；
- **AI 叙述产物**：本地渲染 HTML 落盘到 `<data-dir>/reports/<report_id>/v1.html`，版本历史保留；
- **隐私**：仅向用户配置的模型服务发送统计后的数据包（不发送登录态、Cookie、账号凭据与原始用户 ID/昵称等字段）。

## 七、当前范围与已知限制

- 网络抖动（尤其微博/贴吧/快手）已加导航重试；若登录时提示「二维码加载失败（可能是网络/代理拦截）」，
  点弹窗「重新获取二维码」重试一次即可。
- **快手搜索结果相关性**：快手服务端在相关结果不足时会用推荐流补足；引擎现按关键词/品牌相关词过滤后才入库，
  入库条数可能少于 20（服务端强相关视频只有 5–10 条属正常），但**相关率 100%**；
  可用 `ENGINE_KS_RELEVANCE_EXTRA="关键词1,关键词2"` 追加相关词扩大覆盖。
- Excel 新增 `filtered_reason` 列（`off_brand` = 标题/正文/昵称未命中关键词），概览含「相关行数/off_brand 行数」；
  报告统计阶段应排除 `off_brand`。
- **Excel 导出容错**：平台原文若含不可见控制字符（如贴吧某条 `\x18`）会被 openpyxl 拒绝；现在导出器统一清洗
  XML 非法字符（替换空格）、保留换行/emoji、防公式注入、超长文本先清洗再截断，预览接口同样清洗。
- 抖音偶发「登录面板已打开但二维码未渲染」时会超时结束（不再卡 30s），取消后重新登录一次即可。
- 平台支持：小红书/抖音/快手/B站/微博/贴吧/知乎（微信视频号/公众号不在 MediaCrawler 能力内）。
- 报告中心已落地（AI 叙述 + 本地模板兜底 + 版本修订）；通知提醒、多账号号池、模型 API 配置已在
 「设置」页提供；定时任务、预警规则在后续版本规划中。
- 受限网络环境若平台访问受限，需要配置 proxy（后续迭代）。
- MediaCrawler 为非商业学习许可：本工具仅供个人学习研究，**不得用于商业售卖/数据倒卖**；
  详见根目录 `THIRD_PARTY_NOTICES.md`。

## 八、常见问题：启动失败（GPU / 沙箱）

**现象**：运行 `dev.ps1` 后控制台刷 `GPU process launch failed: error_code=18`，随后
`FATAL: GPU process isn't usable. Goodbye.` 直接退出（或窗口白屏）。

**原因**：不是电脑坏了，也不是账号/代码问题。部分受限网络、远程桌面（RDP）、EDR/安全软件环境会拦截
Chromium 子进程（renderer/GPU）的沙箱启动，导致 Electron 在 GPU 进程启动阶段就 FATAL 退出。

**已内置的自动兼容**：
1. Windows 下默认加 `--disable-gpu-sandbox`，避免 GPU 进程先崩；
2. 首次遇到沙箱启动失败时，自动写兼容标记 `app\data\compat-no-sandbox.flag` 并以 `--no-sandbox` 重启一次；
3. 之后每次启动检测到标记，直接以兼容模式启动（`--no-sandbox` + 关闭硬件加速）；
4. `app\scripts\dev.ps1` 默认就带兼容模式（`FS_ELECTRON_COMPAT=1`），双版本 PowerShell（5.1 / 7）均可运行。

**仍然失败时**：
1. 删除 `app\data\compat-no-sandbox.flag`（重置自动检测）后重试；
2. 手动启动一次带完整兼容参数：

   ```powershell
   cd app\desktop
   .\node_modules\electron\dist\electron.exe . --no-sandbox --disable-gpu --disable-gpu-sandbox
   ```

3. 把 `app\data\logs\main.log` 与 `app\data\logs\sidecar.log` 发出来定位。

**恢复严格沙箱**（确认电脑环境支持时）：删除 `app\data\compat-no-sandbox.flag`，并设置 `FS_ELECTRON_SANDBOX=1` 再启动。

## 九、模型 API 配置（AI 报告）

AI 报告是**可选能力**：在「设置 → 模型 API」里配置一个模型服务后，报告会使用模型生成叙述；
未配置或调用失败时自动回退本地模板（`degraded`），不影响采集、导出与其它功能。

- 支持三种协议：`openai_compatible`（OpenAI / DeepSeek / Ollama / vLLM / 各类兼容中转站）、
  `deepseek`（OpenAI 协议，默认 `https://api.deepseek.com`）、`anthropic`（`/v1/messages`）。
- 可配置多个服务并指定默认；模型名、Base URL、API Key 均可改；「测试连接」发一条最小请求验证。
- **密钥安全**：API Key 使用 Windows DPAPI 加密后写入本地 `app.db`，界面与 API 只显示
  `****abcd` 脱敏值；明文只在本机进程内存中出现，不写日志、不进导出/诊断包。
- **数据边界**：报告生成只把统计结果与代表性样本（去重后的少量文本）发给**用户自己配置的模型服务**；
  不发送平台账号、Cookie、本地路径；不包含任何内置的第三方上报地址。
- 换 Windows 用户/换机后 DPAPI 解不开旧密钥，界面会提示「请重新填写」，不会导致崩溃。

### 9.1 离线/降级行为

- 未配置模型：报告直接本地模板（`degraded`）；
- 模型网络失败/超时/4xx/5xx/返回无法解析：本次报告降级本地模板并记录原因，可在设置页看到；
- 情感打标：优先由模型结构化输出，失败时按中性兜底（不阻断报告）；
- 全部统计数字由 `app/report_pipeline/compute_stats.py` 计算，模型只写叙述、不产生数字。

### 9.2 可选环境变量（一般不需要）

| 变量 | 说明 |
|---|---|
| `FS_REPORT_PIPELINE_DIR` | 覆盖报告统计/模板管线目录（默认 `<repo>/app/report_pipeline`，打包版为 `resources/report_pipeline`） |
| `SIDECAR_REPORT_TIMEOUT` | 报告任务总超时秒数（默认 600） |
| `FS_APP_VERSION` | exe 版本号（桌面端注入，用于报告元信息） |

## 十、报告中心

报告中心由 sidecar 编排：本地按时间窗口从采集库生成数据包 → 本地统计（`app/report_pipeline`）→
调用用户配置的模型生成叙述（或本地模板兜底）→ **本地渲染 HTML** → 版本落盘本地。
UI 只调 sidecar API（后台线程生成，不阻塞界面）。

### 10.1 配置（默认零配置）

**开箱即用**：未配置模型 API 时，报告中心自动使用本地模板生成（状态 `degraded`），不阻塞生成；
在「设置 → 模型 API」配置并设为默认后，自动使用模型生成叙述（调用失败时回退本地模板）。

模型配置保存在本地 `app.db` 的 `llm_providers` 表，API Key 使用 Windows DPAPI 加密；
界面与 API 只显示脱敏值。支持多个 provider 并指定默认，配置变更后对新报告立即生效。

本地报告产物：`<data-dir>/reports/<report_id>/meta.json`（状态/版本/错误，重启可读）、`v1.html`、`v2.html`…、
`narrative_v{n}.json`（模型叙述缓存，修订用）。

**日期选择（2026-11-13）**：开始/结束日期改为 **Ant Design 风格日历浮层**（纯原生 JS 实现，不引入第三方库/CDN）：
点击日期框弹出日历，支持年/月面板切换、上月/下月、今天/清空与键盘操作，不再使用浏览器原生 `<input type="date">`。

> **趋势图交互**：报表内「声量趋势」折线/柱状图支持**滚轮缩放、拖动平移、双击重置**；日期跨度 >45 天时默认聚焦
> 最近 30 天，x 轴自动抽稀避免标签重叠，点「显示全部」恢复完整时间范围。旧 HTML 报告不会追溯更新，重新生成即可。

### 10.2 端点（sidecar 本地 API，均需 Bearer token）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/reports` | 生成报告；body 可选 `{window_start?, window_end?, keywords?, platforms?}`；默认最近 7 天、全部平台、关键词取 `media_crawler.db` 中 distinct 非空 `source_keyword`（≤20）；返回 `202 {report_id,status:"queued",...}`。未配置/未连接 AI 服务不再 409：自动走本地模板生成（degraded） |
| GET | `/api/reports` | 报告列表（status/stage/progress/版本/窗口/错误/`html_available`） |
| GET | `/api/reports/{id}` | 详情：`title`、status∈queued/running/completed/degraded/failed、stage、progress、current_version、versions[]、error、`html_available`、`file_path`、`narrative_generated_by`、`ai_fallback_reason`、`meta` |
| GET | `/api/reports/{id}/html?version=` | 读取本地 HTML：`{report_id,version,html,html_sha256,bytes}`（默认最新版） |
| POST | `/api/reports/{id}/revise` | 修订：`{instruction, base_version?}`；**模型 API 只重写文字叙述，HTML 由本地模板重渲染（版式不变）**；`source=local-ai` 报告可用，`source=local-template`（无 AI 叙述）UI 不提供修订入口；返回 `202` |
| POST | `/api/reports/{id}/open` | 返回本地 `file_path` 供 UI `shell.openPath` |
| GET | `/api/reports/config` | `{configured, provider_id, provider_name, kind, model, base_url(脱敏), token_configured, degraded_fallback, report_data_ready, schema_version, ...}`（不返回 key） |
| GET | `/api/reports/capabilities` | 设置页「测试连接」：测试默认模型 provider，始终 200 + `{ok,error?,model,latency_ms}` |
| GET | `/api/reports/connection` | **自动连接检测**：短超时测试默认 provider，返回 `{ok,configured,connected,provider_name,model,latency_ms,error,local_fallback:true}`；未配置/网络失败也 200，UI 可自动轮询/横幅提示 |

### 10.3 生成流程与状态

`queued → building（report_data 数据包+情感分析）→ generating（模型生成叙述）→ rendering（本地渲染 HTML）→
completed`；模型失败回退本地模板时状态 `degraded`（HTML 仍可用）；
失败写 `error{code,message}`（如 `REPORT_NO_DATA`、`MODEL_TIMEOUT`、`REPORT_DATA_UNAVAILABLE`）。

- 情感分析：`report_data` 收集窗口内文本去重 ≤50 条/批调用模型结构化打标；结果缓存到 `<data-dir>/app.db` 的
  `sentiment_cache` 表（命中不重复请求）；未配置/失败按 `neutral` 兜底并在数据包 `stats.warnings` 记录（不阻断生成）。
- 默认关键词：不传 `keywords` 时取 `media_crawler.db` 各内容表 `source_keyword` 的去重非空集合（≤20）。
  同一内容的 `source_keyword` 只保留一个，把所有出现过的词都带上才能统计全量；库为空/读不到时按全部数据（`[]`）统计。
- 同一时间只跑一个报告任务（本地队列串行），列表/详情轮询为轻量内存读取；生成期间 UI 可正常采集/导出。
- sidecar 重启时未完成的报告标记 `failed(REPORT_INTERRUPTED)`，已生成的 HTML 版本仍可查看。
- **本地模板兜底**：
  - 未配置模型 → 直接用**本地模板**渲染 v1（`narrative_generated_by="local-template"`、
    `source="local-template"`、状态 `degraded`），HTML/file_path/历史 meta 正常；
  - 已配置但模型调用失败（网络/HTTP/超时/返回无法解析）同样回退本地模板，**不标 failed**（`ai_fallback_reason` 写明原因）；
  - **修订语义（2026-11-13）**：模型 API 只产出/修订**文字叙述**，HTML 版式/图表/章节由本地模板固定渲染，AI 无法修改 HTML 格式。
    `source=local-ai` 报告按钮为「修订叙述」，弹窗说明「只重写文字后本地重渲染，版式不变、版本号 +1」；
    `source=local-template` 报告（无 AI 叙述）UI **隐藏修订入口**，提示先在「设置 → 模型 API」配置并测试连接后重新生成；
    旧版本/API 的同数据重渲染能力保留用于兼容。
  - **风险等级与「重点负面事件」口径（2026-11-13 修复）**：风险等级按全量（内容帖 + 评论）负面占比判定，而第四章表格只列
    负面内容帖；当本期无负面内容帖、但评论负面占比高时，报告会说明「风险由评论区负面情绪驱动」（风险卡副标题显示
    `重点负面事件 N 起 · 负面评论 M 条`，第四章空表给出评论负面说明），模型叙述也会在本地渲染前做口径纠偏，
    不再出现「风险等级高但未识别到负面内容、舆情面平稳」的矛盾。
- 依赖：`app/sidecar/report_data.py`（DB→统计→数据包+情感缓存）、`app/report_pipeline/`（统计与模板）与 exporter；
  未就绪时报告生成写接口返回 `503 REPORT_DATA_UNAVAILABLE`（不影响 sidecar 启动与其它功能）。

> ⚠️ 进程管道注意：exe 主进程用 `spawn` 启动 sidecar 时，若把 `stdout/stderr` 接成管道（PIPE），必须**持续排空**
> （后台线程逐行读取，或重定向到 DEVNULL/文件）。否则 sidecar 后台日志写满 64KB 管道会阻塞 worker，
> 表现为报告状态冻结在 `running` 直到超时。sidecar 已把 `httpx/httpcore` 日志压到 WARNING，并同时写
> `<data-dir>/logs/sidecar.log`，但 exe 侧仍应排空或重定向。
