import { chromium } from 'playwright'

const url = process.argv[2] || 'http://127.0.0.1:3000/chats'
const out = process.argv[3] || '/tmp/shot.png'

const browser = await chromium.launch({ headless: true })
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
await ctx.addInitScript(() => {
  try {
    localStorage.setItem('claude-onboarding-complete', 'true')
    localStorage.setItem('claude-last-session', 'main')
    // Find every key the dev set for "seen this update version" and pin a high one
    localStorage.setItem('claude-update-banner-dismissed', '1')
    localStorage.setItem('hermes-update-banner-dismissed', '1')
    localStorage.setItem('hermes-update-seen', '1')
    localStorage.setItem('hermes-update-version', '99.99.99')
    localStorage.setItem('claude-update-seen-version', '99.99.99')
  } catch (e) {}
})
const page = await ctx.newPage()
await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {})
await page.waitForTimeout(3500)
// Force-click the update modal's Continue via raw JS — bypass overlay
// intercept by directly invoking the click handler.
await page.evaluate(() => {
  const all = Array.from(document.querySelectorAll('button'))
  // Look for Continue button — Claude.ai-style "OpenComputer updated" modal
  const cont = all.find(b => /^continue$/i.test((b.textContent || '').trim()))
  if (cont) {
    cont.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
  }
  // Hide any remaining high-z fixed backdrop just for the screenshot
  const overlays = Array.from(document.querySelectorAll('div'))
  for (const el of overlays) {
    const cs = getComputedStyle(el)
    if (cs.position !== 'fixed') continue
    const z = parseInt(cs.zIndex || '0', 10)
    if (z >= 100 && /update/i.test(el.textContent || '')) {
      el.style.display = 'none'
    }
  }
}).catch(() => {})
await page.waitForTimeout(1500)
await page.screenshot({ path: out, fullPage: false })
console.log('OK', out)
await browser.close()
