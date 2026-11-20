# tests/ — 测试、替身与工具脚本

本目录放**所有不随产品发布的代码**：离线测试、测试替身、真实网络探针、工具脚本。
`app/engine/`、`app/sidecar/` 里只保留产品代码（`adapters/`、`runner/`、`report_*.py` 等）。

## 四个目录各放什么

| 目录 | 内容 | 特点 |
|---|---|---|
| `python/` | `test_engine.py`、`test_account_pool.py`、`test_exporter.py`、`test_llm.py`、`test_notify.py`、`test_report_data.py`、`test_run_manager.py`、`test_untrusted_sample_narrative.py`、`selftest.py` | 离线（不联网、不启动真实浏览器）；`selftest.py` 是 sidecar 全链路自测，也会 import 其它测试模块复用断言。`test_untrusted_sample_narrative.py` 在漏洞修复前会失败 |
| `fixtures/` | `_fake_engine.py`、`_fake_exporter.py`、`_fake_llm_server.py`、`_fake_notify_server.py`、`_fake_report_data.py` | 测试替身：假 engine（遵守 CONTRACT §1 事件协议）、假 exporter、进程内假 LLM/通知 HTTP 服务 |
| `tools/` | `_real_login_check.py`、`_probe_xhs_login.py`、`probe_platform_qr.py`、`diagnose_ks_{api,channel,crawler_flow,pagination,search}.py` | 需要**真实网络 / 真机 Edge / 真实账号**的探针与诊断脚本，不参与自测 |
| `js/` | `test_update_logic.js` | 更新逻辑（版本比较 / 安装包挑选）纯 Node 单测，无 Electron、无网络 |

## 单独跑一个测试

用 MediaCrawler 的 venv（含 openpyxl / httpx / fastapi 等依赖）：

```powershell
cd D:\OpinionLens
$py = 'D:\OpinionLens\MediaCrawler\.venv\Scripts\python.exe'

& $py tests\python\test_engine.py        # 引擎 patch / 事件 / 参数（需本机已装 MediaCrawler）
& $py tests\python\test_run_manager.py   # 采集编排（FakeBackend，不启子进程）
& $py tests\python\test_account_pool.py  # 账号池 + 选号/换号 + api_pool
& $py tests\python\test_exporter.py      # Excel 导出
& $py tests\python\test_report_data.py   # 报告数据包 / 情感缓存
& $py tests\python\test_notify.py        # 通知通道 / outbox / 重试
& $py tests\python\test_llm.py           # 模型 API / 报告降级
& $py tests\python\test_untrusted_sample_narrative.py  # 采集样本指令不得改写 AI 叙述（修复前失败）
& $py tests\python\selftest.py           # sidecar 全链路（起真实 sidecar 子进程 + 假 engine）
node tests\js\test_update_logic.js       # 更新逻辑（7 条）
```

一键全跑（含 Electron 截图）：`.\app\scripts\selftest.ps1`

## 为什么替身要在 `sys.path` 里

产品侧有两类「按模块名注入」的入口，运行时靠 `importlib.import_module(name)` 找模块：

- `SIDECAR_EXPORTER_MODULE=_fake_exporter`（`runner/service.py::load_exporter`）
- `SIDECAR_REPORT_DATA_MODULE=_fake_report_data`（`report_manager.py::_load_report_data_module`）
- `FAKE_ENGINE=1` 时 `SidecarConfig` 默认入口指向 `tests/fixtures/_fake_engine.py`

因此：

1. **测试脚本自己**在文件开头显式插入三个路径（顺序：`tests/fixtures` → `app/sidecar` → `app/engine`），
   这样 `import engine_runner` / `import _fake_llm_server` 都能找到；
2. **产品侧**只在模块名以 `_fake_` 开头时，才把 `tests/fixtures` 追加进 `sys.path`
   （`_ensure_module_search_path`），生产环境的导入路径完全不变；
3. `tests/fixtures` 里没有与产品同名的模块（都以 `_fake_` 开头），不会遮蔽 `app/sidecar` 的模块。

## 约定

- 测试目录不 import 产品测试之外的私有脚本；产品代码**不允许** import `tests/*`（替身注入只走环境变量）。
- `app/sidecar/_fixtures/` 是 `test_exporter.py` 的产物目录（fixture sqlite / 导出 Excel），已 gitignore。
- 移动/新增测试后记得同步：`.github/workflows/ci.yml`、`app/scripts/selftest.ps1`、`mypy.ini`、本文件。
