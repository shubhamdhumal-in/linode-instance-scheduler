import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { LogsIndex } from '../api/types'
import { SchedulerBanner, formatTime } from '../components/ActivityFeed'
import { Button, Card, CardHeader, ErrorBanner, Spinner } from '../components/ui'
import { usePolling } from '../hooks/usePolling'
import { PageHeader } from './DashboardLayout'
const SERVICE_LABELS: Record<string, string> = {
  scheduler: 'Scheduler (poller)',
  api: 'API / dashboard',
  backup: 'Backups',
  console: 'Console',
}
const MAX_LINES = 5000
const TAIL_INTERVAL_MS = 3000
function bytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}
function lineClass(line: string): string {
  const upper = line.toUpperCase()
  if (upper.includes('ERROR') || upper.includes('TRACEBACK') || upper.includes('SECURITY WARNING'))
    return 'text-red-300'
  if (upper.includes('WARNING')) return 'text-amber-300'
  return 'text-slate-200'
}
function logLabel(name: string): string {
  return SERVICE_LABELS[name] ?? (name.startsWith('lish-') ? `Console session: ${name.slice(5)}` : name)
}
export function LogsPage() {
  const [params] = useSearchParams()
  const [index, setIndex] = useState<LogsIndex | null>(null)
  const [service, setService] = useState(() => params.get('log') || 'scheduler')
  const [lines, setLines] = useState<string[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [live, setLive] = useState(true)
  const [follow, setFollow] = useState(true)
  const [filter, setFilter] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const offsetRef = useRef<number | null>(null)
  const serviceRef = useRef(service)
  const boxRef = useRef<HTMLDivElement | null>(null)
  const loadIndex = useCallback(() => {
    api
      .getLogsIndex()
      .then((i) => setIndex(i))
      .catch(() => {})
  }, [])
  useEffect(() => {
    loadIndex()
  }, [loadIndex])
  useEffect(() => {
    serviceRef.current = service
    offsetRef.current = null
    setConfirmDelete(false)
    setLines(null)
    setError(null)
    api
      .getLogTail(service, null, 1000)
      .then((t) => {
        if (serviceRef.current !== service) return
        offsetRef.current = t.offset
        setLines(t.lines)
      })
      .catch((e) => {
        if (serviceRef.current === service)
          setError(e instanceof ApiError ? e.message : 'Could not load the log.')
      })
  }, [service])
  const tail = useCallback(() => {
    const current = serviceRef.current
    if (offsetRef.current === null) return
    api
      .getLogTail(current, offsetRef.current)
      .then((t) => {
        if (serviceRef.current !== current) return
        offsetRef.current = t.offset
        if (t.reset) {
          setLines(t.lines.slice(-MAX_LINES))
        } else if (t.lines.length) {
          setLines((prev) => [...(prev ?? []), ...t.lines].slice(-MAX_LINES))
        }
        setError(null)
      })
      .catch(() => {})
    loadIndex()
  }, [loadIndex])
  usePolling(tail, TAIL_INTERVAL_MS, live)
  const shown = useMemo(() => {
    if (!lines) return null
    const f = filter.trim().toLowerCase()
    return f ? lines.filter((l) => l.toLowerCase().includes(f)) : lines
  }, [lines, filter])
  useEffect(() => {
    if (follow && boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight
  }, [shown, follow])
  const download = () => {
    const blob = new Blob([(lines ?? []).join('\n') + '\n'], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${service}.log`
    a.click()
    URL.revokeObjectURL(url)
  }
  const deleteSession = async () => {
    const target = service
    setDeleting(true)
    try {
      await api.deleteConsoleSession(target)
      setConfirmDelete(false)
      if (serviceRef.current === target) setService('scheduler')
      loadIndex()
    } catch (e) {
      if (serviceRef.current === target)
        setError(e instanceof ApiError ? e.message : 'Could not delete the session log.')
    } finally {
      setDeleting(false)
    }
  }
  const services =
    index?.services ??
    Object.keys(SERVICE_LABELS).map((name) => ({ name, exists: false, size: 0, modified: null }))
  const sessions = index?.console_sessions ?? []
  return (
    <div>
      <PageHeader
        title="Logs"
        subtitle="Live output from the scheduler, the API/dashboard server, backups and console commands."
      />
      <div className="space-y-4 px-8 py-6">
        <SchedulerBanner status={index?.scheduler ?? null} />
        <div className="flex flex-wrap items-center gap-2">
          {services.map((s) => (
            <button
              key={s.name}
              onClick={() => setService(s.name)}
              className={`rounded-md px-3 py-1.5 text-sm font-medium ring-1 ring-inset ${service === s.name ? 'bg-indigo-600 text-white ring-indigo-600' : 'bg-white text-slate-700 ring-slate-300 hover:bg-slate-50'}`}
            >
              {logLabel(s.name)}
              <span className={`ml-2 text-xs ${service === s.name ? 'text-indigo-100' : 'text-slate-400'}`}>
                {s.exists ? bytes(s.size) : 'empty'}
              </span>
            </button>
          ))}
          <input
            aria-label="Filter lines"
            className="ml-auto w-56 rounded-md border-0 py-1.5 px-2 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600"
            placeholder="Filter lines"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          <label className="flex items-center gap-1.5 text-sm text-slate-600">
            <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} /> Live
          </label>
          <label className="flex items-center gap-1.5 text-sm text-slate-600">
            <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> Follow
          </label>
          <Button variant="secondary" onClick={download} disabled={!lines || lines.length === 0}>
            Download
          </Button>
        </div>
        {(sessions.length > 0 || service.startsWith('lish-')) && (
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-medium uppercase tracking-wide text-slate-500">
              Migration console sessions
            </span>
            {sessions.length === 0 && <span className="text-xs text-slate-500">none yet</span>}
            {sessions.map((s) => (
              <button
                key={s.name}
                onClick={() => setService(s.name)}
                className={`rounded-md px-2.5 py-1 text-xs font-medium ring-1 ring-inset ${service === s.name ? 'bg-indigo-600 text-white ring-indigo-600' : 'bg-white text-slate-700 ring-slate-300 hover:bg-slate-50'}`}
              >
                {s.instance}
                <span className={`ml-1.5 ${service === s.name ? 'text-indigo-100' : 'text-slate-400'}`}>
                  {bytes(s.size)}
                </span>
              </button>
            ))}
          </div>
        )}
        {service.startsWith('lish-') && (
          <div className="flex flex-wrap items-center gap-3">
            <p className="text-xs text-slate-500">
              {logLabel(service)} — what the instance's Rescue Mode console showed while the tool ran the copy
              (terminal codes removed).
              {index &&
                ` Removed automatically after ${index.database.activity_retention_days} days without new output.`}
            </p>
            {confirmDelete ? (
              <span className="ml-auto flex items-center gap-2 text-xs text-slate-700">
                Delete this session log?
                <Button variant="danger" onClick={deleteSession} disabled={deleting}>
                  {deleting ? 'Deleting…' : 'Delete'}
                </Button>
                <Button variant="secondary" onClick={() => setConfirmDelete(false)} disabled={deleting}>
                  Cancel
                </Button>
              </span>
            ) : (
              <Button variant="secondary" className="ml-auto" onClick={() => setConfirmDelete(true)}>
                Delete session
              </Button>
            )}
          </div>
        )}
        {error && <ErrorBanner message={error} />}
        <div
          ref={boxRef}
          role="log"
          aria-label={`${service} log`}
          className="h-[60vh] overflow-auto rounded-md bg-slate-950 p-3 font-mono text-xs leading-5"
        >
          {shown === null ? (
            <div className="flex justify-center py-8">
              <Spinner />
            </div>
          ) : shown.length === 0 ? (
            <div className="text-slate-500">
              {lines && lines.length ? 'No lines match the filter.' : 'Nothing logged yet.'}
            </div>
          ) : (
            shown.map((line, i) => (
              <div key={i} className={`whitespace-pre-wrap break-words ${lineClass(line)}`}>
                {line}
              </div>
            ))
          )}
        </div>
        {index && (
          <Card>
            <CardHeader
              title="Database"
              subtitle="The local registry (instances, schedules, groups, hooks, history, activity)."
            />
            <dl className="grid grid-cols-1 gap-3 px-5 pb-5 text-sm sm:grid-cols-4">
              <div>
                <dt className="text-slate-500">File</dt>
                <dd className="break-all font-mono text-xs text-slate-900">{index.database.path}</dd>
              </div>
              <div>
                <dt className="text-slate-500">Size</dt>
                <dd className="text-slate-900">{bytes(index.database.size)}</dd>
              </div>
              <div>
                <dt className="text-slate-500">Write-ahead log</dt>
                <dd className="text-slate-900">{bytes(index.database.wal_size)}</dd>
              </div>
              <div>
                <dt className="text-slate-500">Activity kept for</dt>
                <dd className="text-slate-900">{index.database.activity_retention_days} days</dd>
              </div>
            </dl>
            {index.services.find((s) => s.name === 'backup')?.modified && (
              <p className="px-5 pb-5 text-xs text-slate-500">
                Last backup log entry:{' '}
                {formatTime(index.services.find((s) => s.name === 'backup')!.modified!)} — see the Backups log
                for details.
              </p>
            )}
          </Card>
        )}
      </div>
    </div>
  )
}
