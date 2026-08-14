# V4 RAG Merge

这是一个可直接上传部署的完整 V4 工程。它以当前 `psych-agent_v4` 为完整基线，再将
经过筛选的 RAG_MERGE 能力合入相应文件；没有以 V3 或 RAG_MERGE 文件反向覆盖 V4。
因此，V4 的文字对话、语音/视频 WebSocket、AIE 非语言观察和原有记忆管理接口均被
保留。

为避免泄露本机凭据，工程不包含原 V4 的 `.env`、缓存和临时目录。上传服务器后请按
`.env.example` 创建并配置服务器环境的 `.env`。

## 合入的能力

- 知识库：知识源、文档、分块、向量、使用审计；导入脚本、pgvector 检索、RRF 融合和
  可选 reranker；检索内容经上下文预算后进入策略、回复与安全约束链路。
- 嵌入：默认目标为 `BAAI/bge-small-zh-v1.5`，512 维、CLS pooling、L2 normalize、
  cosine distance 和 HNSW `vector_cosine_ops` 索引。相似度阈值与参考实现保持为
  0.92（语义重复）和 0.75（同主题候选）。
- 记忆治理：规范化内容哈希、数据库唯一约束、行锁和事务内确认；关系判断为
  `exact hash -> semantic candidates -> SAME / COMPLEMENTARY / EXPLICIT_UPDATE /
  CONFLICT / UNRELATED`。向量相似度只负责召回候选，属性/偏好冲突优先于相似度判断，
  不会因“相似”而自动覆盖。
- 合并/冲突：相同内容 reinforcement；补充性信息创建待确认的合并记忆；显式更新创建
  待确认的 supersede；冲突只标记冲突、不自动覆盖。确认接口在一个事务中完成状态变更。

## 应用方式

1. 将整个目录上传到服务器，并按 `.env.example` 创建服务器专用的 `.env`。
2. 安装 `pyproject.toml` 新增依赖：`pgvector`、`torch`、`transformers`。
3. 在目标环境执行 Alembic 迁移，顺序包含 `0004_memory_normalization`、
   `0005_knowledge_rag` 和 `0006_knowledge_lifecycle_vector_audit`。
4. 配置 `.env`：先保持 `RAG_ENABLED=false`；确认本地 BGE 模型目录可用后，再开启
   `RAG_ENABLED=true` 与 `EMBEDDING_MODEL_ENABLED=true`。知识导入使用
   `scripts/ingest_knowledge.py`。

本次已在“V4 完整副本 + 本目录覆盖”的临时环境验证知识检索、记忆规范化/策略/检索、
记忆 worker、迁移、上下文构建和运行时配置，共 75 项测试通过。
