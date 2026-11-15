# app/sidecar · 桌面端本地后端（FastAPI）

> 契约：`app/CONTRACT.md` §2（API/鉴权/持久化）、§3（exporter）、§1（engine 事件协议）。

## 1. 启动

```powershell
$PY = "<仓库目录>\MediaCrawler\.venv\Scripts\python.exe"
& $PY app\sidecar\main.py --port 0 --token dev-token --data-dir <仓库目录>\app\data
# stdout 只打印一行（Electron 主进程据此拿真实端口）：
# ##SIDECAR_READY##{"port": 51234}
```

- 只监听 `127.0.0.1`；`/health` 免鉴权；`/api/*` 必须带 `Authorization: Bearer <token>`；CORS 全开。
- `--data-dir` 不传会报错；所有路径都会被解析成绝对路径。
- 日志写 `<data-dir>/logs/sidecar.log`（stderr 同时输出；Windows 控制台如为 GBK 会显示乱码，不影响文件与 API）。

## 2. 环境变量（可选，均有默认值）

| 变量 | 默认 | 说明 |
|---|---|---|
| `MC_ROOT` | `<project>/MediaCrawler` | engine 的 cwd 与 MediaCrawler 根目录 |
| `SIDECAR_PYTHON` / `MC_PYTHON` | `<MC_ROOT>/.venv/Scripts/python.exe` | 启动 engine 用的解释器 |
| `SIDECAR_ENGINE_ENTRY` | `app/engine/entry.py` | 覆盖 engine 入口（调试用） |
| `FAKE_ENGINE` | 未设置 | 设为 `1` 时用 `tests/fixtures/_fake_engine.py` 离线假引擎（自测/UI 联调，不联网） |
| `SIDECAR_LOGIN_HEADLESS` | 未设置（=有头） | `1/true` 时登录强制无头（自动化/无桌面环境）；默认登录打开可见 Edge 窗口 |
| `SIDECAR_LOGIN_TIMEOUT` | **420** | login 模式传给 engine 的 `--timeout`（sidecar 另有 +120s 看门狗） |
| `SIDECAR_TEST_TIMEOUT` | 90 | `/test` 同步等待上限（契约 ≤90s） |
| `SIDECAR_CRAWL_TIMEOUT` | 1800 | 单平台采集硬超时（防引擎挂死） |
| `SIDECAR_EXPORTER_MODULE` | `exporter` | 生产勿改；selftest 用 `_fake_exporter` 注入替身 |
| `SIDECAR_DISABLE_OPEN` | 未设置 | `1` 时 `/api/system/open-file` 只校验不真正打开（selftest 用） |

> 模型 API（OpenAI 兼容 / DeepSeek / Anthropic）**不走环境变量**：用户在 App「设置 → 模型 API」中配置，
> API Key 由 Windows DPAPI 加密后存 `<data-dir>/app.db` 的 `llm_providers` 表；未配置时报告自动使用本地模板（degraded）。

## 2.1 登录流程（有头窗口 + 人工接管 need_manual）

```text
POST /api/accounts/{p}/login
  → sidecar 启动 engine --mode login --headless false（打开可见 Edge 窗口，复用 browser_data profile）
  → login.qrcode            → API qr{seq,image,file}，status=waiting_qr
  → login.status waiting_scan
  → 平台二次验证（滑块/拼图/短信码）
       engine 发 login.status need_manual(kind=sms|slider|captcha, message)
       → API status=need_manual，并返回 kind / message / need_manual{kind,message,ts,active,count} / manual_required=true
       → 不当作失败；用户在**弹出的浏览器窗口**里完成验证，App 弹窗保持打开并轮询
       → engine 检测到验证完成会发 waiting_scan「验证已完成，继续登录…」→ API 恢复 waiting_scan
  → confirmed / expired / failed / cancelled（终态）
```

- 取消登录：sidecar `taskkill /F /T` 杀引擎进程树，并按 `<data-dir>/browser_data/<p>_user_data_dir` 命令行特征兜底清理残留 msedge/chrome，浏览器窗口关闭。
- 超时：engine `--timeout 420`（含人工验证等待），sidecar 看门狗 420+120 = 540s；`SIDECAR_LOGIN_TIMEOUT` 可覆盖。
- 已登录 profile 再次登录：engine 直接 `confirmed`（message=已登录，无需重新扫码），不会重复弹码。
- test / crawl 模式保持无头（sidecar 显式传 `--headless true` 给 test；crawl 由 engine 默认无头）。


## 3. 自测（离线，不联网）

```powershell
& $PY tests\python\selftest.py
# 模拟 exporter 未就绪，验证 503 EXPORTER_UNAVAILABLE 降级分支：
& $PY tests\python\selftest.py --simulate-missing-exporter
```

`selftest.py` 会：起 sidecar（`FAKE_ENGINE=1`）→ 阶段 0 单元校验（登录有头/420s/参数映射/need_manual kind 推断）→ **模型 API + 报告中心单元校验（本地 fake LLM：provider 脱敏/情感批量/narrative/降级）** → 鉴权/config/accounts → 扫码登录（confirmed / expired / cancelled / **need_manual(sms) 人工接管→waiting_scan 恢复→confirmed**，以及老 engine 无 kind 的 message 兜底推断）→ test 连接（**不同平台并发/同平台互斥/与 login·run 互斥**）→ 跑一个 xhs 成功 + dy 失败的 run（串行、部分失败继续、run=done）→ **断点续采 resume（模拟 xhs done/dy failed/ks skipped → 只续跑指定/未完成平台、run_id/started_ts 不变、saved_items 基线保留、全 done→done、重复 resume 409、导出包含全部）** → 日志/详情/列表 → 导出（真实 exporter 就绪则集成校验，未就绪走 503；再用 `_fake_exporter` 严格验证导出→excel_path→open-file 链路）→ **报告中心直连 fake 模型（config/capabilities/connection、生成 narrative+本地渲染、v2 修订、模型失败/非法输出→本地模板 degraded）** → 停止/互斥 → 重启后历史与 running→failed 恢复。

## 4. 常用 API 示例

```powershell
$base = "http://127.0.0.1:51234"; $h = @{ Authorization = "Bearer dev-token" }

# 健康检查 / 配置 / 平台账号
Invoke-RestMethod "$base/health"
Invoke-RestMethod "$base/api/config" -Headers $h
Invoke-RestMethod "$base/api/accounts" -Headers $h

# 扫码登录：立刻返回 login_id，1.5s 轮询 GET 拿 qr
$login = Invoke-RestMethod -Method Post "$base/api/accounts/xhs/login" -Headers $h
Invoke-RestMethod "$base/api/accounts/xhs/login/$($login.login_id)" -Headers $h
Invoke-RestMethod -Method Post "$base/api/accounts/xhs/login/$($login.login_id)/cancel" -Headers $h

# 测试连接（同步，≤90s）
Invoke-RestMethod -Method Post "$base/api/accounts/xhs/test" -Headers $h

# 创建采集任务 / 详情 / 日志 / 停止 / 导出 / 预览
$run = Invoke-RestMethod -Method Post "$base/api/runs" -Headers $h -ContentType "application/json" `
  -Body '{"keywords":["示例科技"],"platforms":["xhs","dy"],"max_notes":15,"comments":true,"max_comments":20,"name":"试用"}'
Invoke-RestMethod "$base/api/runs/$($run.run_id)" -Headers $h
Invoke-RestMethod "$base/api/runs/$($run.run_id)/logs?tail=200" -Headers $h
Invoke-RestMethod -Method Post "$base/api/runs/$($run.run_id)/stop" -Headers $h
Invoke-RestMethod -Method Post "$base/api/runs/$($run.run_id)/export" -Headers $h
Invoke-RestMethod "$base/api/runs/$($run.run_id)/data?limit=20" -Headers $h

# 断点续采：默认续跑未完成平台（failed/skipped/queued）；body 可省略；也可显式指定平台
Invoke-RestMethod -Method Post "$base/api/runs/$($run.run_id)/resume" -Headers $h
Invoke-RestMethod -Method Post "$base/api/runs/$($run.run_id)/resume" -Headers $h -ContentType "application/json" -Body '{"platforms":["dy"]}'

# 打开导出文件（只允许 <data-dir>/exports 下的文件）
Invoke-RestMethod -Method Post "$base/api/system/open-file" -Headers $h -ContentType "application/json" `
  -Body '{"path":"<data-dir>\\exports\\xxx.xlsx"}'
```

错误统一为 `{"error":{"code":"...","message":"..."}}`（4xx/5xx）。`/api/accounts/{p}/login/{id}` 返回的 `status` 枚举：
`starting|waiting_qr|waiting_scan|need_manual|confirmed|failed|expired|cancelled`。

`need_manual`（人工接管）为契约外新增状态，`GET /api/accounts/{p}/login/{id}` 额外返回：

```jsonc
{
  "status": "need_manual",
  "kind": "sms",                    // sms | slider | captcha（老 engine 无 kind 时按 message 兜底推断）
  "message": "[kind=sms] 检测到短信验证码：请在弹出的浏览器窗口完成验证…",
  "manual_required": true,
  "need_manual": {"kind": "sms", "message": "...", "ts": 1760000000, "active": true, "count": 1}
}
```

自动化/无桌面环境可用 `SIDECAR_LOGIN_HEADLESS=1` 回退无头（此时平台二次验证无法人工完成，仅适合 `test`/已登录态采集）。

## 5. 目录与文件

```
app/sidecar/
├─ main.py            FastAPI 入口：参数解析 / Bearer 鉴权 / CORS / 统一错误体 / ##SIDECAR_READY##
├─ engine_runner.py   engine 子进程 + ##EVENT## 解析 + 登录状态机 + 采集串行编排 + 并发闸门 + 日志
├─ llm_client.py      用户模型 API：provider CRUD/加密/测试/chat + LocalLLMClient（narrative/情感）
├─ api_providers.py   /api/llm/providers* REST 路由（密钥只写不读）
├─ report_manager.py  报告中心：数据包 → 模型 narrative → 本地 report_pipeline 渲染 + 任务队列/版本
├─ events.py          进程内事件总线（report.* → 通知服务）
├─ state.py           sqlite3(app.db)：runs / run_platforms / account_state / 账号池 / 通知 / 模型配置
├─ selftest.py        离线自测（FAKE_ENGINE=1 / _fake_exporter / _fake_llm_server，不联网）
├─ _fake_engine.py    自测替身：遵守契约 §1 命令行+事件，能写最小 media_crawler.db
├─ _fake_exporter.py  自测替身：遵守契约 §3 签名（真实 exporter 未就绪时也不阻塞自测）
├─ _fake_llm_server.py 自测替身：OpenAI/Anthropic 兼容假模型 API（记录请求/故障模式）
├─ _real_login_check.py  真实网络登录矩阵检查（有头 Edge；需要网络/会打开浏览器窗口）
├─ exporter.py        sqlite → Excel
└─ test_exporter.py   导出器离线自测
```

运行期数据目录：`<data-dir>/{app.db, crawler/media_crawler.db, browser_data/<p>_user_data_dir, exports/, logs/, qr/}`。

## 6. 对接注意点

- **desktop/main.js**：随机 token + `--port 0`；读 stdout 的 `##SIDECAR_READY##{"port":N}` 后先 `GET /health`；`/api/system/info` 可显示版本；退出时 `taskkill /F /T` 杀进程树（sidecar 会连带终止引擎/浏览器子进程）。
- **renderer**：除 `/health` 外所有请求带 `Authorization: Bearer <token>`；登录弹窗 1.5s 轮询 `GET .../login/{login_id}`，`qr.image` 直接塞 `<img src>`；`status=need_manual` 时展示醒目提示（kind=sms 强调输入短信验证码，slider/captcha 强调完成滑块/拼图），不要自动关弹窗/当失败，仍可取消；`status` 恢复 waiting_scan 后继续正常倒计时；登录成功刷新 `/api/accounts`。
- **engine**：sidecar 用 `MC_ROOT` 作为 cwd 并合并 stdout/stderr；只认 `##EVENT##` 前缀行；最后一条必须是 `engine.finished`（失败也发，进程退出码不参与判断）；取消 = 杀进程树，engine 收不到取消事件时以 sidecar 的 stop 状态为准。
- **exporter**：`from exporter import export_run_excel/preview_run`（默认模块名 `exporter`）；export 的 `out_dir` 传 `<data-dir>`，文件必须生成在 `<out_dir>/exports/` 下，`file_path` 必须真实存在，否则 sidecar 返回 `EXPORT_FAILED`。
- **并发语义（按平台解耦）**：
  - `login`：全局同一时间只允许一个（只有一个可见 Edge 登录窗口）；**不同平台的 `test` 不阻塞 `login`**；
  - `test`：按平台并行（同平台串行）；**不同平台 test 可并发，且不阻塞其它平台 login**；
  - 仅同平台互斥：同平台检测中 → login/test 返回 `409 {"error":{"code":"BUSY","message":"该平台正在检测，请稍候"}}`；
    同平台登录中 → test 返回 `409 ... "该平台正在登录，请稍候"`；
  - `run` 仍**全局保守**：run 进行中拒绝 login/test；有任意 login/test 进行中拒绝 run（全局文案「当前有登录/采集任务进行中，请等待结束或先取消」）；
  - `delete_account` 在任意任务/检测期间保持拒绝；
  - `POST /api/accounts/{p}/test` 是**同步 def**（FastAPI 线程池），多平台请求真正并行（实测 3 平台并发 wall ~19s vs 串行和 ~56s）。
  - 用户场景修复：先点 xhs「测试连接」再点 dy「扫码登录」→ dy login 200 正常进入扫码，不再 0s BUSY（真实网络已验证）。

## 7. 真实登录实测记录（2026-11）

在真实网络 + 有头 Edge 环境下，通过 `_real_login_check.py` 对 7 个平台做过多轮扫码登录实测：

```powershell
# 7 平台矩阵（每平台打开一次可见 Edge，拿到 QR/need_manual 后自动取消）
& $PY tests\tools\_real_login_check.py --wait 55

# 单平台/重试验证（微博最多 3 次尝试）
& $PY tests\tools\_real_login_check.py --platforms xhs,wb --wait 60
```

实测结论（多轮复测后）：

- 7/7 平台均能通过 sidecar 拿到 `login.qrcode` 事件，二维码为真实平台登录页二维码；
- 有头登录浏览器主进程无 `--headless`（真实可见 Edge 窗口）；
- 7/7 `cancel` 后该平台 profile 名下 msedge/chrome 进程数为 0（sidecar 先杀引擎进程树，再按 profile 路径兜底清理）；
- `need_manual` 只在强安全页（滑块）或“扫码后二次验证（短信）”场景出现，sidecar 正确透传 kind/message，
  用户到可见浏览器窗口完成后自动继续；
- `SIDECAR_LOGIN_HEADLESS=1` 回退可用（无桌面环境）：仍能拿到二维码，`/test` 在无登录态时返回 `ok:false`；
- 平台页面结构调整、风控策略变化会导致某平台偶发失败/超时，表现为二维码获取超时或需人工验证；
  建议先「清除登录态」后重试，并关注上游 MediaCrawler 更新。

## 8. 断点续采 resume

`POST /api/runs/{run_id}/resume`：程序异常关闭 / 部分平台 failed|skipped 后，继续同一次任务。

- 请求体可选：`{"platforms":["dy","wb"]}`；**body 可整体省略**（不传 / `{}` / 空数组 → 默认续跑该 run 中 `queued|failed|skipped` 平台）。
- 复用同一 `run_id` 与**原始 started_ts**（导出过滤口径不变，断掉前已完成平台的数据仍会进 Excel）；params/keywords 从 DB 读取。
- 选中平台重置 `queued`（message="等待继续采集"，清 finished_at，**保留 saved_items 基线**），run 置 running，起新 worker，平台仍按原顺序串行。
- 进度/条数从基线续增（progress 事件 saved 与基线取 max，不倒退）。
- 结束判定按**全 run 所有平台**：全部 `done` → run=`done`；否则 run=`failed`（error="部分平台失败"），UI 可再次续采。
- 错误：运行中/重复 resume `409 RUN_BUSY`；全部完成 `409 NO_PENDING_PLATFORMS`；平台不在该 run `422 INVALID_PLATFORM`；有 login/run/test 进行中 `409 BUSY`；未知 run `404 RUN_NOT_FOUND`。
- 示例（PowerShell）：

```powershell
# 默认：续跑所有未完成平台
Invoke-RestMethod -Method Post "$base/api/runs/$runId/resume" -Headers $h
# 只续某个失败平台
Invoke-RestMethod -Method Post "$base/api/runs/$runId/resume" -Headers $h -ContentType "application/json" -Body '{"platforms":["dy"]}'
```

- selftest 覆盖：fake engine 模拟 xhs done(saved=10)/dy failed(saved=7)/ks skipped → 显式续采 dy、默认续采 ks、run_id/started_ts 不变、基线保留、日志只重跑选中平台、全 done 后 done、导出包含首批+续采数据、重复/并发 409、未知平台 422。



## 9. 模型 API 与报告直连（开源版）

报告中心不再连接任何固定远端 AI 服务：用户在 App「设置 → 模型 API」配置自己的模型服务，
报告生成时由 sidecar 直连该服务产出叙述 JSON，再由本地 `app/report_pipeline` 渲染 HTML（数字全部由代码统计）。

- 支持类型：`openai_compatible`（OpenAI / Ollama / vLLM / 中转站，`/chat/completions`）、
  `deepseek`（OpenAI 协议，默认 `https://api.deepseek.com` + `deepseek-chat`）、
  `anthropic`（`/v1/messages` + `x-api-key` + `anthropic-version: 2023-06-01`）；
- base_url 归一化：可填 `https://api.deepseek.com`、`.../v1`、`.../v1/chat/completions`、Ollama `http://127.0.0.1:11434/v1`；
- 密钥：只经 `secret_store.protect`（Windows DPAPI）落库；列表/详情只回 `api_key_masked` / `secret_configured`，
  不写日志、不回渲染进程；PATCH 不传 `api_key` = 保留，`clear_secret: true` 清空；
- API：`GET/POST /api/llm/providers`、`PATCH/DELETE /api/llm/providers/{id}`、
  `POST /api/llm/providers/{id}/test`（同步 ping）、`POST /api/llm/providers/{id}/default`；
- 报告流程：`report_data.build_report_package`（情感打标走模型批量结构化输出，失败中性兜底）
  → `LocalLLMClient.generate_narrative`（只输出 narrative JSON，容忍 Markdown 围栏/前后文字，校验必填字段）
  → `local_report.render_local_report`（本地模板渲染 `v{n}.html`，版本历史/修订沿用旧逻辑）；
- 降级：未配置 provider / 模型调用失败 / 输出不合法/过短 → 本地模板报告（`status=degraded`、
  `source=local-template`、`ai_fallback_reason` 写明原因），**不标 failed，不影响采集与导出**；
  已降级报告在模型恢复后再次修订可重新生成 AI 版本；
- 报告事件（进程内 `events.bus`）：`report.finished` / `report.degraded`（`report:{id}:v{version}:...`）、
  `report.failed`（含 no-data）/ `report.cancelled` / 重启中断，通知服务订阅后推送；
- 离线自测：`test_llm.py`（provider CRUD/脱敏/DPAPI roundtrip、OpenAI+Anthropic 协议、错误分类、
  narrative 提取、情感分批、报告 completed/degraded/修订/事件集成），不联网、不需要真实 Key：
  `& $PY tests\python\test_llm.py`。
