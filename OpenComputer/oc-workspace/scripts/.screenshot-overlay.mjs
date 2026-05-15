import { chromium } from 'playwright'

const url = process.argv[2] || 'http://127.0.0.1:3010/chat/main'
const out = process.argv[3] || '/tmp/shot.png'

const browser = await chromium.launch({ headless: true })
const ctx = await browser.newContext({
  viewport: { width: 1440, height: 900 },
})
await ctx.addInitScript(() => {
  try {
    localStorage.setItem('claude-onboarding-completed', '1')
    localStorage.setItem('hermes-onboarding-completed', '1')
    localStorage.setItem('claude-last-session', 'main')
    localStorage.setItem('claude-update-banner-dismissed', '1')
  } catch (e) {}
})
const page = await ctx.newPage()
page.on('pageerror', e => console.log('[pageerror]', e.message))
try {
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 })
} catch (e) {
  console.log('goto-failed:', e.message)
}

async function dismissByText(re) {
  try {
    const el = page.getByRole('button', { name: re }).first()
    if (await el.isVisible({ timeout: 1200 }).catch(() => false)) {
      await el.click({ delay: 50, force: true }).catch(() => {})
      await page.waitForTimeout(400)
      return true
    }
  } catch {}
  try {
    const el = page.getByText(re).first()
    if (await el.isVisible({ timeout: 1000 }).catch(() => false)) {
      await el.click({ delay: 50, force: true }).catch(() => {})
      await page.waitForTimeout(400)
      return true
    }
  } catch {}
  return false
}

// Aggressively dismiss every modal by direct DOM removal — overlays use high
// z-index and intercept pointer events, so click-based dismissal is unreliable.
// Just wipe any fixed-position modal/backdrop nodes from the DOM.
await page.waitForTimeout(2500)
for (let attempt = 0; attempt < 6; attempt++) {
  const removed = await page.evaluate(() => {
    let n = 0
    // Remove anything fixed at full inset with high z-index (modal/backdrop)
    const all = Array.from(document.querySelectorAll('div'))
    for (const el of all) {
      const style = getComputedStyle(el)
      if (style.position !== 'fixed') continue
      const z = parseInt(style.zIndex || '0', 10)
      if (!z || z < 50) continue
      const r = el.getBoundingClientRect()
      if (r.width < window.innerWidth * 0.4 || r.height < 80) continue
      // Don't remove our own RecentChatsOverlay (z-index 1000 too)
      if (el.getAttribute('role') === 'dialog' && el.getAttribute('aria-label') === 'All chats') continue
      el.remove()
      n++
    }
    return n
  })
  if (removed === 0) break
  await page.waitForTimeout(300)
}

// Wait for the chat header to render and click the title to open the overlay
try {
  const titleBtn = page
    .locator(
      'button[title="Browse all chats"], button[title*="Click to switch"]',
    )
    .first()
  await titleBtn.waitFor({ state: 'visible', timeout: 5000 })
  await titleBtn.click()
  await page.waitForTimeout(1200)
} catch (e) {
  console.log('overlay-click-failed:', e.message)
}

await page.screenshot({ path: out, fullPage: false })
console.log('OK', out)
await browser.close()
