import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, ApiError, errorWarnings } from '../api/client'
import type { PendingDeletion, PendingResource } from '../api/types'
import {
  Button,
  Card,
  CardHeader,
  EmptyState,
  ErrorBanner,
  Spinner,
  TypeToConfirmInput,
  WarningBanner,
  typedConfirmationMatches,
} from '../components/ui'
import { useStatusBar } from '../status/StatusBarContext'
import { PageHeader } from './DashboardLayout'
function resourceLabel(r: PendingResource): string {
  const what =
    r.type === 'ip'
      ? `reserved IP ${r.id}`
      : r.type === 'instance'
        ? `backup instance ${r.id}`
        : `${r.role === 'os' ? 'OS' : r.role === 'data' ? 'data' : 'cloned'} volume ${r.id}`
  return r.action === 'untag' ? `${what} — kept, only this node's tags removed` : `${what} — deleted`
}
function daysLeft(dueAt: string): string {
  const ms = new Date(dueAt).getTime() - Date.now()
  if (ms <= 0) return 'due now'
  const days = Math.ceil(ms / 86400000)
  return `in ${days} day${days === 1 ? '' : 's'}`
}
function PendingRow({ entry, onChanged }: { entry: PendingDeletion; onChanged: () => void }) {
  const statusBar = useStatusBar()
  const navigate = useNavigate()
  const [mode, setMode] = useState<'idle' | 'recover' | 'delete'>('idle')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [warnings, setWarnings] = useState<string[]>([])
  const [done, setDone] = useState<string | null>(null)
  const runningRef = useRef(false)
  const isNode = entry.kind === 'node'
  const label = isNode ? `node “${entry.name}”` : `pre-migration backup of “${entry.name}”`
  async function recover() {
    if (runningRef.current) return
    runningRef.current = true
    setBusy(true)
    setError(null)
    setWarnings([])
    try {
      await statusBar.run(`Recovering ${label}`, (report) =>
        api.recoverPendingDeletion(entry.id, report, setWarnings),
      )
      setDone(
        isNode
          ? `“${entry.name}” is back under management (stopped), with its schedule, group and hooks.`
          : `The backup of “${entry.name}” is kept again.`,
      )
      setMode('idle')
    } catch (e) {
      setError(e instanceof ApiError || e instanceof Error ? e.message : String(e))
      const carried = errorWarnings(e)
      if (carried) setWarnings(carried)
    } finally {
      setBusy(false)
      runningRef.current = false
    }
  }
  async function deleteNow() {
    if (runningRef.current) return
    runningRef.current = true
    setBusy(true)
    setError(null)
    try {
      const result = await statusBar.run(`Deleting ${label} now`, (report) =>
        api.deletePendingNow(entry.id, report),
      )
      if (result.status === 'failed') {
        setError(`Not fully deleted — it stays here and is retried automatically: ${result.detail ?? ''}`)
      } else {
        setMode('idle')
        onChanged()
      }
    } catch (e) {
      setError(e instanceof ApiError || e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
      runningRef.current = false
    }
  }
  const canConfirm = typedConfirmationMatches(confirm, entry.name)
  return (
    <li className="space-y-3 px-5 py-4" data-testid={`pending-${entry.id}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="font-medium text-slate-900">{entry.name}</span>
            <span className="rounded-md bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-700">
              {isNode ? 'offboarded node' : 'pre-migration backup'}
            </span>
            {entry.status === 'failed' && (
              <span className="rounded-md bg-rose-50 px-2 py-0.5 text-xs font-medium text-rose-700 ring-1 ring-inset ring-rose-600/20">
                retrying
              </span>
            )}
          </div>
          <p className="mt-1 text-sm text-slate-600">
            Deleted automatically on {new Date(entry.due_at).toLocaleString()} ({daysLeft(entry.due_at)}).
            Marked {new Date(entry.created_at).toLocaleString()}.
          </p>
          <ul className="mt-1 list-disc pl-5 text-xs text-slate-500">
            {entry.resources.map((r) => (
              <li key={`${r.type}-${r.id}`}>{resourceLabel(r)}</li>
            ))}
          </ul>
          {entry.detail && <p className="mt-1 text-xs text-rose-700">{entry.detail}</p>}
        </div>
        {mode === 'idle' && !done && (
          <div className="flex gap-2">
            <Button
              variant="secondary"
              disabled={busy}
              onClick={() => {
                setMode('recover')
                setConfirm('')
              }}
            >
              Recover
            </Button>
            <Button
              variant="ghost"
              disabled={busy}
              onClick={() => {
                setMode('delete')
                setConfirm('')
              }}
            >
              Delete now
            </Button>
          </div>
        )}
      </div>
      {done && (
        <div className="flex items-center justify-between rounded-md bg-emerald-50 px-3 py-3 text-sm text-emerald-800 ring-1 ring-inset ring-emerald-200">
          <span>{done}</span>
          {isNode && (
            <Button
              variant="secondary"
              onClick={() => navigate(`/instances/${encodeURIComponent(entry.name)}`)}
            >
              Open
            </Button>
          )}
          {!isNode && (
            <Button variant="secondary" onClick={onChanged}>
              OK
            </Button>
          )}
        </div>
      )}
      {mode === 'recover' && (
        <div className="space-y-3 rounded-md border border-emerald-300 bg-emerald-50 p-4">
          <p className="text-sm text-emerald-900">
            {isNode
              ? `Nothing has been deleted yet. Recovering brings “${entry.name}” back exactly as it was before it was offboarded — stopped, with its schedule, group membership, hooks and settings — and cancels the deletion.`
              : `Nothing has been deleted yet. Recovering keeps this backup, so you can still roll back to it.`}
          </p>
          <div className="flex gap-2">
            <Button variant="primary" disabled={busy} onClick={recover}>
              {busy ? 'Recovering…' : 'Recover now'}
            </Button>
            <Button variant="ghost" disabled={busy} onClick={() => setMode('idle')}>
              Cancel
            </Button>
          </div>
        </div>
      )}
      {mode === 'delete' && (
        <div className="space-y-3 rounded-md border border-rose-300 bg-rose-50 p-4">
          <p className="text-sm text-rose-900">
            This carries out the deletion now instead of on {new Date(entry.due_at).toLocaleDateString()}.
            Afterwards{' '}
            {isNode
              ? `“${entry.name}” can't be recovered.`
              : 'this backup is gone and you can no longer roll back to it.'}
          </p>
          <TypeToConfirmInput
            id={`pd-${entry.id}`}
            expected={entry.name}
            value={confirm}
            onChange={setConfirm}
            ringClassName="focus:ring-rose-600"
          />
          <div className="flex gap-2">
            <Button variant="danger" disabled={!canConfirm || busy} onClick={deleteNow}>
              {busy ? 'Deleting…' : 'Delete permanently now'}
            </Button>
            <Button variant="ghost" disabled={busy} onClick={() => setMode('idle')}>
              Cancel
            </Button>
          </div>
        </div>
      )}
      {warnings.length > 0 && <WarningBanner messages={warnings} />}
      {error && <ErrorBanner message={error} />}
    </li>
  )
}
export function PendingDeletionsPage() {
  const [entries, setEntries] = useState<PendingDeletion[] | null>(null)
  const [graceDays, setGraceDays] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const reload = useCallback(() => {
    api
      .listPendingDeletions()
      .then((r) => {
        setEntries(r.entries)
        setGraceDays(r.grace_days)
        setError(null)
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [])
  useEffect(() => {
    reload()
  }, [reload])
  return (
    <>
      <PageHeader
        title="Pending deletion"
        subtitle={`Offboarded nodes and deleted backups wait here ${graceDays ?? 7} day(s) before anything is actually deleted. Recover one, or delete it now.`}
      />
      <div className="space-y-6 px-8 py-6">
        {error && <ErrorBanner message={error} />}
        <Card>
          <CardHeader
            title="Waiting to be deleted"
            subtitle="Their volumes and reserved IPs still bill until they're deleted."
          />
          {entries === null ? (
            <div className="px-5 py-6">
              <Spinner />
            </div>
          ) : entries.length === 0 ? (
            <EmptyState
              title="Nothing is pending deletion"
              subtitle="Offboarding a node or deleting a backup lists it here first."
            />
          ) : (
            <ul className="divide-y divide-slate-100">
              {entries.map((e) => (
                <PendingRow key={e.id} entry={e} onChanged={reload} />
              ))}
            </ul>
          )}
        </Card>
      </div>
    </>
  )
}
