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
```

同一天重复执行会更新而不是复制快照。使用 `--date YYYY-MM-DD` 可以执行确定性的回填，使用多个 `--query` 可以覆盖默认候选查询。

定时工作流当前会把单次 SQLite 结果保留为 14 天 artifact，用于验证字段、配额和任务耗时；artifact **不是** V0.1 的持久数据库。完成 spike 后应把相同的存储接口接到 PostgreSQL，再开始计算跨日趋势。

## 测试

```bash
python -m pip install pytest
pytest
```
