"""Adapter: amazon/track_price — read product price from a logged-in cart.

Strategy.COOKIE — uses ``ctx.fetch_in_page`` so the user's Amazon session
cookies ride along automatically. We don't try to scrape from the public
PDP because Amazon's anti-bot infra makes that a losing battle; the
logged-in path is far more reliable.

This is intentionally a thin sample. Real users will customize the
selectors per locale; the agent's adapter-author skill walks through the
adaptation.
"""

from __future__ import annotations

from extensions.adapter_runner import Strategy, adapter


@adapter(
    site="amazon",
    name="track_price",
    description="Read the price of a product on Amazon (logged-in session required).",
    domain="amazon.com",
    strategy=Strategy.COOKIE,
    browser=True,
    args=[
        {"name": "url", "type": "string", "required": True, "help": "Amazon product URL"},
    ],
    columns=["title", "price", "currency", "in_stock", "url"],
)
async def run(args, ctx):
    url = (args.get("url") or "").strip()
    if not url:
        return []
    await ctx.navigate(url)
    # Read the salient bits via Runtime.evaluate. Amazon's DOM has been
    # stable enough on these classes to be a reasonable starting point;
    # users are expected to tweak per-locale.
    expr = """
        (() => {
          const titleEl = document.getElementById('productTitle');
          const priceEl = document.querySelector('.a-price .a-offscreen, #priceblock_ourprice, #priceblock_dealprice');
          const stockEl = document.getElementById('availability');
          let price = null, currency = null;
          if (priceEl) {
            const txt = priceEl.textContent.trim();
            const m = txt.match(/([£$€₹]|[A-Z]{3})?\\s*([0-9][0-9,.]*)/);
            if (m) { currency = m[1] || null; price = parseFloat(m[2].replace(/,/g, '')); }
          }
          return {
            title: titleEl ? titleEl.textContent.trim() : null,
            price,
            currency,
            in_stock: stockEl ? !/out of stock/i.test(stockEl.textContent) : null,
          };
        })()
    """
    info = await ctx.evaluate(expr)
    if not isinstance(info, dict):
        return []
    return [
        {
            "title": info.get("title") or "",
            "price": info.get("price"),
            "currency": info.get("currency") or "",
            "in_stock": info.get("in_stock"),
            "url": url,
        }
    ]
