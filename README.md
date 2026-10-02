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

## 命令

| 命令 | 作用 |
| --- | --- |
| `python -m pipeline init --ticker MU` | CIK + `eps_basis` |
| `python -m pipeline snapshot` | 全部股票预期快照 |
| `python -m pipeline poll` | 财报窗口检测 / 文字稿 |
| `python -m pipeline run --ticker MU --period FY2026Q4 --stage 1` | 重跑阶段 |
| `python -m pipeline backfill --ticker MU --from FY2021Q4` | 历史回补 |
| `python -m pipeline build` | 生成 `site/` |
| `python -m pipeline stage3-recent` | 近期财报的股价反应 + T+1/3/7 分析师修正（daily 自动跑，无 LLM） |
| `python -m pipeline thesis --ticker MU` | 按仓位档案评估本季（1 次小调用；档案和数据都没变时跳过） |
| `python -m pipeline init-missing` | 给缺 CIK 的股票自动初始化（daily 自动跑，无 LLM） |
| `python -m pipeline comparatives` | 用新闻稿对比列补算同比 / 环比（无 LLM、不联网） |
| `python -m pipeline validate --ticker MU --period FY2026Q4` | 查看状态 |

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
- 次日涨跌同时计算相对对照指数（股票配置 `benchmark`，默认 SPY）的超额涨跌。
- 新增股票：加 `config/tickers/{TICKER}.yaml` 和仓位档案即可，daily 会自动补 CIK（`init-missing`）。

## LLM 成本护栏

只有 Stage1（新闻稿抽取 + 指引，可选变动原因）和 Stage2（电话会指引、问答、变动原因、总结）调用 LLM，其余全部是规则计算。

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
- `data/*/raw/*/transcript*` 不入库（电话会文字稿）；SEC 新闻稿 HTML 与 API 原始响应需提交。
- 新增股票：复制 `config/tickers/MU.yaml`，改配置后 `init`，无需改代码。
- 单季 JSON 经 `PeriodDoc`（`schema_version: 1`）校验后写入。
- 需求细节见项目内开发文档。
