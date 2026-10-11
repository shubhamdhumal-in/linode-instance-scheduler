import { expect, test } from '@playwright/test'
import type { Route } from '@playwright/test'
import { baseInstanceRecord, loginAs, mockGroupList, mockInstanceList } from './mocks'
const due = new Date(Date.now() + 7 * 86400000).toISOString()
const entry = {
  id: 7,
  name: 'redis-1',
  kind: 'node',
  status: 'pending',
  created_at: new Date().toISOString(),
  due_at: due,
  detail: null,
  updated_at: new Date().toISOString(),
  resources: [
    { type: 'volume', id: 10, role: 'os', action: 'delete' },
    { type: 'ip', id: '1.2.3.4', role: 'ip', action: 'delete' },
  ],
}
test('offboarding tells the user nothing was deleted yet and where to recover it', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, { 'redis-1': baseInstanceRecord({ current_status: 'stopped' }) })
  await mockGroupList(page)
  await page.route('**/instances/redis-1/offboard', (r: Route) =>
    r.fulfill({ json: { outcome: 'pending_deletion', detail: null, pending_deletion: entry } }),
  )
  await page.goto('/ui/#/instances/redis-1')
  await page.locator('#offboard-confirm').fill('redis-1')
  await page.getByRole('button', { name: 'Offboard instance' }).click()
  const notice = page.getByTestId('offboard-pending-notice')
  await expect(notice).toContainText('Nothing has actually been deleted yet')
  await notice.getByRole('button', { name: 'Pending deletion' }).click()
  await expect(page).toHaveURL(/pending-deletions/)
})
test('delete now needs the name typed; recover brings the node back', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, {})
  await mockGroupList(page)
  await page.route('**/pending-deletions', (r: Route) =>
    r.fulfill({ json: { grace_days: 7, entries: [entry] } }),
  )
  let deleted = 0
  let recovered = 0
  await page.route('**/pending-deletions/7/delete-now', async (r: Route) => {
    deleted += 1
    await r.fulfill({ json: { operation_id: 'op-d', total_steps: 4 } })
  })
  await page.route('**/pending-deletions/7/recover', async (r: Route) => {
    recovered += 1
    await r.fulfill({ json: { operation_id: 'op-r', total_steps: 6 } })
  })
  await page.route('**/operations/op-r', (r: Route) =>
    r.fulfill({
      json: {
        status: 'done',
        action: 'pending_recover',
        percent: 100,
        current_step: null,
        warnings: [],
        result: { ...entry, status: 'recovered' },
      },
    }),
  )
  await page.goto('/ui/#/pending-deletions')
  const row = page.getByTestId('pending-7')
  await expect(row).toContainText('redis-1')
  await expect(row).toContainText('reserved IP 1.2.3.4')
  await row.getByRole('button', { name: 'Delete now' }).click()
  const confirmButton = row.getByRole('button', { name: 'Delete permanently now' })
  await expect(confirmButton).toBeDisabled()
  await row.getByRole('textbox').fill('redis-')
  await expect(confirmButton).toBeDisabled()
  await row.getByRole('button', { name: 'Cancel' }).click()
  expect(deleted).toBe(0)
  await row.getByRole('button', { name: 'Recover' }).click()
  await expect(row).toContainText('Nothing has been deleted yet')
  await row.getByRole('button', { name: 'Recover now' }).click()
  await expect(row).toContainText('is back under management')
  expect(recovered).toBe(1)
})
