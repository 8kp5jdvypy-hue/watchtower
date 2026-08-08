import { useEffect } from 'react'
import Lenis from 'lenis'
import gsap from 'gsap'
import { ScrollTrigger } from 'gsap/ScrollTrigger'

gsap.registerPlugin(ScrollTrigger)

// Lenis drives real scroll; ScrollTrigger drives our scroll-linked animation.
// Respects prefers-reduced-motion by simply not smoothing — the browser's
// native (instant, no inertia) scroll takes over, and ScrollTrigger still
// works fine against native scroll.
export function useSmoothScroll(enabled) {
  useEffect(() => {
    if (!enabled) return
    const lenis = new Lenis({
      duration: 1.15,
      easing: (t) => Math.min(1, 1.001 - Math.pow(2, -10 * t)),
      smoothWheel: true,
    })
    lenis.on('scroll', ScrollTrigger.update)
    const tick = (time) => lenis.raf(time * 1000)
    gsap.ticker.add(tick)
    // lagSmoothing(0) stops GSAP from capping a big gap between frames --
    // needed so Lenis/ScrollTrigger don't stutter-jump after the tab was
    // backgrounded. But applied from mount, it also strips that same
    // protection from any *other* real-time GSAP timeline, and the initial
    // page-load jank (parse/exec/first paint) reliably produces exactly the
    // kind of frame gap it's meant to guard against -- with it off, the
    // boot sequence's whole ~1.4s timeline got fast-forwarded through that
    // gap in one jump, measured at ~150ms real time instead of ~1.4s. Boot
    // finishes well before any scrolling can happen, so there's no reason
    // this needs to be active yet -- delay it until just after.
    const lagId = setTimeout(() => gsap.ticker.lagSmoothing(0), 1800)
    return () => {
      clearTimeout(lagId)
      lenis.destroy()
      // Must remove the exact function reference passed to add() -- passing
      // lenis.raf here (as before) removes a *different* function, so the
      // real ticker callback stayed registered forever and kept calling
      // .raf() on an already-destroyed Lenis instance on every frame.
      gsap.ticker.remove(tick)
    }
  }, [enabled])
}
