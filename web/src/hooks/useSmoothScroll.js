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
    gsap.ticker.lagSmoothing(0)
    return () => {
      lenis.destroy()
      // Must remove the exact function reference passed to add() -- passing
      // lenis.raf here (as before) removes a *different* function, so the
      // real ticker callback stayed registered forever and kept calling
      // .raf() on an already-destroyed Lenis instance on every frame.
      gsap.ticker.remove(tick)
    }
  }, [enabled])
}
