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
| `python -m pipeline validate --ticker MU --period FY2026Q4` | 查看状态 |

## Secrets

- `SEC_USER_AGENT`（必需）
- `OPENROUTER_API_KEY`（LLM 抽取）
- 可选：`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`

## 说明

- `data/*/raw/*/transcript*` 不入库（电话会文字稿）；SEC 新闻稿 HTML 与 API 原始响应需提交。
- 新增股票：复制 `config/tickers/MU.yaml`，改配置后 `init`，无需改代码。
- 单季 JSON 经 `PeriodDoc`（`schema_version: 1`）校验后写入。
- 需求细节见项目内开发文档。
