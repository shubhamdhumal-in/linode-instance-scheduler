import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { ApiToken, ApiTokenBundle, ApiTokenScope, TokenScopeCatalog } from '../api/types'
import { MultiPicker } from '../components/MultiPicker'
import { Button, Card, CardHeader, EmptyState, ErrorBanner, Spinner, WarningBanner } from '../components/ui'
import { PageHeader } from './DashboardLayout'
const SCOPES: {
  value: ApiTokenBundle
  label: string
  help: string
}[] = [
  {
    value: 'read',
    label: 'Read',
    help: 'view nodes, schedules, groups, savings, pending deletions, snapshots, system status',
  },
  {
    value: 'operate',
    label: 'Operate',
    help: 'start, stop, extend, group start/stop, catch-up, run a backup',
  },
  {
    value: 'configure',
    label: 'Configure',
    help: 'schedules, groups, start order, manual-only, recover a pending deletion, restore an item',
  },
  {
    value: 'admin',
    label: 'Admin',
    help: 'everything: hooks, onboard/offboard, delete now, credentials, tokens, logs',
  },
]
const OPERATION_GROUPS: {
  group: string
  match: (op: string) => boolean
}[] = [
  { group: 'View', match: (op) => /^(instances:(list|status|history)|savings:|activity:|logs:)/.test(op) },
  {
    group: 'Run',
    match: (op) =>
      /^(instances:(start|stop|extend|clear-lock)|groups:(start|stop)|schedules:catch-up)/.test(op),
  },
  { group: 'Schedules & groups', match: (op) => /^(schedules:|groups:|dependencies:|mode:)/.test(op) },
  { group: 'Hooks', match: (op) => op.startsWith('hooks:') },
  { group: 'Lifecycle', match: (op) => /^(instances:|deletions:)/.test(op) },
  { group: 'Backups & system', match: (op) => /^(backups:|system:)/.test(op) },
  { group: 'Admin', match: (op) => /^(tokens:|credentials:)/.test(op) },
]
function groupOperations(
  ops: {
    value: string
    description: string
  }[],
) {
  const out: {
    group: string
    items: {
      value: string
      description: string
    }[]
  }[] = []
  for (const op of ops) {
    const group = OPERATION_GROUPS.find((g) => g.match(op.value))?.group ?? 'Other'
    let bucket = out.find((g) => g.group === group)
    if (!bucket) {
      bucket = { group, items: [] }
      out.push(bucket)
    }
    bucket.items.push(op)
  }
  const order = [...OPERATION_GROUPS.map((g) => g.group), 'Other']
  return out.sort((a, b) => order.indexOf(a.group) - order.indexOf(b.group))
}
function orNull(items: string[]): string[] | null {
  return items.length ? items : null
}
export function TokensPage() {
  const [tokens, setTokens] = useState<ApiToken[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [warnings, setWarnings] = useState<string[]>([])
  const [name, setName] = useState('')
  const [scopes, setScopes] = useState<ApiTokenScope[]>(['read', 'operate'])
  const [instances, setInstances] = useState<string[]>([])
  const [groups, setGroups] = useState<string[]>([])
  const [catalog, setCatalog] = useState<TokenScopeCatalog | null>(null)
  const [nodeOptions, setNodeOptions] = useState<
    {
      value: string
      hint?: string
    }[]
  >([])
  const [groupOptions, setGroupOptions] = useState<
    {
      value: string
      hint?: string
    }[]
  >([])
  const [expiresDays, setExpiresDays] = useState('')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [newToken, setNewToken] = useState<{
    name: string
    token: string
  } | null>(null)
  const [revoking, setRevoking] = useState<string | null>(null)
  const mountedRef = useRef(true)
  const reload = useCallback(() => {
    api
      .listTokens()
      .then((t) => {
        if (mountedRef.current) {
          setTokens(t)
          setError(null)
        }
      })
      .catch((e) => {
        if (mountedRef.current) setError(e instanceof ApiError ? e.message : 'Could not load tokens.')
      })
  }, [])
  useEffect(() => {
    mountedRef.current = true
    reload()
    api
      .tokenScopes()
      .then((c) => {
        if (mountedRef.current) setCatalog(c)
      })
      .catch(() => {})
    api
      .listInstances()
      .then((r) => {
        if (!mountedRef.current) return
        setNodeOptions(
          Object.entries(r)
            .sort(([a], [b]) => a.localeCompare(b))
            .map(([n, rec]) => ({
              value: n,
              hint: rec.schedule_mode === 'manual' ? 'manual-only' : undefined,
            })),
        )
      })
      .catch(() => {})
    api
      .listGroups()
      .then((g) => {
        if (mountedRef.current)
          setGroupOptions(g.map((x) => ({ value: x.name })).sort((a, b) => a.value.localeCompare(b.value)))
      })
      .catch(() => {})
    return () => {
      mountedRef.current = false
    }
  }, [reload])
  const create = async () => {
    if (creating) return
    setCreating(true)
    setCreateError(null)
    setNewToken(null)
    setWarnings([])
    try {
      const days = expiresDays.trim() ? Number(expiresDays) : null
      const created = await api.createToken({
        name: name.trim(),
        scopes,
        instances: orNull(instances),
        groups: orNull(groups),
        expires_days: days,
      })
      if (!mountedRef.current) return
      setNewToken({ name: created.name, token: created.token })
      setWarnings(created.warnings ?? [])
      setName('')
      setInstances([])
      setGroups([])
      setExpiresDays('')
      reload()
    } catch (e) {
      if (mountedRef.current)
        setCreateError(e instanceof ApiError ? e.message : 'Could not create the token.')
    } finally {
      if (mountedRef.current) setCreating(false)
    }
  }
  const revoke = async (tokenName: string) => {
    if (revoking) return
    setRevoking(tokenName)
    try {
      const result = await api.revokeToken(tokenName)
      if (mountedRef.current) setWarnings(result.warnings ?? [])
      reload()
    } catch (e) {
      if (mountedRef.current) setError(e instanceof ApiError ? e.message : 'Could not revoke the token.')
    } finally {
      if (mountedRef.current) setRevoking(null)
    }
  }
  const toggleScope = (scope: ApiTokenScope) =>
    setScopes((cur) => (cur.includes(scope) ? cur.filter((s) => s !== scope) : [...cur, scope]))
  return (
    <>
      <PageHeader
        title="API tokens"
        subtitle="Credentials for scripts and automation. Send as: Authorization: Bearer <token>."
      />
      <div className="space-y-6 p-8">
        {error && <ErrorBanner message={error} />}
        {warnings.length > 0 && <WarningBanner messages={warnings} />}
        <Card>
          <CardHeader
            title="Create a token"
            subtitle="Grant only what the script needs. The token is shown once."
          />
          <div className="space-y-4 px-5 py-4 text-sm">
            {createError && <ErrorBanner message={createError} />}
            {newToken && (
              <div className="rounded-md border border-emerald-200 bg-emerald-50 p-3">
                <p className="font-medium text-emerald-900">
                  Token “{newToken.name}” created. Copy it now; it won't be shown again.
                </p>
                <code
                  aria-label="New token"
                  className="mt-2 block break-all rounded bg-white p-2 text-xs text-slate-900"
                >
                  {newToken.token}
                </code>
              </div>
            )}
            <label className="block">
              <span className="text-slate-700">Name</span>
              <input
                aria-label="Token name"
                className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-1.5"
                placeholder="e.g. ci-deploy"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </label>
            <fieldset>
              <legend className="text-slate-700">Scopes</legend>
              <div className="mt-1 grid grid-cols-2 gap-2">
                {SCOPES.map((s) => (
                  <label key={s.value} className="flex items-start gap-2">
                    <input
                      type="checkbox"
                      checked={scopes.includes(s.value)}
                      onChange={() => toggleScope(s.value)}
                    />
                    <span>
                      <span className="font-medium text-slate-900">{s.label}</span>
                      <span className="block text-xs text-slate-500">{s.help}</span>
                      {catalog?.bundles[s.value] && (
                        <details className="text-xs text-slate-500">
                          <summary className="cursor-pointer">
                            {catalog.bundles[s.value].length} operations
                          </summary>
                          <ul className="mt-1 list-disc pl-4">
                            {catalog.bundles[s.value].map((op) => (
                              <li key={op}>
                                <code>{op}</code>
                              </li>
                            ))}
                          </ul>
                        </details>
                      )}
                    </span>
                  </label>
                ))}
              </div>
              <details className="mt-3">
                <summary className="cursor-pointer text-slate-700">Or pick single operations</summary>
                <div className="mt-2 grid grid-cols-1 gap-3 sm:grid-cols-2">
                  {catalog === null && <Spinner />}
                  {catalog &&
                    groupOperations(catalog.operations).map((g) => (
                      <div key={g.group}>
                        <div className="text-xs font-semibold uppercase text-slate-500">{g.group}</div>
                        {g.items.map((op) => (
                          <label key={op.value} className="flex items-start gap-2">
                            <input
                              type="checkbox"
                              className="mt-1"
                              aria-label={op.value}
                              checked={scopes.includes(op.value)}
                              onChange={() => toggleScope(op.value)}
                            />
                            <span>
                              {op.description.charAt(0).toUpperCase() + op.description.slice(1)}{' '}
                              <code className="text-xs text-slate-500">{op.value}</code>
                            </span>
                          </label>
                        ))}
                      </div>
                    ))}
                </div>
              </details>
            </fieldset>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <MultiPicker
                label="Only these nodes"
                options={nodeOptions}
                selected={instances}
                onChange={setInstances}
                placeholder="Any node"
                emptyText="No onboarded nodes yet"
              />
              <MultiPicker
                label="Only these groups"
                options={groupOptions}
                selected={groups}
                onChange={setGroups}
                placeholder="Any group"
                emptyText="No groups yet"
              />
              <label className="block">
                <span className="text-slate-700">Expires after (days)</span>
                <input
                  aria-label="Expiry days"
                  className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-1.5"
                  placeholder="never"
                  value={expiresDays}
                  onChange={(e) => setExpiresDays(e.target.value)}
                />
              </label>
            </div>
            <p className="text-xs text-slate-500">
              Leave nodes and groups empty for a token that can act on everything its scopes allow. A group
              covers its current members. Manual-only nodes can be picked too: the token can start and stop
              them; only the scheduler leaves them alone.
            </p>
            <Button
              variant="primary"
              disabled={creating || !name.trim() || scopes.length === 0}
              onClick={() => void create()}
            >
              {creating ? 'Creating…' : 'Create token'}
            </Button>
          </div>
        </Card>
        <Card>
          <CardHeader title="Tokens" />
          {tokens === null ? (
            <div className="p-6">
              <Spinner />
            </div>
          ) : tokens.length === 0 ? (
            <EmptyState title="No API tokens yet" />
          ) : (
            <ul className="divide-y divide-slate-100">
              {tokens.map((t) => (
                <li key={t.name} className="flex items-center justify-between gap-4 px-5 py-3 text-sm">
                  <div>
                    <span className="font-medium text-slate-900">{t.name}</span>
                    <span className="ml-2 text-xs text-slate-400">{t.token_prefix}…</span>
                    <div className="text-xs text-slate-500">
                      {t.scopes.join(', ')}
                      {(t.instances || t.groups) &&
                        ` · limited to ${[...(t.instances ?? []), ...(t.groups ?? []).map((g) => `group ${g}`)].join(', ')}`}
                      {' · '}
                      {t.revoked_at
                        ? 'revoked'
                        : t.expires_at
                          ? `expires ${new Date(t.expires_at).toLocaleDateString()}`
                          : 'no expiry'}
                      {' · last used '}
                      {t.last_used_at ? new Date(t.last_used_at).toLocaleString() : 'never'}
                    </div>
                  </div>
                  {!t.revoked_at && (
                    <Button variant="danger" disabled={revoking !== null} onClick={() => void revoke(t.name)}>
                      {revoking === t.name ? 'Revoking…' : 'Revoke'}
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </>
  )
}
