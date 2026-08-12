# AI Tech Radar

AI Tech Radar 是一个面向 LLM 工程师的技术情报产品：持续采集公开项目的客观活动数据，识别近期升温的项目，并用可追溯的 AI 分析回答“它是什么、为什么值得关注、是否值得学”。

仓库当前进入 Sprint 0：已提供可重复运行的 GitHub 候选发现、SQLite 快照和定时采集 spike。产品边界、技术可行性、MVP 架构、数据模型、排期和验收标准见：

- [可行性研究与基本规划](docs/feasibility-plan.md)

## 当前结论

建议立项，但先做一个 **GitHub-only、规则优先、单体部署** 的 V0.1。首个版本的成功标准不是“Agent 看起来聪明”，而是能够用连续快照稳定、可解释地发现最近 7 天真正加速的 AI 项目。

## 本地运行

项目仅需要 Python 3.11+。GitHub token 不是本地试跑的硬性要求，但认证请求具有更合适的 API 配额。

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
export GITHUB_TOKEN=your_token
radar collect --database radar.db --per-query 25
radar score --database radar.db --date 2026-08-12
```

同一天重复执行会更新而不是复制快照。使用 `--date YYYY-MM-DD` 可以执行确定性的回填，使用多个 `--query` 可以覆盖默认候选查询。

SQLite 是本地和 CI 的 fallback。接入托管 PostgreSQL（例如 Supabase）时，先安装可选依赖，再通过环境变量切换后端：

```bash
python -m pip install -e '.[postgres]'
export RADAR_DATABASE_URL='postgresql://user:password@host:5432/radar'
radar collect --database-url "$RADAR_DATABASE_URL" --per-query 25
```

PostgreSQL schema 位于 `src/radar/migrations/001_initial.sql` 和 `002_trend_scores.sql`，采集器和评分器不需要因为存储后端切换而改变。同一天重复执行会更新而不是复制快照；使用 `--date YYYY-MM-DD` 可以执行确定性的回填，使用多个 `--query` 可以覆盖默认候选查询。

`radar score` 使用最近最多 8 个快照计算确定性的 7 日 star velocity、acceleration、数据新鲜度和数据完整度；不足 7 日历史的项目会保留为 `warming_up`，不会伪造正式分数。commit、release 和 contributor 信号存在时会自动加入，否则会在 `reasons` 中标明缺失并重新归一化权重。

只读查询 API 可以复用同一个 SQLite 或 PostgreSQL 数据库。安装 API 依赖并启动服务：

```bash
python -m pip install -e '.[api]'
uvicorn radar.api:app --host 127.0.0.1 --port 8000
```

API 提供 `GET /health`、`GET /trending`、`GET /repositories/{id}`、`GET /repositories/{id}/history` 和 `GET /repositories/{id}/score`。`/trending` 默认读取当天的 `trend-v0.1` 分数，可用 `score_date`、`algorithm_version`、`limit` 和 `include_warming_up` 参数进行确定性查询；完整 OpenAPI 文档位于 `/docs`。

如果没有配置 `RADAR_DATABASE_URL`，定时工作流会把单次 SQLite 结果保留为 14 天 artifact，用于验证字段、配额和任务耗时；artifact **不是** V0.1 的持久数据库。

## 测试

```bash
python -m pip install pytest
pytest
```
