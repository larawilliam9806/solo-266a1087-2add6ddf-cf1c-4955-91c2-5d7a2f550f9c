# 高校成果库 · 作者身份归并

跨系统作者记录归并工具：导入 CSV、逐对判定“同一人 / 不同人”、撤回判定、查看当前分组，并导出 `record_id → group_id` 映射供论文关联使用。Flask + SQLite，单容器可运行。

## 快速启动（Docker Compose）

```bash
docker compose up --build        # http://localhost:8000
```

数据保存在命名卷 `identity-data` 的 `/data/identity.db`，`docker compose down` 后数据仍在（`down -v` 才会清除）。

## 本地直接运行（无 Docker）

```bash
python3 -m pip install -r requirements.txt
DB_PATH=./data/identity.db python3 app.py     # http://localhost:5000
# 或生产模式：
DB_PATH=./data/identity.db gunicorn -b 0.0.0.0:8000 --workers 1 --threads 8 app:app
```

## 演示流程（约 2 分钟）

1. 打开首页，点击「下载示例 CSV」，再把它上传导入（10 条记录，来自 CNKI / 万方 / Scopus / Crossref）。
2. 浏览「记录」表（可按 ID / 姓名 / 单位筛选），确认示例中的跨库同名记录：
   - `CNKI-1001`、`WANFANG-2001`、`SCOPUS-3001`（清华·张伟）→ 判为同一人。
   - `CROSSREF-4001`、`CNKI-1002`（北大·李娜）→ 判为同一人；`WANFANG-2002`（北大·李娜，生命科学学院）→ 判为不同人。
   - `CROSSREF-4002`、`SCOPUS-3002`（复旦·王芳）→ 判为同一人。
3. 勾选两条记录（自动填入下拉框）→ 点「判定为同一人 / 不同人」。
4. 「当前分组」实时显示合并结果；点任意判定行的「撤回」，分组立即按其余判定重算。
5. 点「下载映射 CSV」得到 `record_id,group_id` 文件。

### 值得演示的规则

| 规则 | 操作 | 预期 |
| --- | --- | --- |
| 传递 | A–B 同一人，B–C 同一人 | A、B、C 同组 |
| 冗余直判仍保存 | A、C 已在同组后再判 A–C 同一人 | 判定列表新增一条，分组不变 |
| 撤回不丢后续判定 | 建 A–B、B–C、A–C 三条同一人后撤回 B–C | A、B、C 仍同组（A–C 直判生效）；再撤回 A–C，则分为 {A,B} 与 {C} |
| 冲突指出记录对 | 先判 X–Y 不同人，再构造让 X、Y 间接同组，然后判跨组同一人 | 红色提示列出冲突记录对，判定不保存，分组不变 |
| 并发 | 两个请求同时尝试合并被“不同人”约束隔开的两组 | 最多一个成功；库中不出现“不同人同组” |

## 数据格式

导入 CSV 必需四列（表头可用中文或英文，大小写不敏感，支持带 BOM 的 UTF-8 与 GB18030）：

```
record_id,source,name,org
CNKI-1001,CNKI,张伟,清华大学计算机科学与技术系
```

也接受 `唯一记录ID,来源,姓名,单位`。按 `record_id` 幂等导入：再次导入同 ID 会更新姓名/单位；文件内重复 ID 以最后一行为准。任一数据行缺值则整批拒绝，不写入半成品。

导出 CSV 为 `record_id,group_id`，`group_id` 取组内字典序最小的记录 ID，便于稳定对照。

## 实现要点

- **判定是可撤销的追加日志**：`verdicts` 表保存每条判定（`same`/`different`），撤回只写 `revoked_at`，不删除；分组不落库，每次从“生效判定”重新做并查集。因此撤回某条后，其余判定（包括之后新增的）都自然保留。
- **同对唯一有效判定**：`UNIQUE INDEX ... WHERE revoked_at IS NULL` 保证同一记录对同时只有一条生效判定；对同一对改判会撤回旧行再插入新行（历史仍可审计）。
- **约束检查与写入同事务**：`decide()` 在 `BEGIN IMMEDIATE` 事务内计算合并后的连通分量并查找落在其中的 `different` 对，冲突则 `ROLLBACK` 并返回冲突对；进程内另有可重入锁。因此并发判定也无法越过约束。
- **下载一致性**：`export_rows()` 在单个读事务（`BEGIN`）中读取记录与生效判定，快照不受并发写入影响，不会混合变更前后的分组。
- **持久化**：SQLite WAL 模式；Flask 重启、容器重建（保留卷）后判定仍在。

## 测试

```bash
python3 -m pytest -q tests
```

覆盖：导入/幂等更新/坏文件拒绝、传递分组、冗余直判保存、撤回后重算且保留后续判定、直接与间接冲突拒绝并指出记录对、多线程并发冲突、导出快照一致性、重开数据库后持久化、重复判定去重。

## 已知限制

- 单批导入（一次一个 CSV），未做分页，记录数很大时页面会变慢。
- 冲突提示仅说明“判定未保存”；不提供冲突的批量自动修复，需要人工撤回相关判定。
- 没有登录/权限控制，部署到公网前需自行加认证与 HTTPS。
- 判定时间戳为 SQLite `datetime('now')`（UTC），界面标注为 UTC。
