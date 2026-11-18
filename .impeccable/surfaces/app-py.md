---
version: 1
slug: "app-py"
primary_target: "app.py"
related_targets: []
---

# Product comparison card

Mode: Operate. Extend the existing Food Sale grid; preserve its warm-neutral/brick-red palette, light/dark tokens, typography and responsive two-column phone layout. The user now allows different pack sizes for the same brand and flavour/product type. Use exact identifiers first, then conservative product-family matching; show actual pack sizes and a common price per kg/litre/piece. Missing data remains explicit. The user confirmed OLW for Lidl product 12178463, whose source omits brand. Follow the user's preference for automated checks without visual browser testing.

## Direction contract

THESIS: Price comparison lives behind the product the shopper is already considering.

OWN-WORLD: Existing warm surfaces, restrained red controls, tabular price numerals and flat divided store rows. No new visual identity or decorative assets.

STORY: Activate the product image, choose a precise variant where necessary, then read original/current/discount prices plus prominent unit prices and differences relative to the selected store for the selected store and other Halmstad stores. Store scope, membership, quantities and freshness remain visible.

FIRST VIEWPORT: Only the activated card rotates. Its back has a product heading, 44px return control, progress status, an independently scrolling price list and a clear retry state. The surrounding grid stays stable.

FORM: seed: none (pinned), “it flips just that card around”. One restrained 3D turn is the signature motion; reduced motion switches instantly. Keyboard focus follows the active face. Lazy requests, shared caches and bounded background workers keep filters responsive.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
