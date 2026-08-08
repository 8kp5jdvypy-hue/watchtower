import { useEffect, useRef, useState } from 'react'

// Fires once when the element first enters the viewport, then disconnects.
// For small one-shot reveal animations that don't need scroll-scrubbing --
// cheaper than a GSAP ScrollTrigger for something this trivial.
export function useInViewOnce(threshold = 0.3) {
  const ref = useRef(null)
  const [inView, setInView] = useState(false)

  useEffect(() => {
    if (!ref.current || !('IntersectionObserver' in window)) {
      setInView(true)
      return
    }
    const io = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setInView(true)
          io.disconnect()
        }
      },
      { threshold }
    )
    io.observe(ref.current)
    return () => io.disconnect()
  }, [threshold])

  return [ref, inView]
}
