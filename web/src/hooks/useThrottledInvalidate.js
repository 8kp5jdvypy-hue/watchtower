import { useEffect } from 'react'
import { useThree } from '@react-three/fiber'

// With frameloop="demand", R3F renders nothing unless something calls
// invalidate(). Ambient ticks (particle drift, the kestrel's bob) still
// want to look continuous, so we invalidate on a throttled interval
// instead of every real frame -- ~12fps reads as smooth for slow drift,
// at a fraction of the GPU/CPU cost of a full 60fps loop. Pointer moves
// invalidate immediately for responsive parallax. `active` gates the
// whole thing off (e.g. once the hero scrolls out of view).
export function useThrottledInvalidate(active, fps = 12) {
  const invalidate = useThree((s) => s.invalidate)
  useEffect(() => {
    if (!active) return
    invalidate()
    const id = setInterval(invalidate, 1000 / fps)
    return () => clearInterval(id)
  }, [active, fps, invalidate])
}
