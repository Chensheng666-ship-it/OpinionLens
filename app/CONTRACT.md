# 舆见 OpinionLens · 开发契约（采集闭环 → 报告中心）

> 目标：Electron 桌面应用（采集闭环 → 报告中心），核心闭环：扫码登录 → 测试连接 → 按关键词采集内容+评论 → 导出 Excel → AI 舆情报告。
> 数据口径：数据默认不脱敏不打码（导出保留原始昵称/ID，由使用者自行合规处理）。
> 已验证事实：真实平台扫码二维码可在应用内展示；Playwright 用系统 Edge（`channel="msedge"`）可运行、免装 Chromium。
> 目录约定：`MediaCrawler/` 只读，不得修改其源码；所有适配写在 `app/engine/patches.py` 运行时 patch。

---

## 0. 目录与运行环境

```text
app/
├─ CONTRACT.md            # 本文件
├─ engine/                #   MediaCrawler 包装入口（唯一允许触碰 MediaCrawler 配置/patch 的地方）
│  ├─ entry.py            #   主入口：--mode login|test|crawl
│  ├─ patches.py          #   show_qrcode / channel / 配置 / 事件 全部 patch
│  └─ _probe_xhs_login.py #   已验证的探针（可参考，不用于生产）
├─ sidecar/               #   FastAPI 本地服务（桌面端唯一后端）
│  ├─ main.py             #   入口：--port 0 --token xxx --data-dir <abs>
│  ├─ engine_runner.py    #   启动/解析 engine 事件、登录会话、采集编排
│  ├─ exporter.py         #   sqlite → Excel
│  ├─ selftest.py         #   离线自测（不联网）：API + 事件解析 + 导出
│  └─ state.py            #   app.db 持久化（sqlite3 标准库）
├─ desktop/               #   Electron 壳（main/preload + renderer）
│  ├─ main.js             #   Electron 主进程：spawn sidecar、随机端口/token、健康检查、窗口、IPC
│  ├─ preload.js          #   window.fsapi = { getInfo, openPath, showItemInFolder }
│  ├─ package.json        #   已含 electron 45.2.3（不要改依赖版本）
│  └─ renderer/           #   index.html + app.js + styles.css（纯原生，无构建、无 CDN）
├─ data/                  # 运行期数据（gitignore）
│  ├─ browser_data/{platform}_user_data_dir/  # 登录态（Edge profile）
│  ├─ crawler/media_crawler.db               # MediaCrawler sqlite
│  ├─ exports/                               # Excel
│  ├─ logs/                                  # 引擎/sidecar 原始日志
│  └─ qr/                                    # 抓到的二维码 PNG（调试）
└─ scripts/
   ├─ dev.ps1             # 一键启动（sidecar 由 Electron 自动拉起）
   ├─ run_sidecar.ps1     # 单独启动 sidecar（调试）
   └─ selftest.ps1        # 全量离线自测
```

**Python 解释器**（开发期统一用这个，已装 fastapi/uvicorn/playwright/openpyxl/pandas）：

```text
<仓库目录>\MediaCrawler\.venv\Scripts\python.exe
```

**MediaCrawler 根目录**：`<project>/MediaCrawler`（默认解析为 `app/engine/../../MediaCrawler`，可用环境变量 `MC_ROOT` 覆盖）。
**PATH 约定**：sidecar / engine 收到的所有路径参数均为 **绝对路径**。

---

## 1. Engine 契约（engine/entry.py）

### 1.1 命令行

```text
python engine/entry.py --mode login --platform xhs              --data-dir <abs> [--account-id <id>] [--profile-template <abs 模板>] [--headless true|false] [--timeout 420]
python engine/entry.py --mode test  --platform xhs              --data-dir <abs> [--account-id <id>] [--profile-template <abs 模板>]
python engine/entry.py --mode crawl --platform xhs --keywords "示例科技" --max-notes 15 --comments true --max-comments 20 --data-dir <abs> [--account-id <id>]
```

- `--platform`: `xhs|dy|ks|bili|wb|tieba|zhihu`
- `--account-id`: 多账号号池账号 ID（`[A-Za-z0-9_-]{1,40}`，可空=旧单账号行为）；事件 `engine.started` 带 `account_id`
- `--profile-template`: 浏览器 profile 目录**模板**（绝对路径，必须恰好包含一个 `%s` 作为平台占位；
  例如 `<data>/browser_data/%s/acc_xxx/user_data_dir`）；缺省时使用旧模板 `<data>/browser_data/%s_user_data_dir`
- `--keywords`: 英文/中文逗号分隔多个关键词（同一平台内逐个搜索）
- `--max-notes`: 每关键词条数（映射 `config.CRAWLER_MAX_NOTES_COUNT`）
- `--comments/--max-comments`: 是否抓评论、每条评论上限
- `--headless`: login 默认 `false`（**打开可见 Edge 窗口**，便于用户完成扫码后的滑块/拼图/短信验证；`ENGINE_LOGIN_HEADLESS=1` 可强制无头）；test/crawl 默认 `true`。UI 需提示用户「如出现验证请到浏览器窗口完成」。
- `--timeout`: login 模式最大等待秒数，默认 300
- 环境变量：`MC_ROOT`（覆盖 MediaCrawler 根）、`BROWSER_CHANNEL`（默认 `msedge`，可选 `chrome`）

### 1.2 输出协议

所有行输出到 **stdout/stderr**；结构化事件为单独一行：

```text
##EVENT##{"type":"...", ...}
```

事件类型（**必须全部实现**，字段名不可改）：

```jsonc
{"type":"engine.started","mode":"login","platform":"xhs","pid":1234,"ts":1700000000}
{"type":"login.qrcode","platform":"xhs","seq":1,"image":"data:image/png;base64,....","file":"<abs png path>","bytes":4490,"ts":...}
{"type":"login.status","platform":"xhs","status":"waiting_qr|waiting_scan|need_manual|confirmed|expired|failed","kind":"sms|slider|captcha|","message":"","ts":...}
{"type":"test.result","platform":"xhs","ok":true,"message":"已登录","duration_ms":812,"ts":...}
{"type":"progress","platform":"xhs","stage":"boot|login|search|detail|comments|done","page":1,"saved":7,"message":"","ts":...}
{"type":"engine.finished","ok":true,"mode":"crawl","platform":"xhs","elapsed_ms":123456,"db_path":"<abs>","message":"","ts":...}
```

- **最后一行必须是 `engine.finished`**（成功=ok:true；失败/超时/取消=ok:false + message；失败时进程退出码可为 0，sidecar 以事件为准）。
- 原始 MediaCrawler 日志也照常输出（sidecar 会落盘、UI 只展示尾部）。
- `login` 模式：完成扫码并检测登录成功后 **不进入采集**，直接 `engine.finished ok:true`。
- `test` 模式：只做登录态探测（见 1.3），不启动登录流程、不搜索。
- `crawl` 模式：使用已保存登录态；未登录时 `engine.finished ok:false, message:"未登录"`（不拉起扫码）。

### 1.3 必须实现的 patch（写在 patches.py，入口统一调用）

1. **真实数据不脱敏**：在 import MediaCrawler 业务模块之前，把 `tools.user_hash.anonymize_user_id` / `mask_nickname` 替换为恒等函数（raw user_id → 存入 `creator_hash` 列、raw nickname）。导出时列名改回 `user_id`。**注意导入顺序**：先 `import tools.utils`，再 patch，再 `import main`（否则 `proxy` 循环导入报错，探针已踩过）。
2. **二维码截获**：替换 `tools.utils.show_qrcode`（实现位于 `tools/crawler_util.py:88`）→ 保存 `<data-dir>/qr/<platform>_<sha8>.png` + 发 `login.qrcode` 事件（同图 sha 去重，`seq` 递增）。
3. **浏览器 channel**：patch `playwright.async_api.BrowserType.launch` / `launch_persistent_context`，`channel` 默认取 `BROWSER_CHANNEL`（默认 `msedge`），避免依赖 Playwright Chromium 下载。**user_data_dir 交给 MediaCrawler 的 `config.USER_DATA_DIR` 绝对路径模板**。
4. **配置注入**（在调用 `crawler.start()` 前）：
   ```python
   config.PLATFORM = platform
   config.LOGIN_TYPE = "qrcode"
   config.HEADLESS = headless
   config.SAVE_LOGIN_STATE = True
   config.USER_DATA_DIR = str(data_dir / "browser_data" / "%s_user_data_dir")
   config.SAVE_DATA_OPTION = "sqlite"
   config.ENABLE_GET_COMMENTS = comments
   config.CRAWLER_MAX_NOTES_COUNT = max_notes
   config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES = max_comments
   config.sqlite_db_config["db_path"] = str(data_dir / "crawler" / "media_crawler.db")
   config.SQLITE_DB_PATH = 同上
   ```
   `cwd` 必须是 MediaCrawler 根目录（相对路径 `libs/stealth.min.js` 等依赖 cwd）。
5. **登录成功即停**：login 模式把实例的 `search` 方法替换为抛 `_LoginOnlyDone`；捕获后发 `login.status confirmed` + `engine.finished ok:true`。
6. **进度事件**：给 `logging.getLogger("MediaCrawler")` 挂 Handler，匹配关键日志转换：
   - `[XxxCrawler.search] ... Current search keyword: (.*)` → progress search
   - `search <platform> keyword: ..., page: (\d+)` → progress page
   - `[store.<p>.update_...]` 每行 → saved += 1 并每 5 条发一次 progress
   - `[get_note_detail_async_task]` / `[XxxCrawler.get_comments]` → progress stage
7. **test 模式**：实例化 `CrawlerFactory.create_crawler(platform)`，不调用 `start()`；用 `async_playwright()` + `launch_browser()` + `create_*_client()` + `client.pong()`，发 `test.result`，关闭 context。超时 60s。
8. **Windows 稳定性**：`asyncio.run(...)` 外层 `asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())`（如遇 Playwright 子进程问题）；捕获 `KeyboardInterrupt`/SIGTERM 发 `engine.finished ok:false message:"cancelled"`。

### 1.4 验收（engine）

- `--mode login --platform xhs`：真实网络下 30s 内出现 `login.qrcode` 事件且 `<data-dir>/qr/*.png` 为有效二维码；无人扫码时按 `--timeout` 超时输出 `engine.finished ok:false`（不崩溃）。
- `--mode test --platform xhs`：无登录态时 `test.result ok:false`；有登录态时 ok:true（用户扫码后可测）。
- `--mode crawl ...`：无登录态时立即 `engine.finished ok:false, message:"未登录"`。
- 自测脚本 `engine/test_engine.py`（不联网）：验证事件前缀解析、patch 生效（identity 函数、show_qrcode 替换）、参数映射、`engine.finished` 必出。

---

## 2. Sidecar 契约（sidecar/main.py）

### 2.1 启动

```text
python app/sidecar/main.py --port 0 --token <random> --data-dir <abs>
# 启动成功后 stdout 打印唯一一行：
##SIDECAR_READY##{"port": 51234}
```

- 仅监听 `127.0.0.1`；`/api/*` 全部要求 `Authorization: Bearer <token>`；`/health` 免鉴权。
- 开启 CORS（`allow_origins=["*"]`，Electron renderer 为 file:// Origin）。
- sidecar 内部通过 `subprocess.Popen` 调用 engine（见 §1），cwd=MediaCrawler 根，`PYTHONUNBUFFERED=1`，把 stdout/stderr 合并逐行读取：`##EVENT##` 行解析为事件，其余行写入 `<data-dir>/logs/<scope>.log` 并保留内存尾部 5000 行。
- **并发语义（按平台解耦）**：
  - `login`：全局同一时间只允许一个（同一时间只有一个可见 Edge 登录窗口）；**不同平台的 test 不阻塞 login**；
    仅当同一平台正在 test 时返回 409（`该平台正在检测，请稍候`）；有 run 进行中时返回全局 BUSY。
  - `test`：按平台并行（同平台串行）；**不同平台 test 可并发，且不阻塞 login**；仅同平台已有 login/test 或任意 run 进行中时返回 409。
  - `run`：全局互斥（同一时间一个采集任务）；有 login/run/test 进行中时返回全局 BUSY。
  - 目的：不同平台使用各自 `browser_data/<platform>_user_data_dir`，互不冲突；同一平台必须串行，避免 profile 锁竞争。
  - BUSY 文案矩阵（`409 {"error":{"code":"BUSY","message":...}}`）：
    - 同平台检测中（login/test 撞到该平台正在 test）：`该平台正在检测，请稍候`
    - 同平台登录中（test 撞到该平台正在 login）：`该平台正在登录，请稍候`
    - 其余（run 冲突 / 已有其它登录或采集）：`当前有登录/采集任务进行中，请等待结束或先取消`

### 2.2 REST API（字段名固定）

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/health` | — | `{ok:true}` |
| GET | `/api/config` | — | `{data_dir, mc_root, python, platform_labels:{xhs:"小红书",...}}` |
| GET | `/api/accounts` | — | `{accounts:[{platform, label, logged_in:true|false|null, last_checked_at, profile_exists, message}]}` |
| POST | `/api/accounts/{platform}/login` | — | `{login_id, status:"starting"}` 立刻返回 |
| GET | `/api/accounts/{platform}/login/{login_id}` | — | `{login_id, platform, status:"starting|waiting_qr|waiting_scan|need_manual|confirmed|failed|expired|cancelled", qr:{seq,image,file}|null, kind:"sms|slider|captcha|", manual_required:bool, need_manual:{kind,message,ts,active,count}|null, message, started_at}` |
| POST | `/api/accounts/{platform}/login/{login_id}/cancel` | — | `{ok}` |
| POST | `/api/accounts/{platform}/test` | — | 同步（≤90s）`{ok, message, duration_ms}`（内部走 engine test 模式）。**不同平台可并发检测**（同平台串行）；同平台检测/登录中或任意 run 进行中时返回 409 BUSY，message 区分"该平台正在检测/登录"与全局任务 |
| DELETE | `/api/accounts/{platform}` | — | `{ok}` 删除该平台 profile（重新登录用） |
| POST | `/api/runs` | `{keywords:[str], platforms:[str], max_notes:int, comments:bool, max_comments:int, name?:str}` | `{run_id, status:"running"}` |
| GET | `/api/runs` | — | `{runs:[run摘要]}` |
| GET | `/api/runs/{run_id}` | — | `{run_id,status:"running|done|failed|cancelled",created_at,finished_at,params,platforms:[{platform,label,status:"queued|running|done|failed|skipped",progress:int,stage,saved_items:int,message,started_at,finished_at}],excel_path:str|null}` |
| GET | `/api/runs/{run_id}/logs?tail=200` | — | `{lines:[{ts,level,text}]}` |
| POST | `/api/runs/{run_id}/resume` | 可选 `{platforms?:[str]}`，**body 可整体省略**（不传/`{}`/空数组 → 默认续跑未完成平台） | `{run_id, status:"running", resumed_platforms:[str]}`；`409 RUN_BUSY`（运行中/已有任务）、`409 NO_PENDING_PLATFORMS`（全部已完成且未显式指定）、`422 INVALID_PLATFORM`（平台不在该 run）、`409 BUSY`（有 login/run/test 进行中）、`404 RUN_NOT_FOUND` |
| POST | `/api/runs/{run_id}/stop` | — | `{ok}` |
| POST | `/api/runs/{run_id}/export` | — | `{ok, file_path, sheets:[{name,rows}], total_rows}`（调用 exporter） |
| GET | `/api/runs/{run_id}/data?limit=20` | — | `{sheets:[{name,columns:[str],rows:[[...]]}]}` 预览（调用 exporter.preview） |
| GET | `/api/runs/{run_id}/feed?limit=30&comments=3` | — | `{run_id,status,keywords,counts:{content,comments,by_platform:{xhs:{content,comments}}},items:[{platform,platform_label,kind:"content",id,title,text,author,url,published_at,collected_at,metrics:{likes,comments,collects,shares,plays},comment_total,comments:[{id,author,text,likes,published_at}]}],truncated:bool}` 采集动态内容流（口径同 exporter；表/列缺失容错；DB 不存在 → 空流；`404 RUN_NOT_FOUND`） |
| POST | `/api/system/open-file` | `{path:str}` | `{ok}`（仅允许 `<data-dir>/exports` 下文件） |
| GET | `/api/system/info` | — | `{version, data_dir, logs_dir}` |

- 采集编排：按 `platforms` 顺序串行，每个平台一次 engine `crawl` 子进程；单平台失败标记 `failed` 继续下一个；首次 run 全部结束后 run=`done`（有失败也 done，详情在平台项里；**续采任务按下方 resume 规则判定**）。
- **断点续采 resume**：
  - 复用**同一 run_id 与原始 started_ts**（保证导出过滤口径仍包含断掉前已完成平台的数据）；params/keywords 从 DB 读取；
  - 默认续跑该 run 中 status ∈ `queued|failed|skipped` 的平台；显式 `platforms` 必须是该 run 创建时的平台并去重（未知平台 422）；
  - 选中平台重置为 `queued`（message="等待继续采集"，清 finished_at，**保留 saved_items 作为基线**），run 置 `running`、清 finished_at/error，起新 worker（平台顺序沿用原 run 顺序）；
  - 续采期间独占 run 槽；重复/并发 resume → 409 `RUN_BUSY`；有 login/run/test 进行中 → 409 `BUSY`；
  - 续采结束按**全 run 所有平台**判定：全部 `done` → run=`done`，否则 run=`failed`（error="部分平台失败"，便于再次续采）；
  - 进度/条数从基线续增（progress 事件 saved 与基线取 max，不倒退）；新任务基线为 0、行为不变。
- 关键词含多个时传给 engine `--keywords`，Excel 按 `source_keyword` 归属。
- **采集动态内容流 `GET /api/runs/{run_id}/feed`（sidecar/run_feed.py）**：
  - 用途：给桌面端「采集动态」提供真实帖子/评论内容（替代纯技术日志）；Bearer 鉴权同其它 `/api/*`。
  - 参数：`limit` 内容条数默认 30（clamp 1–100）；`comments` 每条内容附带评论数默认 3（clamp 0–10，0=不附带明细）。
  - 数据口径（与 `exporter._collect` 一致）：只返回**本 run** 数据——内容表 `last_modify_ts >= run.started_ts`（毫秒归一；缺失/0 回退 `add_ts`）且 `source_keyword ∈ run.keywords`；评论表同时间口径且父内容在本 run 内容集合内。覆盖 xhs/dy/ks/bili/wb/tieba/zhihu。
  - 排序：`items` 按 `last_modify_ts` 倒序（最新采集在前）；每条 `comments` 也按 `last_modify_ts` 倒序。
  - 字段语义：`text` 正文摘要 ≤200 字、评论 `text` ≤120 字（清洗控制字符）；`published_at`/`collected_at` 为 `YYYY-MM-DD HH:MM`；`url` 仅 http(s)；`metrics` 5 键固定（likes/comments/collects/shares/plays，计数列 TEXT→int，平台无该指标为 `null`）；`comment_total` 为该内容在本 run 的评论总数（不受 `comments` 限制）；`counts.content/comments` 为 run 全量计数（可大于本页 `items` 数），`counts.by_platform` 只列有数据的平台；`truncated` 表示本页 `items` 是否被 `limit` 截断。
  - 容错：表/列缺失、脏数据（时间/计数无法解析）逐行容错并跳过；DB 文件不存在 → 空 `items` + `counts` 全 0（不报 500）；未知 run → 404 `RUN_NOT_FOUND`；响应无 NaN，可直接 JSON 序列化。
  - **口径差异提示**：`counts.content` 是 **MediaCrawler DB 过滤后**本 run 新增/更新的内容行数（与 `exporter._collect` 同口径）；`GET /api/runs/{run_id}` 的 `platforms[].saved_items` 是 **engine 进度事件上报的 saved 数**（平台侧命中/重复更新/评论等都算过），两者统计对象与去重口径不同，**不应直接对比**；UI 同时展示时建议分别标注（如「内容流 N 条」与「引擎已采集 M 条」）。
- 登录成功后可简单把 `GET /api/accounts` 的 `logged_in` 置 true（以 login 事件 confirmed 为准，并记录 `last_checked_at`）。
- 错误统一：`{"error":{"code":"...","message":"..."}}`，HTTP 4xx/5xx。

### 2.3 持久化

`<data-dir>/app.db`（sqlite3 标准库）：

- 采集/登录：`runs`, `run_platforms`, `account_state`（旧表保留兼容）；
- 多账号号池：`accounts`, `account_quota`, `run_accounts`；`runs.account_policy_json` 记录任务账号策略，
  `run_platforms.account_id/account_attempts` 记录最终生效账号与尝试次数；
- 通知推送：`notify_channels`, `notify_rules`, `notify_outbox`, `notify_log`；
- 模型 API：`llm_providers`（API Key 存 DPAPI 密文 `secret_enc`）；
- 通用设置：`app_settings`（键值 JSON）。

sidecar 重启后 `/api/runs` 仍能列出历史（running 的置为 failed/interrupted）；旧库自动迁移（只加表/列）。

---

## 3. Exporter 契约（sidecar/exporter.py）

```python
def export_run_excel(db_path: str, out_dir: str, run_id: str, keywords: list[str],
                     started_ts: int) -> dict: ...
# -> {"file_path": str, "sheets": [{"name": str, "rows": int}], "total_rows": int}

def preview_run(db_path: str, keywords: list[str], started_ts: int, limit: int = 20) -> dict: ...
# -> {"sheets": [{"name": str, "columns": [str], "rows": [[...]]}]}
```

- 表 → sheet 映射（顺序固定）：`小红书=xhs_note`、`小红书评论=xhs_note_comment`、`抖音=douyin_aweme`、`抖音评论=douyin_aweme_comment`、`快手=kuaishou_video`、`快手评论=kuaishou_video_comment`、`B站=bilibili_video`、`B站评论=bilibili_video_comment`、`微博=weibo_note`、`微博评论=weibo_note_comment`、`贴吧=tieba_note`、`贴吧评论=tieba_comment`、`知乎=zhihu_content`、`知乎评论=zhihu_comment`（表不存在时跳过该 sheet，不报错）。
- 过滤：内容表 `source_keyword ∈ keywords` 且 `add_ts >= started_ts`；评论表先取父内容 ID 集合（同关键词+时间过滤），再 `note_id/aweme_id/video_id/content_id ∈ 父集合` 且 `add_ts >= started_ts`。
- 列规则：
  - `creator_hash` 列改名 `user_id`（现在是 raw ID，不脱敏）；
  - 时间字段（add_ts/last_modify_ts/time/create_time/created_time/updated_time/publish_time/last_update_time）统一格式化为 `YYYY-MM-DD HH:MM:SS`（兼容秒/毫秒时间戳、字符串、Excel 序列号 → 简单处理即可）；
  - 所有 ID 列按文本写入（`note_id/aweme_id/video_id/content_id/comment_id/...`，避免科学计数法；表头后设置单元格为文本格式）；
  - 其余列原样输出（列名保持数据库字段名）。
- 首表 `概览`：run_id、关键词、采集开始时间、导出时间、各 sheet 行数、合计。
- 文件名：`<关键字首个>_舆情数据_<YYYYMMDD_HHMMSS>.xlsx`（放 `<out_dir>/exports/`）。
- 必须附 `sidecar/test_exporter.py`：**用样例 Excel 反向构造一个临时 sqlite**（或直接构造最小行）跑通导出与过滤，不联网。

---

## 4. Desktop 契约

### 4.1 main.js

- 生成随机 token（crypto）+ 随机端口（port=0 由 sidecar 自选），spawn：
  `python app/sidecar/main.py --port 0 --token <t> --data-dir <project>/app/data`
- 读取 stdout 的 `##SIDECAR_READY##{"port":N}`，`GET /health` 通过后创建窗口（1400×900，加载 `renderer/index.html`）。
- 把 `{baseUrl:"http://127.0.0.1:N", token}` 通过 IPC `app:info` 给 preload。
- 其他 IPC：`app:openExternal`（采集动态原文链接 → `shell.openExternal`；校验仅 `http:`/`https:`、长度 ≤2048，
  返回 `{ok, error?}`，日志只记 host 不落完整 URL）、`app:openPath`（`shell.openPath`）、
  `app:showItemInFolder`（`shell.showItemInFolder`）。
- 关闭窗口/退出时杀掉 sidecar 进程树；sidecar 启动失败要在窗口内提示错误（renderer 显示）。
- 开发辅助参数：`electron . --screenshot <out.png>` → 页面加载完成 2s 后 `webContents.capturePage()` 存 PNG 并退出（供验收/自动化截图使用）；`--no-sidecar` 仅调试 UI。
- 日志：sidecar stdout 末尾写 `<data-dir>/logs/sidecar.log`。
- **版本更新（2026-11-13）**：renderer 不直连外网，检查/下载都由主进程代做；确定性逻辑（版本比较、资产挑选）放在
  `app/desktop/update.js`（纯 Node，可离线单测 `app/scripts/test_update_logic.js`）。
  - `app:checkUpdate` → 查 `https://api.github.com/repos/<FS_UPDATE_REPO or SolitudeZY/OpinionLens>/releases/latest`，
    返回 `{ok, current, latest, hasUpdate, releaseUrl, releaseName, notes(≤4000 字), publishedAt,
    download:{name,size,url}|null, source:'api'|'redirect', warning?, error?}`；
    GitHub 匿名 API 限流/不可用时退化为解析 `releases/latest` 的 302 Location 拿 tag（`source:'redirect'`，`download:null`）。
  - `app:downloadUpdate({url,name})` → 仅允许 https 且 host ∈ {github.com, objects.githubusercontent.com, *githubusercontent.com}；
    流式下载到 `<tmp>/OpinionLens-update/<name>`，进度通过 `app:updateProgress {received,total,percent}` 推送；
    落盘后校验 PE 头 `MZ`，不通过则删除并报错。
  - `app:runUpdateInstaller(path)` → 只允许运行 `<tmp>/OpinionLens-update/` 下的安装包，`shell.openPath` 启动。
- **数据目录可迁移（2026-11-13）**：默认 `%APPDATA%\OpinionLens`（源码运行 `app/data`），用户可改到其他盘。
  - 位置记录固定写在 `%APPDATA%\OpinionLens\user-config.json`（`{dataDir, dataDirUpdatedAt}`）——不跟着数据目录走，
    否则换盘后找不到自己的配置；`app:info` 增补 `dataDirDefault / dataDirCustom / userConfigPath`。
  - `app:chooseDataDir` → `dialog.showOpenDialog({properties:['openDirectory','createDirectory']})`；
  - `app:migrateDataDir(target)` → 校验（绝对路径、不与当前目录互含、目标已存在 `app.db` 时拒绝 `DATA_TARGET_OCCUPIED`、
    写探针文件校验可写 `DATA_TARGET_NOT_WRITABLE`）→ 停 sidecar → 复制（跳过 `electron/` 缓存与 `compat-no-sandbox.flag`）
    → 写配置 → `app.relaunch()`；失败回滚（不写配置）并恢复 sidecar（`DATA_MIGRATE_FAILED`）。**旧目录保留不删**。
  - `app:resetDataDir` → 删除配置里的 `dataDir` 并重启，回到默认目录。

### 4.2 preload.js

```js
contextBridge.exposeInMainWorld('fsapi', {
  getInfo: () => ipcRenderer.invoke('app:info'),            // {baseUrl, token, python, dataDir, dataDirDefault, dataDirCustom, userConfigPath, appVersion, ...}
  openExternal: (url) => ipcRenderer.invoke('app:openExternal', url), // 采集动态原文链接（仅 http/https）
  openPath: (p) => ipcRenderer.invoke('app:openPath', p),
  showItemInFolder: (p) => ipcRenderer.invoke('app:showItemInFolder', p),
  // 版本更新
  checkUpdate: () => ipcRenderer.invoke('app:checkUpdate'),
  downloadUpdate: (payload) => ipcRenderer.invoke('app:downloadUpdate', payload),   // {url, name}
  runUpdateInstaller: (p) => ipcRenderer.invoke('app:runUpdateInstaller', p),
  onUpdateProgress: (cb) => subscribe('app:updateProgress', cb),                     // 返回取消订阅函数
  // 数据目录
  chooseDataDir: () => ipcRenderer.invoke('app:chooseDataDir'),
  migrateDataDir: (p) => ipcRenderer.invoke('app:migrateDataDir', p),
  resetDataDir: () => ipcRenderer.invoke('app:resetDataDir'),
  onDataDirProgress: (cb) => subscribe('app:dataDirProgress', cb)                    // {step,label,percent?}
});
```

- `getInfo` 额外字段：`dataDirDefault`（默认目录）、`dataDirCustom`（是否已自定义）、`userConfigPath`、`platform`、`arch`、`updateRepo`。
- 进度类通道由主进程 `webContents.send` 推送，`subscribe()` 只做「注册 + 返回取消函数」，**不得**把 `ipcRenderer` 本身暴露给页面。
- 渲染层必须在 `window.fsapi` 不存在时（`preview.js` 静态预览 / 浏览器直开）优雅降级：更新与数据目录相关按钮显示「桌面端可用」，不得抛错。

### 4.3 renderer

- 纯原生 HTML/CSS/JS，无构建、无 CDN、无外部请求（除 `http://127.0.0.1:<port>` 的 sidecar）。
- 页面（左侧两个导航即可，保持专业简洁，沿用原型视觉语言）：
  1. **平台账号**：7 个平台卡片（小红书/抖音/快手/B站/微博/贴吧/知乎），状态（未登录/已登录/检测中/失败）、按钮「测试连接」「扫码登录」「清除登录态」；扫码弹窗展示 `qr.image`（dataURL）+ 状态文案（等待扫码/已扫码待确认/已确认/过期）+ 取消按钮；1.5s 轮询登录状态；错误/超时提示。
     - **need_manual 状态**（滑块/拼图/短信验证码）：弹窗显示醒目提示「已打开登录浏览器窗口，请在该窗口完成验证（短信码输入手机收到的验证码）；完成后 App 自动继续，无需重复扫码」；`kind=sms` 强调短信码，`kind=slider/captcha` 强调滑块/拼图；不得自动关闭弹窗或按失败处理；可取消。
     - 扫码后如手机收到验证码，提示用户回浏览器窗口输入，**不要重复扫码**（避免重复发码）。
  2. **采集任务**：表单（关键词逗号分隔、平台多选、任务名称）→ 开始采集；**采集量固定产品默认值**（`max_notes=15`、`comments=true`、`max_comments=20`，界面不再提供这三个设置，避免用户误配置）；任务列表/当前运行详情：左栏吸顶任务列表 + 任务速览，右侧运行明细为三个 tab（采集动态 `GET /api/runs/{id}/feed` 内容流 / 分平台进度条 + 状态 / 解析后的技术日志，1.5s 轮询；内容区固定高度内滚动）；结束显示「导出 Excel」→ 导出成功后「打开文件」「打开所在文件夹」；停止按钮。
     - **采集动态（2026-11-13）**：帖子与评论时间显示**完整本地时间** `YYYY-MM-DD HH:mm`（title 提示含秒；跨年数据可区分，不再只显示 `MM-DD HH:mm`）；每条帖子右下角原文链接点击后由主进程 `app:openExternal` 用系统浏览器打开（只允许 http/https，不再复制链接）；「复制摘要」等功能保留。
  3. **报告中心日期选择（2026-11-13）**：开始/结束日期使用 **Ant Design 风格日历浮层**（纯原生 JS `FS.DatePicker`，无第三方库/CDN；输入框 readonly，点击弹出「年/月切换 + 日期网格 + 今天/清空」，支持键盘操作），不使用浏览器原生 `<input type="date">`。
- 请求统一带 `Authorization: Bearer <token>`，`fetch(baseUrl + path)`；错误 Toast。
- 顶部状态栏：sidecar 在线状态、数据目录、版本。
- UI 文案必须准确：登录方式只有扫码（MVP 先做扫码），手机号/验证码不做；滑块/拼图/短信二次验证统一走「可见浏览器窗口人工接管」。
- **报告修订语义（2026-11-13 澄清）**：当前架构模型 API（AI）只产出/修订**文字叙述**，HTML 版式/图表/章节由本地
  `app/report_pipeline` 模板固定渲染，AI **不能修改 HTML 格式**。因此：
  - `source=local-ai`（模型叙述 + 本地渲染）：按钮为「修订叙述」，弹窗明确「只重写 AI 文字叙述后本地重渲染，HTML 版式不变；
    版本号 +1」，示例指令只写文字类诉求（语气/详略/建议），不再提示「改成环形图/增加章节」这类 AI 做不到的事；
  - `source=local-template`（未配置/调用失败、无 AI 叙述）：**隐藏修订入口**，详情提示「本地模板报告没有 AI 文字叙述，无法修订；
    如需 AI 叙述，请先在「设置 → 模型 API」配置并测试连接后重新生成报告」（后端兼容重渲染能力保留）。
- **报告风险口径（2026-11-13 修复）**：风险等级按「全量样本（内容帖 + 评论）」负面占比判定（阈值 <8% 低、8%~15% 中、>15% 高），
  而第四章「重点负面舆情事件」表格只列**负面内容帖**。当本期无负面内容帖、但评论区负面占比高时，报告必须明确说明
  「风险由评论区负面情绪驱动」，不得出现「风险等级高但未识别到负面内容、舆情面平稳」的矛盾表述：
  - stats 风险块补充 `comment_negative_ratio / negative_comments / risk_driver`，`basis` 同时给出内容/评论占比与条数；
  - 概览「风险等级」卡片副标题显示 `重点负面事件 N 起 · 负面评论 M 条`；第四章空表显示评论区负面说明；
  - 客户端 `local_report.reconcile_narrative()` 会对模型 API 叙述做口径纠偏（无负面内容帖且风险中/高时统一改写第四节导语与说明），
    本地规则叙述 `narrator._build_negative()` 也已同步；AI 数据包 `samples` 增加 `negative_comments` 高赞负面评论样例供模型参考。
- 验收：`scripts/dev.ps1` 启动后能看到窗口、能真实发起扫码登录并在弹窗看到二维码。

---

## 5. 自测与验收

1. `python app/sidecar/selftest.py`：离线自测（启动 sidecar → health/config/accounts → 用假 engine（`--mock-engine` 环境变量或注入 fake）跑一个 run → 导出 Excel → 校验文件存在与 sheet → 报告中心 fake LLM → 通知/模型/账号池）。
2. `python app/engine/test_engine.py`：patch/事件/参数自测。
3. 新功能离线测试：`app/sidecar/test_notify.py`（通知通道/outbox/重试/脱敏）、`app/sidecar/test_llm.py`
   （模型 provider/协议/报告直连/降级）、`app/sidecar/test_account_pool.py`（账号池/选号/API/切号）、
   `app/sidecar/test_report_data.py`、`app/sidecar/test_exporter.py`。
4. `scripts/selftest.ps1`：以上 + Electron `--screenshot` 截图验证窗口。
5. 手工验证：真实扫码登录小红书 → 二维码出现在 UI → 用户扫码 → 采集 5 条 → 导出 Excel 打开检查。

## 6. 禁止事项

- 不改 `MediaCrawler/` 源码；不引入新 Python/Node 依赖（现有 venv 已够用）。
- 不引入 CDN、不联网加载前端资源；不写死任何账号/密钥。
- 事件/接口字段名不得擅自改名（上下游已对齐）；如需变更先同步文档与自测，并在 issue/PR 中说明。

---

## 7. 设置中心 API（通知推送 / 模型 API / 多账号号池）

> 全部 `/api/*` 走同一 Bearer 中间件；错误体 `{"error":{"code","message"}}`。
> 密钥字段只写不读：响应只返回 `secret_configured` / `api_key_masked` / `key_masked`。

### 7.1 设置首屏

| 方法 | 路径 | 响应 |
|---|---|---|
| GET | `/api/settings` | `{app:{version,data_dir,platforms,login_headless}, notify:通知摘要, llm:模型摘要, reports:报告配置摘要}` |

### 7.2 通知推送

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/notify/config` | `{brand, enabled, privacy_mode, quiet_hours, muted_until, channels:[{channel_id,type,name,enabled,secret_configured,key_masked,config,last_status,last_error}], rules:[{rule_id,event_type,enabled,min_severity,channel_ids}], worker:{pending,sending,failed,sent,sent_last_at}, channel_types, event_types}` |
| POST/PATCH/DELETE | `/api/notify/channels[/{id}]` | 通道 CRUD；密钥只写不读（PATCH 不传=保留，`clear_secret=true` 清空） |
| POST | `/api/notify/channels/{id}/test` | 同步测试发送 `{ok,latency_ms,http_status,error}`（失败不抛 500） |
| POST | `/api/notify/test` | 给全部启用通道发示例事件 |
| GET/PUT | `/api/notify/rules` | 事件订阅开关（9 类事件默认开启） |
| PUT | `/api/notify/settings` | 总开关/隐私模式/静默时段/临时静音 |
| GET | `/api/notify/outbox?status=&limit=` | outbox 列表（`items`，别名 `outbox`） |
| POST | `/api/notify/outbox/{id}/retry` | 人工重试 |
| GET | `/api/notify/logs?limit=` | 发送明细（`items`，别名 `logs`；不含完整密钥 URL） |

通道类型：`smtp`（host/port/security/username/from/to + 授权码）、`wecom_bot`、`serverchan`、
`bark`（device key + 可选自建 `server_url`/group/level）、`webhook`（https URL + JSON body）。
发送语义：outbox 先落库（`dedupe_key` 唯一）→ 后台 worker 发送 → 退避重试 15/30/60/120/240s（共 6 次尝试）
→ 4xx 永久失败不重试 → 单通道 10 条/分钟令牌桶 → 连续 5 次失败熔断 30min；静默时段默认 22:00–08:00 抑制
info/warn（error 穿透）；默认隐私模式 `minimal`（不含关键词原文/昵称/正文/本地路径）。

### 7.3 模型 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/llm/providers` | `{providers:[{provider_id,name,kind,base_url,model,enabled,is_default,secret_configured,api_key_masked,last_test_ok,last_test_at,last_test_msg,extra}], default_provider_id, kinds, degraded_fallback}` |
| POST | `/api/llm/providers` | `{name,kind,base_url,model,api_key,extra?,enabled?,is_default?}` → provider（脱敏） |
| PATCH/DELETE | `/api/llm/providers/{id}` | 更新/删除；`api_key` 不传=保留，`clear_secret=true` 清空 |
| POST | `/api/llm/providers/{id}/test` | 最小 chat 探测 `{ok,latency_ms,reply,error}`（永不 500） |
| POST | `/api/llm/providers/{id}/default` | 设为默认 |

`kind`：`openai_compatible`（含 DeepSeek/Ollama/vLLM/中转站，`{base}/chat/completions`）、
`deepseek`（OpenAI 协议，默认 base `https://api.deepseek.com`、model `deepseek-chat`）、
`anthropic`（`{base}/v1/messages`，`x-api-key` + `anthropic-version: 2023-06-01`）。
报告中心使用默认 provider 生成叙述（`narrative` JSON）→ 本地 `app/report_pipeline` 渲染 HTML；
无 provider/调用失败 → 本地模板降级（`degraded`），不会阻塞其它功能。

### 7.4 多账号号池

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/accounts/pool?platform=` | `{accounts:[账号], total, default_strategy, limits, platform_min_interval_s, platforms}` |
| GET/PUT | `/api/accounts/pool/settings` | 默认策略 `lru|best_health|round_robin`、日 runs/条数限额、平台最小间隔 |
| POST | `/api/accounts/pool` | `{platform, alias?, proxy_url?, is_default?}` → 账号（201） |
| GET/PATCH/DELETE | `/api/accounts/pool/{account_id}` | 详情/更新/软删（`?purge=true` 连带删 profile）；更新支持 `alias/status/enabled/risk_note/risk_level/is_default/proxy_url` |
| POST | `/api/accounts/pool/{id}/login`、`GET …/login/{login_id}`、`POST …/login/{login_id}/cancel` | 账号级扫码登录/轮询/取消（事件/状态同旧登录，另带 `account_id/account_alias`） |
| POST | `/api/accounts/pool/{id}/test`、`POST /api/accounts/pool/test` | 账号级/批量检测（结果写回账号） |
| POST | `/api/accounts/pool/{id}/adopt` | 旧 profile 迁移到账号目录（幂等） |
| GET | `/api/accounts/schedule/preview?platform=&strategy=` | 干跑选号：`{selected, candidates:[{account_id,alias,eligible,blocked_reasons,score,reasons}], …}` |
| GET | `/api/runs/{run_id}/accounts` | `{run_id, attempts:[{platform,attempt,account_id,account_alias,reason,status,message,…}], platforms:[{platform,account_id,account_alias,account_attempts,status}]}` |

账号对象核心字段：`account_id/platform/platform_label/alias/status(active|cooldown|need_login|disabled|risk|archived)/
login_state(unknown|ok|expired)/profile_layout(legacy|v2)/health_score/cooldown_until/cooldown_remaining_s/
is_default/quota:{day,runs,saved_items,day_runs_limit,day_items_limit,runs_remaining,items_remaining,exceeded}`。

- 旧 `/api/accounts` 路由保留：字段/平台顺序不变，追加 `account_count/default_account_id/default_account_alias/health/accounts[]`；
  `GET` 顶层每平台行旧 6 字段（`platform/label/logged_in/last_checked_at/profile_exists/message`）不变。
- `POST /api/runs` 支持可选 `account_policy`：`{"mode":"auto","strategy":"lru"}` 或
  `{"mode":"manual","account_ids":{"xhs":"acc_xxx"}}`；`GET /api/runs/{id}` 追加 `account_policy` 与
  `platforms[].account_id/account_alias/account_attempts`。
- **legacy 兼容**：旧版单账号（`profile_layout="legacy"`）不会被登录态/冷却硬过滤——始终回退启动 engine，
  由 engine 判定未登录；失败切号只针对 v2 账号池账号。
- **默认账号（0.5 起为 v2）**：`ensure_default_account()` 在平台没有任何账号时创建
  `acc_default_<platform>`，布局 `v2`、独立目录 `browser_data/<platform>/<account_id>/user_data_dir`；
  同一平台可并存多个账号，Cookie 互不覆盖。**legacy 布局不再新建**，只用于旧数据兼容：
  - 检测到旧版共用目录（`browser_data/<platform>_user_data_dir`）非空时，仍按 `acc_legacy_<platform>` +
    `legacy` 建行，原样沿用旧 Cookie（老用户不被迫重新扫码）；
  - 「从未登录过」的空 legacy 行在启动时原地升级为 v2（`_upgrade_empty_legacy_accounts`），
    带登录态的 legacy 目录保持不动。
- **调度口径**：v2 账号未登录（`login_state!=ok`）/冷却中不会被调度，平台直接 `failed` 并给出可解释原因；
  `legacy_fallback` 仅对 legacy 账号生效（旧数据行为不变）。
- **日配额口径（2026-11-13 修正）**：`runs` 在**一次采集尝试开始时** +1（防止并发超用），但若该尝试终态是
  `login_invalid`（未登录/登录失效，根本没打到平台内容接口），`finish_attempt` 会回滚这次计数（幂等：只对
  `running → login_invalid` 的首次跃迁生效）。自动重试与续采各算一次（它们确实发起了平台请求）；
  `risk_blocked` 不退（已打到平台）。`saved_items` 只在成功入库时累加。
- 错误码：`NO_ACCOUNT 409`、`ACCOUNT_BUSY 409`、`ACCOUNT_DISABLED 409`、`ACCOUNT_NOT_FOUND 404`、
  `QUOTA_EXCEEDED 429`、`PROFILE_LAYOUT_CONFLICT 409`、`PROFILE_MIGRATE_FAILED 500`、`DELETE_FAILED 500`；
  旧 `/api/accounts/*` 并发冲突保持 `BUSY 409`。

---

## 8. 事件总线（通知的输入）

sidecar 进程内 `app/sidecar/events.py` 的 `EventBus`：业务模块 `bus.publish(event)`，通知服务 `on_event` 消费。
事件必须带 `type/severity/dedupe_key/ts/data`：

| type | severity | dedupe_key | 触发点 |
|---|---|---|---|
| `run.finished` | info | `run:{run_id}:finished` | 采集 run 终态（全部平台成功） |
| `run.failed` | error | `run:{run_id}:failed` | run 终态失败 |
| `run.partial` | warn | `run:{run_id}:partial` | 部分平台失败/取消/续采未全完成 |
| `report.finished` | info | `report:{report_id}:v{version}:finished` | 报告 completed |
| `report.degraded` | warn | `report:{report_id}:v{version}:degraded` | AI 失败→本地模板 |
| `report.failed` | error | `report:{report_id}:failed` | 报告 failed（含无数据） |
| `report.cancelled` | info | `report:{report_id}:cancelled` | 用户取消 |
| `account.login_invalid` | error | `account:{account_id}:login_invalid:{YYYY-MM-DD}` | 检测/采集发现账号登录失效 |
| `account.risk_blocked` | error | `account:{account_id}:risk:{YYYY-MM-DD}` | 风控/封禁类失败 |
| `login.need_manual` | warn | `login:{login_id}:need_manual` | 登录需要人工验证（首次/换 kind） |

`data` 约定：`run.*` 含 run_id/name/keywords_count/platforms_total/platforms_ok/platforms_failed/
platforms_skipped/saved_items/duration_s/error/platform/account_id/account_alias；`report.*` 含
report_id/version/title/error；`account.*` 含 account_id/account_alias/platform/status/message/run_id；
`login.need_manual` 含 login_id/platform/account_id/account_alias/kind/message。

---

## 9. 渲染层（设置页与多账号）

- 左侧导航：`平台账号 / 采集任务 / 报告中心 / 设置`；设置页 tabs：模型 API、通知推送、账号池、关于。
- 纯原生 HTML/CSS/JS；请求统一 `Authorization: Bearer <token>`；`FS.Api` 命名空间；后端 404/未实现时优雅降级。
- 平台账号页：平台分组 + 多账号卡片（别名/状态/健康度/配额/默认/最近使用），操作新增/扫码登录/测试/启停/
  设默认/软删；一键全检测并发 ≤2。0.5.0 起默认账号为 v2 独立目录，界面无「迁移旧目录」入口
  （后端 `POST /accounts/pool/{id}/adopt` 仅作旧数据修理端点保留）。
- 采集任务页：新任务可选「账号策略（自动/指定账号）」；运行详情分平台显示账号别名与切号原因。
- 密钥输入框 `type=password`、保存后清空并显示 `已配置 ****xxxx`；通知内容默认最小化。
