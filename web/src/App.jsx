import { useEffect } from 'react'
import { ScrollTrigger } from 'gsap/ScrollTrigger'
import BootSequence from './components/BootSequence'
import Grain from './components/Grain'
import Nav from './components/Nav'
import CustomCursor from './components/CustomCursor'
import Hero from './components/Hero'
import MarketField from './components/MarketField'
import SignalVisualization from './components/SignalVisualization'
import MarketContext from './components/MarketContext'
import Manifesto from './components/Manifesto'
import ProductDemo from './components/ProductDemo'
import MarketCoverage from './components/MarketCoverage'
import Waitlist from './components/Waitlist'
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
  useEffect(() => {
    const id = requestAnimationFrame(() => requestAnimationFrame(() => ScrollTrigger.refresh()))
    return () => cancelAnimationFrame(id)
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
        <SignalVisualization />
        <MarketContext />
        <Manifesto />
        <ProductDemo />
        <MarketCoverage />
        <Waitlist />
        <FinalCta />
      </main>
      <Footer />
    </>
  )
}
