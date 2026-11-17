import { renderToString } from 'react-dom/server'
import App from './App.jsx'
import RecordApp from './RecordApp.jsx'

// Build-time only (scripts/prerender.mjs). Renders each page's real
// first-paint markup so the served HTML carries the site's own copy.
// Live numbers stay client-side; the client mounts over this markup.
export const renderHome = () => renderToString(<App />)
export const renderRecord = () => renderToString(<RecordApp />)
