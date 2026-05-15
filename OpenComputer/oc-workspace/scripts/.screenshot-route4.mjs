import { chromium } from 'playwright'

const url = process.argv[2] || 'http://127.0.0.1:3000/chats'
const out = process.argv[3] || '/tmp/shot.png'

const browser = await chromium.launch({ headless: true })
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
await ctx.addInitScript(() => {
  try {
    localStorage.setItem('claude-onboarding-complete', 'true')
    localStorage.setItem('claude-last-session', 'main')
    // Pre-dismiss the update-center for known products (workspace + agent).
    // Format: hermes-update-v2-dismissed:<id> = <id>:<latestHead-or-version-or-unknown>
    localStorage.setItem(
      'hermes-update-v2-dismissed:workspace',
      'workspace:2.3.0',
    )
    localStorage.setItem(
      'hermes-update-v2-dismissed:agent',
      'agent:6122a79aab45041d8b7c8d775f95be3ac6ce579f',
    )
    localStorage.setItem('hermes-update-v2-release-notes-seen', 'seen')
  } catch (e) {}
})
const page = await ctx.newPage()
await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {})
await page.waitForTimeout(5000)
await page.screenshot({ path: out, fullPage: false })
console.log('OK', out)
await browser.close()
