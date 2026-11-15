# `_fixtures/` — exporter 测试产物

本目录由 `tests/python/test_exporter.py` 自动生成/覆盖，仅用于离线自测与人工检查，**不是运行期数据**。

| 文件 | 说明 |
|---|---|
| `media_crawler_fixture.db` | 测试用 MediaCrawler 表结构 sqlite：6 平台 × 内容+评论 = 12 张表；`creator_hash`/`nickname` 按 MediaCrawler 当前 ORM 列名；贴吧表故意缺失，用于验证“表不存在跳过”。行数据的时间字段故意混用毫秒/秒/字符串/Excel 序列号；`add_ts` 与 `last_modify_ts` 递增构造，用于测试“本次运行更新”过滤。 |
| `out/exports/*.xlsx` | `export_run_excel` 实际导出的工作簿（含 `概览` + 数据 sheet），可直接用 Excel 打开检查。 |
| `regression/` | 手工回归留档（可选）；测试自动生成的文件不会覆盖用户 `app/data/exports/` 下的已有导出。 |

> 导出口径与清洗规则（与 `exporter.py` 保持同步）：
> 1. 导出过滤按 **`last_modify_ts >= started_ts`**（毫秒归一；缺失/0 回退 `add_ts`）；
>    `add_ts` 是首次入库时间，重复采集不会变化；`last_modify_ts` 才能反映本次运行新增+更新的数据量。
> 2. 每个数据 sheet 末尾带 **`filtered_reason`** 相关性标记列：
>    `source_keyword`（逗号/空格/顿号拆 token）在 title/desc/正文/nickname 中命中任一 token → 空（相关）；
>    未命中 → `off_brand`；评论按父内容继承。**只标记、不删除行**，报告阶段再排除。
> 3. `概览` 含：相关行数 / off_brand 行数 / 相关性判定规则；sheet 顺序、过滤口径、文件名不变。
> 4. Excel 安全清洗：所有写入 Excel 的字符串统一清洗 XML 非法控制字符
>    （0x00-0x08/0x0B/0x0C/0x0E-0x1F，额外 0x7F，替换为空格；保留 `\t\n\r` 与 emoji）；
>    以 `=`/`+`/`-`/`@` 开头的文本强制按文本写入（`data_type='s'`，防公式注入）；清洗后再做 32000 截断。

重新生成（覆盖本目录 `media_crawler_fixture.db` 与 `out/`，不动 `regression/` 与用户文件）：

```powershell
cd <仓库目录>
& "MediaCrawler\.venv\Scripts\python.exe" tests\python\test_exporter.py
```

测试会额外用真实 `app/data/crawler/media_crawler.db`（若存在）做**只读快照**回归
（快照到临时目录并剔除 run 结束之后的数据，模拟 run 刚结束时的库状态，不会修改用户库）；
样例 Excel 不存在时可指定其他文件或走内置合成数据：

```powershell
& "MediaCrawler\.venv\Scripts\python.exe" tests\python\test_exporter.py --excel D:\data\public_sentiment_sample.xlsx
```

> 注意：测试会先删除并重建本目录下的 `out/`，不要在此存放手工文件。
