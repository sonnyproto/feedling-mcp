"""model_api 多配置：credentials + routes 两张表。

取代单条 user_blobs(kind='model_api')。credentials 一把 key 一行（含 envelope 密文），
routes 是 (credential, model) 的组合，partial unique index 强制每用户恰一条 is_active。

user_blobs 的 model_api blob 原样保留、新代码不读不写——它是回滚快照。等新镜像稳定
运行后另开 PR 删除。

Revision ID: 0014_model_api_profiles
"""
from alembic import op

revision = "0014_model_api_profiles"
down_revision = "0013_genesis_resident_claim"
branch_labels = None
depends_on = None

_DDL = """
CREATE TABLE IF NOT EXISTS model_api_credentials (
    id                 UUID PRIMARY KEY,
    user_id            TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    provider           TEXT NOT NULL,
    label              TEXT NOT NULL DEFAULT '',
    base_url           TEXT NOT NULL DEFAULT '',
    api_key_envelope   JSONB NOT NULL,
    api_key_hint       TEXT NOT NULL DEFAULT '',
    supports_responses BOOLEAN NOT NULL DEFAULT FALSE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT model_api_credentials_user_id_uniq UNIQUE (user_id, id)
);

-- 刻意 NOT 加 (user_id, provider, base_url) 唯一索引：iOS 支持同一 provider、
-- 同一 base_url 下存多把不同的 key。setup 的幂等由代码锚定 active credential。

CREATE TABLE IF NOT EXISTS model_api_routes (
    id                       UUID PRIMARY KEY,
    user_id                  TEXT NOT NULL,
    credential_id            UUID NOT NULL,
    model                    TEXT NOT NULL,
    reasoning_effort         TEXT,
    thinking_fallback        BOOLEAN,
    is_active                BOOLEAN NOT NULL DEFAULT FALSE,
    test_status              TEXT NOT NULL DEFAULT 'untested',
    last_test_at             TIMESTAMPTZ,
    last_test_error          TEXT NOT NULL DEFAULT '',
    last_runtime_error       TEXT NOT NULL DEFAULT '',
    last_runtime_error_class TEXT NOT NULL DEFAULT '',
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT model_api_routes_user_fkey
        FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE,
    CONSTRAINT model_api_routes_credential_fkey
        FOREIGN KEY (user_id, credential_id)
        REFERENCES model_api_credentials (user_id, id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS model_api_routes_one_active
    ON model_api_routes (user_id) WHERE is_active;
CREATE UNIQUE INDEX IF NOT EXISTS model_api_routes_uniq
    ON model_api_routes (credential_id, model);
"""

# 回填：每个有信封的用户一条 credential + 一条 active route。
# 两条 INSERT 的守卫语义对称，统一为「只对尚无任何 credential/route 的用户回填」：
#   - credentials 靠 NOT EXISTS(该用户已有 credential) —— 没有
#     (user_id,provider,base_url) 唯一索引可撞了，因为 iOS 允许同 provider/
#     base_url 存多把 key。
#   - routes 靠 NOT EXISTS(该用户已有 route)，避免复跑时撞 model_api_routes_one_active
#     partial unique index（新插入 route 硬编码 is_active=TRUE，用户已有 active
#     route 就会 abort 整条语句）；同时防止 credentials 跳过、routes 却按
#     (provider,base_url) 匹配到别的 credential 而静默丢/错插数据。
#   - 额外保留 routes 的 ON CONFLICT (credential_id, model) DO NOTHING，防的是
#     另一件事（同 credential+model 重复）。
# 公开给测试直接复用（见 tests/test_model_api_profiles_migration.py），迁移本身
# 也执行同一份 SQL。
BACKFILL_SQL = """
INSERT INTO model_api_credentials
    (id, user_id, provider, label, base_url, api_key_envelope, api_key_hint, supports_responses)
SELECT gen_random_uuid(),
       b.user_id,
       LOWER(COALESCE(b.doc->>'provider', '')),
       INITCAP(REPLACE(COALESCE(b.doc->>'provider', 'provider'), '_', ' ')),
       COALESCE(b.doc->>'base_url', ''),
       b.doc->'api_key_envelope',
       COALESCE(b.doc->>'api_key_hint', ''),
       COALESCE(b.doc->>'supports_responses', '') = 'true'
FROM user_blobs b
JOIN users u ON u.user_id = b.user_id
WHERE b.kind = 'model_api'
  AND b.doc ? 'api_key_envelope'
  AND jsonb_typeof(b.doc->'api_key_envelope') = 'object'
  AND NOT EXISTS (
        SELECT 1 FROM model_api_credentials c WHERE c.user_id = b.user_id
      );

INSERT INTO model_api_routes
    (id, user_id, credential_id, model, reasoning_effort, thinking_fallback, is_active,
     test_status, last_test_at)
SELECT gen_random_uuid(),
       c.user_id,
       c.id,
       COALESCE(b.doc->>'model', ''),
       NULLIF(COALESCE(b.doc->>'reasoning_effort', ''), ''),
       CASE WHEN b.doc ? 'thinking_fallback'
            THEN (b.doc->>'thinking_fallback')::boolean
            ELSE NULL END,
       TRUE,
       COALESCE(NULLIF(b.doc->>'test_status', ''), 'untested'),
       -- 源值是 naive UTC ISO 串（core/util._now_iso() 产出），显式按 UTC 解释，
       -- 不依赖会话 TimeZone GUC（env.py/DATABASE_URL/Dockerfile 都没钉死时区）。
       NULLIF(b.doc->>'last_test_at', '')::timestamp AT TIME ZONE 'UTC'
FROM model_api_credentials c
JOIN user_blobs b
  ON b.user_id = c.user_id
 AND b.kind = 'model_api'
 AND LOWER(COALESCE(b.doc->>'provider', '')) = c.provider
 AND COALESCE(b.doc->>'base_url', '') = c.base_url
WHERE NOT EXISTS (
        SELECT 1 FROM model_api_routes r WHERE r.user_id = b.user_id
      )
ON CONFLICT (credential_id, model) DO NOTHING;
"""

_UP = _DDL + BACKFILL_SQL

_DOWN = """
DROP TABLE IF EXISTS model_api_routes;
DROP TABLE IF EXISTS model_api_credentials;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
