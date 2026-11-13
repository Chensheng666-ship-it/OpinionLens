# 文档索引

舆见 OpinionLens（社媒舆情采集 + AI 报告 Windows 桌面端）的文档入口。产品介绍与快速开始见根目录
[`README.md`](../README.md)。

## 1. 使用与开发

| 文档 | 内容 |
|---|---|
| [`README.md`](../README.md) | 项目定位、核心能力、设置中心（通知/多账号/模型 API）、架构、快速开始、数据与隐私、开源协议与合规、FAQ |
| [`app/README.md`](../app/README.md) | 应用级使用说明：启动、七平台登录矩阵、采集稳定性、报告中心 API、常见问题 |
| [`app/CONTRACT.md`](../app/CONTRACT.md) | 开发契约：Engine 事件协议、Sidecar REST API、Exporter 契约 |
| [`app/sidecar/README.md`](../app/sidecar/README.md) | 本地 FastAPI 服务：启动、环境变量、API 示例、自测 |
| [`app/desktop/renderer/README.md`](../app/desktop/renderer/README.md) | 渲染层结构与界面交互约定 |
| [`AGENTS.md`](../AGENTS.md) | 仓库级 AI Agent 协作约定（图片引用、常用命令、产物与提交、用户环境） |

## 2. 构建与运维

| 文档 | 内容 |
|---|---|
| [`常用命令_构建运行自测.md`](常用命令_构建运行自测.md) | 开发、自测、打包资源、构建安装包、清理命令清单与重要路径 |
| [`打包与安装包_构建说明.md`](打包与安装包_构建说明.md) | 安装包产物、构建步骤、验证清单、常见构建/安装问题与已知限制 |

## 3. 协议与版本

| 文档 | 内容 |
|---|---|
| [`LICENSE`](../LICENSE) | MIT 许可证全文 |
| [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) | 第三方组件许可证说明与 MediaCrawler 非商业许可声明 |
| [`CHANGELOG.md`](../CHANGELOG.md) | 版本变更记录（开源基线 0.1.0 起） |

## 4. 说明

- 本仓库为**产品源码仓库**，不包含 MediaCrawler 源码（由 `scripts/setup_mediacrawler.ps1` 安装）、
  运行期数据（登录态/数据库/导出/日志）、真实采集样例、依赖与构建产物。
- 界面截图统一放在 `docs/preview/`（命名 `NN_描述.png`）。仓库整理期间旧截图已清理，
  新截图整理后补充。
