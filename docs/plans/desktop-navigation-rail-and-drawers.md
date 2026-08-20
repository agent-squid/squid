# Plan: Desktop navigation rail and contextual drawers

Replace the crowded desktop tab bar and hamburger with a single icon-only
navigation rail. Preserve the current mobile header and hamburger navigation.
This is a client-only UX change; no backend or protocol work is expected.

## Goals

- Give status, quota, and help controls more room in the top bar.
- Make every primary destination available in one click on desktop.
- Let users inspect Files, Topics, and Agents without losing their place or
  draft in Chat.
- Keep navigation behavior familiar and space-efficient on mobile.
- Put maintenance actions beside configuration and update controls.

## Information architecture

Desktop rail, from top to bottom:

1. Chat
2. Files
3. Topics
4. Agents
5. Stats
6. Community
7. Squid Flow
8. A flexible spacer
9. Settings, anchored at the bottom

The rail uses icons by default. Every control has an accessible name and a
hover/focus tooltip. The selected destination has a persistent visual state;
hover alone must not resemble selection.

The AgentSquid logo and wordmark remain in the horizontal top bar. The top bar
also retains process status, quotas, and Help because these describe or assist
the current workspace rather than navigate elsewhere. At narrower desktop
widths the wordmark may collapse to the squid logo, but the logo does not move
into the rail.

The desktop hamburger is removed. Its destinations move to the rail and its
maintenance actions move into Settings:

- Update status/action
- Refresh Client
- Restart Server, using the existing themed confirmation modal

The `/refresh` and `/restart` chat commands remain available. Refresh and
Restart are not rail controls because they are infrequent, potentially
disruptive actions rather than destinations.

## Desktop behavior

The new application shell is parented by `#app`. The rail and a new content
column are direct children; the content column contains the existing `#topbar`
and `.view` elements.

Files, Topics, and Agents are contextual drawers:

- Opening one leaves Chat mounted underneath, including its scroll position,
  composer draft, selection, and streaming state.
- The drawer overlays the chat rather than shrinking it.
- Only one drawer can be open at a time.
- `Escape`, the active rail icon, Chat, or the backdrop closes the drawer.
- Focus moves into the drawer on open and returns to the invoking icon on
  close; keyboard focus is trapped while the drawer is modal.
- Switching directly between drawer icons replaces the drawer without first
  revealing Chat.

Stats, Community, Squid Flow, and Settings remain full views because their
content benefits from the full content width. Selecting one closes any drawer
and uses the existing view-loading lifecycle.

The navigation model must distinguish the base view from an open overlay. For
example, Files over Chat means Chat is still the base view while Files is the
active drawer; it must not be represented as a destructive switch away from
Chat in application state or browser history.

## Mobile behavior

At `max-width: 768px`, do not render the rail. Keep the existing fixed header,
view title, hamburger, and browser-back behavior.

The mobile hamburger contains the same destination order as the desktop rail:
Chat, Files, Topics, Agents, Stats, Community, Squid Flow, and Settings. Files,
Topics, and Agents continue to open as full-screen views on mobile rather than
drawers. Stats, Community, Squid Flow, and Settings are full views everywhere.

Update notification placement is responsive:

- Desktop: red dot on the Settings rail icon.
- Mobile: red dot on the hamburger, since Settings is nested inside it.

Opening either navigation surface does not clear the dot. It clears only when
the existing update state says the update no longer requires attention.

## Layout and visual details

- Rail width target: 44–48 px, outside the current 800 px content maximum so
  chat content does not become narrower.
- Icons use the existing icon system and theme tokens; do not introduce a
  second visual language or bitmap assets.
- Drawers use the common themed panel/modal treatment, not a system modal.
- Drawer width should be responsive, with a practical desktop maximum, and
  must not extend underneath the rail or top bar.
- The backdrop should preserve enough chat visibility to reinforce that the
  user has not navigated away.
- Respect safe areas, reduced-motion preferences, and existing z-index layers
  for status, quota, help, file viewer, and confirmation modals.

## Implementation plan

1. Refactor `ui/index.html` into a desktop shell under `#app`: add the rail and
   content-column wrapper, replace desktop text tabs with icon buttons, retain
   mobile hamburger items, and move Refresh/Restart controls into Settings.
2. Separate navigation state in `ui/app.js` into `currentView` and an optional
   `openDrawer`. Reuse existing loaders; do not build duplicate Files, Topics,
   or Agents DOM trees.
3. Add drawer open/close behavior, focus management, backdrop handling, and
   responsive transition between drawer and full-view behavior.
4. Move update-badge rendering from the desktop hamburger to Settings while
   retaining it on the mobile hamburger.
5. Add desktop shell, rail, tooltip, drawer, backdrop, and responsive styles in
   `ui/style.css`. Preserve the current mobile layout below the breakpoint.
6. Update any selectors that assume `.view` and `#topbar` are direct children
   of `#app`.
7. Bump the PWA asset version in all five required `sw.js` and `index.html`
   locations after changing `ui/app.js`, `ui/style.css`, or `ui/index.html`.

## Verification

Add or update Playwright coverage for:

- Every desktop rail destination is keyboard- and pointer-accessible.
- Files, Topics, and Agents open over Chat without clearing the draft,
  transcript scroll position, or active streaming output.
- Drawer close paths and direct drawer-to-drawer switching work.
- Stats, Community, Squid Flow, and Settings remain full views.
- Refresh Client and Restart Server are present in Settings; Restart retains
  the themed confirmation and running-process warning.
- The update dot appears on desktop Settings and mobile hamburger correctly.
- At the mobile breakpoint the rail is absent, the hamburger contains all
  destinations, and browser Back retains current behavior.
- Focus enters and exits drawers correctly, tooltips have accessible labels,
  and reduced motion is respected.
- Existing navigation, settings, files, topics, agents, stats, restart, and PWA
  cache tests remain green.

Perform visual checks at wide desktop, 800 px content width, the breakpoint on
both sides, and representative phone widths. Confirm that status/quota popups,
Help, file viewer, and common modals layer above the drawer correctly.

## Acceptance criteria

- Desktop has no top navigation tabs or hamburger menu.
- Desktop primary navigation is one click from the persistent icon rail.
- Files, Topics, and Agents behave as non-destructive overlays over Chat.
- Mobile retains hamburger navigation and full-screen views.
- AgentSquid branding remains in the horizontal top bar.
- Settings owns update, Refresh Client, and Restart Server actions.
- Update attention remains visible in the appropriate responsive location.
- No server API, stored-data, chat delivery, or realtime behavior changes.
