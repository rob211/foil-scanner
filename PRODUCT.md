# Product

## Register

product

## Users

Rob, downwind foil rider on the Illawarra coast. Checks the dashboard on his
phone (in bed at 6am, in the car, at the beach in full sun) or occasionally on
the desktop. One job: decide in seconds whether a session is on today or this
week, then drill into the numbers if the answer is interesting.

Secondary (added 5 Jul 2026): mates with the shared link. They can subscribe
to the Foiling calendar from the dashboard (public read-only feed, Google or
ICS); no accounts, no email collection, no backend.

## Product Purpose

A glanceable front end for the foil-scanner verdict data (data/latest.json).
Shows upcoming trigger windows colour-coded yellow/green/red, scanner health,
data source status, and near misses (why a windy-looking day did not fire).
The scanner already knows Rob's triggers; the dashboard answers "is it on?"
without maps, toggles or forecasting chrome. Success: the answer is readable
in under three seconds outdoors, and never silently wrong (a broken scanner
must look broken, not calm).

## Brand Personality

Direct, weather-worn, trustworthy. A surf check from a mate who has already
read the models, not a weather product. Detail on demand: verdict first,
model spread / tide / swell one tap deeper.

## Anti-references

Deferred by Rob ("build first, revisit later"). Working defaults: no
corporate SaaS dashboard tropes (KPI tiles, gradient hero metrics), no
weather-app clutter (maps, ad-shaped panels, ten toggles). To be revisited.

## Design Principles

- Verdict before data: every screen answers "is it on?" before showing why.
- Loud failure is a feature: stale or broken states must be the most visible
  thing on the page, matching the scanner's own failure philosophy.
- The grade colours (yellow/green/red) are the information; everything else
  stays quiet so they read from arm's length in sunlight.
- Phone first, one hand, no horizontal scrolling; desktop is a bonus.
- Static and self-contained: one HTML file on GitHub Pages reading committed
  JSON, no build step, no external requests.

## Accessibility & Inclusion

No formal WCAG target set; hold to 4.5:1 body contrast, never encode grade
by colour alone (always pair with a text label), and respect
prefers-reduced-motion. Dark-only by Rob's decision (5 Jul 2026, restyle to
a near-black reference); the earlier follow-the-phone-setting default is
superseded. Trade-off accepted: slightly harder to read in full sun.
