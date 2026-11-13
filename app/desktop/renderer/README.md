# Renderer（Electron 渲染进程 · 采集 + 报告 + 设置）

纯原生 HTML/CSS/JS，无构建、无 CDN、无第三方库。只连接本机 sidecar（`http://127.0.0.1:<随机端口>`）。

## 文件

| 文件 | 说明 |
| --- | --- |
| `index.html` | 应用外壳：左侧四页导航（平台账号/采集任务/报告中心/设置）、顶部状态栏、全局 Toast/弹窗容器、内联 SVG 图标 sprite |
| `styles.css` | 设计变量与全部样式（墨色侧栏 + 温暖纸白 + 铜色强调）；EOF 追加设置页/多账号池/任务账号策略样式块 |
| `api.js` | sidecar REST 客户端（Bearer 鉴权、超时、错误归一化）；含 `FS.Api.llm / notify / pool / appSettings` 扩展命名空间 |
| `preview.js` | 静态预览适配器：无 Electron preload 时使用本地 Mock（含模型 API/通知/多账号池 mock，便于浏览器直开评审） |
| `app.js` | 启动/连接编排、顶部状态栏、路由、Toast/弹窗、服务未就绪引导页、打开文件 IPC |
| `accounts.js` | 平台账号页：平台分组多账号卡片（别名/状态/健康度/配额/默认星标）、新增账号、扫码登录（弹窗二维码 + 1.5s 轮询）、测试连接、启停、设默认、编辑别名、软删除、迁移旧目录、一键全部检测（并发 ≤2）；pool 接口 404 时回退旧版单账号展示 |
| `tasks.js` | 采集任务页：新建表单（关键词/平台/账号策略·自动选号或指定账号）、任务列表、运行详情（进度轮询 + 账号别名/切号记录 + 采集动态内容流 + 技术日志）、停止、续采、导出 Excel |
| `reports.js` | 报告中心：周报表单、生成进度轮询、完成详情/HTML 打开、修订、历史列表、模型 API 未配置/未连接引导 |
| `settings.js` | 设置页（4 tab）：模型 API（provider CRUD/测试连接/设为默认/密钥脱敏）、通知推送（总开关/通道 CRUD/测试发送/事件订阅/静默时段/隐私模式/发送记录重试/worker 状态）、账号池（选号策略/限额/旧目录迁移）、关于（版本/数据目录/MIT/MediaCrawler 免责声明） |

## 运行方式

正常：在 `app/desktop` 执行 `npm start`（Electron 会拉起 sidecar；renderer 通过 `window.fsapi.getInfo()` 获取 baseUrl/token）。
调试 UI：`index.html` 也可直接双击打开，此时无 `window.fsapi`，自动进入**静态预览模式**（Mock 数据、横幅提示，不发任何网络请求）。

截图验证：

```powershell
# 注意：若当前 shell 环境带 ELECTRON_RUN_AS_NODE=1，需要先移除，否则 electron 会当 Node 运行
Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
cd app\desktop

# 正常（真实 sidecar，截图默认页面「平台账号」）
.\node_modules\electron\dist\electron.exe . --screenshot ..\..\docs\preview\app_accounts.png

# 验证「服务未就绪」不白屏
.\node_modules\electron\dist\electron.exe . --screenshot ..\..\docs\preview\app_offline.png --no-sidecar

# 自动点击第一个「扫码登录」并等待真实二维码（依赖 engine 扫码能力）
.\node_modules\electron\dist\electron.exe . --screenshot ..\..\docs\preview\app_qr.png --demo-login
```

## 交互清单

- 平台账号（多账号池）：平台分组 → 每个平台多账号卡片（别名 / 状态 chip / 健康度条 / 最近使用·检测 / 今日配额 / 默认星标）；顶部平台汇总（可用 x/y）与筛选（全部/可用/需登录/异常）；**一键全部检测**使用账号级客户端并发上限 **2**，按钮实时显示「检测中 n/m」，结束后 Toast 汇总「x 可用 / y 需登录 / z 失败」，409 BUSY 计入跳过并显示后端 message；每张卡支持：扫码登录（`POST /api/accounts/pool/{id}/login` + 1.5s 轮询 `.../login/{login_id}`，弹窗内渲染真实 `qr.image` dataURL 二维码）、测试连接、启用/禁用、设为默认、编辑别名、删除（软删二次确认）、迁移旧目录（legacy profile → v2，调 adopt）、新增账号（创建后自动发起扫码登录）。pool 接口 404 时优雅回退旧版单账号展示并提示「多账号接口未启用」。
- **登录人工接管（need_manual）**：登录状态接口返回 `status=need_manual`（可带 `kind=sms|slider|captcha` 与 `message`）时，弹窗内展示橙色「平台需要安全验证」提示区（多账号会附带「账号：<别名>」）：
  - `kind=sms` 强调「请在登录浏览器窗口中输入手机收到的短信验证码」；
  - `kind=slider/captcha` 强调「请在登录浏览器窗口中完成滑块 / 拼图验证」；
  - 统一说明「已打开登录浏览器窗口…完成后 App 会自动继续，无需重复扫码」，并保留「取消登录」；
  - **保持 1.5s 轮询、不自动关闭、不当作失败**；收到 confirmed 后自动刷新账号并关闭，expired/failed 时保留「重新发起登录」。
  - 兼容过渡：若 sidecar 暂时把 need_manual 映射为 `waiting_scan` 但 message 已提示「滑块/安全验证/人工完成」，前端同样展示人工接管提示。
- 采集任务：关键词（中英文逗号/气泡编辑）、平台多选（显示「无可用账号」提示）、任务名称、**账号策略**（自动选号：`{mode:"auto",strategy:"lru"|"best_health"|"round_robin"}`；指定账号：按平台下拉只列已登录可用账号，提交 `{mode:"manual",account_ids:{platform:account_id}}`；接口不可用时回退旧单账号，任务照常创建）；采集量固定产品默认值（每平台每关键词 15 条内容 + 每条内容最多 20 条评论）；开始采集后自动选中新任务并轮询 `/api/runs/{id}`、`/api/runs/{id}/feed`、`/logs?tail=200` 与 `/api/runs/{id}/accounts`（404 忽略）；左栏任务列表吸顶 + 「任务速览」；右侧运行明细三个 tab：**采集动态**、**分平台进度**（每行显示账号别名/尝试次数；存在切号时展示「登录失效切号：账号A → 账号B」记录）、**技术日志**；停止 / 断点续采 / 导出 Excel。
- **断点续采（resume）**：任务非运行中且存在未完成平台（failed / skipped / queued）时：
  - 详情头显示主按钮「继续采集未完成平台（N）」（N=pending 数），点击 `POST /api/runs/{id}/resume`（无 body）续跑全部未完成平台；
  - 每个 failed/skipped 平台行显示「继续此平台」，点击 `POST /api/runs/{id}/resume` body=`{platforms:[...]}` 只续采该平台；
  - 成功后详情/列表刷新为运行中，沿用同一 run_id、原有轮询与日志；BUSY / NO_PENDING_PLATFORMS 等错误 Toast 展示后端 message；续采期间按钮 loading/禁用，完成后按新状态重算 N。
- 全局：sidecar 在线/未就绪/静态预览状态、数据目录、版本、刷新；断线自动重连横幅；所有错误走 Toast/横幅；服务未就绪展示引导页并每 3 秒自动重试。
- **报告中心（周报）**：侧边栏「报告中心」；起止日期默认最近 7 天、关键词可选（留空=全部）、平台多选（默认全选），点「生成周报」→ `POST /api/reports`；生成中每 1.5s 轮询 `GET /api/reports/{id}` 展示阶段/百分比/已用时并禁用按钮；完成后展示数据条数/时间窗口/关键词/平台/版本/生成方式（AI / 本地模板），HTML 卡片可「在浏览器打开」（`fsapi.openPath` 本地 html）或「修订」（弹输入框 POST revise，版本 +1）；失败展示后端 message + 重试；历史列表点击查看（标题为「起~止 · 条数」，旧记录用顶层 window/counts 兜底）。
- **模型 API 连接检测**：报告中心加载时及每 30s 调用 `GET /api/reports/connection`，chip 显示「检测中 / 已连接(provider·model) / 未连接(tooltip 显示 error)」，提供手动刷新；未配置/未连接（或 configured=false）时显示橙色横幅「模型 API 未连接：<error>。生成时将使用本地模板（无 AI 叙述）。」，**生成按钮保持可用**并显示「生成周报（本地模板）」；连接恢复后横幅消失、按钮恢复。`degraded` 且 `meta.source=local-template` 的报告显示「本地模板」标签/说明，历史项同样标记；报告来源判定兼容 `local-ai` / `local-template`（旧数据兼容，不再出现旧 AI 服务文案）。
- **设置页（侧边栏第 4 项）**：竖向分类导航 + 4 个 tab，全部保存动作直连后端，接口 404 时展示「后端暂未实现，请稍后重试」降级横幅（不白屏/不抛未捕获异常）：
  - **模型 API**：provider 列表（名称/kind/Base URL/模型/默认星标/最近测试结果/密钥脱敏 `已配置 ****abcd`）+ 新增/编辑（kind: openai_compatible|deepseek|anthropic；API Key 密码框只写不读，编辑留空=保留）+ 测试连接（显示延迟/回复/错误）+ 设为默认 + 删除；未配置时顶部提示「未配置模型 API：报告将使用本地模板生成（degraded）」。
  - **通知推送**：worker 状态（待发送/发送中/失败/已发送/最近成功）+ 全部通道测试发送；总开关、隐私模式（minimal/standard）即时保存；静默时段（默认 22:00–08:00，可设抑制级别 info/warn/error，error 穿透）；通道列表（类型/名称/启用开关/状态/密钥脱敏）+ 新增/编辑/删除/测试发送（类型：SMTP host/port/security/用户名/授权码/发件人/收件人；企业微信 Webhook；Server酱 SENDKEY；Bark device key + 自建地址/分组/level；通用 Webhook URL）；9 类事件订阅开关；发送记录（最近 20 条，失败/过滤可重试）。
  - **账号池**：默认选号策略（lru/best_health/round_robin）、平台最小间隔（秒）、单账号日 runs / 日条数上限，保存到 `/api/accounts/pool/settings`；旧版登录目录迁移入口（每平台 legacy 账号状态 + 「迁移旧目录」调 adopt）。
  - **关于**：应用/服务版本、服务地址、数据目录/日志目录（可打开）、MIT 开源协议、MediaCrawler 非商业许可免责声明、密钥与隐私说明、复制诊断信息。

## 静态预览的登录演示场景

双击 `index.html`（无 `window.fsapi`）进入静态预览后，登录弹窗会按平台演示不同状态：

| 平台 | 演示状态 |
| --- | --- |
| 小红书 | `need_manual + kind=sms`（短信验证人工接管） |
| 快手 | `expired`（二维码过期） |
| 抖音等其它平台 | `confirmed`（登录成功） |

需要固定演示某一状态时，可用查询参数覆盖（例如 `file:///.../index.html?loginDemo=sms`）：
`loginDemo=sms|slider|confirmed|expired`。

「一键全部检测」在静态预览下也会用 mock 延迟并发返回；可加 `?testDemo=busy|fail|both` 演示 409 BUSY 与单平台失败（`busy`=小红书返回 BUSY，`fail`=知乎返回模拟超时，`both`=两者同时）。

## 断点续采演示（静态预览）

双击 `index.html` → 「采集任务」→ 自动选中最近任务「全量搜索-1」（4 平台已完成、微博失败、贴吧/知乎中断未执行）：
- 详情头显示「继续采集未完成平台（3）」，横幅列出未完成平台，failed/skipped 行有「继续此平台」；
- 点头部按钮 → 续跑全部 3 个平台（参数同 run_id，模拟约 15s 完成）；
- 点单平台「继续此平台」→ 只跑该平台，其他失败/跳过状态不被改变，完成后头部按剩余 pending 重算 N。

## 报告中心演示（静态预览）

双击 `index.html` → 「报告中心」→ 默认最近 7 天、关键词为空、平台全选，点「生成周报」：
- 生成中显示阶段/进度/已用时，按钮禁用；静态 mock 约 6 秒完成；
- 完成后展示条数/窗口/版本/生成方式 + HTML 卡片：「在浏览器打开」（静态预览下提示）、「打开所在文件夹」、「修订」；
- 修订弹窗填写说明后提交，版本 v2 自动重新生成；
- 测试参数：`?reportConfig=missing` 或 `?reportConn=down` 演示未配置/未连接模型 API（本地模板生成）；`?reportConn=recover` 初始未连接，可在控制台执行 `FS.PreviewApi.setReportConnection(true)` 后点连接刷新模拟恢复；`?reportFail=1` 演示生成失败 + 重试；`?reportDegraded=1` 演示本地模板兜底完成。

## 设置页 / 多账号池演示（静态预览）

双击 `index.html` → 侧边栏「设置」（或直接 `index.html#/settings`）：
- 「模型 API」默认展示 2 个 mock provider（DeepSeek 官方默认 / 本地 Ollama 不可达），可新增/编辑/测试连接/设为默认/删除；
- 「通知推送」展示 3 个 mock 通道、SMTP/企微/Server酱/Bark/Webhook 动态表单、9 类事件订阅、静默时段、发送记录与重试；
- 「账号池」展示选号策略与限额、7 个平台的旧目录迁移入口；
- 「关于」展示版本/数据目录/MIT 与 MediaCrawler 免责声明。

多账号页（`index.html#/accounts`）默认展示每平台 1–2 个 mock 账号（含 legacy 账号、风险关注、禁用、需登录、默认星标），可演示扫码登录二维码、启停、设默认、编辑别名、软删、迁移旧目录。

URL 参数演示 404 降级（后端未接线场景）：

| 参数 | 场景 |
| --- | --- |
| `?settingsTab=model|notify|pool|about` | 设置页默认 tab |
| `?settingsDemo=api-missing` | 模型 API 接口 404 → 页面降级横幅 |
| `?settingsDemo=notify-down` | 通知接口 404 → 页面降级横幅 |
| `?settingsDemo=pool-missing` | 账号池接口 404 → 账号页回退旧版单账号展示 |
| `?settingsDemo=empty` | 模型 API 空态（未配置 provider banner） |

## 自测截图（本地生成，不入库）

开发自测截图位于 `_dev/shots/`（由本地脚本生成，已 gitignore）；对外演示截图见仓库根目录 `docs/preview/`。

静态预览状态可通过 URL 参数演示：

| 参数 | 场景 |
| --- | --- |
| `?loginDemo=sms` / `slider` / `expired` / `confirmed` | 登录结果状态（默认：小红书短信验证、快手二维码过期、其余成功） |
| `?reportConfig=missing` / `?reportConn=down` | 报告中心未配置 / 未连接（本地模板生成） |
| `?reportConn=recover` | 初始未连接，可模拟连接恢复 |
| `?reportFail=1` / `?reportDegraded=1` | 生成失败 / 本地模板兜底完成 |
| `?feedDemo=down` / `network` / `empty` | 采集动态演示：接口不可用降级 / 网络失败 / 未采集到内容 |
| `?settingsTab=notify` 等 | 设置页默认 tab（model/notify/pool/about） |
| `?settingsDemo=api-missing` / `notify-down` / `pool-missing` / `empty` | 设置/账号页 404 降级与空态演示 |

## 截图

对外演示截图位于仓库根目录 `docs/preview/`（`07*` 为设计改版前历史图，本次新增）：

| 文件 | 内容 |
| --- | --- |
| `77_settings_model_api.png` | 设置 · 模型 API（provider 列表/密钥脱敏/测试状态） |
| `78_settings_notify.png` | 设置 · 通知推送（开关/通道/事件订阅/静默时段） |
| `79_accounts_pool.png` | 平台账号 · 多账号卡片（健康度/配额/状态/操作） |
| `80_tasks_account_policy.png` | 采集任务 · 账号策略（自动选号/指定账号） |
| `81_settings_account_pool.png` | 设置 · 账号池（策略/限额/旧目录迁移） |
| `82_settings_about.png` | 设置 · 关于（版本/数据目录/MIT/免责声明） |
| `83_accounts_login_qr.png` | 扫码登录弹窗（应用内二维码） |
| `84_accounts_create_modal.png` | 新增账号弹窗 |
| `85_tasks_account_manual.png` | 采集任务 · 指定账号模式 |
| `86_tasks_run_accounts.png` | 运行详情 · 账号策略标签 |
| `87~94_real_*.png` | 真实 sidecar 联调截图（模型 API CRUD、通知规则/通道、账号池 legacy、任务策略、报告中心） |
| `95_tasks_run_platform_accounts.png` | 运行详情 · 分平台进度账号别名 / 切号记录 |

## 已知简化

- MVP 登录方式仅扫码（不提供手机号/短信/Cookie；文案已注明）。
- 人工接管仅负责「提示 + 等待」：真正的滑块/拼图/短信验证码需在 engine 打开的可见浏览器窗口中完成，renderer 不做自动破解；轮询到 confirmed 后自动继续。
- 任务列表/日志仅在页面活跃时轮询；切页会停止轮询，重新进入后自动恢复。
- 静态预览模式的进度/导出为本地模拟，仅用于 UI 评审。
