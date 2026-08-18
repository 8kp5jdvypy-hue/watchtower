import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import RecordApp from './RecordApp.jsx'
import ErrorBoundary from './components/ErrorBoundary.jsx'
import { installGlobalErrorReporting } from './errorReporter.js'

installGlobalErrorReporting()

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <ErrorBoundary>
      <RecordApp />
    </ErrorBoundary>
  </StrictMode>,
)
