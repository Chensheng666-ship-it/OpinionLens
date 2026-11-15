# report_pipeline — 统计与 HTML 报告管线（运行时组件）

> 目标：把本地采集库的数据稳定地变成一份**可离线打开的中文 HTML 舆情报告**。
> 核心原则：**数字全部由代码计算，模型只写叙述**；HTML 单文件、内联 Chart.js、无外链依赖，
> 由桌面端在本地渲染。

本目录是报告中心的**统计与模板管线**，被 sidecar 在运行时复用：

- `app/sidecar/report_data.py`：复用 `excel_loader.NormalizedDataset` 与 `compute_stats.compute_stats`
  生成确定性统计数据包（`stats` / `samples` / `contents`）；
- `app/sidecar/local_report.py`：复用 `narrator` + `render_html` + 内联 Chart.js 渲染 HTML；
  渲染失败时退内置极简模板，保证总能产出可打开的 HTML；
- 模型（用户配置的 OpenAI 兼容 / DeepSeek / Anthropic）只负责生成「叙述 JSON」，
  数字与图表数据全部来自本管线，避免模型编数字。

## 文件清单

| 文件 | 作用 |
|---|---|
| `excel_loader.py` | 读分平台 sheet；时间归一化；情感归一化；ID 精度修复（数字型 ID 从原文链接取回）；去重；数据质量统计 |
| `compute_stats.py` | **确定性统计**：总量、情感分布、平台排名、按天×平台趋势、Top 负面/正面事件、热词 TopN、评论统计、风险等级、数据质量；输出 stats JSON |
| `narrator.py` | 叙述层本地规则实现（降级）：只把 stats 拼成中文叙述 JSON，不做任何统计；也用于校验模型输出的叙述结构 |
| `render_html.py` | 模板渲染器：HTML 转义、`data-stat` 质检标记、图表数据注入、内联 Chart.js、章节 id 固定 |
| `schema_validator.py` | 轻量 JSON Schema 校验器（venv 无 jsonschema 时使用，支持 `$ref`/`required`/`enum`/`oneOf` 等） |
| `templates/weekly_report.template.html` | 报告模板（`public-sentiment-weekly@v1`）；7 章节 + 3 图表 + CSS + 图表初始化脚本 |
| `assets/chart.umd.min.js` | Chart.js v4.4.1（本地副本，MIT License），渲染时内联进 HTML |
| `stats.schema.json` | stats JSON 契约（Draft-07） |
| `narrative.schema.json` | 叙述 JSON 契约（模型输出/本地规则共用） |

## 设计要点

1. **统计与写作分离**：`compute_stats.py` 是唯一数字来源；模型只消费 stats 写叙述，不产生数字。
2. **模板固定**：7 个 section id、3 个 canvas id、`report-template-version` meta；CSS/JS 全内联、
   无外链/相对路径资源，单文件可双击打开。
3. **离线优先**：Chart.js 从本地 `assets/` 内联；正文原文链接仅作为“内容溯源锚点”（点击才联网）。
4. **数据质量透明**：数字型 ID 精度丢失时从 `*_url` 取回；数据质量问题记录在 `stats.data_quality`。
5. **可复现**：本地规则叙述 + 确定性统计 → 同一份数据每次生成相同数字；payload 指纹写入
   `manifest`、HTML meta、stats 文件，便于对账。

## 与桌面端的关系

- `app/sidecar/report_data.py`：从 MediaCrawler SQLite 读取并按时间窗口过滤、去重、组装数据包；
- `app/sidecar/local_report.py`：本地模板兜底渲染（`narrator` + `render_html` + 内联 Chart.js）；
- 打包脚本把本目录复制到安装包 `resources/report_pipeline`；
  桌面端通过 `FS_REPORT_PIPELINE_DIR` 或相对路径定位（默认 `<repo>/app/report_pipeline`）。
- 离线回归：`tests/python/test_report_data.py`（数据包/统计）、`tests/python/test_llm.py`（模型叙述与降级）。

## 环境

- Python：`MediaCrawler\.venv\Scripts\python.exe`（含 pandas/openpyxl/jieba/Pillow）。
- 所有文件 UTF-8（无 BOM）；控制台乱码时设置 `$env:PYTHONIOENCODING="utf-8"`。
