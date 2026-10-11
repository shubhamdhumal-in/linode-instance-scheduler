
CREATE TABLE IF NOT EXISTS schedule_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    timezone TEXT NOT NULL,
    rules TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    account_holidays TEXT NOT NULL DEFAULT 'follow',
    depends_on_group_id INTEGER REFERENCES schedule_groups(id)
);

CREATE TABLE IF NOT EXISTS group_dependencies (
    group_id INTEGER NOT NULL REFERENCES schedule_groups(id) ON DELETE CASCADE,
    depends_on_group_id INTEGER NOT NULL REFERENCES schedule_groups(id),
    PRIMARY KEY (group_id, depends_on_group_id),
    CHECK (group_id != depends_on_group_id)
);

CREATE TABLE IF NOT EXISTS instances (
    name TEXT PRIMARY KEY,
    label TEXT,
    region TEXT,
    network_interface_model TEXT
        CHECK (network_interface_model IS NULL OR network_interface_model IN ('legacy_config', 'linode')),
    network_config TEXT,
    network_helper_enabled INTEGER,
    vpc_prefix INTEGER,
    os_volume_id INTEGER,
    data_volumes TEXT,
    reserved_ip TEXT,
    authorized_keys TEXT,
    tags TEXT,
    instance_attrs TEXT,
    group_id INTEGER REFERENCES schedule_groups(id),
    current_linode_id INTEGER,
    current_status TEXT
        CHECK (current_status IS NULL OR current_status IN ('running', 'stopped', 'unreachable', 'needs_manual_recovery')),
    transitioning INTEGER NOT NULL DEFAULT 0,
    manual_override_expires_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS schedules (
    instance_name TEXT PRIMARY KEY REFERENCES instances(name) ON DELETE CASCADE,
    timezone TEXT NOT NULL,
    rules TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS schedule_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_name TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('create', 'delete')),
    triggered_by TEXT NOT NULL CHECK (triggered_by IN ('schedule', 'manual', 'api')),
    timestamp TIMESTAMP NOT NULL,
    result TEXT NOT NULL,
    error_message TEXT,
    actor TEXT
);

CREATE TABLE IF NOT EXISTS locks (
    name TEXT PRIMARY KEY,
    locked_at TIMESTAMP NOT NULL,
    locked_by TEXT
);

CREATE TABLE IF NOT EXISTS migrations (
    name TEXT PRIMARY KEY,
    phase TEXT,
    checkpoint TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS orphaned_migration_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    dest_volume_id INTEGER,
    archived_at TIMESTAMP NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_tokens (
    name TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    token_prefix TEXT NOT NULL,
    scopes TEXT NOT NULL,
    instances TEXT,
    groups TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS api_sessions (
    token TEXT PRIMARY KEY,
    linode_username TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    expires_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS instance_settings (
    instance_name TEXT PRIMARY KEY REFERENCES instances(name) ON DELETE CASCADE,
    schedule_mode TEXT NOT NULL DEFAULT 'auto' CHECK (schedule_mode IN ('auto', 'manual')),
    account_holidays TEXT
);

CREATE TABLE IF NOT EXISTS instance_hooks (
    instance_name TEXT PRIMARY KEY REFERENCES instances(name) ON DELETE CASCADE,
    config TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS group_hooks (
    group_id INTEGER PRIMARY KEY REFERENCES schedule_groups(id) ON DELETE CASCADE,
    config TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL CHECK (target IN ('instance', 'group')),
    instance_name TEXT NOT NULL,
    hook TEXT NOT NULL CHECK (hook IN ('pre_stop', 'post_start', 'config')),
    triggered_by TEXT NOT NULL CHECK (triggered_by IN ('schedule', 'manual', 'api')),
    actor TEXT,
    timestamp TIMESTAMP NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('success', 'failure', 'skipped', 'warning', 'changed')),
    exit_code INTEGER,
    output_tail TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS hook_events_by_instance ON hook_events (instance_name, timestamp);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('info', 'warning', 'error')),
    source TEXT NOT NULL,
    instance_name TEXT,
    action TEXT,
    actor TEXT,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS activity_log_by_instance ON activity_log (instance_name, id);
CREATE INDEX IF NOT EXISTS activity_log_by_time ON activity_log (timestamp);

CREATE TABLE IF NOT EXISTS scheduler_heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_tick TIMESTAMP NOT NULL,
    interval_seconds INTEGER,
    instances_checked INTEGER NOT NULL,
    fired INTEGER NOT NULL,
    failed INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS migration_backups (
    name TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    record TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS system_backup_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    finished_at TIMESTAMP NOT NULL,
    ok INTEGER NOT NULL,
    trigger TEXT,
    object_storage_key TEXT,
    local_path TEXT,
    problems TEXT
);

CREATE TABLE IF NOT EXISTS override_sources (
    instance_name TEXT PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('instance', 'group', 'hold')),
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS holidays (
    date TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('all', 'group', 'instance')),
    target TEXT NOT NULL DEFAULT '',
    note TEXT,
    created_at TIMESTAMP NOT NULL,
    PRIMARY KEY (date, scope, target)
);

CREATE TABLE IF NOT EXISTS deployment_secrets (
    name TEXT PRIMARY KEY,
    blob BLOB NOT NULL,
    fingerprint TEXT NOT NULL,
    public_key TEXT,
    object_storage_fingerprint TEXT,
    linode_ssh_key_id INTEGER,
    escrow_stackscript_id INTEGER,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS continuous_backup (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    changes INTEGER NOT NULL DEFAULT 0,
    backed_up INTEGER NOT NULL DEFAULT 0,
    last_backup_at TEXT,
    last_attempt_at TEXT,
    last_key TEXT,
    last_error TEXT
);
INSERT OR IGNORE INTO continuous_backup (id, changes, backed_up) VALUES (1, 0, 0);

CREATE TABLE IF NOT EXISTS alert_state (
    key TEXT PRIMARY KEY,
    message TEXT NOT NULL,
    last_sent_at TEXT
);

CREATE TABLE IF NOT EXISTS env_backup_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    fingerprint TEXT,
    uploaded_at TEXT,
    last_attempt_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS backup_verify_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    checked_at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    snapshot_key TEXT,
    problems TEXT
);

CREATE TABLE IF NOT EXISTS scheduler_lease_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    role TEXT NOT NULL,
    holder_host TEXT,
    holder_renewed_at TEXT,
    updated_at TEXT NOT NULL
);
