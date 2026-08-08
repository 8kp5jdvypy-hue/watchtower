import { useMemo, useRef } from 'react'
import { useFrame, useThree } from '@react-three/fiber'
import * as THREE from 'three'
import { makeKestrelTexture } from './kestrelTexture'
import { makeSoftDotTexture } from './particleTexture'

const PARTICLE_COUNT = 420

function ParticleField({ reduced }) {
  const ref = useRef(null)
  const texture = useMemo(() => makeSoftDotTexture(), [])
  const positions = useMemo(() => {
    const arr = new Float32Array(PARTICLE_COUNT * 3)
    for (let i = 0; i < PARTICLE_COUNT; i++) {
      arr[i * 3 + 0] = (Math.random() - 0.5) * 22
      arr[i * 3 + 1] = (Math.random() - 0.5) * 14
      arr[i * 3 + 2] = -Math.random() * 18
    }
    return arr
  }, [])

  useFrame((state, delta) => {
    if (reduced || !ref.current) return
    ref.current.rotation.y += delta * 0.012
    ref.current.position.x = state.pointer.x * 0.4
    ref.current.position.y = state.pointer.y * 0.25
  })

  return (
    <points ref={ref}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" args={[positions, 3]} />
      </bufferGeometry>
      <pointsMaterial
        map={texture}
        size={0.09}
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
  const ref = useRef(null)
  const texture = useMemo(() => makeKestrelTexture(), [])

  useFrame((state) => {
    if (!ref.current) return
    const hover = reduced ? 0 : Math.sin(state.clock.elapsedTime * 0.6) * 0.08
    ref.current.position.y = 0.4 + hover
    if (!reduced) {
      ref.current.rotation.z = state.pointer.x * -0.04
      ref.current.position.x = 2.1 + state.pointer.x * 0.15
    }
  })

  return (
    <sprite ref={ref} position={[2.1, 0.4, -3]} scale={[4.2, 4.2, 1]}>
      <spriteMaterial map={texture} transparent depthWrite={false} opacity={0.92} />
    </sprite>
  )
}

function CameraRig({ reduced }) {
  const { camera } = useThree()
  useFrame((state) => {
    if (reduced) return
    const tx = state.pointer.x * 0.35
    const ty = state.pointer.y * 0.2
    camera.position.x += (tx - camera.position.x) * 0.04
    camera.position.y += (ty - camera.position.y) * 0.04
    camera.lookAt(0, 0.2, -4)
  })
  return null
}

export default function HeroScene({ reduced }) {
  return (
    <>
      <color attach="background" args={['#05070a']} />
      <fog attach="fog" args={['#05070a', 6, 20]} />
      <ambientLight intensity={0.4} />
      <directionalLight position={[4, 6, 2]} intensity={0.6} color="#bcd6e8" />
      <CameraRig reduced={reduced} />
      <ParticleField reduced={reduced} />
      <Kestrel reduced={reduced} />
    </>
  )
}
