import type {
  BackupDeleteResult,
  BackupEstimate,
  DeregisterResult,
  GroupExtendResult,
  Holiday,
  MigrationBackup,
  RollbackResult,
  SystemBackupStatus,
  KeyProtectionResult,
  HealthStatus,
  ScheduleMismatch,
  CatchUpResult,
  MigrateAbandonResult,
  HookConfig,
  HookEvent,
  HookRunResult,
  InstanceHooks,
  InstanceRecord,
  LinodeRawInstance,
  MigrateResumeResult,
  AssistedCopyReadiness,
  AccountHolidaySetting,
  AssistedCopyResult,
  MigrateStartResult,
  MigrateStatus,
  OffboardResult,
  OnboardResult,
  OperationStatus,
  PatchInstanceGroupResult,
  Savings,
  Schedule,
  ScheduleEvent,
  ScheduleGroup,
  ScheduleGroupSummary,
  StartResult,
  StopResult,
  ApiToken,
  ApiTokenScope,
  GroupActionResult,
  ActivityLevel,
  ActivityResponse,
  ConsoleRun,
  LogsIndex,
  LogTail,
} from './types'
export class ApiError extends Error {
  status: number
  body: Record<string, unknown> | null
  constructor(status: number, detail: string, body: Record<string, unknown> | null = null) {
    super(detail)
    this.body = body
    this.status = status
  }
}
export function errorWarnings(e: unknown): string[] | null {
  if (e instanceof ApiError && Array.isArray(e.body?.warnings) && e.body.warnings.length > 0) {
    return e.body.warnings as string[]
  }
  return null
}
let pollingCancelled = false
let pollingSession = 0
export class PollingCancelledError extends Error {
  constructor() {
    super('Cancelled: signed out.')
  }
}
export function cancelBackgroundPolling(): void {
  pollingCancelled = true
  pollingSession += 1
}
export function resetBackgroundPollingCancellation(): void {
  pollingCancelled = false
}
async function pollOperation<T>(
  operationId: string,
  onProgress?: (percent: number, currentStep: string | null) => void,
  onWarning?: (warnings: string[]) => void,
): Promise<T> {
  let lastReported: readonly [number, string | null] | null = null
  const session = pollingSession
  for (;;) {
    if (pollingCancelled || session !== pollingSession) throw new PollingCancelledError()
    const op = await request<OperationStatus<T>>('GET', `/operations/${encodeURIComponent(operationId)}`)
    if (lastReported === null || lastReported[0] !== op.percent || lastReported[1] !== op.current_step) {
      onProgress?.(op.percent, op.current_step)
      lastReported = [op.percent, op.current_step]
    }
    if (op.status === 'done') {
      if (op.warnings.length > 0) onWarning?.(op.warnings)
      return op.result as T
    }
    await new Promise((resolve) => setTimeout(resolve, 800))
  }
}
const SESSION_STORAGE_KEY = 'linode-scheduler-session-token'
export function getStoredToken(): string | null {
  return localStorage.getItem(SESSION_STORAGE_KEY)
}
export function storeToken(token: string): void {
  localStorage.setItem(SESSION_STORAGE_KEY, token)
}
export function clearStoredToken(): void {
  localStorage.removeItem(SESSION_STORAGE_KEY)
}
async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const token = getStoredToken()
  const headers: Record<string, string> = {}
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const res = await fetch(path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (res.status === 401) {
    clearStoredToken()
  }
  const text = await res.text()
  let data: unknown = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = null
    }
  }
  if (!res.ok) {
    const obj = data && typeof data === 'object' ? (data as Record<string, unknown>) : null
    const fromBody =
      obj && (typeof obj.detail === 'string' ? obj.detail : typeof obj.error === 'string' ? obj.error : null)
    const detail =
      fromBody ??
      (obj === null && text ? text.slice(0, 200) : null) ??
      `request failed with status ${res.status}`
    throw new ApiError(res.status, detail, obj)
  }
  return data as T
}
export const api = {
  listInstances: () => request<Record<string, InstanceRecord>>('GET', '/instances'),
  listLinodeInstances: () => request<LinodeRawInstance[]>('GET', '/linode/instances'),
  reserveIp: (address: string) =>
    request<{
      reserved: boolean
    }>('POST', `/linode/ips/${encodeURIComponent(address)}/reserve`),
  checkSshReachable: (instanceId: number, port: number) =>
    request<{
      reachable: boolean
      detail?: string
      host?: string
    }>('GET', `/linode/ssh-check?instance_id=${instanceId}&port=${port}`),
  onboardInstance: (body: {
    name: string
    instance_id: number
    vpc_id?: number | null
    force?: boolean
    ssh_private_key?: string | null
    ssh_password?: string | null
  }) =>
    request<OnboardResult>('POST', '/instances', {
      name: body.name,
      instance_id: body.instance_id,
      vpc_id: body.vpc_id ?? null,
      force: body.force ?? false,
      ssh_private_key: body.ssh_private_key ?? null,
      ssh_password: body.ssh_password ?? null,
    }),
  offboardInstance: (name: string, deleteVolumes: boolean) =>
    request<OffboardResult>('POST', `/instances/${encodeURIComponent(name)}/offboard`, {
      delete_volumes: deleteVolumes,
    }),
  setVpcAddress: (name: string, address: string, current: string | null = null) =>
    request<{
      name: string
      previous_address: string
      address: string
      warnings?: string[]
    }>('POST', `/instances/${encodeURIComponent(name)}/vpc-address`, { address, current }),
  deregisterInstance: (name: string) =>
    request<DeregisterResult>('POST', `/instances/${encodeURIComponent(name)}/deregister`),
  clearLock: (name: string) =>
    request<{
      name: string
      outcome: string
    }>('POST', `/instances/${encodeURIComponent(name)}/clear-lock`),
  abandonMigration: async (
    name: string,
    bootOriginal = true,
    onProgress?: (percent: number, currentStep: string | null) => void,
    onWarning?: (warnings: string[]) => void,
  ) => {
    const kickoff = await request<{
      operation_id: string
    }>('POST', `/instances/${encodeURIComponent(name)}/migrate-abandon`, { boot_original: bootOriginal })
    return pollOperation<MigrateAbandonResult>(kickoff.operation_id, onProgress, onWarning)
  },
  getScheduleMismatches: () =>
    request<{
      mismatches: ScheduleMismatch[]
    }>('GET', '/schedule/missed'),
  applyScheduleCatchUp: async (
    names: string[],
    onProgress?: (percent: number, currentStep: string | null) => void,
    onWarning?: (warnings: string[]) => void,
  ) => {
    const kickoff = await request<{
      operation_id: string
    }>('POST', '/schedule/missed/apply', { names })
    return pollOperation<CatchUpResult>(kickoff.operation_id, onProgress, onWarning)
  },
  protectSshKey: () => request<KeyProtectionResult>('POST', '/system/ssh-key/protect'),
  getHealth: () => request<HealthStatus>('GET', '/system/health'),
  setEnvBackupRecipients: (recipients: string) =>
    request<{
      status: NonNullable<SystemBackupStatus['env_backup']>
      result: {
        outcome: string
        detail: string | null
      } | null
    }>('PUT', '/system/env-backup/recipients', { recipients }),
  runEnvBackup: () =>
    request<{
      outcome: string
      detail: string | null
      ssh_key_included: boolean
    }>('POST', '/system/env-backup/run'),
  testAlerts: () =>
    request<{
      results: Record<string, boolean>
    }>('POST', '/system/alerts/test'),
  getInstance: (name: string) =>
    request<InstanceRecord>('GET', `/instances/${encodeURIComponent(name)}/status`),
  getHistory: (name: string, limit = 50) =>
    request<ScheduleEvent[]>('GET', `/instances/${encodeURIComponent(name)}/history?limit=${limit}`),
  startInstance: async (
    name: string,
    overrideWindowHours?: number,
    onProgress?: (percent: number, currentStep: string | null) => void,
    onWarning?: (warnings: string[]) => void,
  ) => {
    const kickoff = await request<{
      operation_id: string
    }>('POST', `/instances/${encodeURIComponent(name)}/start`, {
      override_window_hours: overrideWindowHours ?? null,
    })
    return pollOperation<StartResult>(kickoff.operation_id, onProgress, onWarning)
  },
  stopInstance: async (
    name: string,
    opts?: {
      skipPrecapture?: boolean
      force?: boolean
      skipHooks?: boolean
    },
    onProgress?: (percent: number, currentStep: string | null) => void,
    onWarning?: (warnings: string[]) => void,
  ) => {
    const kickoff = await request<{
      operation_id: string
    }>('POST', `/instances/${encodeURIComponent(name)}/stop`, {
      skip_precapture: opts?.skipPrecapture ?? false,
      force: opts?.force ?? false,
      skip_hooks: opts?.skipHooks ?? false,
    })
    return pollOperation<StopResult>(kickoff.operation_id, onProgress, onWarning)
  },
  migrateStart: async (
    name: string,
    instanceId: number,
    creds?: {
      ssh_private_key: string | null
      ssh_password: string | null
    },
    onProgress?: (percent: number, currentStep: string | null) => void,
    onWarning?: (warnings: string[]) => void,
    force = false,
    backup = false,
  ) => {
    const kickoff = await request<{
      operation_id: string
    }>('POST', `/instances/${encodeURIComponent(name)}/migrate-start`, {
      instance_id: instanceId,
      ssh_private_key: creds?.ssh_private_key ?? null,
      ssh_password: creds?.ssh_password ?? null,
      force,
      backup,
    })
    return pollOperation<MigrateStartResult>(kickoff.operation_id, onProgress, onWarning)
  },
  getMigrateStatus: (name: string) =>
    request<MigrateStatus>('GET', `/instances/${encodeURIComponent(name)}/migrate-status`),
  migrateResume: async (
    name: string,
    onProgress?: (percent: number, currentStep: string | null) => void,
    onWarning?: (warnings: string[]) => void,
  ) => {
    const kickoff = await request<{
      operation_id: string
    }>('POST', `/instances/${encodeURIComponent(name)}/migrate-resume`)
    return pollOperation<MigrateResumeResult>(kickoff.operation_id, onProgress, onWarning)
  },
  getAssistedCopy: (name: string) =>
    request<AssistedCopyReadiness>('GET', `/instances/${encodeURIComponent(name)}/migrate-copy`),
  runAssistedCopy: async (
    name: string,
    onProgress?: (percent: number, currentStep: string | null) => void,
    onWarning?: (warnings: string[]) => void,
  ) => {
    const kickoff = await request<{
      operation_id: string
    }>('POST', `/instances/${encodeURIComponent(name)}/migrate-copy`)
    return pollOperation<AssistedCopyResult>(kickoff.operation_id, onProgress, onWarning)
  },
  getBackupEstimate: (instanceId: number) =>
    request<BackupEstimate>('GET', `/linode/instances/${instanceId}/backup-estimate`),
  listBackups: () => request<MigrationBackup[]>('GET', '/backups'),
  rollbackBackup: async (
    name: string,
    boot: boolean,
    onProgress?: (percent: number, currentStep: string | null) => void,
    onWarning?: (warnings: string[]) => void,
  ) => {
    const kickoff = await request<{
      operation_id: string
    }>('POST', `/backups/${encodeURIComponent(name)}/rollback`, { boot })
    return pollOperation<RollbackResult>(kickoff.operation_id, onProgress, onWarning)
  },
  deleteBackup: async (name: string, onProgress?: (percent: number, currentStep: string | null) => void) => {
    const kickoff = await request<{
      operation_id: string
    }>('DELETE', `/backups/${encodeURIComponent(name)}`)
    return pollOperation<BackupDeleteResult>(kickoff.operation_id, onProgress)
  },
  getSystemBackup: () => request<SystemBackupStatus>('GET', '/system/backup'),
  configureSystemBackup: (body: {
    bucket: string
    endpoint: string
    access_key: string
    secret_key: string
  }) => request<SystemBackupStatus>('PUT', '/system/backup/config', body),
  disableSystemBackup: () => request<SystemBackupStatus>('DELETE', '/system/backup/config'),
  testSystemBackup: () =>
    request<{
      ok: boolean
    }>('POST', '/system/backup/test'),
  runSystemBackup: async (onProgress?: (percent: number, currentStep: string | null) => void) => {
    const kickoff = await request<{
      operation_id: string
    }>('POST', '/system/backup/run')
    return pollOperation<{
      ok: boolean
      problems: string[]
      object_storage_key: string | null
      local_path: string | null
    }>(kickoff.operation_id, onProgress)
  },
  listHolidays: (all = false) => request<Holiday[]>('GET', `/holidays${all ? '?all=true' : ''}`),
  addHolidays: (body: {
    date: string
    to?: string | null
    group_name?: string | null
    name?: string | null
    note?: string | null
  }) =>
    request<{
      added: string[]
      warnings: string[]
    }>('POST', '/holidays', body),
  removeHolidays: (params: {
    date: string
    to?: string | null
    group_name?: string | null
    name?: string | null
  }) => {
    const q = new URLSearchParams({ date: params.date })
    if (params.to) q.set('to', params.to)
    if (params.group_name) q.set('group_name', params.group_name)
    if (params.name) q.set('name', params.name)
    return request<{
      removed: number
      warnings: string[]
    }>('DELETE', `/holidays?${q.toString()}`)
  },
  setGroupHolidaySetting: (group: string, mode: 'follow' | 'ignore') =>
    request<{
      account_holidays: string
      warnings?: string[]
    }>('PUT', `/groups/${encodeURIComponent(group)}/holiday-setting`, { account_holidays: mode }),
  setInstanceHolidaySetting: (name: string, mode: 'follow' | 'ignore' | 'inherit') =>
    request<
      AccountHolidaySetting & {
        warnings?: string[]
      }
    >('PUT', `/instances/${encodeURIComponent(name)}/holiday-setting`, { account_holidays: mode }),
  extendGroup: (name: string, hours: number) =>
    request<GroupExtendResult>('POST', `/groups/${encodeURIComponent(name)}/extend`, { hours }),
  extendOverride: (name: string, hours?: number) =>
    request<{
      manual_override_expires_at: string
    }>('POST', `/instances/${encodeURIComponent(name)}/extend`, { hours: hours ?? null }),
  getSchedule: (name: string) =>
    request<Schedule | null>('GET', `/instances/${encodeURIComponent(name)}/schedule`),
  setSchedule: (name: string, schedule: Schedule) =>
    request<Schedule>('POST', `/instances/${encodeURIComponent(name)}/schedule`, schedule),
  clearSchedule: (name: string) =>
    request<{
      cleared: boolean
      warnings?: string[]
    }>('DELETE', `/instances/${encodeURIComponent(name)}/schedule`),
  patchInstanceGroup: (
    name: string,
    body: {
      group_name: string | null
      copy_group_rules_as_individual?: boolean
    },
  ) => request<PatchInstanceGroupResult>('PATCH', `/instances/${encodeURIComponent(name)}`, body),
  setScheduleMode: (name: string, mode: 'auto' | 'manual') =>
    request<{
      schedule_mode: 'auto' | 'manual'
      warnings?: string[]
    }>('PATCH', `/instances/${encodeURIComponent(name)}`, { schedule_mode: mode }),
  listTokens: () => request<ApiToken[]>('GET', '/tokens'),
  createToken: (body: {
    name: string
    scopes: ApiTokenScope[]
    instances?: string[] | null
    groups?: string[] | null
    expires_days?: number | null
  }) =>
    request<
      ApiToken & {
        token: string
        warnings?: string[]
      }
    >('POST', '/tokens', body),
  revokeToken: (name: string) =>
    request<{
      revoked: boolean
      warnings?: string[]
    }>('DELETE', `/tokens/${encodeURIComponent(name)}`),
  groupAction: async (
    name: string,
    action: 'start' | 'stop',
    opts: {
      withDependencies?: boolean
    },
    onProgress?: (percent: number, currentStep: string | null) => void,
    onWarning?: (warnings: string[]) => void,
  ) => {
    const kickoff = await request<{
      operation_id: string
    }>('POST', `/groups/${encodeURIComponent(name)}/${action}`, {
      with_dependencies: opts.withDependencies ?? false,
    })
    return pollOperation<GroupActionResult>(kickoff.operation_id, onProgress, onWarning)
  },
  getInstanceSavings: (name: string, days = 7) =>
    request<Savings>('GET', `/instances/${encodeURIComponent(name)}/savings?days=${days}`),
  listGroups: () => request<ScheduleGroupSummary[]>('GET', '/groups'),
  getGroup: (name: string) => request<ScheduleGroup>('GET', `/groups/${encodeURIComponent(name)}`),
  createGroup: (name: string, timezone: string) =>
    request<ScheduleGroup>('POST', '/groups', { name, timezone }),
  deleteGroup: (name: string) =>
    request<{
      deleted: boolean
    }>('DELETE', `/groups/${encodeURIComponent(name)}`),
  setGroupDependencies: (name: string, dependsOn: string[]) =>
    request<ScheduleGroup>('PATCH', `/groups/${encodeURIComponent(name)}`, { depends_on: dependsOn }),
  setGroupSchedule: (name: string, schedule: Schedule) =>
    request<ScheduleGroup>('POST', `/groups/${encodeURIComponent(name)}/schedule`, schedule),
  getGroupSavings: (name: string, days = 7) =>
    request<Savings>('GET', `/groups/${encodeURIComponent(name)}/savings?days=${days}`),
  getInstanceHooks: (name: string) =>
    request<InstanceHooks>('GET', `/instances/${encodeURIComponent(name)}/hooks`),
  setInstanceHooks: (name: string, config: HookConfig) =>
    request<{
      own: HookConfig
      warnings?: string[]
    }>('PUT', `/instances/${encodeURIComponent(name)}/hooks`, config),
  clearInstanceHooks: (name: string) =>
    request<{
      cleared: boolean
    }>('DELETE', `/instances/${encodeURIComponent(name)}/hooks`),
  runInstanceHook: (name: string, hook: 'pre_stop' | 'post_start') =>
    request<HookRunResult>('POST', `/instances/${encodeURIComponent(name)}/hooks/run`, { hook }),
  getHookEvents: (name: string, limit = 20) =>
    request<HookEvent[]>('GET', `/instances/${encodeURIComponent(name)}/hook-events?limit=${limit}`),
  getGroupHooks: (name: string) =>
    request<{
      hooks: HookConfig | null
    }>('GET', `/groups/${encodeURIComponent(name)}/hooks`),
  setGroupHooks: (name: string, config: HookConfig) =>
    request<{
      hooks: HookConfig
      warnings?: string[]
    }>('PUT', `/groups/${encodeURIComponent(name)}/hooks`, config),
  clearGroupHooks: (name: string) =>
    request<{
      cleared: boolean
    }>('DELETE', `/groups/${encodeURIComponent(name)}/hooks`),
  getActivity: (
    params: {
      name?: string
      groupName?: string
      level?: ActivityLevel
      source?: string
      q?: string
      afterId?: number
      beforeId?: number
      limit?: number
    } = {},
  ) => {
    const qs = new URLSearchParams()
    if (params.name) qs.set('name', params.name)
    if (params.groupName) qs.set('group_name', params.groupName)
    if (params.level) qs.set('level', params.level)
    if (params.source) qs.set('source', params.source)
    if (params.q) qs.set('q', params.q)
    if (params.afterId !== undefined) qs.set('after_id', String(params.afterId))
    if (params.beforeId !== undefined) qs.set('before_id', String(params.beforeId))
    qs.set('limit', String(params.limit ?? 200))
    return request<ActivityResponse>('GET', `/activity?${qs.toString()}`)
  },
  getLogsIndex: () => request<LogsIndex>('GET', '/logs'),
  getLogTail: (service: string, offset: number | null, lines = 500) =>
    request<LogTail>(
      'GET',
      `/logs/${encodeURIComponent(service)}?lines=${lines}${offset === null ? '' : `&offset=${offset}`}`,
    ),
  deleteConsoleSession: (service: string) =>
    request<{
      deleted: string[]
    }>('DELETE', `/logs/${encodeURIComponent(service)}`),
  listConsoleCommands: () =>
    request<{
      commands: string[]
    }>('GET', '/console/commands'),
  runConsoleCommand: (command: string) => request<ConsoleRun>('POST', '/console/run', { command }),
  getConsoleRun: (id: string, offset = 0) =>
    request<ConsoleRun>('GET', `/console/runs/${encodeURIComponent(id)}?offset=${offset}`),
  listConsoleRuns: () => request<Omit<ConsoleRun, 'lines'>[]>('GET', '/console/runs'),
  cancelConsoleRun: (id: string) =>
    request<ConsoleRun>('POST', `/console/runs/${encodeURIComponent(id)}/cancel`),
  logout: () =>
    request<{
      ok: boolean
    }>('POST', '/logout'),
}
