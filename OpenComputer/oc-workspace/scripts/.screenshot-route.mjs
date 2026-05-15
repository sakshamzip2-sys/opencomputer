import { chromium } from 'playwright'

const url = process.argv[2] || 'http://127.0.0.1:3000/chats'
const out = process.argv[3] || '/tmp/shot.png'

const browser = await chromium.launch({ headless: true })
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
await ctx.addInitScript(() => {
  try {
    localStorage.setItem('claude-onboarding-complete', 'true')
    localStorage.setItem('claude-last-session', 'main')
    localStorage.setItem('claude-update-banner-dismissed', '1')
    localStorage.setItem('hermes-update-seen-version', 'all')
  } catch (e) {}
})
const page = await ctx.newPage()
await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {})
await page.waitForTimeout(2500)
// Dismiss splash
await page.evaluate(() => {
  if (typeof window.__dismissSplash === 'function') window.__dismissSplash()
}).catch(() => {})
await page.waitForTimeout(1500)
// Dismiss any modal with a Continue or Close button
for (let i = 0; i < 4; i++) {
  const dismissed = await page.evaluate(() => {
    const buttons = Array.from(document.querySelectorAll('button'))
    const target = buttons.find(b => /^(continue|close|dismiss|got it)$/i.test((b.textContent || '').trim()))
    if (target) { target.click(); return true }
    return false
  })
  if (!dismissed) break
  await page.waitForTimeout(500)
}
await page.waitForTimeout(800)
await page.screenshot({ path: out, fullPage: false })
console.log('OK', out)
await browser.close()
