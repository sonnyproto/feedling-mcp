"""per-user 表补 users FK (ON DELETE CASCADE) — 让删号原子化

Revision ID: 0012_per_user_cascade_fk
Revises: 0011_world_book_entries
Create Date: 2026-07-03

根因：account_reset 里 delete_user(删 users 行) 与 delete_user_data(删 per-user 行)
不在一个事务，delete_user_data 回滚会留下半删(users 行没了、token/记忆/日志残留)。
只有 agent_runtime_instances 有 users FK(0009)。本迁移给其余 8 张 per-user 表补
ON DELETE CASCADE FK，删 users 行即原子级联清净，delete_user_data 退化为冗余兜底。

genesis_import_chunks/outputs 已 CASCADE 引 genesis_import_jobs；给 jobs 加到 users
的 FK 后整条链补全(删 users→级联 jobs→级联 chunks/outputs)。

加约束前先清"users 已不存在"的孤儿(否则 ADD CONSTRAINT 校验失败)。幂等。

NOTE: 本迁移原为 0011_per_user_cascade_fk；因 test 分支并入了同号的
0011_world_book_entries 导致 alembic 双头，改挂其后重编号为 0012（内容不变）。
"""

from alembic import op

revision = "0012_per_user_cascade_fk"
down_revision = "0011_world_book_entries"
branch_labels = None
depends_on = None

_TABLES = [
    "chat_messages", "frame_envelopes", "memory_moments", "perception_daily",
    "perception_items", "user_blobs", "user_logs", "genesis_import_jobs",
]


def upgrade() -> None:
    for t in _TABLES:
        # 1) 清孤儿(删 genesis_import_jobs 孤儿会级联清其 chunks/outputs)
        op.execute(
            f"DELETE FROM {t} a "
            f"WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.user_id = a.user_id);"
        )
        # 2) 幂等补 FK
        op.execute(f"ALTER TABLE {t} DROP CONSTRAINT IF EXISTS {t}_user_id_fkey;")
        op.execute(
            f"ALTER TABLE {t} ADD CONSTRAINT {t}_user_id_fkey "
            f"FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE;"
        )


def downgrade() -> None:
    for t in _TABLES:
        op.execute(f"ALTER TABLE {t} DROP CONSTRAINT IF EXISTS {t}_user_id_fkey;")
