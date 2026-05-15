import { chromium } from 'playwright'

const url = process.argv[2] || 'http://127.0.0.1:3000/chats'
const out = process.argv[3] || '/tmp/shot.png'

const browser = await chromium.launch({ headless: true })
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
await ctx.addInitScript(() => {
  try {
    localStorage.setItem('claude-onboarding-complete', 'true')
    localStorage.setItem('claude-last-session', 'main')
    // Mark every conceivable "seen" key so update banners stay hidden
    localStorage.setItem('claude-update-banner-dismissed', '1')
    localStorage.setItem('hermes-update-seen', '1')
    localStorage.setItem('hermes-update-version-seen', '99.99.99')
  } catch (e) {}
})
const page = await ctx.newPage()
await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {})
await page.waitForTimeout(4000)
// Dismiss splash via the exposed window helper (if any)
await page.evaluate(() => {
  if (typeof window.__dismissSplash === 'function') window.__dismissSplash()
}).catch(() => {})
await page.waitForTimeout(800)
// Specifically target the "OpenComputer updated" modal Continue button
// (it's in a backdrop with text "OpenComputer updated")
await page.evaluate(() => {
  const modals = Array.from(document.querySelectorAll('div'))
  for (const m of modals) {
    if (!/OpenComputer updated|update available/i.test(m.textContent || '')) continue
    const btns = m.querySelectorAll('button')
    for (const b of btns) {
      if (/^continue$/i.test((b.textContent || '').trim())) {
        b.click()
        return true
      }
    }
  }
  return false
}).catch(() => {})
await page.waitForTimeout(1200)
await page.screenshot({ path: out, fullPage: false })
console.log('OK', out)
await browser.close()
