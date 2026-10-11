export type InstanceStatus = 'running' | 'stopped' | 'unreachable' | 'needs_manual_recovery'
export interface DataVolume {
  volume_id: number
  device_slot: string
  fstab_identifier: string | null
}
export interface InstanceRecord {
  label: string | null
  region: string | null
  network_interface_model: string | null
  network_config: unknown
  authorized_keys: string[] | null
  tags: string[] | null
  instance_attrs: Record<string, unknown> | null
  network_helper_enabled: boolean | null
  vpc_prefix: number | null
  os_volume_id: number | null
  data_volumes: DataVolume[] | null
  reserved_ip: string | null
  group_id: number | null
  schedule_mode?: 'auto' | 'manual'
  holiday_today?: string
  account_holidays?: AccountHolidaySetting
  current_linode_id: number | null
  current_status: InstanceStatus
  transitioning: boolean
  manual_override_expires_at: string | null
}
export interface StartResult {
  outcome:
    | 'started'
    | 'already_running'
    | 'confirmed_gone_reset_to_stopped'
    | 'found_offline_marked_unreachable'
    | 'transitioning'
    | 'reachability_check_failed'
    | 'create_failed'
    | 'post_start_hook_failed'
  instance_id: number | null
  reserved_ip: string | null
  live_status: string | null
  security_warning: boolean
  detail: string | null
  manual_override_expires_at: string | null
  hook_output?: string | null
}
export const START_SUCCESS_OUTCOMES: ReadonlySet<StartResult['outcome']> = new Set([
  'started',
  'already_running',
])
export interface StopResult {
  outcome:
    | 'stopped'
    | 'already_stopped'
    | 'confirmed_gone_reset_to_stopped'
    | 'aborted_by_user'
    | 'prepare_failed'
    | 'pre_stop_hook_failed'
    | 'delete_failed'
  instance_id: number | null
  detail: string | null
  hook_output?: string | null
}
export const STOP_SUCCESS_OUTCOMES: ReadonlySet<StopResult['outcome']> = new Set([
  'stopped',
  'already_stopped',
  'confirmed_gone_reset_to_stopped',
])
export interface ScheduleRule {
  days_of_week: string[]
  start_time: string
  stop_time: string
}
export interface Schedule {
  timezone: string
  rules: ScheduleRule[]
  enabled: boolean
  warnings?: string[]
}
export interface ScheduleGroup {
  id: number
  name: string
  timezone: string
  rules: ScheduleRule[]
  enabled: boolean
  members: string[]
  depends_on?: string[]
  dependents?: string[]
  account_holidays?: 'follow' | 'ignore'
  warnings?: string[]
}
export interface ScheduleGroupSummary {
  id: number
  name: string
  timezone: string
  rules: ScheduleRule[]
  enabled: boolean
  member_count: number
  depends_on?: string[]
  account_holidays?: 'follow' | 'ignore'
}
export interface ScheduleEvent {
  action: 'create' | 'delete'
  triggered_by: 'schedule' | 'manual' | 'api'
  timestamp: string
  result: 'success' | 'failure'
  error_message: string | null
  actor: string | null
}
export interface LinodeRawInstance {
  id: number
  label: string
  region: string
  status: string
  tags: string[]
  ipv4: string[]
  onboarded_as: string | null
}
export interface OnboardResult {
  outcome: 'onboarded' | 'identity_change_declined'
  record: InstanceRecord | null
  warnings?: string[]
}
export interface OffboardResult {
  outcome: 'aborted_by_user' | 'incomplete' | 'offboarded'
  detail: string | null
  warnings?: string[]
}
export interface DeregisterResult {
  outcome: 'not_onboarded' | 'aborted_by_user' | 'deregistered'
}
export interface PatchInstanceGroupResult {
  group_id?: number | null
  outcome?: 'removed_had_individual_schedule' | 'removed_kept_manual' | 'removed_copied_schedule'
  warnings?: string[]
}
export interface MigrateStartResult {
  outcome: 'started' | 'identity_change_declined' | 'backup_declined'
  instance_id: number | null
  backup_instance_id?: number | null
  dest_volume_id: number | null
  dest_volume_size_gb: number | null
  local_disk_size_mb: number | null
}
export interface MigrateResumeResult {
  outcome: 'resumed' | 'incomplete'
  instance_id: number | null
  os_volume_id: number | null
  reserved_ip: string | null
  previous_attempts_count: number
  detail: string | null
  fstab_entries_disabled: string[]
}
export interface AssistedCopyReadiness {
  available: boolean
  reasons: string[]
  username: string | null
  gateway: string | null
  public_key: string | null
  key_registered: boolean
  keys_allowed: boolean
  backup_kept: boolean
  instance_id: number | null
}
export interface AssistedCopyResult {
  outcome: 'resumed' | 'copied' | 'resume_failed'
  resume: MigrateResumeResult | null
  detail: string | null
}
export interface MigrateStatus {
  in_progress: boolean
  phase?: string | null
  instance_id?: number | null
  dest_volume_id?: number | null
  dest_volume_size_gb?: number | null
  local_disk_size_mb?: number | null
}
export interface OperationStatus<T> {
  status: 'running' | 'done' | 'error'
  action: string
  percent: number
  current_step: string | null
  warnings: string[]
  result?: T
}
export interface Savings {
  scheduled_savings_percent: number | null
  actual_savings_percent: number | null
  window_days: number
  schedule_state?: 'none' | 'disabled' | 'active'
  note?: string
}
export const VALID_DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'] as const
export type Weekday = (typeof VALID_DAYS)[number]
export type HookFailurePolicy = 'abort' | 'continue'
export interface PreStopHook {
  command?: string
  script?: string
  timeout_s: number
  on_failure: HookFailurePolicy
}
export interface PostStartHook {
  command?: string
  script?: string
  timeout_s: number
}
export interface HookConfig {
  pre_stop: PreStopHook | null
  post_start: PostStartHook | null
}
export interface EffectiveHooks {
  pre_stop:
    | (PreStopHook & {
        source: 'instance' | 'group'
      })
    | null
  post_start:
    | (PostStartHook & {
        source: 'instance' | 'group'
      })
    | null
}
export interface HookEvent {
  hook: 'pre_stop' | 'post_start' | 'config'
  triggered_by: 'schedule' | 'manual' | 'api'
  actor: string | null
  timestamp: string
  result: 'success' | 'failure' | 'skipped' | 'warning' | 'changed'
  exit_code: number | null
  output_tail: string | null
  detail: string | null
}
export interface InstanceHooks {
  own: HookConfig | null
  effective: EffectiveHooks
  last_post_start_failure: HookEvent | null
}
export interface HookRunResult {
  ok: boolean
  summary: string
  exit_code: number | null
  output_tail: string | null
  attempts: number
}
export type ApiTokenBundle = 'read' | 'operate' | 'configure' | 'admin'
export type ApiTokenOperation =
  | 'instances:list'
  | 'instances:status'
  | 'instances:history'
  | 'savings:read'
  | 'activity:read'
  | 'instances:start'
  | 'instances:stop'
  | 'instances:extend'
  | 'groups:start'
  | 'groups:stop'
  | 'schedules:read'
  | 'schedules:write'
  | 'groups:read'
  | 'groups:write'
  | 'groups:membership'
  | 'dependencies:write'
  | 'mode:write'
  | 'hooks:read'
  | 'hooks:write'
  | 'hooks:run'
  | 'instances:onboard'
  | 'instances:migrate'
  | 'instances:offboard'
  | 'instances:vpc-address'
  | 'tokens:manage'
  | 'logs:read'
export type ApiTokenScope = ApiTokenBundle | ApiTokenOperation
export interface ApiToken {
  name: string
  token_prefix: string
  scopes: ApiTokenScope[]
  instances: string[] | null
  groups: string[] | null
  created_by: string | null
  created_at: string
  expires_at: string | null
  revoked_at: string | null
  last_used_at: string | null
}
export interface GroupMemberResult {
  name: string
  group: string
  outcome: string
  ok: boolean
  detail: string | null
  security_warning: boolean
}
export interface GroupActionResult {
  group: string
  action: 'start' | 'stop'
  stages: string[]
  members: GroupMemberResult[]
  ok: boolean
}
export type ActivityLevel = 'info' | 'warning' | 'error'
export interface ActivityEntry {
  id: number
  timestamp: string
  level: ActivityLevel
  source: string
  instance_name: string | null
  action: string | null
  actor: string | null
  message: string
}
export interface SchedulerStatus {
  running: boolean
  last_tick: string | null
  interval_seconds: number | null
  age_seconds?: number
  instances_checked?: number
  fired?: number
  failed?: number
  message: string | null
}
export interface ActivityResponse {
  entries: ActivityEntry[]
  scheduler: SchedulerStatus
}
export interface LogService {
  name: string
  exists: boolean
  size: number
  modified: string | null
}
export interface ConsoleSessionLog extends LogService {
  instance: string
}
export interface LogsIndex {
  services: LogService[]
  console_sessions?: ConsoleSessionLog[]
  database: {
    path: string
    size: number
    wal_size: number
    activity_retention_days: number
  }
  scheduler: SchedulerStatus
}
export interface LogTail {
  lines: string[]
  offset: number
  reset: boolean
}
export interface ConsoleRun {
  id: string
  command: string
  actor: string
  status: 'running' | 'done' | 'error' | 'cancelled'
  exit_code: number | null
  started_at: string
  finished_at: string | null
  lines: string[]
  next_offset: number
  truncated: boolean
}
export interface BackupEstimate {
  instance_monthly: number
  volumes_monthly: number
  total_monthly: number
  plan: string
  region: string
  volume_gb: number
}
export interface MigrationBackup {
  name: string
  status: 'creating' | 'kept' | 'restored' | 'failed'
  created_at: string
  updated_at: string
  original_instance_id: number | null
  original_label: string | null
  region: string | null
  plan: string | null
  public_ipv4: string[]
  backup_instance_id: number | null
  backup_label: string | null
  backup_volumes: {
    slot: string
    volume_id: number
    size: number
    source_volume_id: number
  }[]
  estimated_monthly_cost: BackupEstimate | null
  restored_at: string | null
}
export interface RollbackResult {
  outcome: 'restored' | 'aborted_by_user'
  backup_instance_id: number | null
  label: string | null
  public_ipv4: string[]
  original_deleted_instance_id: number | null
  reachable: boolean | null
  warnings: string[]
}
export interface BackupDeleteResult {
  outcome: 'deleted' | 'forgotten' | 'aborted_by_user' | 'incomplete'
  problems: string[]
}
export interface SystemBackupStatus {
  object_storage: {
    configured: boolean
    bucket: string | null
    endpoint: string | null
    access_key_hint: string | null
    secret_key_set: boolean
  }
  local_backup_dir: string | null
  last_backup: {
    finished_at: string
    ok: boolean
    trigger: string | null
    object_storage_key: string | null
    local_path: string | null
    problems: string[]
  } | null
  continuous?: {
    enabled: boolean
    pending_changes: number
    last_backup_at: string | null
    last_attempt_at: string | null
    last_key: string | null
    last_error: string | null
    min_interval_seconds: number
  } | null
  ssh_key?: {
    passphrase_set: boolean
    stored_in_database: boolean
    fingerprint: string | null
    in_object_storage: boolean
    linode_ssh_key_id: number | null
    updated_at: string | null
    passphrase_escrowed?: boolean
  } | null
}
export interface HealthStatus {
  problems: {
    key: string
    message: string
  }[]
  alerts: {
    webhook: boolean
    email: boolean
    email_to: string[]
  }
}
export interface KeyProtectionResult {
  fingerprint: string | null
  stored_in_database: boolean
  object_storage: string
  linode_ssh_key_id: number | null
  problems: string[]
  ok: boolean
}
export interface ScheduleMismatch {
  name: string
  current: 'running' | 'stopped'
  desired: 'running' | 'stopped'
  via_group: string | null
  missed_at: string | null
  changed_by_hand: boolean
  reason: string
}
export interface CatchUpResult {
  items: {
    name: string
    action: string
    outcome: string
    ok: boolean
    detail: string | null
  }[]
}
export interface MigrateAbandonResult {
  instance_id: number | null
  instance_state: string
  volume_id: number | null
  volume_state: string
  previous_attempts_archived: number
  backup_kept: boolean
  notes: string[]
}
export interface GroupExtendResult {
  group: string
  stops_at: string
  extended: string[]
  skipped: {
    name: string
    reason: string
  }[]
  dependencies_held: string[]
}
export interface Holiday {
  date: string
  scope: 'all' | 'group' | 'instance'
  target: string | null
  note: string | null
  created_at: string
}
export interface AccountHolidaySetting {
  own: 'follow' | 'ignore' | null
  effective: 'follow' | 'ignore'
  source: 'instance' | 'group' | 'default'
}
