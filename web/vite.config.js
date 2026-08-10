import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        // The hero's WebGL scene needs three/@react-three/*/postprocessing
        // and gsap immediately -- none of that can be deferred without
        // touching the opening animation, which is explicitly off-limits.
        // Splitting them into their own chunk doesn't change *when*
        // anything loads (everything here is still imported eagerly, so
        // total time-to-interactive for this deploy is unchanged) -- it
        // changes what happens on the *next* deploy: a change to page
        // copy or a marketing component gets a new hash only for the
        // small app chunk, and a repeat visitor's already-cached vendor
        // chunk (see public/_headers' immutable Cache-Control) is
        // reused untouched instead of being invalidated by an unrelated
        // change.
        // Rolldown (Vite 8's bundler) only accepts the function form of
        // manualChunks, not Rollup's object shorthand.
        manualChunks(id) {
          if (/node_modules\/(three|@react-three|postprocessing|gsap)\//.test(id)) {
            return 'vendor'
          }
        },
      },
    },
  },
})
