---
name: accessibility-audit
description: Use when checking WCAG compliance, ARIA roles, keyboard navigation, screen reader testing, or a11y issues in UI
---

# Accessibility Audit

## When to use

- New UI component / page
- Pre-launch audit
- Bug report from a user with assistive tech

## Steps

1. **Keyboard-only pass.** Disconnect the mouse. Tab through every flow. Can you do everything? Visible focus rings? Logical tab order?
2. **Screen reader pass.** macOS VoiceOver (`Cmd+F5`) or NVDA on Windows. Read the page top-to-bottom. Does the experience make sense without sight?
3. **Semantic HTML first.** `<button>` not `<div onclick>`. Native elements have a11y for free; ARIA is a fallback when semantics don't fit.
4. **Color contrast.** WCAG AA: 4.5:1 for normal text, 3:1 for large/UI. Test in a contrast checker; don't eyeball.
5. **Don't rely on color alone.** Red/green error states need an icon or text label too.
6. **Form fields.** Every input has a `<label for>`. Errors are programmatically linked via `aria-describedby`.
7. **Focus management.** Modals trap focus; closing returns focus to the trigger. Route changes announce the new page title.
8. **Automated scan.** axe DevTools / Lighthouse a11y audit. Catches ~30% of issues automatically. The other 70% need manual.

## Notes

- "Skip to main content" link at top of page — first tab stop. Trivial to add, huge UX win for keyboard users.
- `alt=""` on decorative images, `alt="meaningful description"` on informative ones. Never omit `alt`.
- Reduced motion: respect `prefers-reduced-motion: reduce` and disable parallax / autoplay.
- Don't outsource a11y to a third-party widget; it usually makes things worse.
