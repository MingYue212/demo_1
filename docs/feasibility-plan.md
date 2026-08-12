# AI Tech Radar：可行性研究与基本规划

> 规划日期：2026-08-11
> 阶段：立项前 / V0.1 方案
> 决策建议：**可行，建议立项；但必须缩小首版范围并设置数据质量闸门。**

## 1. 执行摘要

AI Tech Radar 的产品方向成立。它解决的不是“找到热门仓库”，而是三个连续问题：

1. **发现**：哪些 AI 项目正在加速，而不只是历史 star 总量高？
2. **理解**：项目解决什么问题，证据是什么，与同类项目有什么差异？
3. **决策**：它是否值得当前用户投入时间学习，应该学到什么程度？

项目与云端开发方式匹配：代码、迁移和工作流可由 Codex 在临时环境中修改并通过 GitHub 交付；真正的持久状态放在托管 PostgreSQL，定时采集由 GitHub Actions 执行，Web/API 部署在托管平台。Codex Cloud 不进入线上运行时，也不承担永久存储。

**总体判断：8/10，技术上无关键阻塞。** 最大不确定性不是开发，而是候选集覆盖率、趋势分数可信度、LLM 结论的证据约束，以及 GitHub API 配额。应先证明“趋势发现”有用，再依次增加 AI 分析、Deep Dive、RAG 和 MCP。

## 2. 范围与非目标

### 2.1 V0.1 必须完成

- 建立可维护的 AI 仓库候选集；
- 每日保存仓库指标快照，不依赖完整 stargazer 事件历史；
- 计算 1/3/7 日增长、活跃度和可解释的 Trend Score；
- 提供 Today、Trending、分类筛选和项目详情页；
- 展示分数构成、数据时间和原始 GitHub 链接；
- 采集任务可重跑、可观测、失败后不会产生重复数据。

### 2.2 V0.1 明确不做

- 不做聊天首页、多 Agent、向量检索、论文/模型/新闻采集；
- 不做 Redis、Milvus、消息队列或微服务拆分；
- 不宣称“全网项目排行”，只说明候选集和观察窗口；
- 不让 LLM 决定热度，LLM 仅负责内容理解；
- 不自动抓取任意网站，避免版权、robots、稳定性和提示注入风险。

这组边界能把首版控制在 3–4 周的个人项目规模内，并让每个后续阶段都有可测量的前置条件。

## 3. 可行性分析

| 维度 | 结论 | 关键说明 |
|---|---|---|
| 数据 | 可行，有约束 | GitHub REST/GraphQL 可提供仓库元数据、提交、release 等；趋势必须靠自有连续快照。候选发现不能只靠 Trending 页面。 |
| 算法 | 可行 | 快照 delta、项目年龄归一化、活动信号和异常抑制足够支持首版，不需要机器学习。 |
| LLM | 可行 | README/release 可转为结构化分析；必须保存证据、模型/提示词版本及失败状态。 |
| 云部署 | 可行 | Next.js + FastAPI + 托管 PostgreSQL 是合理组合；首版也可合并 API 以降低运维面。 |
| 定时任务 | 可行 | GitHub Actions schedule 适合每日批处理，但不保证精确触发时间，任务必须幂等并支持补跑。 |
| 成本 | 可控 | V0.1 不调用 LLM；V0.2 只分析筛选后的少量项目，并按内容哈希缓存。 |
| 合规/安全 | 可控 | 仅采集公开元数据和必要文本，保留来源；外部内容一律视为不可信数据。 |
| 产品价值 | 需验证 | “值不值得学”有差异化，但评分必须针对用户画像，否则只是伪精确数字。 |

### 3.1 关键产品假设

立项后应尽快验证以下假设：

- 连续 7 天的数据足以让用户发现至少 3 个此前不知道、且确实相关的项目；
- 分数解释比单一排行榜更能建立信任；
- 用户愿意给出角色、经验和学习目标，以获得个性化“值得学”；
- 用户会从卡片进入项目详情，而不是只消费摘要；
- 每天分析 10–20 个项目已经足够，不需要覆盖所有 GitHub 仓库。

建议用 5–8 名目标用户进行两轮访谈，并连续人工复核 14 天 Top 20。在此之前不要投入多 Agent 架构。

## 4. 建议架构

```text
GitHub API
    │
    ▼
GitHub Actions (daily + manual backfill)
    │  collector / normalize / score
    ▼
Supabase PostgreSQL ────── OpenAI API（V0.2 起，异步分析）
    │
    ├──────── FastAPI（查询、管理、未来 Deep Dive）
    │                    │
    └──────────────── Next.js Web
                         │
                      Vercel
```

### 4.1 架构原则

1. **数据库是真相源**：任务状态、快照、分数、分析与来源全部持久化。
2. **批处理幂等**：以 `(repository_id, snapshot_date)` 唯一约束支持安全重跑。
3. **规则与生成分离**：趋势分数由确定性代码生成；LLM 不能覆盖原始指标。
4. **先单体后拆分**：一个 Python 包承载采集和评分；Web 与 API 只在确有需要时分离。
5. **证据优先**：每条生成结论关联来源 URL、抓取时间和内容版本。
6. **可替换供应商**：模型调用、向量存储和部署平台通过窄接口隔离，但不提前抽象所有实现。

### 4.2 是否一开始就用 FastAPI

若目标是最快验证，可用 Next.js Route Handlers + PostgreSQL 完成只读接口，采集仍用 Python。若目标包含系统化练习 Python/Agent 工程，则保留 FastAPI，但会增加一个部署单元、鉴权边界与日志系统。建议采用后者，同时把 V0.1 API 限制为只读查询和受保护的管理接口。

## 5. 数据设计

### 5.1 V0.1 核心表

```text
repositories
  id (GitHub repository id, PK)
  owner, name, full_name, url
  description, primary_language, license_spdx
  created_at, pushed_at, archived, fork
  discovered_at, discovery_source, relevance_status

repository_snapshots
  repository_id, snapshot_date (composite unique)
  stars, forks, open_issues, watchers
  default_branch, pushed_at
  release_count_30d, commit_count_30d
  contributor_count_approx
  collected_at, collector_version, raw_payload_hash

trend_scores
  repository_id, score_date, algorithm_version (composite unique)
  total_score
  star_velocity_score, acceleration_score
  activity_score, freshness_score, quality_score
  reasons_json, calculated_at

collection_runs
  id, job_name, started_at, finished_at, status
  candidate_count, success_count, failure_count
  rate_limit_remaining, error_summary
```

`repository_analyses`、`sources` 和 `analysis_claims` 在 V0.2 migration 中再加入；`documents`、`chunks`、embedding 列在 V0.4 再加入。这样不会为了未来能力污染首版模型。

### 5.2 数据保留与删除

- 原始 API 响应只保留调试所需字段或短期对象存储，核心字段长期保留；
- snapshot 建议长期保留，后期可将一年以上数据按周降采样；
- 仓库删除或转私有时标记不可用，不继续展示缓存正文；
- 每个页面明确 `collected_at`，避免把陈旧数据描述为“今天”。

## 6. 候选发现与趋势算法

### 6.1 候选集

每天组合多个确定性来源并去重：

- GitHub Search：topic、语言、创建/更新时间、star 区间的分桶查询；
- 已追踪仓库的持续观察；
- 官方组织和经过审核的生态种子列表；
- V0.2 后由分类器复核相关性，但低置信度项目进入人工队列。

搜索接口通常存在结果上限和配额，必须按创建时间、star 区间或 topic 分桶，记录每个查询的覆盖范围。不要把 `topic:ai` 当作完整宇宙，也不要把第三方 Trending 页面作为唯一输入。

### 6.2 首版评分

先对每个特征做 winsorize，再在同年龄段/规模段内计算 percentile：

```text
trend_score =
  0.35 * star_velocity_percentile_7d
+ 0.20 * star_acceleration_percentile
+ 0.15 * commit_activity_percentile_30d
+ 0.10 * release_activity_percentile_30d
+ 0.10 * contributor_signal_percentile
+ 0.10 * freshness_and_quality
- anomaly_penalty
```

其中：

- `star_velocity_7d = (stars_today - stars_7d_ago) / 7`；
- acceleration 比较最近 3 日速度与此前 4 日速度；
- 小项目可同时展示相对增长，但评分应对极小基数设下限；
- fork、归档、明显镜像、批量生成仓库和异常突增应用惩罚项；
- 缺少完整 7 日历史时标注 `warming_up`，不进入正式 Top 榜。

权重不是事实。上线前用 14 天人工标注集比较 Top-K precision，并发布算法版本。页面同时展示“+1,240 stars / 7d、2 releases / 30d”等原因，不能只展示 93 分。

### 6.3 质量指标

- **Top-20 Precision**：人工认为“AI 相关且近期显著升温”的比例，目标 ≥ 85%；
- **Snapshot completeness**：应采快照成功率 ≥ 98%；
- **Freshness**：95% 数据在计划采集窗口后 6 小时内更新；
- **Duplicate rate**：重命名/转移后重复展示 < 1%；
- **Explainability**：100% 上榜项目至少给出两个数值原因。

## 7. V0.2 的 LLM 分析设计

只对规则筛出的 Top N 或用户主动请求的项目分析。输入限制为已获取且允许使用的 README、release 摘要、仓库元数据及明确来源，不默认把 issues 全量送入模型。

结构化输出建议包括：

```json
{
  "one_liner": "string",
  "category": "agent_framework",
  "maturity": {"score": 7, "confidence": 0.78},
  "difficulty": "intermediate",
  "learning_value": {"score": 8, "audience": "llm_app_engineer"},
  "key_ideas": [{"claim": "...", "source_ids": ["src_1"]}],
  "risks": [{"claim": "...", "source_ids": ["src_2"]}],
  "insufficient_evidence": []
}
```

必须执行 JSON Schema/Pydantic 校验；任何没有 `source_ids` 的事实性结论不进入展示。保存模型、提示词版本、输入内容哈希、token、延迟和错误。内容未变化时复用缓存；分析失败不影响趋势页面。

“学习价值”不是项目固有属性。V0.2 先提供 2–3 个固定画像（LLM 应用工程师、推理工程师、研究入门者），V0.3 再加入个人目标，避免一个 9 分适用于所有人。

## 8. 分阶段路线图

| 阶段 | 目标 | 交付物 | 退出条件 |
|---|---|---|---|
| Sprint 0（2–3 天） | 降低未知 | API 配额实验、100 仓库样本、页面线框、成本表 | 能稳定采集样本并确认字段与配额 |
| V0.1（3 周） | 可信趋势 | 候选发现、每日快照、评分、榜单、详情、任务监控 | 连续运行 14 天；Top-20 precision ≥ 85% |
| V0.2（2 周） | 可追溯理解 | 结构化摘要、分类、画像化学习价值、来源 | Schema 成功率 ≥ 98%；抽检事实支持率 ≥ 90% |
| V0.3（2–3 周） | 单项目 Deep Dive | 异步研究任务、报告、成本/状态 UI | 10 个基准项目报告可复现且预算可控 |
| V0.4（2–3 周） | Ask Project | 文档采集、chunk、pgvector、引用回答 | 引用正确率和答案相关性通过评测集 |
| V0.5（1–2 周） | MCP | 只读 GitHub Research MCP tools | 权限最小化；工具契约和集成测试通过 |
| V0.6（持续） | Evals 闭环 | 数据集、trace、回归门禁、成本/延迟看板 | 每次 prompt/model 变更均有可比较报告 |

论文、Hugging Face 模型和行业新闻应在 GitHub 产品达到稳定留存后再进入路线图，而不是按版本号机械加入。

## 9. V0.1 工作分解

### Week 1：数据闭环

- 初始化 monorepo、环境变量模板和 CI；
- 建 PostgreSQL migrations 和本地测试数据库；
- 实现 GitHub client：分页、超时、重试、限流与 ETag/条件请求；
- 实现候选发现、快照 upsert 和 collection run 日志；
- 添加 `workflow_dispatch` 与每日 schedule。

### Week 2：评分与 API

- 实现历史窗口、缺失数据和异常值处理；
- 建立版本化 Trend Score 与离线回算命令；
- 完成排行榜、分类、详情 API；
- 用固定 fixtures 做单元/集成测试；
- 建立 14 天人工审核表。

### Week 3：Web 与上线

- 完成移动优先的 Today/Trending/Project 页面；
- 展示更新时间、升温原因、数据不足和失败状态；
- 加入缓存、错误追踪、基础访问统计和健康检查；
- staging 回放、生产迁移、运行手册与回滚演练。

### 建议仓库结构

```text
apps/
  web/                  # Next.js
  api/                  # FastAPI
packages/
  radar/                # collector, scoring, shared domain logic
migrations/
tests/
  fixtures/
  unit/
  integration/
.github/workflows/
docs/
```

## 10. 风险与应对

| 风险 | 影响 | 应对 |
|---|---|---|
| GitHub 配额或接口变化 | 采集不完整 | GitHub App/token、条件请求、预算器、分桶、退避、保存 `rate_limit_remaining` |
| schedule 延迟/丢失 | 当日数据陈旧 | 幂等任务、手动补跑、日期回填、freshness 告警；不要依赖精确分钟 |
| star 操纵或事件营销 | 假趋势 | 多信号评分、异常惩罚、人工审核、显示原始指标 |
| 新项目冷启动 | 无 7 日 delta | warming-up 榜与正式榜分开，不伪造历史数据 |
| LLM 幻觉 | 误导学习决策 | claim-source 约束、拒答字段、事实抽检、评测集 |
| README 提示注入 | Agent 越权或污染报告 | 外部文本按数据处理；工具白名单；分析 Agent 无写权限和秘密；输出校验 |
| 成本失控 | 无法持续 | Top N 门控、内容哈希缓存、分层模型、每任务预算和 usage 日志 |
| 平台耦合 | 迁移困难 | 标准 PostgreSQL migrations、无状态 API、对象存储保存大文本、导出脚本 |
| 排名引发信任问题 | 产品口碑受损 | 公开方法和版本、解释分数、反馈/纠错入口 |

## 11. 测试、可观测性与安全基线

### 测试

- GitHub client 使用录制/固定响应测试分页、304、403、429 和超时；
- snapshot upsert 重跑两次结果一致；
- score 使用冻结数据集做 golden tests，版本变更显式更新；
- migration 从空库和上一版本均可执行，并验证回滚/前滚策略；
- Web 做关键路径 e2e：榜单 → 筛选 → 项目详情；
- V0.2 起加入 schema、citation、分类、成本和延迟 evals。

### 可观测性

- 每个采集运行有 run id，日志包含 repo id 而不包含 token；
- 指标至少覆盖成功率、API 请求数、剩余配额、任务时长、数据新鲜度；
- LLM 阶段额外记录 token、缓存命中、schema 失败、模型与 prompt 版本；
- 告警必须指向补跑或降级动作，避免只有错误通知。

### 安全

- GitHub、数据库与模型密钥只放部署平台 secrets；浏览器绝不持有服务端密钥；
- 数据库启用最小权限，公开读模型通过 API 或严格 RLS 暴露；
- Actions 固定第三方 action 的 commit SHA，并限制 workflow token 权限；
- Deep Dive/MCP 工具默认只读，网络目标设 allowlist，限制响应大小和执行时间；
- 用户输入和仓库文本不能改变系统工具策略。

## 12. 成本估算方法

不在没有真实用量时给出虚假的固定月费。Sprint 0 建立以下参数表，并用各供应商当日价格计算低/中/高三档：

```text
monthly_cost =
  database_and_storage
+ web_and_api_compute
+ github_api_related_infrastructure
+ projects_analyzed_per_day * 30
   * average_input_output_tokens * model_unit_price
+ embeddings_created_per_day * 30 * embedding_unit_price
+ observability
```

V0.1 的 LLM 成本为零。V0.2 设置每日硬上限、单项目 token 上限和月度告警；Deep Dive 必须由用户触发或有独立预算。价格和免费额度会变化，实施时应从官方定价页复核，而不是写死在架构决策里。

## 13. Go / No-Go 闸门

### 现在：Go，但有条件

批准 Sprint 0 和 V0.1，暂不批准多 Agent、RAG、MCP 和多数据源并行开发。

### V0.1 → V0.2

只有同时满足以下条件才进入 LLM 分析：

- 连续 14 天自动采集，无未解释的数据缺口；
- Top-20 precision ≥ 85%，且至少 5 位目标用户中 3 位认为榜单有新增价值；
- 上榜原因对非作者用户可理解；
- 配额、补跑、迁移和恢复有运行手册。

### 停止或转向条件

- 维护候选集需要每天大量手工操作；
- 趋势结果长期等同于现有热门榜，无新增发现；
- 用户只阅读 AI 摘要而不信任/使用趋势数据；
- 公开数据不足以支撑稳定评分，且额外数据源成本超过预期。

## 14. 第一批产品任务

1. 用 100 个仓库做 7 天采集 spike，记录请求量、耗时和字段缺失率；
2. 定义 AI relevance taxonomy 与 50 条正负样本；
3. 建立 20 个已知历史升温项目的离线 score fixture；
4. 画出移动端 Today、分类页、项目详情三张低保真图；
5. 写 ADR：API 是否独立 FastAPI、认证方式、原始响应保留策略；
6. 创建 V0.1 backlog，并把“可解释 Top 20”设为唯一产品里程碑。

## 15. 需要你确认的三个决策

1. **首要用户画像**：建议先选“有 Python/LLM API 经验、希望学习 Agent 工程的开发者”；
2. **V0.1 是否坚持 FastAPI**：建议坚持，用于后续 Agent/RAG 演进，但接受额外运维复杂度；
3. **产品语言**：建议数据层语言无关，首版 UI 中文优先、关键技术名保留英文，内容表预留 locale。

确认后即可进入 Sprint 0。第一周不应写 Agent，而应先拿到真实的 7 天时间序列和人工相关性标注。

## 16. 实施时应复核的官方资料

以下链接用于实施核对；配额、能力与价格可能变化，应在编码时重新查看：

- [GitHub REST API rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)
- [GitHub Actions schedule event](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [GitHub REST repositories endpoints](https://docs.github.com/en/rest/repos/repos)
- [Vercel FastAPI documentation](https://vercel.com/docs/frameworks/backend/fastapi)
- [Vercel Cron Jobs](https://vercel.com/docs/cron-jobs)
- [Supabase pgvector / vector columns](https://supabase.com/docs/guides/ai/vector-columns)
- [OpenAI Structured Outputs](https://platform.openai.com/docs/guides/structured-outputs)
- [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)

本次环境的外网代理拒绝访问，未能在线复核这些页面。因此本文避免写入易变化的具体配额、免费额度、价格和“最新功能”结论；进入实现前必须完成一次官方资料复核。
