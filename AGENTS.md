# 规则（所有人/所有 agent，含未来迁移与重新部署）

## 永久表 `public.sel_keyword_log`（043，关键词查询记录）
- **禁止** `DROP` / `TRUNCATE` / `DELETE` / `UPDATE` 此表或其行；禁止 `drop ... cascade` 连带删除；禁止禁用或删除触发器 `sel_keyword_log_no_change`、`sel_keyword_log_no_truncate`、事件触发器 `sel_keyword_log_no_drop`。
- 任何清理（`sel_purge_older_than` 3 天清理、`sel_delete_run` 逐条删除、`ehunt_lookup_purge` 等）都不得引用此表；不得给它加指向 `sel_runs` / `auth.users` 等可删除表的外键（会被 on delete cascade 波及）。
- 迁移只能用 `create table if not exists` / `add column if not exists` 增量修改；"重建数据库""重置"脚本必须跳过此表。
- 改结构需 Hongyan 书面批准。
