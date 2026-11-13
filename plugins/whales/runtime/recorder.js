// whales runtime capture: injected into every page before its own scripts.
//
// Records motion as numbers, not pixels: every CSS animation, CSS transition
// and Web Animation the moment it starts, with exact timing and keyframes,
// relative to the current interaction's start. Motion driven by JavaScript is
// not listed by the browser, so `sample` reads chosen styles every frame.
// Animations on pseudo-elements count too: a View Transition runs entirely on
// ::view-transition-* pseudo-elements of <html>, and a skeleton's shimmer often
// on ::after. The element's own getAnimations() leaves those out.
(() => {
  if (window.__whalesCap) return
  const W = (window.__whalesCap = { t0: performance.now(), rows: [], seen: new WeakSet(), hold: false, held: [] })

  function describe(el) {
    if (!el || el.nodeType !== 1) return null
    const r = el.getBoundingClientRect()
    const cls = typeof el.className === 'string' ? el.className : el.getAttribute('class') || ''
    return {
      tag: el.tagName.toLowerCase(),
      id: el.id || null,
      label: el.getAttribute('aria-label') || el.getAttribute('title') || null,
      role: el.getAttribute('role'),
      text: (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60) || null,
      classes: cls.slice(0, 300),
      box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
    }
  }
  W.describe = describe

  // A CSS animation's curve lives on its keyframes; the animation-level easing reads 'linear'.
  function curveOf(anim, kf, timing) {
    if (anim.constructor.name !== 'CSSAnimation') return timing.easing
    const easings = [...new Set(kf.map((k) => k.easing))]
    return easings.length === 1 ? easings[0] : 'per-keyframe'
  }

  function record(anim, via) {
    if (W.seen.has(anim)) return
    W.seen.add(anim)
    const eff = anim.effect
    if (!eff) return
    const name = anim.animationName || anim.transitionProperty || anim.id || null
    if (name === 'claude-pulse') return // the Claude browser extension's own animation
    const t = eff.getTiming()
    let kf = []
    try { kf = eff.getKeyframes() } catch {}
    W.rows.push({
      kind: anim.constructor.name, // CSSAnimation | CSSTransition | Animation (JS)
      name,
      target: describe(eff.target),
      pseudo: eff.pseudoElement || null, // e.g. ::view-transition-group(page), ::after
      start_ms: Math.round(performance.now() - W.t0),
      duration_ms: typeof t.duration === 'number' ? Math.round(t.duration * 1000) / 1000 : t.duration,
      delay_ms: t.delay,
      curve: curveOf(anim, kf, t),
      iterations: t.iterations === Infinity ? 'infinite' : t.iterations,
      fill: t.fill,
      keyframes: kf.map(({ offset, computedOffset, easing, composite, ...props }) => ({ at: computedOffset, easing, ...props })),
      via,
      method: 'exact',
    })
    if (W.hold) { anim.pause(); W.held.push(anim) }
  }
  W.sweep = () => document.getAnimations().forEach((a) => record(a, 'sweep'))
  for (const ev of ['animationstart', 'transitionrun']) {
    document.addEventListener(ev, (e) => {
      const el = e.target
      const pseudo = e.pseudoElement || null
      // subtree: true is what lists the element's pseudo-element animations; keep only this event's own.
      const anims = el.getAnimations
        ? el.getAnimations({ subtree: !!pseudo }).filter((a) => a.effect?.target === el && (a.effect.pseudoElement || null) === pseudo)
        : []
      anims.forEach((a) => record(a, ev))
      // Already over (reduced motion's 0.001ms, say): keep what the event itself says.
      if (!anims.length && el.nodeType === 1) {
        const cs = getComputedStyle(el, pseudo)
        W.rows.push({
          kind: 'event-only', name: e.animationName || e.propertyName, target: describe(el), pseudo,
          start_ms: Math.round(performance.now() - W.t0),
          duration_ms: e.animationName ? cs.animationDuration : cs.transitionDuration,
          curve: e.animationName ? cs.animationTimingFunction : cs.transitionTimingFunction,
          via: ev, method: 'event only: finished before it could be read',
        })
      }
    }, true)
  }
  // Web Animations fire no event, and a short one is over before the next sweep:
  // record each one as it is made.
  const animate = Element.prototype.animate
  Element.prototype.animate = function (...args) {
    const anim = animate.apply(this, args)
    try { record(anim, 'animate') } catch {}
    return anim
  }
  W.begin =(hold = false) => { W.t0 = performance.now(); W.rows = []; W.seen = new WeakSet(); W.hold = hold; W.held = [] }
  W.take = () => { W.sweep(); return W.rows }
  // Seek every held animation to the same fraction of its own active time.
  W.seek = (f) => W.held.forEach((a) => {
    const t = a.effect.getComputedTiming()
    a.currentTime = (t.delay || 0) + f * (t.activeDuration || 0)
  })
  W.release = () => { W.held.forEach((a) => a.play()); W.hold = false; W.held = [] }

  // An element's look as the eye sees it: opacity and filter multiply down from its ancestors.
  W.look = (el) => {
    const cs = getComputedStyle(el)
    let opacity = 1, filters = []
    for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
      const s = getComputedStyle(e)
      opacity *= +s.opacity
      if (s.filter !== 'none') filters.push(s.filter)
    }
    return {
      opacity: Math.round(opacity * 1000) / 1000, filter: filters.join(' ') || 'none',
      transform: cs.transform, color: cs.color, background: cs.backgroundColor,
      top: Math.round(el.getBoundingClientRect().top),
    }
  }

  // Elements worth watching while the page scrolls: landmarks, headings, media, and anything sticky or fixed.
  W.scrollCandidates = (limit = 120) => {
    const picked = new Set()
    const add = (el) => { if (picked.size < limit && el.getBoundingClientRect().height > 0) picked.add(el) }
    document.querySelectorAll('header, nav, main > *, section, h1, h2, h3, img, video, canvas, [class*="sticky"], [class*="fixed"]').forEach(add)
    for (const el of document.querySelectorAll('body *')) {
      if (picked.size >= limit) break
      const p = getComputedStyle(el).position
      if (p === 'sticky' || p === 'fixed') add(el)
    }
    return [...picked].map((el, i) => { el.dataset.whalesScroll = String(i); return i })
  }

  // What the page offers to hover / focus, read from its own stylesheets.
  W.stateRules = () => {
    const out = { hover: new Set(), focusVisible: new Set(), focus: new Set() }
    for (const sheet of document.styleSheets) {
      let rules
      try { rules = sheet.cssRules } catch { continue }
      const walk = (list) => { for (const r of list) {
        if (r.cssRules && !r.selectorText) { walk(r.cssRules); continue }
        for (const part of (r.selectorText || '').split(',')) {
          const base = part.replace(/:(hover|focus-visible|focus|active|focus-within)\b/g, '').replace(/::?[a-z-]+(\([^)]*\))?$/, '').trim()
          if (/:hover\b/.test(part)) out.hover.add(base)
          if (/:focus-visible\b/.test(part)) out.focusVisible.add(base)
          else if (/:focus\b/.test(part)) out.focus.add(base)
        }
      } }
      walk(rules)
    }
    return { hover: [...out.hover], focusVisible: [...out.focusVisible], focus: [...out.focus] }
  }

  // A dev server's build-error overlay, which would otherwise be captured as the page.
  W.buildError = () => {
    const vite = document.querySelector('vite-error-overlay')
    if (vite) return (vite.shadowRoot?.querySelector('.message')?.textContent || 'vite error overlay').slice(0, 400)
    const next = document.querySelector('nextjs-portal')
    if (next && next.shadowRoot?.querySelector('[data-nextjs-dialog], [data-nextjs-toast-errors]')) return 'next.js error overlay'
    return null
  }
})()
