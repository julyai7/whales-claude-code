// whales runtime capture: what a page actually does when it runs.
//
// Run by whales_runtime.py after a turn that changed UI, against the
// designer's own dev server, headless. Reads numbers, not pixels: motion
// (exact for CSS and Web Animations, sampled per frame for scroll), hover and
// focus states with a pixel check of whether the change is visible, reduced
// motion, a phone-sized layout, and any recipes the repo keeps in
// .whales/recipes. Writes capture.json, media and report.html to the job's out_dir.
//
// Read-only by construction: every request that could write (anything but
// GET/HEAD/OPTIONS) is aborted, on every host. A local app can forward to a
// hosted backend, so localhost is no exception.
//
// usage: node capture.mjs <job.json>
import { chromium, devices } from 'playwright'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const RECORDER = fs.readFileSync(path.join(HERE, 'recorder.js'), 'utf8')
const job = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))
const OUT = job.out_dir
const MEDIA = path.join(OUT, 'media')
fs.mkdirSync(MEDIA, { recursive: true })
const deadline = Date.now() + (job.budget_ms || 80000)
const timeLeft = () => deadline - Date.now()
const DESKTOP = { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 }
const PHONE = 'iPhone 15 Pro'
const LIMIT = { hover: 30, focus: 20, scroll: 120 }
const STYLE_PROPS = ['color', 'backgroundColor', 'borderColor', 'boxShadow', 'outlineStyle', 'outlineColor', 'outlineWidth', 'opacity', 'transform', 'translate', 'scale', 'rotate', 'filter', 'textDecorationLine']

const result = {
  version: 1, base_url: job.base_url, started_at: new Date().toISOString(), tool: 'playwright chromium headless-shell',
  routes: [], recipes: [], blocked_writes: [], status: 'ok', notes: [],
}
let mediaCount = 0
const saveMedia = (buf, name) => {
  const file = `${String(++mediaCount).padStart(3, '0')}_${name.replace(/[^\w.-]+/g, '_').slice(0, 60)}.png`
  fs.writeFileSync(path.join(MEDIA, file), buf)
  return `media/${file}`
}

const browser = await chromium.launch()

async function context(opts = {}) {
  const ctx = await browser.newContext({ ...(opts.phone ? devices[PHONE] : DESKTOP), reducedMotion: opts.reducedMotion || 'no-preference' })
  await ctx.addInitScript(RECORDER)
  await ctx.route('**/*', (route) => {
    const m = route.request().method()
    if (m === 'GET' || m === 'HEAD' || m === 'OPTIONS') return route.continue()
    result.blocked_writes.push(`${m} ${route.request().url().slice(0, 160)}`)
    return route.abort()
  })
  return ctx
}

async function open(ctx, route) {
  const page = await ctx.newPage()
  const url = new URL(route, job.base_url).toString()
  const res = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 20000 })
  await page.waitForLoadState('networkidle', { timeout: 8000 }).catch(() => {})
  return { page, status: res?.status() ?? null }
}

const cap = (page, fn, arg) => page.evaluate(([fn, arg]) => window.__whalesCap[fn](arg), [fn, arg])
const styleOf = (handle) => handle.evaluate((el, props) => { const cs = getComputedStyle(el), o = {}; for (const p of props) o[p] = cs[p]; return o }, STYLE_PROPS)
const diffStyles = (a, b) => Object.fromEntries(Object.keys(a).filter((k) => a[k] !== b[k]).map((k) => [k, [a[k], b[k]]]))
const pad = (box, n = 6) => box && { x: Math.max(0, box.x - n), y: Math.max(0, box.y - n), width: box.width + 2 * n, height: box.height + 2 * n }
// Whether an animation still takes time (reduced motion often shortens it to ~0).
const runs = (r) => typeof r.duration_ms === 'number' ? r.duration_ms > 1 : !/^0(\.0+)?(e-?\d+)?s|^1e-0\d+s|^0\.00\d*s/.test(String(r.duration_ms))
// Whether it moves something: position, size, transform. A fade or a colour change is what reduced motion keeps, so
// it isn't counted. With no keyframes to read (an animation over before it was seen), assume it does.
const TRAVEL = /^(transform|translate|scale|rotate|offset|top|left|right|bottom|inset|width|height|margin|padding)/i
const travels = (r) => !Array.isArray(r.keyframes) || !r.keyframes.length ||
  r.keyframes.some((k) => Object.keys(k).some((p) => TRAVEL.test(p) && k[p] !== 'none' && k[p] !== ''))
const slim = (rows) => rows.map(({ target, keyframes, ...r }) => ({
  ...r, on: target && { tag: target.tag, label: target.label, text: target.text, classes: target.classes, box: target.box },
  keyframes: keyframes && keyframes.length <= 6 ? keyframes : undefined,
}))

// Changed pixels between two PNG crops, counted inside the page.
async function pixelDiff(page, a, b) {
  return page.evaluate(async ([a, b]) => {
    const load = (src) => new Promise((ok) => { const i = new Image(); i.onload = () => ok(i); i.src = src })
    const [ia, ib] = await Promise.all([load(a), load(b)])
    const w = Math.min(ia.width, ib.width), h = Math.min(ia.height, ib.height)
    const c = new OffscreenCanvas(w, h).getContext('2d')
    c.drawImage(ia, 0, 0); const da = c.getImageData(0, 0, w, h).data
    c.clearRect(0, 0, w, h); c.drawImage(ib, 0, 0); const db = c.getImageData(0, 0, w, h).data
    let n = 0
    for (let i = 0; i < da.length; i += 4) if (Math.abs(da[i] - db[i]) + Math.abs(da[i + 1] - db[i + 1]) + Math.abs(da[i + 2] - db[i + 2]) > 24) n++
    return n
  }, [`data:image/png;base64,${a.toString('base64')}`, `data:image/png;base64,${b.toString('base64')}`])
}

// Elements on screen that contain a class this turn added, first; then the rest.
function byRelevance(list) {
  const want = new Set(job.focus_classes || [])
  const hit = (d) => (d.classes || '').split(/\s+/).some((c) => want.has(c))
  return [...list.filter(hit), ...list.filter((d) => !hit(d))]
}

async function hoverSweep(page) {
  const rules = await cap(page, 'stateRules')
  const found = await page.evaluate((bases) => {
    const seen = new Set(), out = []
    for (const b of bases) {
      let els = []
      try { els = [...document.querySelectorAll(b)] } catch { continue }
      for (let el of els) {
        if (b.includes('.group') && el.closest('.group')) el = el.closest('.group')
        if (seen.has(el)) continue
        const r = el.getBoundingClientRect()
        if (r.width < 4 || r.height < 4 || r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue
        seen.add(el)
        el.dataset.whalesH = String(out.length)
        out.push({ i: out.length, classes: typeof el.className === 'string' ? el.className : '' })
      }
    }
    return out
  }, rules.hover)
  const results = []
  for (const { i } of byRelevance(found).slice(0, LIMIT.hover)) {
    if (timeLeft() < 15000) { result.notes.push('hover sweep cut short by the time budget'); break }
    const h = await page.$(`[data-whales-h="${i}"]`)
    if (!h) continue
    await page.mouse.move(1, 1)
    await page.waitForTimeout(200)
    const box = pad(await h.boundingBox())
    if (!box) continue
    const target = await h.evaluate((el) => window.__whalesCap.describe(el))
    const parts = await h.evaluate((el) => [el, ...el.querySelectorAll('*')].slice(0, 25).map((e, k) => { e.dataset.whalesW = `${el.dataset.whalesH}-${k}`; return e.dataset.whalesW }))
    const styles = () => Promise.all(parts.map((w) => page.$(`[data-whales-w="${w}"]`).then((x) => (x ? styleOf(x) : {}))))
    const before = await styles()
    const shotA = await page.screenshot({ clip: box })
    await cap(page, 'begin', false)
    await h.hover({ force: true, timeout: 2000 }).catch(() => {})
    await page.waitForTimeout(450)
    const motion = slim(await cap(page, 'take'))
    const after = await styles()
    const shotB = await page.screenshot({ clip: box })
    const changes = parts.map((_, k) => ({ part: k === 0 ? 'self' : `child ${k}`, diff: diffStyles(before[k], after[k]) })).filter((c) => Object.keys(c.diff).length)
    if (!changes.length && !motion.length) continue
    const changed = await pixelDiff(page, shotA, shotB)
    results.push({
      target, style_changes: changes, motion, visible: changed > 0, changed_px: changed,
      media: changed > 0 ? [saveMedia(shotA, `hover_rest_${i}`), saveMedia(shotB, `hover_${i}`)] : [],
    })
  }
  await page.mouse.move(1, 1)
  return { hover_rules: rules.hover.length, hoverable_on_screen: found.length, with_change: results.length, results }
}

async function focusSweep(page) {
  const found = await page.evaluate(() => [...document.querySelectorAll('a[href],button,input,textarea,select,[tabindex]:not([tabindex="-1"])')]
    .filter((el) => { const r = el.getBoundingClientRect(); return r.width > 2 && r.height > 2 && r.top < innerHeight && r.bottom > 0 && !el.disabled })
    .map((el, i) => { el.dataset.whalesF = String(i); return { i, classes: typeof el.className === 'string' ? el.className : '' } }))
  const results = []
  for (const { i } of byRelevance(found).slice(0, LIMIT.focus)) {
    if (timeLeft() < 10000) { result.notes.push('focus sweep cut short by the time budget'); break }
    const h = await page.$(`[data-whales-f="${i}"]`)
    if (!h) continue
    await page.evaluate(() => document.activeElement?.blur())
    await page.mouse.move(1, 1)
    await page.waitForTimeout(150)
    const box = pad(await h.boundingBox(), 4)
    if (!box) continue
    const target = await h.evaluate((el) => window.__whalesCap.describe(el))
    const before = await styleOf(h)
    const a = await page.screenshot({ clip: box })
    await page.keyboard.press('Shift') // the last input is the keyboard, so focus counts as :focus-visible
    await h.evaluate((el) => el.focus())
    await page.waitForTimeout(300)
    const focusVisible = await h.evaluate((el) => el.matches(':focus-visible'))
    const after = await styleOf(h)
    const b = await page.screenshot({ clip: box })
    const changed = await pixelDiff(page, a, b)
    const visible = changed > 12
    results.push({
      target, focus_visible: focusVisible, style_changes: diffStyles(before, after), visible, changed_px: changed,
      browser_default_ring: after.outlineStyle === 'auto',
      media: [saveMedia(a, `focus_rest_${i}`), saveMedia(b, `focus_${i}`)],
    })
  }
  await page.evaluate(() => document.activeElement?.blur())
  return { focusable_on_screen: found.length, tested: results.length, invisible: results.filter((r) => !r.visible).length, results }
}

// One line per change: "opacity 1 → 0 between 0–405px", "pinned between 0–3107px".
function summarizeScroll(s, changes) {
  const out = []
  for (const k of changes.filter((c) => c !== 'position')) {
    const first = s.findIndex((p) => p[k] !== s[0][k])
    const last = s.findLastIndex((p, i) => i > 0 && p[k] !== s[i - 1][k])
    if (first < 0) continue
    out.push(`${k} ${s[0][k]} → ${s[last][k]} between ${s[Math.max(0, first - 1)].y}–${s[last].y}px`)
  }
  if (changes.includes('position')) {
    // How far it moves on screen per pixel scrolled: -1 is ordinary scrolling, 0 is pinned.
    let segStart = null
    const rates = s.slice(1).map((p, i) => (p.top - s[i].top) / Math.max(1, p.y - s[i].y))
    rates.forEach((r, i) => {
      const pinned = Math.abs(r) < 0.05
      if (pinned && segStart === null) segStart = s[i].y
      if ((!pinned || i === rates.length - 1) && segStart !== null) { out.push(`pinned between ${segStart}–${pinned ? s[i + 1].y : s[i].y}px`); segStart = null }
    })
    const moving = rates.filter((r) => Math.abs(r) >= 0.05 && Math.abs(r + 1) > 0.05)
    if (moving.length) out.push(`moves at ${Math.round(-moving.reduce((a, b) => a + b, 0) / moving.length * 100)}% of scroll speed (parallax) in ${moving.length} of ${rates.length} steps`)
  }
  return out
}

// Scroll a quarter-viewport at a time and keep every element whose look changes with scroll.
async function scrollSample(page) {
  const height = await page.evaluate(() => Math.min(document.documentElement.scrollHeight - innerHeight, innerHeight * 4))
  if (height < 50) return { scrollable: false }
  const ids = await cap(page, 'scrollCandidates', LIMIT.scroll)
  await cap(page, 'begin', false)
  const step = Math.max(40, Math.round(900 / 8))
  const series = {}
  for (let y = 0; y <= height + step; y += step) {
    // Instant, and the position read back: a page with smooth scrolling would still be travelling.
    await page.evaluate((y) => window.scrollTo({ top: y, behavior: 'instant' }), Math.min(y, height))
    await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))))
    await page.waitForTimeout(60)
    const { at, looks } = await page.evaluate((ids) => ({ at: Math.round(scrollY), looks: ids.map((i) => { const el = document.querySelector(`[data-whales-scroll="${i}"]`); return el ? window.__whalesCap.look(el) : null }) }), ids)
    looks.forEach((l, k) => { if (l) (series[ids[k]] ||= []).push({ y: at, ...l }) })
  }
  const triggered = slim(await cap(page, 'take'))
  const moving = []
  for (const [i, s] of Object.entries(series)) {
    const keys = ['opacity', 'filter', 'transform', 'color', 'background']
    const changes = keys.filter((k) => new Set(s.map((p) => p[k])).size > 1)
    // `top` moving at the scroll rate is ordinary scrolling; anything else is pinned or parallax.
    const tops = s.map((p, k) => p.top + p.y - s[0].y)
    if (new Set(tops).size > 1 && Math.max(...tops) - Math.min(...tops) > 20) changes.push('position')
    if (!changes.length) continue
    const target = await page.evaluate((i) => window.__whalesCap.describe(document.querySelector(`[data-whales-scroll="${i}"]`)), i)
    moving.push({ target, changes, summary: summarizeScroll(s, changes), samples: s.map(({ y, top, ...rest }) => ({ y, top, ...Object.fromEntries(changes.filter((c) => c !== 'position').map((c) => [c, rest[c]])) })) })
  }
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }))
  return { scrollable: true, sampled_to_px: height, step_px: step, scroll_linked: moving, triggered_by_scroll: triggered }
}

async function captureRoute(route) {
  const out = { route }
  const ctx = await context()
  try {
    const { page, status } = await open(ctx, route)
    out.http_status = status
    await page.waitForTimeout(Math.min(3500, Math.max(0, timeLeft() - 30000)))
    const buildError = await cap(page, 'buildError')
    if (buildError) { out.build_error = buildError; out.screenshot = saveMedia(await page.screenshot(), `build_error`); return out }
    out.load_motion = slim(await cap(page, 'take'))
    out.screenshot = saveMedia(await page.screenshot(), `load_${route}`)
    if (timeLeft() > 20000) out.scroll = await scrollSample(page)
    if (timeLeft() > 20000) out.hover = await hoverSweep(page)
    if (timeLeft() > 12000) out.focus = await focusSweep(page)
  } catch (e) {
    out.error = String(e).slice(0, 300)
  } finally {
    await ctx.close()
  }
  if (timeLeft() > 10000) {
    const rm = await context({ reducedMotion: 'reduce' })
    try {
      const { page } = await open(rm, route)
      await page.waitForTimeout(2500)
      const rows = await cap(page, 'take')
      const moving = (r) => runs(r) && travels(r)
      out.reduced_motion = { animations_on_load: rows.length, still_moving: rows.filter(moving).length,
                             still_fading: rows.filter((r) => runs(r) && !travels(r)).length, examples: slim(rows.filter(moving).slice(0, 8)) }
    } catch (e) { out.reduced_motion = { error: String(e).slice(0, 200) } } finally { await rm.close() }
  }
  if (timeLeft() > 8000) {
    const ph = await context({ phone: true })
    try {
      const { page } = await open(ph, route)
      await page.waitForTimeout(1200)
      const revealedOnHover = (out.hover?.results || []).flatMap((h) => h.style_changes.filter((c) => c.diff.opacity && c.diff.opacity[0] === '0').map(() => h.target.label || h.target.text)).filter(Boolean)
      out.phone = {
        device: PHONE,
        ...(await page.evaluate(() => ({ viewport: [innerWidth, innerHeight], horizontal_overflow_px: Math.max(0, document.documentElement.scrollWidth - innerWidth) }))),
        hover_only_controls: [...new Set(revealedOnHover)].slice(0, 10),
        screenshot: saveMedia(await page.screenshot(), `phone_${route}`),
      }
    } catch (e) { out.phone = { error: String(e).slice(0, 200) } } finally { await ph.close() }
  }
  return out
}

// A recipe: steps the host wrote for something interactive it built. Each step's motion is recorded.
async function runRecipe(recipe) {
  const out = { name: recipe.name, route: recipe.route || '/', steps: [] }
  const ctx = await context()
  try {
    const { page } = await open(ctx, out.route)
    await page.waitForTimeout(800)
    for (const step of recipe.steps || []) {
      if (timeLeft() < 6000) { out.cut_short = true; break }
      const loc = step.selector ? page.locator(step.selector).first() : null
      const rec = { do: step.do, selector: step.selector, label: step.label }
      await cap(page, 'begin', !!step.frames)
      const t0 = Date.now()
      try {
        if (step.do === 'click') await loc.click({ timeout: 4000 })
        else if (step.do === 'hover') await loc.hover({ timeout: 4000 })
        else if (step.do === 'fill') await loc.fill(String(step.text ?? ''), { timeout: 4000 })
        else if (step.do === 'press') await page.keyboard.press(step.key)
        else if (step.do === 'scroll') await page.evaluate((y) => window.scrollTo(0, y), step.y || 0)
        else if (step.do === 'wait') await page.waitForTimeout(step.ms || 500)
        else if (step.do === 'goto') await page.goto(new URL(step.route, job.base_url).toString(), { waitUntil: 'domcontentloaded' })
      } catch (e) { rec.error = String(e).split('\n')[0].slice(0, 200) }
      if (step.frames) {
        await page.waitForTimeout(120)
        rec.frames = []
        for (const f of [0, 0.25, 0.5, 0.75, 1]) {
          await cap(page, 'seek', f)
          await page.waitForTimeout(30)
          rec.frames.push({ at: f, image: saveMedia(await page.screenshot(), `${recipe.name}_${step.label || step.do}_${f * 100}`) })
        }
        await cap(page, 'release')
      }
      await page.waitForTimeout(step.settle_ms ?? 700)
      rec.motion = slim(await cap(page, 'take'))
      rec.settle_ms = Date.now() - t0
      if (step.capture !== false) {
        rec.state = await page.evaluate(() => ({ url: location.pathname + location.search, title: document.title, dialogs: document.querySelectorAll('[role="dialog"],dialog[open]').length }))
        rec.screenshot = saveMedia(await page.screenshot(), `${recipe.name}_${step.label || step.do}`)
      }
      out.steps.push(rec)
    }
  } catch (e) { out.error = String(e).slice(0, 300) } finally { await ctx.close() }
  return out
}

// A recipe belongs to the page its route opens, query aside: '/?empty=1' is a state of '/'.
const pathOf = (route) => String(route || '/').split('?')[0].split('#')[0] || '/'

try {
  // Each page, then straight away the recipes on it: the time budget runs out on what comes last, and the pages the
  // turn touched come first (job.routes), so their flows are never starved by a full sweep of another page.
  const pending = [...(job.recipes || [])]
  const runRecipes = async (which) => {
    for (const recipe of which) {
      pending.splice(pending.indexOf(recipe), 1)
      if (timeLeft() < 10000) { result.notes.push(`recipe ${recipe.name} skipped: time budget`); continue }
      result.recipes.push(await runRecipe(recipe))
    }
  }
  for (const route of job.routes || ['/']) {
    if (timeLeft() < 15000) { result.notes.push(`route ${route} skipped: time budget`) }
    else result.routes.push(await captureRoute(route))
    await runRecipes(pending.filter((r) => pathOf(r.route) === pathOf(route)))
  }
  await runRecipes([...pending])
} catch (e) {
  result.status = 'error'
  result.error = String(e).slice(0, 400)
} finally {
  await browser.close()
}
result.finished_at = new Date().toISOString()
if (result.routes.some((r) => r.build_error)) result.status = 'build_error'
fs.writeFileSync(path.join(OUT, 'capture.json'), JSON.stringify(result, null, 1))
fs.writeFileSync(path.join(OUT, 'report.html'), report(result))
console.log(JSON.stringify({ status: result.status, routes: result.routes.length, recipes: result.recipes.length }))

function report(r) {
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c])
  const name = (t) => esc(t?.label || t?.text || t?.tag || '')
  const motionRows = (rows) => (rows || []).filter((m) => m.name).map((m) => `<tr><td>${esc(m.kind)}</td><td>${esc(m.name)}</td><td>${name(m.on)}${m.pseudo ? ` <code>${esc(m.pseudo)}</code>` : ''}</td><td>${esc(m.start_ms)}</td><td>${esc(m.duration_ms)}</td><td>${esc(m.delay_ms)}</td><td><code>${esc(m.curve)}</code></td></tr>`).join('')
  const motionTable = (rows) => rows?.length ? `<table><tr><th>kind</th><th>what</th><th>on</th><th>starts ms</th><th>duration ms</th><th>delay ms</th><th>curve</th></tr>${motionRows(rows)}</table>` : '<p class="dim">none</p>'
  const img = (src) => src ? `<img src="${esc(src)}" loading="lazy">` : ''
  const route = (x) => `
  <section><h2>${esc(x.route)} <span class="dim">${esc(x.http_status ?? '')}</span></h2>
  ${x.build_error ? `<p class="bad">Build error on the page: ${esc(x.build_error)}</p>${img(x.screenshot)}` : ''}
  ${x.error ? `<p class="bad">${esc(x.error)}</p>` : ''}
  ${x.screenshot && !x.build_error ? `<div class="shot">${img(x.screenshot)}</div>` : ''}
  <h3>Motion on load</h3>${motionTable(x.load_motion)}
  <h3>Scroll</h3>${!x.scroll ? '<p class="dim">not sampled</p>' : !x.scroll.scrollable ? '<p class="dim">page does not scroll</p>' :
    (x.scroll.scroll_linked.length ? x.scroll.scroll_linked.map((s) => `<div class="card"><b>${name(s.target)}</b> <span class="dim">${esc(s.target?.tag)}</span><div>${s.summary.map(esc).join('<br>')}</div><details><summary class="dim">samples</summary><table><tr><th>scroll px</th>${Object.keys(s.samples[0]).filter((k) => k !== 'y').map((k) => `<th>${esc(k)}</th>`).join('')}</tr>${s.samples.map((p) => `<tr><td>${p.y}</td>${Object.entries(p).filter(([k]) => k !== 'y').map(([, v]) => `<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</table></details></div>`).join('') : '<p class="dim">nothing changes with scroll</p>') +
    `<h4>Started by scrolling</h4>${motionTable(x.scroll.triggered_by_scroll)}`}
  <h3>Hover <span class="dim">${x.hover ? `${x.hover.with_change} of ${x.hover.hoverable_on_screen} hoverable elements change` : ''}</span></h3>
  ${(x.hover?.results || []).map((h) => `<div class="card"><b>${name(h.target)}</b> ${h.visible ? '' : '<span class="bad">no visible change</span>'}<div>${h.media.map(img).join('')}</div>${h.style_changes.map((c) => `<div class="dim">${esc(c.part)}: ${Object.entries(c.diff).map(([k, v]) => `${esc(k)} ${esc(v[0])} → ${esc(v[1])}`).join('; ')}</div>`).join('')}${motionTable(h.motion)}</div>`).join('')}
  <h3>Focus <span class="dim">${x.focus ? `${x.focus.tested} tested, ${x.focus.invisible} invisible` : ''}</span></h3>
  ${(x.focus?.results || []).map((f) => `<div class="card"><b>${name(f.target)}</b> ${f.visible ? '' : '<span class="bad">invisible</span>'} ${f.browser_default_ring ? '<span class="dim">browser default ring</span>' : ''}<div>${f.media.map(img).join('')}</div><div class="dim">${Object.entries(f.style_changes).map(([k, v]) => `${esc(k)} ${esc(v[0])} → ${esc(v[1])}`).join('; ')}</div></div>`).join('')}
  <h3>Reduced motion</h3>${x.reduced_motion ? `<p>${x.reduced_motion.still_moving ?? '?'} of ${x.reduced_motion.animations_on_load ?? '?'} animations on load still move with reduced motion on${x.reduced_motion.still_fading ? `; ${x.reduced_motion.still_fading} more only fade or change colour, which reduced motion allows` : ''}.</p>${motionTable(x.reduced_motion.examples)}` : '<p class="dim">not checked</p>'}
  <h3>Phone</h3>${x.phone ? `<p>${esc(x.phone.device)} ${esc((x.phone.viewport || []).join('×'))}; overflow ${esc(x.phone.horizontal_overflow_px)}px${x.phone.hover_only_controls?.length ? `; only reachable by hover: ${esc(x.phone.hover_only_controls.join(', '))}` : ''}</p><div class="phone">${img(x.phone.screenshot)}</div>` : '<p class="dim">not checked</p>'}
  </section>`
  const recipe = (x) => `<section><h2>Recipe: ${esc(x.name)}</h2>${x.steps.map((s) => `<div class="card"><b>${esc(s.do)} ${esc(s.label || s.selector || '')}</b> ${s.error ? `<span class="bad">${esc(s.error)}</span>` : ''} <span class="dim">${esc(s.settle_ms)}ms</span>${s.frames ? `<div class="frames">${s.frames.map((f) => img(f.image)).join('')}</div>` : ''}${motionTable(s.motion)}${img(s.screenshot)}</div>`).join('')}</section>`
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>whales capture</title><style>
  :root{--bg:#fbfaf8;--fg:#1d1d1b;--dim:#6b6b66;--line:#e4e2dc;--bad:#b42318}
  @media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe7;--dim:#9a9993;--line:#2e2e2b;--bad:#f97066}}
  body{background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,system-ui,sans-serif;margin:0 auto;max-width:1100px;padding:24px 16px}
  h1{font-size:20px}h2{font-size:17px;border-top:1px solid var(--line);padding-top:20px;margin-top:32px}h3{font-size:14px;margin:20px 0 6px}
  table{border-collapse:collapse;width:100%;font-size:12px;margin:4px 0}td,th{border-bottom:1px solid var(--line);padding:3px 6px;text-align:left;vertical-align:top}
  code{font-size:12px}.dim{color:var(--dim)}.bad{color:var(--bad)}.card{border:1px solid var(--line);border-radius:6px;padding:8px 10px;margin:8px 0}
  img{max-width:100%;border:1px solid var(--line);margin:4px 6px 4px 0;vertical-align:top}.card img{max-height:80px}.shot img{max-height:420px}.phone img{max-height:520px}.frames img{max-height:160px}
  </style></head><body><h1>whales runtime capture</h1>
  <p class="dim">${esc(r.base_url)} · ${esc(r.started_at)} · status ${esc(r.status)} · writes blocked: ${r.blocked_writes.length}</p>
  ${r.notes.length ? `<p class="dim">${r.notes.map(esc).join('<br>')}</p>` : ''}
  ${r.routes.map(route).join('')}${r.recipes.map(recipe).join('')}</body></html>`
}
