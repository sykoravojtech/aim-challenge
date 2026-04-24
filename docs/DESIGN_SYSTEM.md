# Design system — Aim demo frontend

Visual tokens for the Phase 4.5 static HTML demo. Lifted from [startaiming.com](https://www.startaiming.com) so the interviewer sees their own aesthetic rendered locally. Source: scraped `_next/static/css/*.css` on 2026-04-23, extracted the `:root` CSS custom properties and the dominant Tailwind utility colors.

The goal is **"looks like Aim's site"**, not pixel-identical. Close enough that the visual cue lands; simple enough to build in 45 minutes with one hand-written CSS file.

---

## 1. Colors

### Brand

| Token | Hex | `rgb()` | Source var on startaiming.com |
|---|---|---|---|
| `--aim-purple` | `#552CD9` | `rgb(85, 44, 217)` | `--purple-aim: 85,44,217` |
| `--aim-purple-hover` | `#4423AE` | `rgb(68, 35, 174)` | `--purple-aim-hover: 68,35,174` |

**Where to use:** primary button background, active chip background, headline word-accent, link underline color, section divider accents.

### Neutrals

| Token | Hex | Tailwind equivalent | Use |
|---|---|---|---|
| `--bg-page` | `#FFFFFF` | `white` | Page background |
| `--bg-muted` | `#F9FAFB` | `gray-50` | Subtle section backgrounds, hover on list rows |
| `--bg-card` | `#F3F4F6` | `gray-100` | Unfilled input fields, tag backgrounds |
| `--text-primary` | `#111827` | `gray-900` | Headlines, primary prose |
| `--text-secondary` | `#4B5563` | `gray-600` | Body copy, meta information |
| `--text-muted` | `#6B7280` | `gray-500` | Timestamps, de-emphasised labels |
| `--border-default` | `#E5E7EB` | `gray-200` | Card borders, input borders |

**Never invent shades outside this list.** If a shade is missing, promote the need into this table rather than one-offing it in CSS.

### Semantic

| Token | Hex | Use |
|---|---|---|
| `--ok-green` | `#22C55E` | Success states (digest ready) |
| `--warn-amber` | `#F59E0B` | Loading/polling states |
| `--error-red` | `#EF4444` | Failed pipeline, 404s |

Aim's site uses these sparingly — they appear in shadcn/ui default Tailwind palette. Use them only for genuine state signalling, never decorative.

---

## 2. Typography

### Font family

- **Inter**, loaded from Google Fonts CDN:

  ```html
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
  ```

- **Fallback stack:** `Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif` — matches what Aim's CSS declares after `--font-sans`.

- **No serif.** Aim's brand is a sans-only tech aesthetic.

### Scale

| Role | Size | Weight | Line height | CSS |
|---|---|---|---|---|
| Hero / page headline | `2.25rem` (36 px) | 700 | 1.15 | `font-size: 2.25rem; font-weight: 700;` |
| Section title | `1.5rem` (24 px) | 600 | 1.25 | |
| Card title | `1.125rem` (18 px) | 600 | 1.35 | |
| Body | `1rem` (16 px) | 400 | 1.55 | |
| Meta / caption | `0.875rem` (14 px) | 500 | 1.4 | |
| Chip / badge | `0.75rem` (12 px) | 600 | 1.2 | Uppercase, tracking `0.04em` |

**Rule of thumb:** only these six sizes. Mixing 17/19/22 adds noise and reads as amateur.

---

## 3. Radii, spacing, shadows

### Border radius

| Token | Value | Use |
|---|---|---|
| `--radius-sm` | `0.25rem` (4 px) | Tiny chips, inline tags |
| `--radius` | `0.5rem` (8 px) | **Default** — buttons, cards, inputs. Matches Aim's `--radius: 0.5rem` |
| `--radius-lg` | `0.75rem` (12 px) | Large cards, digest container |
| `--radius-full` | `9999px` | Pill chips, avatar circles |

### Spacing scale (8-point grid)

```
4, 8, 12, 16, 20, 24, 32, 40, 48, 64  (px)
```

Expose as CSS custom properties `--space-1` through `--space-10` if you prefer, or just use raw values. Either is fine for 45 min of CSS.

### Shadows

Two levels only:

```css
--shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.05);
--shadow-md: 0 1px 3px rgba(0, 0, 0, 0.05), 0 4px 12px rgba(0, 0, 0, 0.05);
```

Use `--shadow-sm` for inputs/buttons, `--shadow-md` for cards and hover states. Nothing heavier. Aim's site is shadow-light — crisp edges beat blurry elevation.

---

## 4. Components — minimum viable

### Button (primary)

```css
background: var(--aim-purple);
color: white;
padding: 10px 20px;
border-radius: var(--radius);
font-weight: 600;
font-size: 0.875rem;
transition: background-color 120ms ease;
```

Hover: `background: var(--aim-purple-hover)`. Disabled: `opacity: 0.5; cursor: not-allowed`.

### Button (secondary)

```css
background: var(--bg-muted);
color: var(--text-primary);
border: 1px solid var(--border-default);
/* rest same as primary */
```

Hover: `background: var(--bg-card)`.

### Card (digest section, aim row)

```css
background: white;
border: 1px solid var(--border-default);
border-radius: var(--radius-lg);
padding: 24px;
box-shadow: var(--shadow-sm);
```

Hover (for interactive cards): swap to `--shadow-md` + lift 2 px (`transform: translateY(-2px)`). 200 ms ease.

### Input / textarea

```css
background: white;
border: 1px solid var(--border-default);
border-radius: var(--radius);
padding: 10px 12px;
font-size: 1rem;
font-family: inherit;
```

Focus: `border-color: var(--aim-purple); box-shadow: 0 0 0 3px rgba(85, 44, 217, 0.15);`

### Chip / tag (for source URLs, regions, entity tags)

```css
display: inline-flex;
align-items: center;
gap: 6px;
padding: 4px 10px;
border-radius: var(--radius-full);
background: rgba(85, 44, 217, 0.08);
color: var(--aim-purple);
font-size: 0.75rem;
font-weight: 600;
text-decoration: none;
```

Hover (if clickable): `background: rgba(85, 44, 217, 0.14)`.

---

## 5. Layout

- **Page max-width:** `1120px`, centered with `margin: 0 auto; padding: 0 24px`.
- **Section vertical spacing:** 48 px between major sections, 24 px between a section's subsections.
- **Card grid:** single column up to ~900 px viewport; nothing fancier for v1.
- **Header:** fixed-height 64 px, white background, bottom border `--border-default`, contains logo-mark + current-user label + nav.

---

## 6. Motion

- Transitions: `120ms ease` for color/background, `200ms ease` for transform/shadow.
- No entry animations for v1 — they eat the 45-minute budget without adding real signal.
- Loading states: CSS-only spinner (8-border-segment trick) with `animation: spin 0.8s linear infinite`. No Lottie, no SVG animation.

---

## 7. Iconography

- **No icon library.** A few inline SVGs is cheaper than adding Lucide/Heroicons.
- Icons needed for v1: check-circle (success), x-circle (error), refresh (loading), external-link (on source chips). Four SVGs, ~200 bytes each inline.

---

## 8. What NOT to copy from startaiming.com

The live site has a fair amount we should *not* replicate:

- **Hero imagery** — portraits, testimonials, logo walls. Scope creep; we're not selling the product, we're demoing the pipeline.
- **Gradient backgrounds** — the site has subtle gradient zones. Our demo uses flat whites; clearer visual hierarchy for data-dense content.
- **Complex hover-reveal animations** — the site has a few "lift + glow" card animations. The simple shadow-md/lift above is enough.
- **Marketing copy tone** — use functional labels ("Create Aim", "Generate Digest"), not marketing ("Start aiming today!").

---

## 9. Accessibility floor (not aspiration)

- Contrast: brand purple on white passes WCAG AA for large text; for small purple text use `--aim-purple` on `--bg-page` only at 14 px+ 600 weight. For smaller purple text, use `--text-primary` instead.
- Focus rings: never `outline: none` without replacing. Use `:focus-visible { outline: 2px solid var(--aim-purple); outline-offset: 2px }`.
- Every button must have visible text or an `aria-label`.
- Form inputs must have associated `<label>` elements.

Not aspirational — baseline. Anything below this is broken.

---

## 10. Review checklist before Phase 4.5 ships

- [ ] All colors come from tokens, no inline hex codes scattered in CSS
- [ ] All typography uses the 6-size scale, no one-off font sizes
- [ ] All border radii come from the 4 tokens
- [ ] All shadows come from the 2 shadow tokens
- [ ] No component uses more than 2 levels of nesting in CSS
- [ ] Focus-visible styling exists on every interactive element
- [ ] Page works at 375 px width (iPhone SE) without horizontal scroll
- [ ] Dark mode toggle is NOT attempted (scope)

---

## Reference

- Live site: https://www.startaiming.com
- Tailwind gray scale: https://tailwindcss.com/docs/customizing-colors
- Inter font specimen: https://rsms.me/inter/
- WCAG contrast checker: https://webaim.org/resources/contrastchecker/
