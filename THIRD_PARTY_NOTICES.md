# 第三方组件与许可证说明

本项目以「自有代码 + 第三方开源组件」方式使用以下依赖。**本仓库不包含 MediaCrawler 源码**，
由 `scripts/setup_mediacrawler.ps1` 从上游仓库克隆。

## 1. MediaCrawler（采集引擎，外部依赖）

| 项 | 内容 |
|---|---|
| 项目 | [NanmiCoder/MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) |
| 固定版本 | commit [`b059657a872614bc0cb887af801b753ff4587d12`](https://github.com/NanmiCoder/MediaCrawler/commit/b059657a872614bc0cb887af801b753ff4587d12)（2026-11-05） |
| 许可证 | 见上游仓库 `LICENSE`：**非商业学习许可**（Non-Commercial Learning License） |
| 集成方式 | 子进程 + 运行时 patch，不修改其源码 |
| 分发方式 | **本仓库不随附 MediaCrawler 源码**，由使用者自行通过安装脚本从上游获取 |

> **重要**：本项目的采集能力完全依赖该上游项目，其许可证限制**非商业学习用途**。
> 请勿将本项目及其采集结果用于商业售卖、数据倒卖或其他违反平台规则/法律法规的用途；
> 如需商业使用，请自行向原作者取得书面授权，或替换为合规的采集层。
> 因使用本工具产生的合规责任由使用者自行承担。

## 2. 桌面端与前端运行时

| 组件 | 许可证 | 用途 |
|---|---|---|
| [Electron](https://www.electronjs.org/) 45.2.3 | MIT | 桌面壳 |
| [Chromium](https://www.chromium.org/)（Electron 内置） | BSD-style 等（见 Chromium `LICENSE`） | 渲染引擎 |
| [electron-builder](https://www.electron.build/) 26.18.0 | MIT | Windows 安装包构建（仅构建期依赖） |
| [Chart.js](https://www.chartjs.org/)（`app/report_pipeline/assets/chart.umd.min.js`） | MIT | 报告 HTML 图表 |
| 系统 Microsoft Edge / Google Chrome | 各自许可 | Playwright 通过 `channel=msedge/chrome` 复用系统浏览器，不随包分发 Chromium |

## 3. 本地服务（Python）依赖

以下依赖通过 MediaCrawler 的 `pyproject.toml` / `uv sync` 安装（侧车运行时复用该虚拟环境），
许可证以各自项目为准：

| 组件 | 许可证 | 用途 |
|---|---|---|
| [FastAPI](https://fastapi.tiangolo.com/) | MIT | 本地 REST 服务 |
| [uvicorn](https://www.uvicorn.org/) | BSD-3-Clause | ASGI 服务器 |
| [pydantic](https://docs.pydantic.dev/) | MIT | 数据校验 |
| [httpx](https://www.python-httpx.org/) | BSD-3-Clause | HTTP 客户端 |
| [Playwright](https://playwright.dev/python/) | Apache-2.0 | 浏览器自动化（复用系统 Edge） |
| [pandas](https://pandas.pydata.org/) / [openpyxl](https://openpyxl.readthedocs.io/) | BSD-3-Clause / MIT | 数据处理与 Excel 导出 |
| [Pillow](https://python-pillow.org/) | HPND | 二维码/图片处理 |
| [jieba](https://github.com/fxsjy/jieba) | MIT | 中文分词（热词统计） |
| [loguru](https://github.com/Delgan/loguru) | MIT | 日志 |
| [OpenCV](https://opencv.org/)（opencv-python） | Apache-2.0 | 二维码有效性校验 |

> 若打包安装包，便携 Python 运行时与其 `site-packages` 中的第三方许可证文本随包放在
> 安装目录 `resources/LICENSES/` 下（构建脚本自动收集 MediaCrawler 与 Python 的许可证）。

## 4. 数据与商标

- 平台名称（小红书、抖音、快手、B站、微博、百度贴吧、知乎）为其各自权利人的商标，仅用于功能描述；
- 本项目不包含、不分发任何平台用户数据；运行期采集的数据仅保存在使用者本机
  （源码运行为 `app/data/`，安装版为 `%APPDATA%\OpinionLens\`）；
- 使用者应自行确保数据采集与使用行为符合所在地法律法规及各平台用户协议。
