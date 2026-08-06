## Widget page contract — the iframe card

Container and label requirements live in
`references/app-contract.md`; this file only covers the page that
`nimoos.widget.path` points to. The desktop wraps that page in a
glass card (shell, title, and icon are drawn by the desktop — you are
responsible for the card CONTENT only). The iframe is sandboxed with
`sandbox="allow-scripts allow-same-origin allow-forms"` (**no**
allow-top-navigation).

### The page MUST

1. **Be reachable without authentication** — the desktop passes no
   token; a page that requires login shows "cannot connect" forever.
2. **Load within 8 seconds** — otherwise the desktop shows a
   placeholder.
3. **Handle three query parameters** (the desktop appends them):
   - `theme`: `dark` or `light` — set it on
     `<html data-theme="...">`;
   - `lang`: e.g. `zh_cn` — localize copy as needed;
   - `home`: the desktop's origin — used to load the design kit.
4. **Load the design kit with this exact template** (copy it into
   `<head>` verbatim — the `?v=2` version parameter is REQUIRED, the
   gateway cache depends on it):

```html
<script>
  const q = new URLSearchParams(location.search)
  document.documentElement.dataset.theme = q.get('theme') || 'dark'
  const l = document.createElement('link'); l.rel = 'stylesheet'
  l.href = (q.get('home') || '') + '/app/widget-kit.css?v=2'; document.head.appendChild(l)
</script>
```

### The page MUST NOT

- set its own background on `<html>`/`<body>` — the kit makes the
  background transparent so the desktop's glass card shows through.
  A custom background breaks dark mode: Chrome puts a white backing
  behind iframes whose `color-scheme` disagrees, and the kit's
  `color-scheme` declaration already handles this — do not override
  it;
- navigate with `top.location` or `<a target="_top">` (blocked by the
  sandbox). To open the app full-page use `window.open(url)` (opens a
  new tab);
- depend on cookies or localStorage from the NimoOS login session.

### Design kit classes (plain HTML + these classes = native look; use `var(--nk-*)` for every color, never hard-code)

| class | purpose | minimal example |
|---|---|---|
| `.nk-title` | small section title | `<p class="nk-title">下载任务</p>` |
| `.nk-stat` | big number (unit in nested `<small>`) | `<div class="nk-stat">3<small>个进行中</small></div>` |
| `.nk-label` | dimmed small label | `<span class="nk-label">今日</span>` |
| `.nk-list` + `.nk-row` (value in `.nk-value`) | key-value rows | `<ul class="nk-list"><li class="nk-row"><span>速度</span><span class="nk-value">2.1 MB/s</span></li></ul>` |
| `.nk-progress` (with `<i style="width:62%">`) | progress bar | `<div class="nk-progress"><i style="width:62%"></i></div>` |
| `.nk-badge` (optionally `.good`/`.bad`) | status dot | `<span class="nk-badge good">健康</span>` |
| `.nk-accent`/`.nk-good`/`.nk-bad` | text emphasis colors | `<span class="nk-value nk-accent">2.1 MB/s</span>` |

Available tokens (auto-switch between dark/light): `--nk-fg / --nk-muted / --nk-faint / --nk-accent / --nk-good / --nk-bad / --nk-divider / --nk-track / --nk-radius / --nk-font / --nk-num-font`.

Optional offline hardening: vendor a copy of the NAS's
`/app/widget-kit.css` into the app and fall back to it when the
templated load fails.

### The card resizes — the page MUST be written responsive

Exception — fixed-size widgets: declare `nimoos.widget.resize: "false"` (or
`minw`/`minh`/`maxw`/`maxh` with min == max) to lock the card size. A locked
widget never resizes, so the responsive rules below relax to just "look right
at the one locked size". Prefer a locked size only when the layout genuinely
cannot flex (e.g. a fixed board); otherwise stay responsive — users like
resizing. Partial ranges work too: e.g. `nimoos.widget.maxh: "2"` keeps the
widget short while width stays flexible.

Unless locked via the min/max labels above, users can drag-resize the
widget within its declared range (global bounds 2×1..4×4);
`nimoos.widget.w`/`h` set only the INITIAL size. Grid cells are 58–92px depending on the user's
screen, so after card padding and header the iframe viewport ranges
roughly **100–385px wide × 26–355px tall** (an h=1 card is a thin
strip). The iframe viewport IS the card interior: `vw`/`vh`/`vmin`
units, `@media` queries, and `resize` events all track the card live
while the user drags — plain CSS is enough, no desktop API involved.

Three rules (violating rule 1 is the #1 cause of clipped widgets):

1. **Flex skeleton filling `100vh`, never fixed pixel heights.**
   The scrollable middle area absorbs all size changes:

```css
body { height: 100vh; margin: 0; display: flex; flex-direction: column; }
.content { flex: 1; min-height: 0; overflow-y: auto; }  /* NOT max-height: 180px */
```

2. **Scale numbers/type with `clamp()` + `vmin`** (this is how the
   kit's `.nk-stat` behaves): `font-size: clamp(14px, 12vmin, 32px)`.

3. **Degrade content with media queries** — a small card is not a
   shrunken big card, it shows less:

```css
@media (max-height: 90px)  { .input-row, .content { display: none; } } /* 2×1 strip: title + stat only */
@media (max-width: 140px)  { .nk-label, .secondary { display: none; } }
```

Debug without the desktop: open the widget URL directly in a browser
tab (`http://<host>:<port><widget.path>?theme=dark&home=http://<host>`)
and resize the window — the window is the iframe viewport.

### Complete example page (copy whole, then change the business content)

`html/widget/index.html`:

```html
<!doctype html>
<html><head><meta charset="utf-8"><title>My Widget</title>
<script>
  const q = new URLSearchParams(location.search)
  document.documentElement.dataset.theme = q.get('theme') || 'dark'
  const l = document.createElement('link'); l.rel = 'stylesheet'
  l.href = (q.get('home') || '') + '/app/widget-kit.css?v=2'; document.head.appendChild(l)
</script>
<style>
  body { height: 100vh; display: flex; flex-direction: column; }
  .rows { flex: 1; min-height: 0; overflow-y: auto; }
  @media (max-height: 90px) { .rows, .nk-progress { display: none; } }
</style>
</head><body>
  <p class="nk-title">演示任务</p>
  <div class="nk-stat">3<small>个进行中</small></div>
  <div class="nk-progress" style="margin:8px 0"><i style="width:62%"></i></div>
  <ul class="nk-list rows">
    <li class="nk-row"><span>速度</span><span class="nk-value nk-accent">2.1 MB/s</span></li>
    <li class="nk-row"><span>状态</span><span class="nk-badge good">健康</span></li>
  </ul>
</body></html>
```

### Widget self-check (run 1 yourself via run_command; hand 2 to the user)

1. `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:<port><widget.path>` —
   must print `200` with no auth header of any kind.
2. Ask the user to check the widget in BOTH themes (desktop settings →
   theme toggle): text must be readable in each. White-on-white in
   dark mode almost always means the page set its own background or
   dropped `?v=2` from the kit URL — restore the verbatim template
   and delete any custom background styles.

### Troubleshooting

| symptom | cause → fix |
|---|---|
| icon appears but widget says "cannot connect" | `nimoos.port` missing / set to the container-internal port / widget page requires login / page took >8 s to load |
| white-on-white in dark mode | page set its own background or skipped the kit template → restore the verbatim `<head>` template, remove custom backgrounds |
