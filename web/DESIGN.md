---
name: Stillwater
description: >-
  A calm, professional B2B design system built on the Tailwind CSS + shadcn/ui
  token contract. Cool grey paper, deep ink text, one harbor-blue primary, status
  colours borrowed from the chart sequence, Geist type with a Playfair Display serif
  for display moments, a 4px grid and quiet, ink-tinted, border-first elevation.
  Still water: the content moves, the chrome stays calm.
version: 2.0.0

colors:
  # Raw ramp. Source values are oklch (web/src/index.css holds them exactly); the hex here is the sRGB
  # equivalent, for reading. Cool, low-chroma greys tinted toward blue (hue ~250) plus one harbor-blue hue.
  white: "#FFFFFF"
  paper: "#F7F7F8"           # oklch(0.9764 0.0013 286.38)
  slate-50: "#F1F3F6"        # oklch(0.9635 0.0045 258.32)
  slate-100: "#EBEEF2"       # oklch(0.9480 0.0063 255.48)
  slate-200: "#DBE0E6"       # oklch(0.9046 0.0098 252.82)
  slate-500: "#5A6573"       # oklch(0.5025 0.0261 254.52)
  ink: "#1A2530"             # oklch(0.2593 0.0259 248.86)
  harbor-100: "#D9E6EE"      # oklch(0.9175 0.0177 234.51)
  harbor-300: "#83C4E7"      # oklch(0.7893 0.0828 232.78)
  harbor-600: "#1D6C96"      # oklch(0.5051 0.0989 237.38)
  harbor-700: "#1A6289"      # oklch(0.4721 0.0927 237.79)
  harbor-800: "#144A67"      # oklch(0.3890 0.0736 237.00)
  harbor-900: "#0F4866"      # oklch(0.3818 0.0757 237.41)
  sea-green: "#2F7A5C"       # oklch(0.5239 0.0887 163.87)
  sand: "#D9A24C"            # oklch(0.7479 0.1214 76.07)
  brick: "#B8413C"           # oklch(0.5413 0.1550 26.05)

  # Dark ramp ("deep water")
  depth-950: "#0B0F14"       # oklch(0.1665 0.0124 254.17)
  depth-900: "#0F1419"       # oklch(0.1884 0.0128 248.51)
  depth-850: "#161C24"       # oklch(0.2241 0.0181 255.71)
  depth-800: "#1C232D"       # oklch(0.2539 0.0216 257.28)
  depth-750: "#1E2630"       # oklch(0.2654 0.0220 254.20)
  depth-700: "#28313D"       # oklch(0.3100 0.0250 255.71)
  mist-400: "#8590A0"        # oklch(0.6498 0.0271 257.67)
  mist-100: "#E5E9EE"        # oklch(0.9324 0.0080 253.86)
  harbor-dark-200: "#A8CFE3" # oklch(0.8330 0.0499 230.37)
  harbor-dark-300: "#7AB0D0" # oklch(0.7313 0.0733 235.15)
  harbor-dark-400: "#5BA3CC" # oklch(0.6848 0.0943 235.39)
  sea-green-dark: "#56AD84"  # oklch(0.6820 0.1050 161.30)
  sand-dark: "#E8B25C"       # oklch(0.7963 0.1210 77.14)
  brick-dark: "#DC6E69"      # oklch(0.6657 0.1387 24.18)

  # Semantic aliases — light theme (default). Always design with these, never raw values.
  background: "{colors.paper}"
  foreground: "{colors.ink}"
  card: "{colors.white}"
  card-foreground: "{colors.ink}"
  popover: "{colors.white}"
  popover-foreground: "{colors.ink}"
  primary: "{colors.harbor-700}"
  primary-foreground: "{colors.white}"
  secondary: "{colors.slate-100}"
  secondary-foreground: "{colors.ink}"
  muted: "{colors.slate-100}"
  muted-foreground: "{colors.slate-500}"
  accent: "{colors.harbor-100}"
  accent-foreground: "{colors.harbor-900}"
  destructive: "{colors.brick}"
  destructive-foreground: "{colors.white}"
  border: "{colors.slate-200}"
  input: "{colors.slate-200}"
  ring: "{colors.harbor-600}"
  sidebar: "{colors.slate-50}"
  sidebar-border: "{colors.slate-200}"

  # Status — derived from the palette, never new hues. They follow each mode's chart / primary values.
  success: "{colors.chart-4}"
  warning: "{colors.chart-5}"
  info: "{colors.primary}"
  success-fg: "color-mix(in oklab, {colors.success} 86%, black)"
  warning-fg: "color-mix(in oklab, {colors.warning} 70%, black)"
  info-fg: "color-mix(in oklab, {colors.info} 87%, black)"

  # Chart sequence (data visualisation; chart-4 and chart-5 double as success and warning)
  chart-1: "{colors.harbor-600}"
  chart-2: "{colors.harbor-300}"
  chart-3: "{colors.harbor-800}"
  chart-4: "{colors.sea-green}"
  chart-5: "{colors.sand}"

# Dark theme overrides — applied by a `.dark` class on <html> or any ancestor.
colors-dark:
  background: "{colors.depth-900}"
  foreground: "{colors.mist-100}"
  card: "{colors.depth-850}"
  card-foreground: "{colors.mist-100}"
  popover: "{colors.depth-800}"
  popover-foreground: "{colors.mist-100}"
  primary: "{colors.harbor-dark-300}"
  primary-foreground: "{colors.depth-900}"
  secondary: "{colors.depth-750}"
  secondary-foreground: "{colors.mist-100}"
  muted: "{colors.depth-750}"
  muted-foreground: "{colors.mist-400}"
  accent: "{colors.depth-750}"
  accent-foreground: "{colors.harbor-dark-200}"
  destructive: "{colors.brick-dark}"
  destructive-foreground: "{colors.depth-900}"   # light text on brick-dark is only 3.2:1
  border: "{colors.depth-700}"
  input: "{colors.depth-700}"
  ring: "{colors.harbor-dark-300}"
  sidebar: "{colors.depth-950}"
  sidebar-border: "{colors.depth-750}"
  success-fg: "color-mix(in oklab, {colors.success} 70%, white)"
  warning-fg: "{colors.warning}"
  info-fg: "color-mix(in oklab, {colors.info} 80%, white)"
  chart-1: "{colors.harbor-dark-300}"
  chart-2: "{colors.harbor-dark-200}"
  chart-3: "{colors.harbor-dark-400}"
  chart-4: "{colors.sea-green-dark}"
  chart-5: "{colors.sand-dark}"

typography:
  fontFamily-sans: "Geist, ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
  fontFamily-serif: "'Playfair Display', ui-serif, Georgia, serif"   # display moments only
  fontFamily-mono: "'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, Consolas, monospace"
  display:
    fontFamily: Playfair Display
    fontSize: 3.75rem      # 60px — marketing hero only
    fontWeight: 400
    lineHeight: 1.1
    letterSpacing: -0.01em
  h1:
    fontFamily: Playfair Display
    fontSize: 2.5rem       # 40px — page heading (a display moment)
    fontWeight: 400
    lineHeight: 1.2
    letterSpacing: -0.01em
  h2:
    fontFamily: Geist
    fontSize: 1.5rem       # 24px
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: -0.02em
  title:
    fontFamily: Geist
    fontSize: 1.25rem      # 20px — section / dialog title
    fontWeight: 600
    lineHeight: 1.35
    letterSpacing: -0.02em
  topbar-title:
    fontFamily: Geist
    fontSize: 1.125rem     # 18px — app top bar page title
    fontWeight: 600
    lineHeight: 1.35
    letterSpacing: -0.02em
  card-title:
    fontFamily: Geist
    fontSize: 1.125rem     # 18px
    fontWeight: 600
    lineHeight: 1.35
    letterSpacing: -0.02em
  stat-value:
    fontFamily: Geist
    fontSize: 1.75rem      # 28px — KPI numbers
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: -0.02em
  blockquote:
    fontFamily: Playfair Display
    fontSize: 1.25rem      # 20px — quotes in documents and chat answers (a display moment)
    fontWeight: 400
    fontStyle: italic
    lineHeight: 1.5
    letterSpacing: 0em
  body:
    fontFamily: Geist
    fontSize: 1rem         # 16px
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: 0em
  body-sm:
    fontFamily: Geist
    fontSize: 0.875rem     # 14px — default UI text, tables, nav
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: 0em
  label:
    fontFamily: Geist
    fontSize: 0.875rem     # 14px — form labels, buttons
    fontWeight: 500
    lineHeight: 1.35
    letterSpacing: 0em
  caption:
    fontFamily: Geist
    fontSize: 0.75rem      # 12px — meta, timestamps, helper text
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: 0em
  overline:
    fontFamily: Geist
    fontSize: 0.75rem      # 12px — table headers, section eyebrows (UPPERCASE)
    fontWeight: 500
    lineHeight: 1.35
    letterSpacing: 0.08em
    textTransform: uppercase
  code:
    fontFamily: JetBrains Mono
    fontSize: 0.92em
    fontWeight: 400
    lineHeight: 1.5
  scale:                   # full size ramp (rem; 1rem = 16px)
    xs: 0.75rem
    sm: 0.875rem
    base: 1rem
    lg: 1.125rem
    xl: 1.25rem
    2xl: 1.5rem
    3xl: 1.875rem
    4xl: 2.25rem
    5xl: 3rem
    6xl: 3.75rem
    7xl: 4.5rem

spacing:                   # strict 4px grid
  "0": 0px
  "1": 4px
  "2": 8px
  "3": 12px
  "4": 16px
  "5": 20px
  "6": 24px
  "8": 32px
  "10": 40px
  "12": 48px
  "16": 64px
  "20": 80px
  "24": 96px
  gap-inline: "{spacing.2}"      # icon ↔ text
  gap-control: "{spacing.3}"     # between related controls
  gap-section: "{spacing.8}"     # between page sections
  pad-control-x: "{spacing.4}"
  pad-control-y: "{spacing.2}"
  pad-card: "{spacing.6}"
  pad-page: 28px                 # main content area padding

rounded:                   # all derived from one 10px knob
  base: 10px
  sm: 6px                  # checkbox, small chips
  md: 8px                  # buttons, inputs, selects, nav items, tab triggers
  lg: 10px                 # cards, alerts, tab list, dialogs
  xl: 14px
  2xl: 18px
  full: 9999px             # badges, avatars, switches, progress bars

elevation:                 # light: quiet and ink-tinted (hsl 210 30% 15%), never neutral black
  shadow-xs: "0 1px 4px -1px hsl(210 30% 15% / 0.03)"
  shadow-sm: "0 1px 4px -1px hsl(210 30% 15% / 0.05), 0 1px 2px -2px hsl(210 30% 15% / 0.05)"
  shadow-md: "0 1px 4px -1px hsl(210 30% 15% / 0.05), 0 2px 4px -2px hsl(210 30% 15% / 0.05)"
  shadow-lg: "0 1px 4px -1px hsl(210 30% 15% / 0.05), 0 4px 6px -2px hsl(210 30% 15% / 0.05)"
  shadow-xl: "0 1px 4px -1px hsl(210 30% 15% / 0.05), 0 8px 10px -2px hsl(210 30% 15% / 0.05)"
  # dark: stronger black shadows, because an ink tint disappears on deep water
  shadow-sm-dark: "0 1px 2px 0 rgba(0,0,0,0.4)"
  shadow-lg-dark: "0 12px 24px -4px rgba(0,0,0,0.6)"
  shadow-xl-dark: "0 24px 48px -8px rgba(0,0,0,0.7)"
  ring-width: 3px
  ring-offset: 2px

motion:
  duration-fast: 120ms     # hovers, colour changes
  duration-base: 180ms     # toggles, switches
  duration-slow: 260ms     # progress width, panel reveals
  ease-out: "cubic-bezier(0.16, 1, 0.3, 1)"
  ease-in-out: "cubic-bezier(0.65, 0, 0.35, 1)"
  press-scale: 0.98

components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-foreground}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    height: 40px
    padding: 0 16px
    shadow: "{elevation.shadow-xs}"
  button-primary-hover:
    backgroundColor: "color-mix(in oklab, {colors.primary} 88%, {colors.background})"
  button-secondary:
    backgroundColor: "{colors.secondary}"
    textColor: "{colors.secondary-foreground}"
    border: "1px solid {colors.border}"
    rounded: "{rounded.md}"
    height: 40px
    padding: 0 16px
  button-secondary-hover:
    backgroundColor: "{colors.accent}"
  button-outline:
    backgroundColor: transparent
    textColor: "{colors.foreground}"
    border: "1px solid {colors.border}"
    rounded: "{rounded.md}"
    height: 40px
    padding: 0 16px
  button-outline-hover:
    backgroundColor: "{colors.accent}"
  button-ghost:
    backgroundColor: transparent
    textColor: "{colors.foreground}"
    rounded: "{rounded.md}"
    height: 40px
    padding: 0 16px
  button-ghost-hover:
    backgroundColor: "{colors.accent}"
  button-destructive:
    backgroundColor: "{colors.destructive}"
    textColor: "{colors.destructive-foreground}"
    rounded: "{rounded.md}"
    height: 40px
    padding: 0 16px
    shadow: "{elevation.shadow-xs}"
  button-destructive-hover:
    backgroundColor: "color-mix(in oklab, {colors.destructive} 88%, black)"
  button-link:
    backgroundColor: transparent
    textColor: "{colors.foreground}"
    textDecoration: underline
    underlineOffset: 4px
  button-link-hover:
    opacity: 0.8
  button-size-sm:
    height: 32px
    padding: 0 12px
    typography: "{typography.label}"
  button-size-lg:
    height: 44px
    padding: 0 22px
    fontSize: 1rem
  button-size-icon:
    size: 40px
    padding: 0
  button-disabled:
    opacity: 0.5
  input:
    backgroundColor: "{colors.card}"
    textColor: "{colors.foreground}"
    border: "1px solid {colors.input}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.md}"
    height: 40px          # sm 32px, lg 44px
    padding: 0 12px
  input-focus:
    border: "1px solid {colors.ring}"
    shadow: "0 0 0 3px color-mix(in oklab, {colors.ring} 30%, transparent)"
  input-invalid:
    border: "1px solid {colors.destructive}"
    shadow: "0 0 0 3px color-mix(in oklab, {colors.destructive} 30%, transparent)"
  textarea:
    backgroundColor: "{colors.card}"
    border: "1px solid {colors.input}"
    rounded: "{rounded.md}"
    padding: 8px 12px
    resize: vertical
  select:
    backgroundColor: "{colors.card}"
    border: "1px solid {colors.input}"
    rounded: "{rounded.md}"
    height: 40px
    padding: 0 36px 0 12px   # room for 16px chevron at right 12px
  label:
    typography: "{typography.label}"
    textColor: "{colors.foreground}"
  checkbox:
    size: 18px
    rounded: "{rounded.sm}"
    border: "1px solid {colors.input}"
    backgroundColor: "{colors.card}"
  checkbox-checked:
    backgroundColor: "{colors.primary}"
    border: "1px solid {colors.primary}"
    textColor: "{colors.primary-foreground}"
  radio:
    size: 18px
    rounded: "{rounded.full}"
    border: "1px solid {colors.input}"
    dotSize: 9px
    dotColor: "{colors.primary}"
  switch:
    width: 36px
    height: 20px
    padding: 2px
    rounded: "{rounded.full}"
    backgroundColor: "{colors.input}"
    thumbSize: 16px
    thumbColor: "{colors.card}"
  switch-on:
    backgroundColor: "{colors.primary}"
  card:
    backgroundColor: "{colors.card}"
    textColor: "{colors.card-foreground}"
    border: "1px solid {colors.border}"
    rounded: "{rounded.lg}"
    shadow: "{elevation.shadow-sm}"
    padding: "{spacing.pad-card}"
  card-footer:
    borderTop: "1px solid {colors.border}"
    padding: 16px 24px 24px
  badge:
    typography: "{typography.caption}"
    fontWeight: 500
    rounded: "{rounded.full}"
    height: 22px
    padding: 0 8px
  badge-solid:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-foreground}"
  badge-secondary:
    backgroundColor: "{colors.secondary}"
    textColor: "{colors.secondary-foreground}"
    border: "1px solid {colors.border}"
  badge-outline:
    backgroundColor: transparent
    textColor: "{colors.foreground}"
    border: "1px solid {colors.border}"
  badge-success:
    backgroundColor: "color-mix(in oklab, {colors.success} 14%, {colors.card})"
    textColor: "{colors.success-fg}"
    border: "1px solid color-mix(in oklab, {colors.success} 30%, transparent)"
  badge-warning:
    backgroundColor: "color-mix(in oklab, {colors.warning} 16%, {colors.card})"
    textColor: "{colors.warning-fg}"
    border: "1px solid color-mix(in oklab, {colors.warning} 35%, transparent)"
  badge-destructive:
    backgroundColor: "color-mix(in oklab, {colors.destructive} 12%, {colors.card})"
    textColor: "{colors.destructive}"
    border: "1px solid color-mix(in oklab, {colors.destructive} 30%, transparent)"
  alert:
    rounded: "{rounded.lg}"
    padding: 16px
    gap: 12px
    iconSize: 18px
    border: "1px solid {colors.border}"
  alert-info:
    backgroundColor: "{colors.card}"
    iconColor: "{colors.info}"
  alert-success:
    backgroundColor: "color-mix(in oklab, {colors.success} 7%, {colors.card})"
    iconColor: "{colors.success}"
  alert-warning:
    backgroundColor: "color-mix(in oklab, {colors.warning} 9%, {colors.card})"
    iconColor: "{colors.warning}"
  alert-destructive:
    backgroundColor: "color-mix(in oklab, {colors.destructive} 6%, {colors.card})"
    textColor: "{colors.destructive}"
    iconColor: "{colors.destructive}"
  progress:
    height: 8px            # sm 4px, lg 12px
    rounded: "{rounded.full}"
    trackColor: "{colors.secondary}"
    fillColor: "{colors.primary}"
  skeleton:
    rounded: "{rounded.md}"
    backgroundColor: "{colors.secondary}"
    animation: "shimmer, linear gradient sweep"
  avatar:
    size: 40px             # 32px in sidebar footer, 24px in table rows
    rounded: "{rounded.full}"
    backgroundColor: "{colors.secondary}"
    fallback: "two-letter initials, label weight"
  tabs-list:
    backgroundColor: "{colors.secondary}"
    rounded: "{rounded.lg}"
    padding: 4px
    gap: 2px
  tabs-trigger:
    textColor: "{colors.muted-foreground}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: 6px 14px
  tabs-trigger-active:
    backgroundColor: "{colors.card}"
    textColor: "{colors.foreground}"
    shadow: "{elevation.shadow-xs}"
  separator:
    thickness: 1px
    color: "{colors.border}"
  sidebar:
    width: 240px
    backgroundColor: "{colors.sidebar}"
    borderRight: "1px solid {colors.sidebar-border}"
  sidebar-item:
    typography: "{typography.label}"
    textColor: "{colors.muted-foreground}"
    rounded: "{rounded.md}"
    padding: 9px 10px
    gap: 10px
    iconSize: 18px
  sidebar-item-active:
    backgroundColor: "{colors.secondary}"
    textColor: "{colors.foreground}"
  topbar:
    backgroundColor: "{colors.card}"
    borderBottom: "1px solid {colors.border}"
    padding: 14px 28px
    gap: 16px
  table-header:
    typography: "{typography.overline}"
    textColor: "{colors.muted-foreground}"
    padding: 10px 24px
  table-row:
    typography: "{typography.body-sm}"
    borderTop: "1px solid {colors.border}"
    padding: 12px 24px
  popover:
    backgroundColor: "{colors.popover}"
    border: "1px solid {colors.border}"
    rounded: "{rounded.lg}"
    shadow: "{elevation.shadow-lg}"
  dialog:
    backgroundColor: "{colors.popover}"
    border: "1px solid {colors.border}"
    rounded: "{rounded.lg}"
    shadow: "{elevation.shadow-xl}"
    padding: 24px
---

# Stillwater — Design System

## Overview

**Stillwater** is the calm interior of a serious B2B product: cool grey paper, deep ink text and one harbor-blue accent, so that the user's content carries the screen, not the chrome. Still water: the content moves, the surface stays quiet. It is implemented on the **Tailwind CSS + shadcn/ui token contract** — every semantic token name (`background`, `foreground`, `primary`, `muted`, `accent`, `border`, `ring`, …) is exactly what shadcn ships — so any project on that stack can adopt it with zero translation, and any AI tool that knows shadcn already knows this vocabulary.

Key character traits:

- **One hue.** The greys are cool and low-chroma, tinted toward blue; the only saturated colour in the chrome is the harbor-blue `primary`. Status and chart colours come from one shared sequence.
- **Border-first.** Structure comes from 1px hairline borders; shadows are a quiet, ink-tinted, secondary cue.
- **Flat.** No gradients, imagery, textures, patterns or glassmorphism.
- **A serif for display moments.** Playfair Display sets page headings and quotes; everything else is Geist.
- **Restrained motion.** Quick, purposeful transitions. No bounces, no decorative loops.
- **Light and dark from one contract.** A `.dark` class on any ancestor flips every semantic token.

### Voice and content

- Plain, calm, professional. Clarity over personality; never cute, never hype.
- Address the user as **you**; use **we** for the product sparingly ("You've used 92% of your quota.").
- **Sentence case everywhere** — buttons, titles, menu items. The only uppercase is the overline style (table headers, eyebrows).
- Buttons are verb-first and specific: "Save changes", "Upload", "View all", "Retry" — never "Submit" or "OK".
- Numbers are concrete and compact: "$0.04", "2h ago", "2/3 chunks", "+3".
- No exclamation marks. Periods on full-sentence descriptions, none on labels or fragments.
- **No emoji, ever.** Not as content, not as icons.
- Status words are single and short — Active, Queued, Pending, Done, Failed, Draft — always paired with a colour-coded badge.

## Colors

The palette is a **cool slate ramp** (paper, `slate-*`, ink) plus one **harbor blue** (`harbor-*`), with a matching "deep water" ramp for dark mode (`depth-*`, `mist-*`, `harbor-dark-*`). Source values are oklch; `web/src/index.css` holds them exactly and the hex below is the sRGB equivalent. Design only with the **semantic aliases**; the raw ramp exists so the aliases can be re-mapped.

| Role | Light | Dark | Use |
| --- | --- | --- | --- |
| `background` | `#F7F7F8` | `#0F1419` | App canvas behind cards |
| `foreground` | `#1A2530` | `#E5E9EE` | Primary text |
| `card` | `#FFFFFF` | `#161C24` | Cards, top bar, inputs |
| `popover` | `#FFFFFF` | `#1C232D` | Menus, dialogs, toasts |
| `sidebar` | `#F1F3F6` | `#0B0F14` | App sidebar and the mobile navigation sheet |
| `primary` | `#1A6289` | `#7AB0D0` | The one high-emphasis action per view; checked controls; progress fill; logo tile |
| `primary-foreground` | `#FFFFFF` | `#0F1419` | Text on primary |
| `secondary` / `muted` | `#EBEEF2` | `#1E2630` | Subtle fills, active nav item, tab list track, code |
| `accent` | `#D9E6EE` | `#1E2630` | Hover backgrounds; pressed toggles |
| `accent-foreground` | `#0F4866` | `#A8CFE3` | Text on accent |
| `muted-foreground` | `#5A6573` | `#8590A0` | Secondary text, descriptions, timestamps, placeholders |
| `border` / `input` | `#DBE0E6` | `#28313D` | Hairlines, control outlines |
| `sidebar-border` | `#DBE0E6` | `#1E2630` | The sidebar's edge and internal dividers |
| `ring` | `#1D6C96` | `#7AB0D0` | Focus ring |
| `destructive` | `#B8413C` | `#DC6E69` | Delete, errors, failed state |
| `destructive-foreground` | `#FFFFFF` | `#0F1419` | Text on destructive (ink in dark: light text is only 3.2:1 there) |
| `success` | = `chart-4` | = `chart-4` | Done / active / positive delta |
| `warning` | = `chart-5` | = `chart-5` | Pending / approaching a limit |
| `info` | = `primary` | = `primary` | Neutral informational alerts, in-progress emphasis |

**Primary is harbor blue**: deep (`#1A6289`) in light mode, pale (`#7AB0D0`) on deep water. It is the product's one accent; use it for one high-emphasis action per view, never as a decorative fill.

**Status colours are derived, not added.** `success` is `chart-4` (sea green), `warning` is `chart-5` (sand) and `info` is `primary`, so each follows its mode's palette and the screen never gains a new hue. Small status text uses the `*-fg` variants, mixed toward black (light) or white (dark) until 12px text reaches 4.5:1 on its tint: `success-fg` = success 86% / black, `warning-fg` = warning 70% / black, `info-fg` = info 87% / black in light; success 70% / white, warning as is, info 80% / white in dark.

**Status colour is used as a tint, not a fill.** Badges and alerts mix the status hue at 6–16% into the card colour for the background, use the `*-fg` text (or the full hue for icons), and a 28–35% mix for the border. Solid saturated surfaces are reserved for the destructive button only.

**Charts** use `chart-1 … chart-5` in order: harbor blue, pale harbor, deep harbor, sea green, sand (each re-mapped for dark). Because `chart-4` and `chart-5` double as success and warning, a status badge always carries a word, so a series colour is never mistaken for a status.

## Typography

Three families with strict jobs: **Geist** (sans) for all UI and body text; **Playfair Display** (400 and italic) for display moments only; **JetBrains Mono** for code, IDs, file names, tokens and numeric data that must align. All three are self-hosted from Fontsource (`@fontsource-variable/geist`, `@fontsource/playfair-display`, `@fontsource-variable/jetbrains-mono`); there are no Google Fonts requests.

| Style | Size / line height | Weight | Tracking | Use |
| --- | --- | --- | --- | --- |
| Display (serif) | 60px / 1.1 | 400 | −0.01em | Marketing hero only |
| H1 (serif) | 40px / 1.2 | 400 | −0.01em | Page heading in content area |
| H2 | 24px / 1.2 | 600 | −0.02em | Section heading, document headings |
| Title | 20px / 1.35 | 600 | −0.02em | Dialog titles, sub-sections |
| Top-bar / Card title | 18px / 1.35 | 600 | −0.02em | Page title in top bar; card headers |
| Stat value | 28px / 1.2 | 600 | −0.02em | KPI numbers |
| Blockquote (serif italic) | 20px / 1.5 | 400 | 0 | Quotes in documents (`.prose-doc`) and chat answers (`.prose-chat`): `foreground` text behind a 3px `primary` bar |
| Body | 16px / 1.5 | 400 | 0 | Long-form reading (documents) |
| Body small | 14px / 1.5 | 400 | 0 | Default UI text, tables, nav, inputs |
| Label | 14px / 1.35 | 500 | 0 | Form labels, buttons, tabs |
| Caption | 12px / 1.5 | 400 | 0 | Helper text, meta, timestamps |
| Overline | 12px / 1.35 | 500 | +0.08em, UPPERCASE | Table headers, sidebar group headings |

Rules: **the serif is for display moments only**: the page-level H1 and blockquotes. Nothing interactive, no body copy, no card or dialog titles, and not the headings inside a document (those stay Geist so the page title stands apart). The serif is always regular (400), never bold, with −0.01em tracking. Geist headings are always **semibold (600)** with tight negative tracking; body is regular (400); medium (500) is for interactive labels. Never use bold 700 for UI chrome. Keep reading measure to roughly 65–75 characters for long-form text. Enable `font-feature-settings: 'cv01', 'ss01'` and antialiasing.

## Layout

- **4px grid, strictly.** All spacing comes from the scale 4, 8, 12, 16, 20, 24, 32, 40, 48, 64, 80, 96.
- **Semantic spacing:** 8px between icon and text; 12px between related controls; 16–20px between cards in a stack; 24px card padding; 32px between page sections; 28px main-area padding.
- **App shell (canonical):** a fixed **240px sidebar** (`sidebar` fill, `sidebar-border` right edge) + a flexible main column. The main column has a **top bar** (card fill, bottom border, 14px × 28px padding) holding the page title on the left and search / theme toggle / primary action on the right, then a scrolling content area with 28px padding.
- **Sidebar anatomy:** logo mark (28px) + product name (15px semibold) at top; vertical nav of 36px-tall items (18px icon + label, 10px gap); user block (32px avatar + name + role) pinned to the bottom above a top border.
- **Content widths:** dashboards are full-width grids (e.g. 4-up stat cards with 16px gaps); forms and settings sit in a single column with **max-width 640px**; long-form reading should cap at ~720px.
- **Density:** comfortable, not cramped. Controls are 40px tall by default (32px small, 44px large). Table rows ~48px.
- **Responsive:** below ~768px the sidebar collapses to an off-canvas sheet opened from a menu button in the top bar; grids drop to 2-up then 1-up; page padding drops to 16px.

## Elevation & Depth

Depth is quiet. **The 1px border does most of the separation work; shadow is the secondary cue.**

| Level | Token | Used for |
| --- | --- | --- |
| 0 | none + border | Sidebar, top bar, table rows, inputs |
| 1 | `shadow-xs` | Primary/destructive buttons, active tab trigger |
| 2 | `shadow-sm` | Cards (always with a border) |
| 3 | `shadow-md` | Hovered draggable or raised elements (rare) |
| 4 | `shadow-lg` | Popovers, dropdown menus, context menus, toasts |
| 5 | `shadow-xl` | Dialogs, sheets |

In light mode shadows are **ink-tinted** (`hsl(210 30% 15%)`) at 3–5% alpha: a short 1px contact shadow plus, for higher levels, a soft drop. In dark mode they switch to stronger black (40–70% alpha), since an ink tint vanishes against deep water. Never brand-coloured, never stacked on top of each other. Overlays behind dialogs use black at ~50% (light) / ~70% (dark) without blur.

### Motion

- `120ms` ease-out for hover/colour changes; `180ms` for toggles; `260ms` for progress width and panel reveals. Easing `cubic-bezier(0.16, 1, 0.3, 1)`.
- **Press:** buttons scale to `0.98`. No colour flash.
- **Hover:** ghost/outline/secondary pick up the `accent` fill; solid buttons darken ~12%; links drop to 80% opacity.
- **Focus:** always-visible 3px soft ring in `ring` (or `destructive` on invalid fields), offset 2px on buttons.
- The only looping animation allowed is the skeleton shimmer (and a spinner for in-flight work). Respect `prefers-reduced-motion`.

## Shapes

A single **10px radius knob** drives every corner:

- `sm 6px` — checkboxes, small chips, inline code
- `md 8px` — buttons, inputs, selects, textareas, nav items, tab triggers, menu items
- `lg 10px` — cards, alerts, tab list track, popovers, dialogs
- `xl 14px` / `2xl 18px` — large feature panels (rare)
- `full` — badges, avatars, switches, radio buttons, progress bars

Nested corners step down one level (a `md` control inside an `lg` container). No sharp 0px corners on interactive elements; no pill-shaped buttons.

### Iconography

- **Lucide** icons only (the shadcn pairing): 24×24 viewBox rendered at 16–18px, **1.8–2px stroke**, round caps and joins, outline only, no fill, `currentColor`.
- Icons sit 8px from their label. Icon-only buttons are 40×40 (36×36 in the top bar) and always have an `aria-label`.
- No emoji, no Unicode glyphs as icons, no filled or duotone icon styles.
- **Logo:** the Interlixir mark (four diagonal pills joined by two links, plus a dot) in `primary-foreground` on a `primary` tile, filling 58% of the tile, plus a Geist semibold wordmark in `foreground`. The links are `primary-foreground` mixed 55% toward `primary` as a solid fill, not an opacity, because each one overlaps the pill it joins. It follows the theme: deep harbor tile with a white mark in light, pale harbor tile with an ink mark in dark. The favicon is the light-mode mark.

## Components

All components are composed from semantic tokens; none hard-code a colour.

**Button** — variants `primary` (solid ink, one per view), `secondary` (subtle fill + border), `outline` (border only), `ghost` (transparent until hover), `destructive` (solid red), `link` (underlined text). Sizes `sm 32px`, `md 40px`, `lg 44px`, `icon 40×40`. 14px medium label, 8px radius. Disabled = 50% opacity, no pointer events.

**Input / Textarea / Select** — white (`card`) fill, 1px `input` border, 8px radius, 14px text, 12px horizontal padding, 40px tall. Focus: border turns `ring` + 3px ring at 30%. Invalid: border and ring switch to `destructive`, with the error message in 12–14px destructive text directly below the field. Select has a 16px chevron 12px from the right. Use Select for 4+ options, RadioGroup for 2–3.

**Label** — 14px medium, always bound to its control. Helper text below the control in 12px `muted-foreground`.

**Checkbox / Radio / Switch** — 18px checkbox (6px radius) and radio (full radius, 9px dot); 36×20 switch with 16px thumb. Checked = `primary`. Switch is for settings that apply immediately; Checkbox is for forms that are submitted.

**Card** — `card` fill, 1px border, 10px radius, `shadow-sm`, 24px padding. Parts: Header (title 18px semibold + description 14px muted, 4px apart), Content, Footer (top border, actions aligned with 12px gap). No coloured left-border accents; no heavy shadows.

**Badge** — pill, 22px tall, 12px medium text, 8px horizontal padding. Variants `solid`, `secondary`, `outline`, `success`, `warning`, `destructive` (status variants are tinted, not solid). One or two words.

**Alert** — inline (not a toast). 10px radius, 16px padding, 18px leading Lucide icon in the tone colour, 14px semibold title + 14px muted body. Tones `info`, `success`, `warning`, `destructive`.

**Progress** — 8px track (`secondary`) with `primary` fill, full radius, width animates at 260ms. Determinate 0–100.

**Skeleton** — `secondary` blocks with a soft shimmer; mirror the shape of the real content.

**Tabs** — segmented control: `secondary` track, 4px padding, 10px radius; triggers 14px medium, `muted-foreground`; active trigger lifts to `card` fill + `shadow-xs` + `foreground` text.

**Avatar** — circular, `secondary` fill with two-letter initials fallback. 40px default, 32px in the sidebar footer, 24px in table rows.

**Separator** — 1px `border` line, horizontal or vertical.

**Table** — lives inside a Card with zero content padding. Header row uses the overline style in `muted-foreground`; rows are 14px with 1px top borders and 12px × 24px cell padding; numeric columns right-aligned in tabular figures; status columns use Badges; timestamps in `muted-foreground`.

**Sidebar nav item** — 14px medium, 18px icon, 9px × 10px padding, 8px radius. Idle: `muted-foreground`, transparent. Hover: `accent` fill. Active: `secondary` fill + `foreground` text.

**Menus, popovers, dialogs** (shadcn conventions) — `popover` fill, 1px border, 10px radius, `shadow-lg` (menus) / `shadow-xl` (dialogs). Menu items 32px tall, 8px radius, 14px text, destructive items in red text. Dialogs: 20px semibold title, 14px muted description, footer actions right-aligned (secondary/outline then primary/destructive).

**Toast** (when needed) — bottom-right, `popover` fill, border, `shadow-lg`, 14px text, auto-dismiss; status shown with a leading icon, not a coloured fill.

### Accessibility

- Target WCAG 2.1 AA: 4.5:1 for body and 12px text, 3:1 for large text and UI parts. Every pair below is checked in both themes (light / dark):
  - `foreground` on `background` 14.5 / 15.2, on `card` 15.5 / 14.1, on `sidebar` 14.0 / 15.8
  - `muted-foreground` on `background` 5.5 / 5.7, on `card` 5.9 / 5.3, on `sidebar` 5.3 / 5.9, on `secondary` 5.1 / 4.7, on `accent` 4.7 / 4.7
  - `primary-foreground` on `primary` 6.7 / 7.9; `destructive-foreground` on `destructive` 5.4 / 5.7
  - tint text on its tint: success 5.9 / 7.2, info 7.2 / 7.4, warning 4.9 / 6.7, destructive 4.6 / 4.6
- When a pair fails, adjust the **derived** value (a `*-fg` mix, a foreground choice), never the Stillwater base colours. That is why dark `destructive-foreground` is ink rather than white.
- Never communicate status by colour alone — always pair a hue with a word and/or icon.
- Focus rings are always visible for keyboard users. Every icon-only button has an accessible name; every input has a bound label.
- Layouts must reflow cleanly at 200% browser zoom.

## Do's and Don'ts

**Do**

- Use semantic tokens (`bg-background`, `text-muted-foreground`, `border-border`) — never raw greys or hex.
- Use exactly one `primary` button per view; make everything else `outline`, `secondary` or `ghost`.
- Separate surfaces with a 1px border first, a soft shadow second.
- Use sentence case and specific verb-first labels.
- Pair every status colour with a text label (Badge) or icon.
- Use JetBrains Mono for IDs, file names, code, JSON and token counts.
- Keep Playfair Display to page-level H1s and blockquotes.
- Design light and dark together; check both.
- Use skeletons that match the real layout while loading.

**Don't**

- Don't introduce new hues, gradients, background images, textures or glassmorphism. Status colours come from the chart sequence.
- Don't spend harbor blue on decoration: it marks the one primary action, checked state, progress and focus.
- Don't use solid saturated fills for status (except the destructive button).
- Don't add coloured left-border accent stripes to cards or alerts.
- Don't stack shadows or use brand-coloured shadows (the ink tint is the only tint).
- Don't use emoji, Unicode symbols or filled icons.
- Don't use Title Case, exclamation marks, or vague labels like "Submit" / "OK".
- Don't add bouncy, springy or decorative looping animation.
- Don't use font weights above 600 for UI headings, bold the serif, or use the serif for UI, body text or document headings.
