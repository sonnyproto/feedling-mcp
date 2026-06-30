"""agent_runtime_instances.user_id FK → users (ON DELETE CASCADE)

Revision ID: 0009_agent_runtime_user_fk
Revises: 0008_genesis_imports
Create Date: 2026-06-30

孤儿/僵尸 instance 行的根因修复。

账号 reset(backend/content/routes.py 先 delete_user 删 users 行,再 delete_user_data
清所有 per-user 表)与 supervisor 的 discover→acquire→spawn tick 之间存在 TOCTOU race:
tick 用「删号前快照的 roster」跑到一半,在 leases.acquire 的 INSERT ON CONFLICT 里
**重建**已删账号的 instance 行,spawn 一个账号已不存在的 consumer;它因 whoami/读 blob
失败立刻退出,下个 tick 被 reap → release() 成 (status=idle, lease_owner=NULL,
lease_expires_at=NULL, pid=NULL) 的孤儿。该用户不再进 roster,行永久静止。

agent_runtime_instances 此前只有主键、没有指向 users 的外键,于是:
  (1) 删 users **不级联**清掉 instance 行;
  (2) acquire 能在 users 不存在时照常 INSERT。
两者叠加正是孤儿的成因。本迁移补上外键:
  - 删 users 自动级联删该用户的 instance 行(再不残留);
  - race 中对已删账号的 acquire INSERT 被 FK 拒绝(配套 leases.acquire 捕获
    ForeignKeyViolation 返回 False,tick 静默跳过)。

升级先清掉现存孤儿(user_id 不在 users —— 否则约束无法创建),再加约束。幂等。
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0009_agent_runtime_user_fk"
down_revision = "0008_genesis_imports"
branch_labels = None
depends_on = None


_UP = """
-- 1) 清掉删号 race 留下的孤儿行(对应 users 已不存在),否则 FK 创建会失败。
DELETE FROM agent_runtime_instances a
 WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.user_id = a.user_id);

-- 2) 补外键:删 users 级联清 instance 行;对已删账号的 acquire INSERT 被拒。
ALTER TABLE agent_runtime_instances
    DROP CONSTRAINT IF EXISTS agent_runtime_instances_user_id_fkey;
ALTER TABLE agent_runtime_instances
    ADD CONSTRAINT agent_runtime_instances_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE;
"""

_DOWN = """
ALTER TABLE agent_runtime_instances
    DROP CONSTRAINT IF EXISTS agent_runtime_instances_user_id_fkey;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
