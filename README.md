# 财报解读看板

美股财报解读 MVP：新闻稿 / 电话会 → 结构化 JSON → 静态解读页（GitHub Pages）。

## 技术栈

- Python 3.11+（数据、计算、LLM 抽取）
- Jinja2 静态 HTML + Chart.js
- 数据存于 `data/`，页面输出到 `site/`
- GitHub Actions：`daily` / `poll` / `manual` / `deploy`

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 初始化股票（CIK + EPS 口径识别）
export SEC_USER_AGENT="stock_investing_dashboard you@example.com"
python -m pipeline init --ticker MU

# 每日一致预期快照（尽早上线）
python -m pipeline snapshot --ticker MU

# 按阶段跑某季（需 OPENROUTER_API_KEY）
export OPENROUTER_API_KEY=...
python -m pipeline run --ticker MU --stage 1
python -m pipeline run --ticker MU --period FY2026Q4 --stage 2

# 生成页面
python -m pipeline build
```

仓库含 MU 页面骨架；联调可用 mock 夹具 `tests/fixtures/mu_fy2025q4_mock.json`（标明为 mock，非正式数据）。

```bash
python -m pipeline build
# 打开 site/index.html
```

季度页的历史序列在 `site/stocks/{代码}/history.json`，同一只股票只存一份。页面用 `fetch` 读取，不再写进每个财季 HTML。

直接双击打开（`file://`）时，浏览器会拦截 `fetch`。页面会改读同目录的 `history.js`（内容和 `history.json` 相同，用脚本加载，`file://` 可以读到）。两份都失败时，金额图和占比图仍用页面里的数据；依赖历史序列的水平值 / 环比 / 同比会空着，并在财报分析下面提示。稳定打开方式：

```bash
cd site && python3 -m http.server
# 打开 http://127.0.0.1:8000/
```

## 命令

| 命令 | 作用 |
| --- | --- |
| `python -m pipeline init --ticker MU` | CIK + `eps_basis` |
| `python -m pipeline snapshot` | 全部股票预期快照 |
| `python -m pipeline poll` | 财报窗口检测 / 文字稿 |
| `python -m pipeline run --ticker MU --period FY2026Q4 --stage 1` | 重跑阶段 |
| `python -m pipeline backfill --ticker MU --from FY2021Q4` | 历史回补。已有季度 JSON 不覆盖。ADR 可用 `--press-only` 只解析 6-K 里的官方美元和 US$ per ADR，拒绝 `--xbrl-only` |
| `python -m pipeline replay-filings --ticker TSM` | 回放 6-K 过滤器（不调用 LLM） |
| `python -m pipeline history --ticker META` | 生成分部、报表和 XBRL 活动历史。`listing: hk` / `adr` 跳过。`--if-new` 只在新的 10-Q/10-K 时刷新；全池扫描不会给还没有这三份文件的股票做首次抓取。`--from-periods` 不改季度 JSON：指定美股时用 SEC 重建这三份文件，港股和 ADR 只汇总已有季度里写明的数字。不指定 ticker 时离线处理，并保留已有的 META/TSM 历史 |
| `python -m pipeline build` | 生成 `site/` |
| `python -m pipeline stage3-recent` | **已停用**：保留命令为空操作，避免旧 workflow 失败 |
| `python -m pipeline thesis --ticker MU` | 按仓位档案评估本季（1 次小调用；档案和数据都没变时跳过） |
| `python -m pipeline init-missing` | 给缺 CIK 的股票自动初始化（daily 自动跑，无 LLM） |
| `python -m pipeline comparatives` | 用新闻稿对比列补算同比 / 环比（无 LLM、不联网） |
| `python -m pipeline validate --ticker MU --period FY2026Q4` | 查看状态 |
| `python -m pipeline hkex-poll --ticker 0700.HK` | 披露易业绩公布试跑，默认只列链接 |

## Secrets

仅在 GitHub Actions 中配置（不要写入本地 `.env` / 代码）：

- `SEC_USER_AGENT`（必需）
- `OPENROUTER_API_KEY`（LLM；仅 Actions）
- 可选：`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`

本地无 key 时可运行：`pytest`、`python -m pipeline build`、`python -m pipeline snapshot`（无 LLM）。
LLM 相关命令（`run` Stage1/2、`backfill`、`llm-ping`、`eval`）请用 Actions → `manual` / `eval`。

## 仓位档案与增长质量

- `config/theses/{TICKER}.yaml`：每只股票一个仓位档案，`阶段` 为 观察 / 等待 / 持仓。
  - 观察：只写想搞清楚的 `问题`（可不写），每季用事实回答；页面同时列出管理层在问答里新给出的数字。
  - 等待 / 持仓：`论点` 每条三行（看好 / 担心 / 证伪，证伪里写日期会显示倒计时），`条件` 写买入或加仓 / 卖出。
  - Stage2 完成时自动评估（1 次小调用，档案和数据都没变时跳过；观察仓没写问题时不调用）；改了档案后用 manual → `thesis` 重评。
- 增长质量 = 通用层（`settings.yaml` 的 `quality.defaults`，会计质量类）+ 公司层（股票配置 `quality_checks`，同 key 覆盖通用阈值）。
- Stage3 次日股价反应与 T+1/3/7 分析师修正已停用，不再计算或展示。
- 新增股票：加 `config/tickers/{TICKER}.yaml` 和仓位档案即可，daily 会自动补 CIK（`init-missing`）。
- 上市地：`listing: domestic`（默认）走 **8-K Item 2.02** + EX-99。`listing: adr` 走 **6-K / 6-K/A**，用 `sec.earnings_6k` 的 exhibit 描述过滤（Press release / earnings），并排除月报、合并报表、股息、董事会。不要用 Item 2.02 滤 6-K，也不要把 TWSE / 港交所 / 巨潮接成每天的 poll。港股试跑命令 `hkex-poll` 是单独入口，不在 poll cron 里。第二只 ADR 只加一份 yaml，不要为单票写死文件名。
- ADR 另写 `reporting_currency`（TSM 为 USD）和 `accounting_basis`（备注，不参与 FY 计算）。稿里的 NT$ / TWD 不能当成美元。历史回补走 6-K 新闻稿：`python -m pipeline backfill --ticker TSM --limit 4`。`backfill --xbrl-only` 会拒绝 ADR。过滤器回放：`python -m pipeline replay-filings --ticker TSM`（不调用 LLM）。确认回放命中 Press release、排除 monthend / revenue / fsx / dividend / board 之后，这只 ADR 才随 poll 自动检查近期 6-K。

## LLM 成本护栏

只有 Stage1（新闻稿抽取 + 指引，可选变动原因）和 Stage2（电话会指引、行业展望、问答、变动原因、总结）调用 LLM，其余全部是规则计算。行业展望由 Stage2 的 DeepSeek 按 `prompts/extract_industry_outlook.md` 写入，构建页面时不再另写一版。

- poll 只在「财报窗口 + 已知财报日前 7 天至后 4 天」检查 SEC，且只处理发布不超过 `polling.new_filing_max_age_days`（默认 4 天）的 8-K；历史季度走 `backfill`。
- 同一份 8-K Stage1 失败达到 `polling.max_retries` 次后不再自动重试（计数在 `data/*/processed.json` 的 `failures`）。
- 文字稿超过 `polling.transcript_max_hours` 仍未取到时停止自动重试 Stage2。
- 每轮进程有金额上限 `max_cost_per_run_usd`、token 上限 `max_tokens_per_run`（拿不到单价时依然生效）和超时熔断 `max_timeouts_per_run`；单次请求有 `max_tokens` 与墙钟超时。
- Actions 的 LLM 缓存每次运行都会回写（key 带 run_id），重跑同样输入不再重复付费；Stage2 已完成时默认跳过，`--force` 才重跑。
- `llm.reasoning_effort`（默认 off）关闭推理模型的思考：思考 token 计入 max_tokens 且计费，曾把 4096 额度耗尽导致正文为空（DeepSeek V4 Pro 不支持 low 档）；输出被截断时不原样重试。
- 问答批次失败时拆半各试一次（每批最多 3 次调用）；Stage2 已完成但有失败轮次时，不加 `--force` 重跑只补失败的几轮。
- `llm.stage1_drivers: false` 可省掉 Stage1 的变动原因调用（Stage2 会重算）。

## 说明

- Stage2 文字稿默认自动获取：`motley_fool`（主）→ `ir_page`/`ir_prepared_remarks_url`（备）→ `manual`。
- 电话会文字稿默认不入库（`data/*/raw/*/transcript*`）。0700.HK 的 local_import `transcript.txt` 与 `transcript_meta.json` 入库，供 Actions Stage2 读取。SEC 新闻稿 HTML 与 API 原始响应需提交。
- 新增股票：复制 `config/tickers/MU.yaml`，改配置后 `init`，无需改代码。
- 港股（`listing: hk`，例如 `0700.HK`）不走 SEC。`init` / `poll` / `backfill` / `av-estimates` 都不会替它查 Edgar 或拉 AV 共识。业绩公告和电话会用 `import-period` 放进 `data/0700.HK/raw/{FY…}/`。文件名可以带点，`0700.HK.yaml` 的股票代码就是 `0700.HK`，不要改成 `0700HK` 或 `TCEHY`。

### 腾讯 0700.HK

财报数字保持人民币（¥）。股价和市值按港股报价用港币（HK$）。财报页 Forward PE、TTM、NTM 先用当天 HKDCNY（Yahoo `HKDCNY=X`，1 港币折合的人民币）；当天没有收盘时，用截至行情日最近一笔把股价换成人民币，再除以人民币 EPS，并在 P/E 旁标出汇率和日期。没有任何可用汇率时才显示「待核对」。

```bash
python -m pipeline fx-refresh --date 2026-10-23
```

中期「六个月」不能写成 Q2。Non-IFRS 经营盈利进经营利润，不进 `eps_nongaap`。0700.HK 的 local_import 电话会写入 `raw/*/transcript.txt` 并随仓库提交，Actions Stage2 才能读到。

```bash
python -m pipeline import-period --ticker 0700.HK --period FY2026Q1 \
  --pdf "/path/腾讯2026年一季报.pdf" \
  --transcript "/path/Tencent_2026_Q1.md"

# 还没有更新的业绩公告时，只导入电话会，不写财务数字
python -m pipeline import-period --ticker 0700.HK --period FY2026Q2 \
  --transcript "/path/Tencent_2026_Q2.md"

# 本地已有 PDF 时重跑 Stage1（仍不调用 SEC / LLM）
python -m pipeline run --ticker 0700.HK --period FY2026Q1 --stage 1

# Stage2 问答和变动原因需要 OPENROUTER_API_KEY，且 raw/FY2026Q1/transcript.txt 还在
python -m pipeline run --ticker 0700.HK --period FY2026Q1 --stage 2
```

#### 披露易试跑 `hkex-poll`

只找腾讯（`0700.HK` / 主板代号 `00700`）的业绩公布：季度业绩、中期业绩、全年业绩（披露易类别叫末期业绩）。默认 dry-run，只打印 JSON。加 `--download` 才把 PDF 写到 `data/0700.HK/raw/hkex/`。这个目录已 gitignore，不要把 PDF 提交进仓库。不做上市以来的历史回补，窗口最长 3 年，默认是香港今天往前 18 个月。

```bash
python -m pipeline hkex-poll --ticker 0700.HK
python -m pipeline hkex-poll --ticker 0700.HK --since 2026-01-01 --until 2026-10-23
python -m pipeline hkex-poll --ticker 0700.HK --download
```

每条输出有 `title`、`published_at`（香港时间）、`doc_type`（`quarterly_results` / `interim_results` / `annual_results`）、`url`。只有下载过的才有 `sha256` 和 `path`。

用了三个官方入口，都在 `www1.hkexnews.hk`：

1. 代号：`GET /search/prefix.do?callback=callback&lang=ZH&type=A&name=00700&market=SEHK`。这是 JSONP，腾讯的 `stockId` 从这里来，代码不写死。2026-10-23 返回的是 `7609`。
2. 公告列表：`GET /search/titleSearchServlet.do`，查询方式和标题搜索页 `https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh` 一样。`market=SEHK`，`lang=zh`，`searchType=1`，`fromDate` / `toDate` 为 `YYYYMMDD`，`t1code=10000`（公告及通告），`t2Gcode=3`（財務資料），`t2code=-2`，`rowRange` 从 100 起，最多再取 200 和 300。响应是 JSON，`result` 字段本身又是一段 JSON 字符串。类别中文名来自 `/ncms/script/eds/tierone_c.json` 和 `tiertwo_c.json`。
3. PDF：`https://www1.hkexnews.hk` 加上返回里的 `FILE_LINK`，例如 `/listedco/listconews/sehk/2026/0513/2026051300335_c.pdf`（2026 年一季业绩公布）。

財務資料这一组里还有董事会召开日期和股息表格。命令会留下标题是业绩公布的季度 / 中期 / 末期业绩，丢掉董事会日期、股息公告表格、年报、中期报告、回购、延迟发表、修订和附属公司业绩。全年业绩当天通常另有一份「末期股息」表格，不要拿去导入。

反爬：披露易会下发 Akamai 的 `bm_s` / `bm_so` cookie；`/robots.txt` 目前不是规则文件，而是一段搬迁页。User-Agent 标明本仓库，请求串行，默认间隔 1.2 秒。403 / 429 / 5xx 最多 3 次，中间指数退避。失败时 stdout 是 `ok: false`，带上 HTTP 状态、最终 URL 和正文片段，这样能把「被拦」和「窗口里没有业绩」分开。不要并发，也不要改成伪装浏览器后连续重刷。这个命令没有接进 `.github/workflows/poll.yml`。

和 `import-period` 的衔接：下载文件放在 `data/0700.HK/raw/hkex/{日期}_{doc_type}_{news_id}.pdf`，不会写成 `raw/FY…/press_release.pdf`，所以 `run --stage 1` 不会自动读它。人看过标题后指定财季再导入，例如 2026-05-13 那份一季业绩：

```bash
python -m pipeline import-period --ticker 0700.HK --period FY2026Q1 \
  --pdf data/0700.HK/raw/hkex/2026-05-13_quarterly_results_12157227.pdf
```

中期公告标题同时有「三个月及六个月」。导入器会拒绝把纯六个月写成单季；`hkex-poll` 自己不猜 `FY2026Q2` 还是半年度。
- 单季 JSON 经 `PeriodDoc`（`schema_version: 1`）校验后写入。
- 需求细节见项目内开发文档。
