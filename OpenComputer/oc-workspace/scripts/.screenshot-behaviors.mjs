import { chromium } from 'playwright'

const browser = await chromium.launch({ headless: true })
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
await ctx.addInitScript(() => {
  try {
    localStorage.setItem('claude-onboarding-complete', 'true')
    localStorage.setItem('claude-last-session', 'main')
    localStorage.setItem('claude-update-banner-dismissed', '1')
  } catch (e) {}
})
const page = await ctx.newPage()

async function shoot(name, url, fn) {
  await page.goto(url, { waitUntil: 'domcontentloaded' }).catch(() => {})
  await page.waitForTimeout(3000)
  if (fn) await fn()
  await page.screenshot({ path: `/tmp/oc-behavior-${name}.png` })
  console.log('OK', name)
}

// 1) Hover state on a row
await shoot('hover', 'http://127.0.0.1:3010/chats-preview', async () => {
  await page.hover('text=Redesigning layout to match second image').catch(() => {})
  await page.waitForTimeout(400)
})

// 2) Typing in search
await shoot('search', 'http://127.0.0.1:3010/chats-preview', async () => {
  const input = page.locator('input[placeholder="Search chats..."]').first()
  await input.fill('claude').catch(() => {})
  await page.waitForTimeout(400)
})

// 3) Selection mode with rows selected
await shoot('selected-3', 'http://127.0.0.1:3010/chats-preview?mode=select', async () => {
  // Click on a few row checkboxes
  const rows = page.locator('li.group')
  const count = await rows.count()
  for (let i = 0; i < Math.min(3, count); i++) {
    await rows.nth(i).click({ force: true }).catch(() => {})
  }
  await page.waitForTimeout(400)
})

// 4) "Select all" pressed (all selected → button shows "Deselect all")
await shoot('all-selected', 'http://127.0.0.1:3010/chats-preview?mode=select', async () => {
  await page.locator('button:has-text("Select all")').first().click({ force: true }).catch(() => {})
  await page.waitForTimeout(500)
})

await browser.close()
