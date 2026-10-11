import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { HealthStatus, SystemBackupStatus } from '../api/types'
import { Button, Card, CardHeader, ErrorBanner, Spinner } from '../components/ui'
import { useStatusBar } from '../status/StatusBarContext'
import { PageHeader } from './DashboardLayout'
function message(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : String(e)
}
const INPUT =
  'mt-1 block w-full rounded-md border-0 py-1.5 px-3 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600'
export function SystemBackupPage() {
  const statusBar = useStatusBar()
  const [status, setStatus] = useState<SystemBackupStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [editing, setEditing] = useState(false)
  const [form, setForm] = useState({ bucket: '', endpoint: '', access_key: '', secret_key: '' })
  const [busy, setBusy] = useState<string | null>(null)
  const [recipientsInput, setRecipientsInput] = useState('')
  const runningRef = useRef(false)
  const [health, setHealth] = useState<HealthStatus | null>(null)
  const reload = useCallback(() => {
    api.getSystemBackup().then(
      (s) => {
        setStatus(s)
        setError(null)
      },
      (e) => setError(message(e)),
    )
    api.getHealth().then(setHealth, () => setHealth(null))
  }, [])
  useEffect(() => {
    reload()
  }, [reload])
  async function act(label: string, fn: () => Promise<string | null>) {
    if (runningRef.current) return
    runningRef.current = true
    setBusy(label)
    setError(null)
    setNotice(null)
    try {
      const done = await fn()
      if (done) setNotice(done)
      reload()
    } catch (e) {
      setError(message(e))
    } finally {
      setBusy(null)
      runningRef.current = false
    }
  }
  const obj = status?.object_storage
  const last = status?.last_backup
  const formComplete = Object.values(form).every((v) => v.trim() !== '')
  return (
    <>
      <PageHeader
        title="System backup"
        subtitle="Backups of this tool itself: its database (schedules, groups, history, API tokens), instance records and trusted host keys."
      />
      <div className="space-y-6 px-8 py-6">
        {error && <ErrorBanner message={error} />}
        {notice && <p className="rounded-md bg-emerald-50 px-4 py-3 text-sm text-emerald-800">{notice}</p>}
        {status === null ? (
          <div className="flex justify-center p-8">
            <Spinner />
          </div>
        ) : (
          <>
            <Card>
              <CardHeader
                title="Last backup"
                subtitle="The scheduled backup (hourly by default) and “Back up now” both record their result here."
                action={
                  <Button
                    variant="primary"
                    disabled={busy !== null}
                    onClick={() =>
                      act('run', async () => {
                        const r = await statusBar.run('Backing up the scheduler', (report) =>
                          api.runSystemBackup(report),
                        )
                        return r.ok
                          ? 'Backup completed.'
                          : `Backup finished with problems: ${r.problems.join('; ')}`
                      })
                    }
                  >
                    {busy === 'run' ? 'Backing up…' : 'Back up now'}
                  </Button>
                }
              />
              <div className="px-5 py-4 text-sm">
                {!last ? (
                  <p className="text-slate-600">No backup has run yet.</p>
                ) : (
                  <div className="space-y-1">
                    <p className={last.ok ? 'text-emerald-700' : 'text-rose-700'}>
                      {last.ok ? 'OK' : 'Problems'} — {new Date(last.finished_at).toLocaleString()}
                      {last.trigger
                        ? ` (${last.trigger === 'api' ? 'dashboard/API' : 'scheduled or command line'})`
                        : ''}
                    </p>
                    {last.object_storage_key && (
                      <p className="text-slate-600">Object Storage: {last.object_storage_key}</p>
                    )}
                    {last.local_path && <p className="text-slate-600">On this host: {last.local_path}</p>}
                    {last.problems.map((p) => (
                      <p key={p} className="text-rose-700">
                        {p}
                      </p>
                    ))}
                  </div>
                )}
              </div>
            </Card>

            <Card>
              <CardHeader
                title="Off-host backups (Linode Object Storage)"
                subtitle="Strongly recommended: without it, backups stay on this host and are lost with it."
                action={
                  obj?.configured && !editing ? (
                    <div className="flex gap-2">
                      <Button
                        disabled={busy !== null}
                        onClick={() =>
                          act('test', async () => {
                            await api.testSystemBackup()
                            return 'Connection test passed (a test object was written, read back and deleted).'
                          })
                        }
                      >
                        {busy === 'test' ? 'Testing…' : 'Test connection'}
                      </Button>
                      <Button
                        disabled={busy !== null}
                        onClick={() => {
                          setForm({
                            bucket: obj.bucket ?? '',
                            endpoint: obj.endpoint ?? '',
                            access_key: '',
                            secret_key: '',
                          })
                          setEditing(true)
                        }}
                      >
                        Change
                      </Button>
                    </div>
                  ) : undefined
                }
              />
              <div className="px-5 py-4 text-sm space-y-3">
                {obj?.configured && !editing && (
                  <div className="space-y-1 text-slate-700">
                    <p>
                      Bucket <strong>{obj.bucket}</strong> at {obj.endpoint}
                    </p>
                    <p>Access key {obj.access_key_hint}; secret key stored (never shown).</p>
                    <Button
                      variant="ghost"
                      disabled={busy !== null}
                      onClick={() =>
                        act('disable', async () => {
                          await api.disableSystemBackup()
                          return 'Object Storage backups turned off.'
                        })
                      }
                    >
                      Turn off off-host backups
                    </Button>
                  </div>
                )}
                {(!obj?.configured || editing) && (
                  <form
                    className="grid max-w-xl gap-3"
                    onSubmit={(e) => {
                      e.preventDefault()
                      void act('save', async () => {
                        await api.configureSystemBackup(form)
                        setEditing(false)
                        setForm({ bucket: '', endpoint: '', access_key: '', secret_key: '' })
                        return 'Saved. The connection test passed; backups now also go to Object Storage.'
                      })
                    }}
                  >
                    {!obj?.configured && (
                      <p className="text-amber-700">
                        Not configured — backups currently stay on this host only.
                      </p>
                    )}
                    <label className="block">
                      <span className="text-xs font-medium text-slate-500">Bucket</span>
                      <input
                        className={INPUT}
                        value={form.bucket}
                        onChange={(e) => setForm({ ...form, bucket: e.target.value })}
                      />
                    </label>
                    <label className="block">
                      <span className="text-xs font-medium text-slate-500">
                        Endpoint (e.g. https://in-maa-1.linodeobjects.com)
                      </span>
                      <input
                        className={INPUT}
                        value={form.endpoint}
                        onChange={(e) => setForm({ ...form, endpoint: e.target.value })}
                      />
                    </label>
                    <label className="block">
                      <span className="text-xs font-medium text-slate-500">Access key</span>
                      <input
                        className={INPUT}
                        autoComplete="off"
                        value={form.access_key}
                        onChange={(e) => setForm({ ...form, access_key: e.target.value })}
                      />
                    </label>
                    <label className="block">
                      <span className="text-xs font-medium text-slate-500">Secret key</span>
                      <input
                        className={INPUT}
                        type="password"
                        autoComplete="new-password"
                        value={form.secret_key}
                        onChange={(e) => setForm({ ...form, secret_key: e.target.value })}
                      />
                    </label>
                    <p className="text-xs text-slate-500">
                      The settings are tested (a small test object is written, read back and deleted) before
                      they are saved. Use a key limited to this bucket.
                    </p>
                    <div className="flex gap-2">
                      <Button variant="primary" type="submit" disabled={!formComplete || busy !== null}>
                        {busy === 'save' ? 'Testing and saving…' : 'Test and save'}
                      </Button>
                      {editing && (
                        <Button variant="ghost" type="button" onClick={() => setEditing(false)}>
                          Cancel
                        </Button>
                      )}
                    </div>
                  </form>
                )}
              </div>
            </Card>

            <Card>
              <CardHeader
                title="Backup on every change"
                subtitle="A fresh database snapshot goes to Object Storage within a minute of any change (schedules, groups, starts and stops, tokens, activity), on top of the scheduled backup."
              />
              <div className="space-y-1 px-5 py-4 text-sm text-slate-700">
                {!status.continuous?.enabled ? (
                  <p className="text-amber-700">Off — it needs Object Storage (above).</p>
                ) : (
                  <>
                    <p>
                      Last upload:{' '}
                      {status.continuous.last_backup_at
                        ? new Date(status.continuous.last_backup_at).toLocaleString()
                        : 'not yet'}
                      {status.continuous.pending_changes > 0
                        ? ` — ${status.continuous.pending_changes} change(s) waiting (uploaded by the scheduler within a minute)`
                        : ' — up to date'}
                    </p>
                    {status.continuous.last_error && (
                      <p className="text-rose-700">Last attempt failed: {status.continuous.last_error}</p>
                    )}
                  </>
                )}
              </div>
            </Card>

            <Card>
              <CardHeader
                title="Deployment SSH key"
                subtitle="Every managed instance trusts this key. It's kept encrypted (passphrase in .env, LIS_SSH_KEY_PASSPHRASE) in the database and every backup, in Object Storage, and its public half in your Linode profile's SSH keys, so a replacement host can get it back."
                action={
                  <Button
                    disabled={busy !== null}
                    onClick={() =>
                      act('protect', async () => {
                        const r = await api.protectSshKey()
                        return r.ok
                          ? 'The deployment SSH key is stored in every place.'
                          : `Not fully protected: ${r.problems.join('; ')}`
                      })
                    }
                  >
                    {busy === 'protect' ? 'Checking…' : 'Check now'}
                  </Button>
                }
              />
              <ul className="space-y-1 px-5 py-4 text-sm">
                {(() => {
                  const k = status.ssh_key
                  const rows: [string, boolean][] = [
                    ['Encryption passphrase set', !!k?.passphrase_set],
                    ['Encrypted copy in the database (and every backup)', !!k?.stored_in_database],
                    ['Encrypted copy in Object Storage', !!k?.in_object_storage],
                    ['Public key in Linode SSH keys', !!k?.linode_ssh_key_id],
                    [
                      'Passphrase recovery copy in the Linode account (private StackScript)',
                      !!k?.passphrase_escrowed,
                    ],
                  ]
                  return rows.map(([label, ok]) => (
                    <li key={label} className={ok ? 'text-emerald-700' : 'text-amber-700'}>
                      {ok ? '✓' : '✗'} {label}
                    </li>
                  ))
                })()}
              </ul>
            </Card>

            <Card>
              <CardHeader
                title="Encrypted settings backup (age)"
                subtitle="A copy of this deployment's settings (.env: API token, bucket keys, OAuth and alert settings, the key passphrase) and its SSH key, encrypted to your own age public key and kept in Object Storage. This host can write it but never read it back; only your age identity, kept off this host, opens it. A replacement host then needs just the bucket settings and your identity."
                action={
                  status.env_backup?.configured ? (
                    <Button
                      disabled={busy !== null}
                      onClick={() =>
                        act('env-backup', async () => {
                          const r = await api.runEnvBackup()
                          return `Encrypted settings${r.ssh_key_included ? ' and SSH key' : ''} uploaded.`
                        })
                      }
                    >
                      {busy === 'env-backup' ? 'Uploading…' : 'Back up now'}
                    </Button>
                  ) : undefined
                }
              />
              <div className="space-y-3 px-5 py-4 text-sm text-slate-700">
                {status.env_backup?.configured ? (
                  <>
                    <p>
                      Encrypted to {status.env_backup.recipients.join(', ')} — last uploaded{' '}
                      {status.env_backup.uploaded_at
                        ? new Date(status.env_backup.uploaded_at).toLocaleString()
                        : 'not yet'}
                      .
                    </p>
                    {status.env_backup.last_error && (
                      <p className="text-rose-700">Last attempt failed: {status.env_backup.last_error}</p>
                    )}
                  </>
                ) : (
                  <p className="text-amber-700">Not set up.</p>
                )}
                <form
                  className="flex max-w-2xl gap-2"
                  onSubmit={(e) => {
                    e.preventDefault()
                    void act('env-recipients', async () => {
                      const r = await api.setEnvBackupRecipients(recipientsInput)
                      setRecipientsInput('')
                      if (!recipientsInput.trim()) return 'Encrypted settings backup turned off.'
                      return r.result?.outcome === 'uploaded'
                        ? 'Saved; first encrypted copy uploaded.'
                        : `Saved. ${r.result?.detail ?? ''}`
                    })
                  }}
                >
                  <input
                    className={INPUT}
                    aria-label="Age public key"
                    placeholder="age1… (your age public key; empty turns it off)"
                    value={recipientsInput}
                    onChange={(e) => setRecipientsInput(e.target.value)}
                  />
                  <Button type="submit" disabled={busy !== null}>
                    {busy === 'env-recipients' ? 'Saving…' : 'Save'}
                  </Button>
                </form>
                <p className="text-xs text-slate-500">
                  Make a keypair on your own machine with <code>age-keygen -o key.txt</code> and paste only
                  the public key (age1…) here. Keep key.txt in your password manager; recovery uses it with
                  <code> install.sh recover --env-from-backup</code>.
                </p>
              </div>
            </Card>

            <Card>
              <CardHeader
                title="Alerts"
                subtitle="Sent when the scheduler stops, backups fail, the deployment key isn't stored in every place, or nodes stay off their schedule; again every 6 hours while it lasts, and once when it's resolved. Channels are set in .env (LIS_ALERT_WEBHOOK_URL, LIS_ALERT_EMAIL_TO and the SMTP settings)."
                action={
                  <Button
                    disabled={busy !== null || !(health?.alerts.webhook || health?.alerts.email)}
                    onClick={() =>
                      act('alert-test', async () => {
                        const r = await api.testAlerts()
                        const failed = Object.entries(r.results)
                          .filter(([, ok]) => !ok)
                          .map(([c]) => c)
                        return failed.length
                          ? `Test alert failed on: ${failed.join(', ')}`
                          : 'Test alert sent.'
                      })
                    }
                  >
                    {busy === 'alert-test' ? 'Sending…' : 'Send test alert'}
                  </Button>
                }
              />
              <div className="space-y-2 px-5 py-4 text-sm text-slate-700">
                <p>
                  Webhook: {health?.alerts.webhook ? 'set' : 'not set'} · Email:{' '}
                  {health?.alerts.email ? health.alerts.email_to.join(', ') : 'not set'}
                </p>
                {health && health.problems.length === 0 && (
                  <p className="text-emerald-700">✓ Nothing needs attention.</p>
                )}
                {health?.problems.map((p) => (
                  <p key={p.key} className="text-rose-700">
                    ✗ {p.message}
                  </p>
                ))}
              </div>
            </Card>

            <Card>
              <CardHeader title="On this host" />
              <p className="px-5 py-4 text-sm text-slate-700">
                {status.local_backup_dir ? (
                  <>
                    Snapshots are also written to <code>{status.local_backup_dir}</code>.
                  </>
                ) : (
                  'No local backup folder is set up.'
                )}
              </p>
            </Card>
          </>
        )}
      </div>
    </>
  )
}
