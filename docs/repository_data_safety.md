# Repository 数据安全约定

本页记录 Repository 层已经实现的最小数据安全边界。测试只能使用人工构造数据，不得使用真实用户心理咨询内容。

## Long-term memory

所有已有记忆的修改都在数据库查询中同时限定 `memory_id` 和当前 `user_id`。目标不存在与目标属于其他用户统一返回 `applied=False`，不返回其他用户的记忆内容或状态。新记忆始终写入当前 `user_id`；持久化前还会确认所有 `source_message_ids` 都属于当前用户的 session。

最终写入内容按以下规则选择：

1. `decision.sanitized_content is not None` 时，使用 sanitized content；
2. 只有 sanitized content 为 `None` 时，才使用 `candidate.content`；
3. 最终内容为空或全空白时返回 `applied=False`，不写数据库；
4. reinforce 会合并已有与本次来源 ID，supersede 的新行保留本次来源 ID。

操作合同如下：

| Operation | `target_memory_id` | 行为 |
|---|---|---|
| `CREATE` | 必须为空 | 创建当前用户的新记忆 |
| `REINFORCE` | 必须提供且属于当前用户 | 更新脱敏内容、来源和强化元数据 |
| `SUPERSEDE` | 必须提供且属于当前用户 | 验证旧目标后再创建替代记忆 |
| `MARK_CONFLICT` | 必须提供且属于当前用户 | 逻辑标记 conflicted |
| `EXPIRE` | 必须提供且属于当前用户 | 逻辑标记 expired |
| `DELETE` | 必须提供且属于当前用户 | 逻辑标记 deleted，不物理删除 |

未知 operation 会先被公共 Pydantic 枚举拒绝，不会静默降级为 CREATE。

## User/Session 幂等与事务

`ensure_user` 和 `ensure_session` 先执行普通幂等读取；只有缺失时才在 nested transaction/savepoint 中插入。仅当驱动错误被精确分类为 unique violation 时，Repository 才重新读取目标：

- User 仍须为 active；disabled/deleted 不会自动恢复；
- Session 必须属于同一 user 且为 active；同 ID、不同 owner 仍被拒绝；
- foreign key、not-null、check 和未知 IntegrityError 不会被吞掉。

Repository 只调用 `flush`，不调用 `commit` 或 `rollback`。事务提交与回滚继续由外层 `transactional_session` 控制。SQLite legacy transaction 模式在创建 savepoint 前使用不命中任何行的 DML 启动父事务，以保证外层 rollback 仍然有效；该语义已有 SQLite 回归测试。

## State 来源追踪

`StateRepository.save_version` 支持向后兼容的可选关键字参数 `source_message_id`。未传入时列保持 NULL，显式传入时写入现有外键列。公共 `SessionState` schema 没有该字段，因此查询仍返回原有 schema，不暴露 ORM。

当前 TurnOrchestrator 仍使用旧调用方式；是否把当前 user message ID 接入该参数，由 A 后续收口决定。

## PostgreSQL 与后续并发工作

IntegrityError 分类优先使用 PostgreSQL SQLSTATE：23505、23503、23502、23514；SQLite 优先使用扩展错误码。PostgreSQL 真实 savepoint、SQLSTATE 和并发行为仍需在 Linux 环境验收，当前不能描述为已验证。

B-5B 将单独处理 message sequence、state version、summary version、pending intervention 和同一 session 并发请求。本步骤没有加入 `SELECT FOR UPDATE`、advisory lock、Redis lock、重试循环或原子计数器。
