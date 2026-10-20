# SQLite 会话索引测试说明

## 对象与目的

- 源码：[store.py](../../src/omt/coding_agent/store.py)。
- 测试：[test_store.py](../../tests/coding_agent/test_store.py)。
- 保证 SessionStore 只读写调用方指定的已有数据库和 sessions 表，避免路径错误时创建空库、重复 ID 覆盖旧会话，以及按目录列出时混入其他项目。
- 本模块保存会话元数据，不保存消息、执行图状态或管理 Checkpointer 的内部表。CLI /resume 通过本模块查询列表和选中记录，再调用 Runtime 恢复，交互测试见 [resume.md](resume.md)。

## 接口和预建表约定

调用方先在程序外创建数据库和表，再实例化 SessionStore(db_path)。构造不连接数据库；每个异步方法独立连接，写入提交后关闭连接，无需调用 close。四个方法为 insert_session、get_session、list_sessions、update_session_updated_at；查询返回有明确字段的 SessionRecord，更新时间方法返回提交后的 updated_at 字符串。

表结构由外部维护，不会在启动或查询时自动初始化、迁移：

```sql
CREATE TABLE sessions (
    thread_id TEXT PRIMARY KEY NOT NULL,
    workspace_path TEXT NOT NULL,
    title TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_sessions_workspace ON sessions(workspace_path, updated_at);
```

thread_id 由调用方提供，作为不透明标识保留原值，不强制 UUID 格式；插入及更新时间时拒绝空串和全空白。数据库路径与工作目录均执行 expanduser 和 resolve；列表精确匹配规范化后的目录，不自动归并子目录或查找 Git 根目录。外部直接插入记录时也必须遵守同样的路径规范和一致的时间格式。

插入时 created_at、updated_at 使用表中的默认值，查询不重建或更新时间。update_session_updated_at(thread_id) 只把目标记录的 updated_at 改为当前 UTC 时间，提交后返回原样写入的时间字符串，不修改 created_at、标题、目录、ID 或其他会话；记录不存在时抛 ValueError，不创建记录。时间格式为 YYYY-MM-DD HH:MM:SS，秒精度，同一秒内多次更新可能显示相同值。CLI 成功恢复后主动调用；当前不提供修改标题或删除方法。排序依次为 updated_at 降序、created_at 降序、thread_id 升序。

## 场景

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 构造无数据库副作用 | 父目录和数据库均不存在 | 仅记录规范化路径，不创建目录或文件 | test_constructor_does_not_create_database_or_parent |
| 插入和持久化 | 外部建表；Unicode、空标题或含 SQL 字符串的标题；数据库文件名含空格、井号和中文 | 新实例能读回原始字段、非空创建时间和更新时间；SQL 内容作为数据保存，表仍可查询 | test_insert_session_is_committed_and_can_be_read_by_a_new_store |
| 默认与空结果 | 不传 title；查询不存在的 ID 或空目录 | title 为 None，缺失查询返回 None，空列表返回 [] | test_insert_session_without_title_and_missing_queries |
| 重复 ID | 相同 thread_id 再次插入不同目录和标题 | IntegrityError 原样传播，旧记录不覆盖；之后仍可插入其他会话 | test_duplicate_id_does_not_overwrite_and_store_remains_usable |
| 目录过滤及排序 | 同目录多条不同时间记录、时间相同的记录、其他目录和子目录记录 | 仅返回精确目录匹配，最近更新优先，同时间按创建时间和 ID 稳定排序 | test_list_sessions_filters_exact_workspace_and_orders_deterministically |
| 路径规范化和 ID 保留 | 相对数据库路径、临时 HOME 下的 ~/project/../project、带首尾空格的非空 ID | 同一规范目录能查到，数据库使用绝对路径，ID 不被修改 | test_paths_are_normalized_without_changing_thread_id |
| 非法 ID | 空串、全空白，数据库尚不存在 | 连接之前抛 ValueError，不创建数据库 | test_insert_rejects_blank_id_before_database_access |
| 更新时间持久化与隔离 | 固定旧时间，更新正常 ID 或含空格、引号的 ID，再用新 Store 查询 | 返回当前 UTC 秒时间且已提交；只改目标 updated_at，创建时间和其他字段不变，其他会话不变，列表按新修改时间重排 | test_update_session_updated_at_persists_and_only_changes_target_timestamp |
| 更新不存在的记录 | 已有表但 ID 不存在 | ValueError，不插入记录 | test_update_session_updated_at_rejects_missing_record_without_inserting |
| 更新空 ID | 空串或全空白，数据库不存在 | 连接前抛 ValueError，不创建数据库 | test_update_session_updated_at_rejects_blank_id_before_database_access |
| 数据库缺失 | 四个公开异步方法访问不存在的文件 | OperationalError 原样传播，不创建新文件 | test_missing_database_is_not_created |
| 表缺失 | 已有空库但没有 sessions 表，分别调用四个方法 | 报 no such table，不自动建表或迁移 | test_missing_table_is_reported_without_initializing_schema |

## 隔离方式

所有 SQLite 文件、工作目录均在 tmp_path 中；测试用同步 sqlite3 显式建表，应用接口用真实 aiosqlite 读写，不 mock 数据库，以验证提交、约束和 SQL 行为。每次连接均被关闭，不保留后台工作线程。

依赖 [Coding Agent 共享 fixture](../../tests/coding_agent/conftest.py) 隔离配置根目录与 cwd，路径测试额外将 HOME 指向临时目录。不读取真实凭据，不请求模型或 MCP 服务，不访问用户现有数据库。排序通过固定时间数据验证；更新时间以调用前后的 UTC 秒时间为范围，并重新打开数据库检查提交及字段保留，不依赖 sleep。

## 执行

```bash
uv run pytest -q tests/coding_agent/test_store.py --tb=no
basedpyright --pythonpath .venv/bin/python src/omt/coding_agent/store.py tests/coding_agent/test_store.py
```

## 覆盖限制

- 本页不验证 CLI 命令、会话切换或 Checkpointer 恢复；/resume 的接入由 [恢复交互测试](resume.md) 验证，Store 本身不负责加载消息。
- 不负责建库、建表或迁移，不验证已有用户数据库的实际表结构。
- 不提供数据库与文件工具副作用之间的事务，也不保证会话索引插入与检查点写入原子提交。
- 未覆盖多进程争用、数据库损坏、只读权限或磁盘故障；此类数据库异常不被吞掉。
- resolve 不是目录沙箱；目录移动、Git 根目录归并和跨目录授权不属于本模块功能。
