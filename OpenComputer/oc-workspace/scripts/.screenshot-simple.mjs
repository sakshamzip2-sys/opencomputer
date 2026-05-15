import { chromium } from 'playwright'

const url = process.argv[2] || 'http://127.0.0.1:3010/chats-preview'
const out = process.argv[3] || '/tmp/shot.png'
const waitMs = Number(process.argv[4] || 4000)

const browser = await chromium.launch({ headless: true })
const ctx = await browser.newContext({
  viewport: { width: 1440, height: 900 },
})
await ctx.addInitScript(() => {
  try {
    // Mark onboarding complete (real key from claude-onboarding.tsx)
    localStorage.setItem('claude-onboarding-complete', 'true')
    localStorage.setItem('hermes-onboarding-completed', '1')
    localStorage.setItem('claude-onboarding-completed', '1')
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
await page.waitForTimeout(waitMs)
await page.screenshot({ path: out, fullPage: false })
console.log('OK', out)
await browser.close()
