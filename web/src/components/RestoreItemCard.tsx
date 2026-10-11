import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { RestoreItemRequest, RestoreItemResult } from '../api/types'
import { MultiPicker } from './MultiPicker'
import { Button, Card, CardHeader, ErrorBanner, WarningBanner } from './ui'
const INPUT =
  'mt-1 block w-full rounded-md border-0 py-1.5 px-3 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600'
function message(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : String(e)
}
function snapshotLabel(key: string): string {
  const name = key.split('/').pop() ?? key
  return name === 'instances-continuous.db' ? 'Latest change (continuous copy)' : name
}
export function RestoreItemCard() {
  const [snapshots, setSnapshots] = useState<string[] | null>(null)
  const [kind, setKind] = useState<RestoreItemRequest['kind']>('schedule')
  const [name, setName] = useState('')
  const [snapshot, setSnapshot] = useState('')
  const [withMembers, setWithMembers] = useState(false)
  const [preview, setPreview] = useState<{
    snapshot: unknown
    current: unknown
  } | null>(null)
  const [result, setResult] = useState<RestoreItemResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [nodeOptions, setNodeOptions] = useState<
    {
      value: string
      hint?: string
    }[]
  >([])
  const [groupOptions, setGroupOptions] = useState<
    {
      value: string
    }[]
  >([])
  useEffect(() => {
    let live = true
    api
      .listSnapshots()
      .then((r) => {
        if (live) {
          setSnapshots(r.snapshots)
          setSnapshot(r.snapshots[r.snapshots.length - 1] ?? '')
        }
      })
      .catch((e) => {
        if (live) setError(message(e))
      })
    api
      .listInstances()
      .then((r) => {
        if (!live) return
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
        if (live)
          setGroupOptions(g.map((x) => ({ value: x.name })).sort((a, b) => a.value.localeCompare(b.value)))
      })
      .catch(() => {})
    return () => {
      live = false
    }
  }, [])
  const body: RestoreItemRequest = {
    kind,
    name: name.trim(),
    snapshot: snapshot || null,
    with_members: withMembers,
  }
  async function show() {
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      setPreview(await api.previewRestoreItem(body))
    } catch (e) {
      setError(message(e))
      setPreview(null)
    } finally {
      setBusy(false)
    }
  }
  async function restore() {
    setBusy(true)
    setError(null)
    try {
      setResult(await api.restoreItem(body))
      setPreview(null)
    } catch (e) {
      setError(message(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Card>
      <CardHeader
        title="Restore one item from a snapshot"
        subtitle="Undo a change to one schedule, one instance's hooks, or one group, without restoring the whole database."
      />
      <div className="space-y-3 px-5 py-4" data-testid="restore-item-card">
        <div className="grid gap-3 sm:grid-cols-3">
          <label className="text-sm text-slate-700">
            What
            <select
              className={INPUT}
              value={kind}
              onChange={(e) => {
                const next = e.target.value as RestoreItemRequest['kind']
                if ((next === 'group') !== (kind === 'group')) setName('')
                setKind(next)
                setPreview(null)
              }}
            >
              <option value="schedule">An instance's schedule</option>
              <option value="hooks">An instance's hooks</option>
              <option value="group">A group</option>
            </select>
          </label>
          <div className="text-sm">
            <MultiPicker
              key={kind === 'group' ? 'group' : 'instance'}
              label={kind === 'group' ? 'Group' : 'Instance'}
              options={kind === 'group' ? groupOptions : nodeOptions}
              selected={name ? [name] : []}
              onChange={(next) => {
                setName(next[0] ?? '')
                setPreview(null)
              }}
              placeholder={kind === 'group' ? 'Pick a group' : 'Pick an instance'}
              emptyText={
                kind === 'group'
                  ? 'No groups now -- type a name to restore a deleted one'
                  : 'No onboarded instances'
              }
              single
              allowCustom
            />
          </div>
          <label className="text-sm text-slate-700">
            Snapshot
            <select
              className={INPUT}
              value={snapshot}
              onChange={(e) => {
                setSnapshot(e.target.value)
                setPreview(null)
              }}
            >
              {(snapshots ?? [])
                .slice()
                .reverse()
                .map((k) => (
                  <option key={k} value={k}>
                    {snapshotLabel(k)}
                  </option>
                ))}
            </select>
          </label>
        </div>
        {kind === 'group' && (
          <label className="flex items-center gap-2 text-sm text-slate-700">
            <input type="checkbox" checked={withMembers} onChange={(e) => setWithMembers(e.target.checked)} />
            Also add back its members (those that exist and aren't in another group)
          </label>
        )}
        <Button variant="secondary" disabled={busy || !name.trim() || !snapshot} onClick={show}>
          Show what the snapshot has
        </Button>
        {preview && (
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <p className="text-xs font-medium uppercase text-slate-500">In the snapshot</p>
              <pre className="mt-1 max-h-64 overflow-auto rounded bg-slate-50 p-2 text-xs">
                {JSON.stringify(preview.snapshot, null, 2)}
              </pre>
            </div>
            <div>
              <p className="text-xs font-medium uppercase text-slate-500">Now</p>
              <pre className="mt-1 max-h-64 overflow-auto rounded bg-slate-50 p-2 text-xs">
                {JSON.stringify(preview.current, null, 2) ?? 'nothing'}
              </pre>
            </div>
            <div className="sm:col-span-2">
              {preview.snapshot === null ? (
                <p className="text-sm text-slate-600">The snapshot has nothing for this {kind}.</p>
              ) : (
                <Button variant="primary" disabled={busy} onClick={restore}>
                  Restore this {kind}
                </Button>
              )}
            </div>
          </div>
        )}
        {result && (
          <div className="rounded-md bg-emerald-50 px-3 py-2 text-sm text-emerald-800 ring-1 ring-inset ring-emerald-200">
            Restored {result.kind} of “{result.name}”: {result.applied.join(', ')}.
          </div>
        )}
        {result && result.problems.length > 0 && (
          <WarningBanner messages={result.problems.map((p) => `Not restored: ${p}`)} />
        )}
        {result?.warnings && <WarningBanner messages={result.warnings} />}
        {error && <ErrorBanner message={error} />}
      </div>
    </Card>
  )
}
