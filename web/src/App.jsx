import { useEffect } from 'react'
import { ScrollTrigger } from 'gsap/ScrollTrigger'
import BootSequence from './components/BootSequence'
import Grain from './components/Grain'
import Nav from './components/Nav'
import CustomCursor from './components/CustomCursor'
import Hero from './components/Hero'
import MarketField from './components/MarketField'
import AlertSequence from './components/AlertSequence'
import Manifesto from './components/Manifesto'
import AlertReveal from './components/AlertReveal'
import MarketCoverage from './components/MarketCoverage'
import SignalGlyph from './components/SignalGlyph'
import ProductValue from './components/ProductValue'
import Pricing from './components/Pricing'
import FinalCta from './components/FinalCta'
import Footer from './components/Footer'
import { useSmoothScroll } from './hooks/useSmoothScroll'
import { useReducedMotion } from './hooks/usePrefs'

export default function App() {
  const reduced = useReducedMotion()
  useSmoothScroll(!reduced)

  // Each section creates its own ScrollTrigger pin independently. A pin
  // created before a later sibling's pin-spacer exists caches a start/end
  // based on incomplete layout. One refresh after everything has mounted
  // and painted forces GSAP to recalculate every trigger against the final
  // DOM -- the standard fix for stacked pins across separate components.
  //
  // That first refresh fires ~2 frames after mount (~30ms) -- long before
  // the self-hosted display:swap webfonts are done loading (measured
  // ~500ms on a cold load). Every heading and paragraph on the page uses
  // those fonts, so the swap reflows section heights out from under every
  // trigger computed before it, and nothing re-measures afterward. The
  // visible symptom is scroll-linked reveals firing at the wrong scroll
  // position -- content that's supposed to animate in instead appears
  // already-finished, which reads as the page "skipping." A second
  // refresh once the fonts actually settle fixes it.
  useEffect(() => {
    const id = requestAnimationFrame(() => requestAnimationFrame(() => ScrollTrigger.refresh()))
    let cancelled = false
    if (document.fonts?.ready) {
      document.fonts.ready.then(() => { if (!cancelled) ScrollTrigger.refresh() })
    }
    return () => {
      cancelled = true
      cancelAnimationFrame(id)
    }
  }, [])

  return (
    <>
      <BootSequence />
      <Grain />
      <CustomCursor />
      <Nav />
      <main>
        <Hero />
        <MarketField />
        <AlertSequence />
        <Manifesto />
        <AlertReveal />
        <MarketCoverage />
        <div className="section-divider"><SignalGlyph /></div>
        <ProductValue />
        <Pricing />
        <FinalCta />
      </main>
      <Footer />
    </>
  )
}
