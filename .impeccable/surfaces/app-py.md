---
version: 1
slug: "app-py"
primary_target: "app.py"
related_targets: []
---

# Trustworthy product and comparison prices

Mode: Operate. Extend the existing Food Sale grid; preserve its warm-neutral/brick-red palette, light/dark tokens, typography and responsive two-column phone layout. The user now allows different pack sizes for the same brand and flavour/product type. Use exact identifiers first, then conservative product-family matching; show a compact store list with only a current price per kilo/liter/styck. Pack sizes and source metadata stay collapsed behind the keyboard/touch-accessible price details. Missing data remains explicit. The user confirmed OLW for Lidl product 12178463, whose source omits brand. Follow the user's preference for automated checks without visual browser testing.

## Direction contract

THESIS: Each product card leads with the shop’s sale price in large red type, then compact Jmf-pris in kr/kg, kr/l or kr/st with purchase limit inline, ordinary price, its Jmf-pris, a source-provided 30-day low if present and bold Besparing. No repeated price or membership lines. Comparisons retain their trustworthy unit-price focus.

OWN-WORLD: Existing warm surfaces, restrained red controls, tabular price numerals and flat divided store rows. No new visual identity or decorative assets.

STORY: Read the shop’s prominent offer price followed by its compact unit quote and ordinary-price comparison. The front and back use one Decimal-based engine and one server-rendered display value. Explicit source quantity ranges are shown as ranges, never ranked as exact quotes. Conflicting or incomplete data shows an honest unavailable state. Activate the product image, choose a precise variant where necessary, then read one comparable unit price per store. Remove original/pack prices, discount percentages, difference percentages, and verbose metadata. Keep only short mandatory eligibility markers; metadata remains in keyboard/touch-accessible native details on the unit price. Unavailable is not an absence claim.

FIRST VIEWPORT: Every front card leads with the original offer price followed by Jmf-pris, a labelled range or an explicit unavailable quote; native details explain its source/calculation. Only the activated card rotates. Its back has a product heading, 44px return control, progress status, an independently scrolling price list and a clear retry state. The surrounding grid stays stable.

FORM: seed: none (pinned), “it flips just that card around”. One restrained 3D turn is the signature motion; reduced motion switches instantly. Keyboard focus follows the active face. Lazy requests, shared caches and bounded background workers keep filters responsive.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
