-- 中小农户智能财务管理系统 · 数据库结构（SQLite）
-- 对应 docs/技术方案V2.md §5.2。约定：
--   · 金额一律整数「分」；数量/面积用 TEXT 存定点十进制字符串，绝不存 float；
--   · 事件只增不改（改 = 作废 + 新记），报表由事件重算，不存结果账；
--   · 敏感字段（姓名、手机号）以 SM4-GCM 密文存 BLOB，明文只在内存中出现。

PRAGMA foreign_keys = ON;

-- ---------- 元信息与计数器 ----------

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS counter (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

-- ---------- 主体与地块 ----------

CREATE TABLE IF NOT EXISTS village (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    region_code   TEXT NOT NULL,
    rule_version  TEXT NOT NULL,
    param_version TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS farmer (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token       TEXT NOT NULL UNIQUE,
    name_enc    BLOB NOT NULL,
    group_name  TEXT,
    phone_enc   BLOB,
    note        TEXT,
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plot (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    farmer_id        INTEGER NOT NULL REFERENCES farmer(id),
    name             TEXT,
    area_mu          TEXT NOT NULL,
    tenure           TEXT NOT NULL CHECK (tenure IN ('owned', 'rented')),
    crop             TEXT NOT NULL,
    rent_fen_per_mu  INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plot_farmer ON plot(farmer_id);

-- 自有农机年度作业量：折旧按工作量法计提的依据。
-- 不把它塞进事件表，是因为"作业量"不是一笔现金流，混进事件会污染成本归集。
CREATE TABLE IF NOT EXISTS machinery_use (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    farmer_id             INTEGER NOT NULL REFERENCES farmer(id),
    period                TEXT NOT NULL,
    name                  TEXT NOT NULL,
    mu                    TEXT NOT NULL,
    prior_depreciation_fen INTEGER NOT NULL DEFAULT 0,
    year_index            INTEGER NOT NULL DEFAULT 1,
    updated_at            TEXT NOT NULL,
    UNIQUE (farmer_id, period, name)
);

-- ---------- 事件（核心事实表） ----------

CREATE TABLE IF NOT EXISTS event (
    id                 TEXT PRIMARY KEY,
    seq                INTEGER NOT NULL UNIQUE,
    farmer_id          INTEGER NOT NULL REFERENCES farmer(id),
    plot_id            INTEGER REFERENCES plot(id),
    date               TEXT NOT NULL,
    type               TEXT NOT NULL CHECK (
                           type IN ('income', 'variable_cost', 'fixed_cost', 'asset', 'liability')
                       ),
    category           TEXT NOT NULL,
    amount_fen         INTEGER NOT NULL CHECK (amount_fen >= 0),
    qty                TEXT,
    unit               TEXT,
    counterparty_token TEXT,
    confidence         REAL NOT NULL DEFAULT 1.0,
    evidence_id        TEXT REFERENCES evidence(id),
    source             TEXT NOT NULL CHECK (source IN ('ocr', 'asr', 'manual', 'import')),
    state              TEXT NOT NULL CHECK (state IN ('draft', 'confirmed', 'voided')),
    estimated          INTEGER NOT NULL DEFAULT 0,
    note               TEXT,
    created_by         TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_farmer_date ON event(farmer_id, date);
CREATE INDEX IF NOT EXISTS idx_event_state ON event(state);
CREATE INDEX IF NOT EXISTS idx_event_category ON event(category);

-- ---------- 原始凭证（文件加密存放，库里只留哈希与相对路径） ----------

CREATE TABLE IF NOT EXISTS evidence (
    id            TEXT PRIMARY KEY,
    content_hash  TEXT NOT NULL,
    hash_alg      TEXT NOT NULL DEFAULT 'SM3',
    rel_path      TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('image', 'pdf', 'audio', 'xlsx', 'csv', 'other')),
    size          INTEGER NOT NULL,
    original_name TEXT,
    captured_at   TEXT NOT NULL,
    created_by    TEXT NOT NULL
);

-- ---------- 规则包与参数包（版本化、不可变） ----------

CREATE TABLE IF NOT EXISTS rule_pack (
    version        TEXT PRIMARY KEY,
    json           TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    note           TEXT
);

CREATE TABLE IF NOT EXISTS param_pack (
    version     TEXT NOT NULL,
    region_code TEXT NOT NULL,
    json        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    note        TEXT,
    PRIMARY KEY (region_code, version)
);

-- ---------- 快照与成品报表 ----------

CREATE TABLE IF NOT EXISTS snapshot (
    id                TEXT PRIMARY KEY,
    period            TEXT NOT NULL,
    farmer_id         INTEGER NOT NULL REFERENCES farmer(id),
    event_ids_json    TEXT NOT NULL,
    inputs_json       TEXT NOT NULL,
    event_set_hash    TEXT NOT NULL,
    merkle_root       TEXT NOT NULL,
    rule_version      TEXT NOT NULL,
    param_version     TEXT NOT NULL,
    output_json       TEXT NOT NULL,
    snapshot_json     TEXT NOT NULL,
    signature         TEXT,
    public_key        TEXT,
    signed_by         TEXT NOT NULL,
    device_id         TEXT,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshot_farmer_period ON snapshot(farmer_id, period);

CREATE TABLE IF NOT EXISTS report (
    id          TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshot(id),
    template    TEXT NOT NULL,
    format      TEXT NOT NULL,
    rel_path    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    created_by  TEXT NOT NULL
);

-- ---------- 审计哈希链 ----------

CREATE TABLE IF NOT EXISTS audit_log (
    seq            INTEGER PRIMARY KEY,
    ts             TEXT NOT NULL,
    actor          TEXT NOT NULL,
    action         TEXT NOT NULL,
    object_ref     TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    prev_hash      TEXT NOT NULL,
    hash           TEXT NOT NULL
);

-- ---------- 代币映射（对外只用 token，本地保留可逆密文） ----------

CREATE TABLE IF NOT EXISTS token_map (
    token      TEXT PRIMARY KEY,
    cipher     BLOB NOT NULL,
    scope      TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- ---------- 账号与会话 ----------

CREATE TABLE IF NOT EXISTS user (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'clerk', 'viewer')),
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS session (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES user(id),
    csrf       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    user_agent TEXT
);

-- ---------- 受控外发记录（谁、何时、发往何处、包含哪些字段） ----------

CREATE TABLE IF NOT EXISTS outbound (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    actor       TEXT NOT NULL,
    purpose     TEXT NOT NULL,
    recipient   TEXT NOT NULL,
    snapshot_id TEXT,
    fields_json TEXT NOT NULL,
    rel_path    TEXT,
    decision    TEXT NOT NULL CHECK (decision IN ('blocked', 'approved', 'cancelled')),
    audit_seq   INTEGER,
    note        TEXT
);
