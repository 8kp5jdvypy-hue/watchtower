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
    gsap.ticker.add((time) => lenis.raf(time * 1000))
    gsap.ticker.lagSmoothing(0)
    return () => {
      lenis.destroy()
      gsap.ticker.remove(lenis.raf)
    }
  }, [enabled])
}
