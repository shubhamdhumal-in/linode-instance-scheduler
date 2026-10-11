import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { AlertSettings } from '../api/types'
import { Button } from './ui'
const INPUT =
  'mt-1 block w-full rounded-md border-0 py-1.5 px-3 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600'
export function AlertSettingsForm({ onSaved }: { onSaved: (message: string) => void }) {
  const [settings, setSettings] = useState<AlertSettings | null>(null)
  const [form, setForm] = useState({
    webhook_url: '',
    email_to: '',
    smtp_host: '',
    smtp_port: '',
    smtp_security: 'starttls',
    smtp_user: '',
    smtp_password: '',
    email_from: '',
  })
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  useEffect(() => {
    api.getAlertSettings().then(
      (s) => {
        setSettings(s)
        setForm((f) => ({
          ...f,
          email_to: s.email_to.join(', '),
          smtp_host: s.smtp_host ?? '',
          smtp_port: s.smtp_port ?? '',
          smtp_security: s.smtp_security,
          smtp_user: s.smtp_user ?? '',
          email_from: s.email_from ?? '',
        }))
      },
      () => setSettings(null),
    )
  }, [])
  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      const body: Record<string, string> = {
        email_to: form.email_to,
        smtp_host: form.smtp_host,
        smtp_port: form.smtp_port,
        smtp_security: form.smtp_security,
        smtp_user: form.smtp_user,
        email_from: form.email_from,
      }
      if (form.webhook_url.trim()) body.webhook_url = form.webhook_url.trim()
      if (form.smtp_password) body.smtp_password = form.smtp_password
      const s = await api.setAlertSettings(body)
      setSettings(s)
      setForm((f) => ({ ...f, webhook_url: '', smtp_password: '' }))
      onSaved('Alert settings saved. Send a test alert to check them.')
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not save the alert settings.')
    } finally {
      setSaving(false)
    }
  }
  const clearWebhook = async () => {
    try {
      setSettings(await api.setAlertSettings({ webhook_url: '' }))
      onSaved('Webhook alerts turned off.')
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not change the alert settings.')
    }
  }
  const field = (key: keyof typeof form, label: string, extra: Record<string, string> = {}) => (
    <label className="block">
      <span className="text-xs font-medium text-slate-500">{label}</span>
      <input
        className={INPUT}
        aria-label={label}
        value={form[key]}
        {...extra}
        onChange={(e) => setForm({ ...form, [key]: e.target.value })}
      />
    </label>
  )
  return (
    <form
      className="grid max-w-2xl gap-3"
      onSubmit={(e) => {
        e.preventDefault()
        void save()
      }}
    >
      <div>
        {field('webhook_url', 'Webhook URL', {
          placeholder: settings?.webhook_set
            ? `set (${settings.webhook_host}) — paste a new one to replace it`
            : 'https://hooks.slack.com/services/…',
          autoComplete: 'off',
        })}
        {settings?.webhook_set && (
          <button
            type="button"
            className="mt-1 text-xs text-indigo-600 hover:underline"
            onClick={() => void clearWebhook()}
          >
            Turn webhook alerts off
          </button>
        )}
      </div>
      <p className="text-xs text-slate-500">
        Email: alerts are sent through an SMTP server you already use — your mail provider (e.g. Google
        Workspace: smtp.gmail.com, port 587, an app password), or a sending service (SendGrid, Mailgun, Amazon
        SES, Brevo). Linode blocks outbound SMTP ports on new accounts until you ask support to open them; a
        provider that accepts port 2525 works without that.
      </p>
      {field('email_to', 'Send alerts to (comma-separated)', { placeholder: 'ops@example.com' })}
      <div className="grid grid-cols-3 gap-3">
        {field('smtp_host', 'SMTP server', { placeholder: 'smtp.example.com' })}
        {field('smtp_port', 'Port', { placeholder: '587' })}
        <label className="block">
          <span className="text-xs font-medium text-slate-500">Security</span>
          <select
            className={INPUT}
            aria-label="Security"
            value={form.smtp_security}
            onChange={(e) => setForm({ ...form, smtp_security: e.target.value })}
          >
            <option value="starttls">STARTTLS (587)</option>
            <option value="ssl">SSL/TLS (465)</option>
            <option value="none">None</option>
          </select>
        </label>
      </div>
      <div className="grid grid-cols-2 gap-3">
        {field('smtp_user', 'SMTP user', { autoComplete: 'off' })}
        {field('smtp_password', 'SMTP password', {
          type: 'password',
          autoComplete: 'new-password',
          placeholder: settings?.smtp_password_set ? 'set — type a new one to replace it' : '',
        })}
      </div>
      {field('email_from', 'From address (optional)', { placeholder: 'scheduler@example.com' })}
      {error && <p className="text-sm text-rose-700">{error}</p>}
      <div>
        <Button variant="primary" type="submit" disabled={saving}>
          {saving ? 'Saving…' : 'Save alert settings'}
        </Button>
      </div>
    </form>
  )
}
