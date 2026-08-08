import { useMemo, useRef } from 'react'
import { useFrame, useThree } from '@react-three/fiber'
import { Billboard } from '@react-three/drei'
import { EffectComposer, Bloom, Vignette } from '@react-three/postprocessing'
import { makeKestrelTexture } from './kestrelTexture'
import { makeSoftDotTexture } from './particleTexture'
import { useThrottledInvalidate } from '../hooks/useThrottledInvalidate'
import './KestrelMaterial'

// Cut hard from the previous pass: fewer points, no chromatic aberration
// (real GPU cost for the least essential effect), and everything now
// only renders when useThrottledInvalidate ticks it -- not a continuous
// 60fps loop. See Hero.jsx for the IntersectionObserver that stops the
// tick entirely once this scene scrolls out of view.
const PARTICLE_COUNT_FAR = 180
const PARTICLE_COUNT_NEAR = 50

function ParticleLayer({ count, depth, spread, size, speed, reduced }) {
  const ref = useRef(null)
  const texture = useMemo(() => makeSoftDotTexture(), [])
  const positions = useMemo(() => {
    const arr = new Float32Array(count * 3)
    for (let i = 0; i < count; i++) {
      arr[i * 3 + 0] = (Math.random() - 0.5) * spread
      arr[i * 3 + 1] = (Math.random() - 0.5) * (spread * 0.6)
      arr[i * 3 + 2] = -Math.random() * depth
    }
    return arr
  }, [count, spread, depth])

  useFrame((state, delta) => {
    if (reduced || !ref.current) return
    ref.current.rotation.y += delta * 0.012 * speed
    ref.current.position.x = state.pointer.x * 0.4 * speed
    ref.current.position.y = state.pointer.y * 0.25 * speed
  })

  return (
    <points ref={ref}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" args={[positions, 3]} />
      </bufferGeometry>
      <pointsMaterial
        map={texture}
        size={size}
        sizeAttenuation
        transparent
        opacity={0.55}
        color="#bcd6e8"
        depthWrite={false}
      />
    </points>
  )
}

function Kestrel({ reduced }) {
  const groupRef = useRef(null)
  const matRef = useRef(null)
  const texture = useMemo(() => makeKestrelTexture(), [])

  useFrame((state) => {
    if (!groupRef.current) return
    const t = state.clock.elapsedTime
    const hover = reduced ? 0 : Math.sin(t * 0.6) * 0.09
    groupRef.current.position.y = 0.4 + hover
    if (!reduced) {
      groupRef.current.rotation.z = state.pointer.x * -0.05
      groupRef.current.position.x = 2.1 + state.pointer.x * 0.18
    }
    if (matRef.current) matRef.current.time = t
  })

  return (
    <group ref={groupRef} position={[2.1, 0.4, -3]}>
      <Billboard>
        <mesh scale={[4.6, 4.6, 1]}>
          <planeGeometry args={[1, 1]} />
          {/* eslint-disable-next-line react/no-unknown-property */}
          <kestrelMaterial
            ref={matRef}
            map={texture}
            transparent
            depthWrite={false}
            toneMapped={false}
          />
        </mesh>
      </Billboard>
    </group>
  )
}

function CameraRig({ reduced }) {
  const { camera } = useThree()
  useFrame((state) => {
    if (reduced) return
    const tx = state.pointer.x * 0.6
    const ty = state.pointer.y * 0.35
    camera.position.x += (tx - camera.position.x) * 0.035
    camera.position.y += (ty - camera.position.y) * 0.035
    camera.fov = 45 + Math.sin(state.clock.elapsedTime * 0.15) * 0.6
    camera.updateProjectionMatrix()
    camera.lookAt(0, 0.2, -4)
  })
  return null
}

export default function HeroScene({ reduced, isMobile, active }) {
  useThrottledInvalidate(active && !reduced, 12)

  return (
    <>
      <color attach="background" args={['#05070a']} />
      <fog attach="fog" args={['#05070a', 5, 19]} />
      <ambientLight intensity={0.35} />
      <directionalLight position={[4, 6, 2]} intensity={0.6} color="#bcd6e8" />
      <CameraRig reduced={reduced} />
      <ParticleLayer count={PARTICLE_COUNT_FAR} depth={18} spread={22} size={0.08} speed={1} reduced={reduced} />
      {!isMobile && (
        <ParticleLayer count={PARTICLE_COUNT_NEAR} depth={6} spread={14} size={0.05} speed={2.2} reduced={reduced} />
      )}
      <Kestrel reduced={reduced} />
      {!reduced && !isMobile && (
        <EffectComposer multisampling={0}>
          <Bloom intensity={0.85} luminanceThreshold={0.15} luminanceSmoothing={0.4} mipmapBlur radius={0.7} />
          <Vignette eskil={false} offset={0.25} darkness={0.9} />
        </EffectComposer>
      )}
    </>
  )
}
