import * as THREE from 'three'
import { shaderMaterial } from '@react-three/drei'
import { extend } from '@react-three/fiber'

// Edge-detects the alpha channel of the silhouette texture and adds a
// restrained glowing rim along the boundary -- the thing that makes a flat
// cutout read as "backlit against atmosphere" instead of a paper sticker.
// Deliberately toned down: a quiet edge accent, not a full-body glow --
// "90% refined silhouette, 10% cyan accent," not a gaming-logo neon
// outline. No constant pulsing.
const KestrelMaterial = shaderMaterial(
  {
    map: null,
    rimColor: new THREE.Color('#7fe8ff'),
    rimStrength: 0.5,
    time: 0,
    texel: 1 / 1024,
    fade: 1,
  },
  /* glsl */ `
    varying vec2 vUv;
    void main() {
      vUv = uv;
      gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    }
  `,
  /* glsl */ `
    uniform sampler2D map;
    uniform vec3 rimColor;
    uniform float rimStrength;
    uniform float time;
    uniform float texel;
    uniform float fade;
    varying vec2 vUv;

    void main() {
      vec4 tex = texture2D(map, vUv);
      float t = texel * 2.5;
      float aUp = texture2D(map, vUv + vec2(0.0, t)).a;
      float aDown = texture2D(map, vUv - vec2(0.0, t)).a;
      float aLeft = texture2D(map, vUv - vec2(t, 0.0)).a;
      float aRight = texture2D(map, vUv + vec2(t, 0.0)).a;
      float edge = clamp(abs(tex.a - aUp) + abs(tex.a - aDown) + abs(tex.a - aLeft) + abs(tex.a - aRight), 0.0, 1.0);

      vec3 color = tex.rgb + rimColor * edge * rimStrength;
      float alpha = clamp(tex.a + edge * 0.4, 0.0, 1.0);
      gl_FragColor = vec4(color, alpha * fade);
    }
  `
)

extend({ KestrelMaterial })

export { KestrelMaterial }
