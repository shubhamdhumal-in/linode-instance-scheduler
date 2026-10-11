import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, errorWarnings } from '../api/client'
import type { MigrationBackup } from '../api/types'
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
const STATUS_STYLE: Record<MigrationBackup['status'], string> = {
  kept: 'bg-emerald-50 text-emerald-700 ring-emerald-600/20',
  restored: 'bg-indigo-50 text-indigo-700 ring-indigo-600/20',
  creating: 'bg-amber-50 text-amber-700 ring-amber-600/20',
  failed: 'bg-rose-50 text-rose-700 ring-rose-600/20',
}
function formatCost(b: MigrationBackup): string {
  const c = b.estimated_monthly_cost
  return c ? `about $${c.total_monthly.toFixed(2)}/month (Linode's price when it was taken)` : 'billable'
}
function BackupRow({ backup, onChanged }: { backup: MigrationBackup; onChanged: () => void }) {
  const statusBar = useStatusBar()
  const [mode, setMode] = useState<'idle' | 'rollback' | 'delete'>('idle')
  const [confirm, setConfirm] = useState('')
  const [boot, setBoot] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [warnings, setWarnings] = useState<string[]>([])
  const [done, setDone] = useState<string | null>(null)
  const runningRef = useRef(false)
  async function rollback() {
    if (runningRef.current) return
    runningRef.current = true
    setBusy(true)
    setError(null)
    setWarnings([])
    try {
      const result = await statusBar.run(`Rolling back “${backup.name}”`, (report) =>
        api.rollbackBackup(backup.name, boot, report, setWarnings),
      )
      setDone(
        `Restored: instance ${result.backup_instance_id} (“${result.label}”) is the original system again` +
          (result.public_ipv4.length ? ` at ${result.public_ipv4.join(', ')}` : '') +
          (result.original_deleted_instance_id
            ? `. The original instance ${result.original_deleted_instance_id} was replaced (its volumes were kept).`
            : '.'),
      )
      setMode('idle')
      onChanged()
    } catch (e) {
      setError(e instanceof ApiError || e instanceof Error ? e.message : String(e))
      const carried = errorWarnings(e)
      if (carried) setWarnings(carried)
    } finally {
      setBusy(false)
      runningRef.current = false
    }
  }
  async function remove() {
    if (runningRef.current) return
    runningRef.current = true
    setBusy(true)
    setError(null)
    try {
      const result = await statusBar.run(`Deleting the backup of “${backup.name}”`, (report) =>
        api.deleteBackup(backup.name, report),
      )
      if (result.outcome === 'incomplete') {
        setError(`Not fully deleted: ${result.problems.join('; ')}`)
      } else if (result.outcome === 'pending_deletion' && result.pending_deletion) {
        setDone(
          `Nothing has been deleted yet: the backup instance and its cloned volumes are deleted automatically on ${new Date(result.pending_deletion.due_at).toLocaleDateString()}. To keep it after all, or delete it now, go to Pending deletion.`,
        )
        setMode('idle')
      } else {
        onChanged()
      }
    } catch (e) {
      setError(e instanceof ApiError || e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
      runningRef.current = false
    }
  }
  const canConfirm = typedConfirmationMatches(confirm, backup.name)
  return (
    <li className="px-5 py-4 space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="font-medium text-slate-900">{backup.name}</span>
            <span
              className={`rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${STATUS_STYLE[backup.status]}`}
            >
              {backup.status}
            </span>
          </div>
          <p className="mt-1 text-sm text-slate-600">
            Backup instance {backup.backup_instance_id ?? '—'} ({backup.backup_label ?? '—'}),{' '}
            {backup.backup_volumes.length} cloned volume(s) — a copy of instance {backup.original_instance_id}{' '}
            ({backup.original_label}
            {backup.public_ipv4.length ? `, ${backup.public_ipv4.join(', ')}` : ''}) taken{' '}
            {new Date(backup.created_at).toLocaleString()}.
          </p>
          {backup.status === 'kept' && (
            <p className="mt-1 text-xs text-amber-700">
              Powered off but still billable: {formatCost(backup)}, until deleted.
            </p>
          )}
          {backup.status === 'restored' && backup.restored_at && (
            <p className="mt-1 text-xs text-slate-500">
              Restored {new Date(backup.restored_at).toLocaleString()} — it is now the running system.
              Deleting this entry only removes the record.
            </p>
          )}
        </div>
        {mode === 'idle' && (
          <div className="flex gap-2">
            {backup.status === 'kept' && (
              <Button
                variant="secondary"
                disabled={busy}
                onClick={() => {
                  setMode('rollback')
                  setConfirm('')
                }}
              >
                Roll back
              </Button>
            )}
            <Button
              variant="ghost"
              disabled={busy}
              onClick={() => {
                setMode('delete')
                setConfirm('')
              }}
            >
              {backup.status === 'restored' ? 'Remove record' : 'Delete backup'}
            </Button>
          </div>
        )}
      </div>
      {mode === 'rollback' && (
        <div className="rounded-md border border-amber-300 bg-amber-50 p-4 space-y-3">
          <p className="text-sm text-amber-900">
            The backup takes back the original public address, VPC/VLAN addresses and label, and the scheduler
            stops managing “{backup.name}”. If the original instance still exists it is replaced (its volumes
            are detached and kept). If “{backup.name}” is running as a managed instance, stop it first.
          </p>
          <label className="flex items-center gap-2 text-sm text-slate-700">
            <input type="checkbox" checked={boot} onChange={(e) => setBoot(e.target.checked)} />
            Boot the restored instance
          </label>
          <TypeToConfirmInput
            id={`rb-${backup.name}`}
            expected={backup.name}
            value={confirm}
            onChange={setConfirm}
          />
          <div className="flex gap-2">
            <Button variant="primary" disabled={!canConfirm || busy} onClick={rollback}>
              {busy ? 'Rolling back…' : 'Roll back now'}
            </Button>
            <Button variant="ghost" disabled={busy} onClick={() => setMode('idle')}>
              Cancel
            </Button>
          </div>
        </div>
      )}
      {mode === 'delete' && (
        <div className="rounded-md border border-rose-300 bg-rose-50 p-4 space-y-3">
          <p className="text-sm text-rose-900">
            {backup.status === 'restored'
              ? 'Only the record is removed; nothing on Linode changes.'
              : 'This deletes the backup instance and its cloned volumes after a 7-day wait on the Pending deletion page, where you can still keep it. You can no longer roll back once it is deleted.'}
          </p>
          <TypeToConfirmInput
            id={`del-${backup.name}`}
            expected={backup.name}
            value={confirm}
            onChange={setConfirm}
            ringClassName="focus:ring-rose-600"
          />
          <div className="flex gap-2">
            <Button variant="danger" disabled={!canConfirm || busy} onClick={remove}>
              {busy ? 'Deleting…' : 'Delete'}
            </Button>
            <Button variant="ghost" disabled={busy} onClick={() => setMode('idle')}>
              Cancel
            </Button>
          </div>
        </div>
      )}
      {done && <p className="text-sm text-emerald-700">{done}</p>}
      {warnings.length > 0 && <WarningBanner messages={warnings} />}
      {error && <ErrorBanner message={error} />}
    </li>
  )
}
export function BackupsPage() {
  const [backups, setBackups] = useState<MigrationBackup[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const reload = useCallback(() => {
    api.listBackups().then(
      (list) => {
        setBackups(list)
        setError(null)
      },
      (e) => setError(e instanceof Error ? e.message : String(e)),
    )
  }, [])
  useEffect(() => {
    reload()
  }, [reload])
  return (
    <>
      <PageHeader
        title="Backups"
        subtitle="Powered-off copies of instances taken before migrating them to Block Storage. Roll back to put the original system back."
      />
      <div className="space-y-6 px-8 py-6">
        {error && <ErrorBanner message={error} />}
        <Card>
          <CardHeader
            title="Pre-migration backups"
            subtitle="Each one is a billable resource until you delete it."
          />
          {backups === null ? (
            <div className="flex justify-center p-8">
              <Spinner />
            </div>
          ) : backups.length === 0 ? (
            <EmptyState
              title="No backups"
              subtitle="Choose “Keep a backup” when starting a migration to get one."
            />
          ) : (
            <ul className="divide-y divide-slate-100">
              {backups.map((b) => (
                <BackupRow key={b.name} backup={b} onChanged={reload} />
              ))}
            </ul>
          )}
        </Card>
      </div>
    </>
  )
}
