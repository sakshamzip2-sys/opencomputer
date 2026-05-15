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
page.on('pageerror', e => console.log('[pageerror]', e.message))

async function dismissUpdateBanner() {
  try {
    const cont = page.locator('button:has-text("Continue")').first()
    if (await cont.isVisible({ timeout: 800 }).catch(() => false)) {
      await cont.click({ force: true }).catch(() => {})
      await page.waitForTimeout(400)
    }
  } catch {}
}

async function shoot(name, action) {
  if (action) await action()
  await page.screenshot({ path: `/tmp/oc-realflow-${name}.png` })
  console.log('OK', name)
}

await page.goto('http://127.0.0.1:3000/chat/main', { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2500)
// Dismiss the "Connecting to your backend..." splash that gates the shell
await page.evaluate(() => {
  if (typeof window.__dismissSplash === 'function') window.__dismissSplash()
}).catch(() => {})
await page.waitForTimeout(2500)
await dismissUpdateBanner()
await page.waitForTimeout(800)
await shoot('00-loaded')

// Find and click the chat title to open the overlay
const titleBtn = page.locator('button[title="Browse all chats"]').first()
try {
  await titleBtn.waitFor({ state: 'visible', timeout: 6000 })
  await titleBtn.click()
  await page.waitForTimeout(1200)
  await shoot('01-overlay-open')
} catch (e) {
  console.log('title-not-found:', e.message)
}

// Enter select mode
const selectBtn = page.locator('button:has-text("Select chats")').first()
try {
  if (await selectBtn.isVisible({ timeout: 1500 }).catch(() => false)) {
    await selectBtn.click()
    await page.waitForTimeout(700)
    await shoot('02-select-mode')
    // Click a few rows
    const rows = page.locator('li.group')
    const n = await rows.count()
    for (let i = 0; i < Math.min(2, n); i++) {
      await rows.nth(i).click({ force: true }).catch(() => {})
    }
    await page.waitForTimeout(500)
    await shoot('03-two-selected')
  }
} catch (e) {
  console.log('select-flow-failed:', e.message)
}

await browser.close()
