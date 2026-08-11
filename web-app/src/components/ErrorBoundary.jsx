import { Component } from 'react'
import { reportError } from '../errorReporter'
import PerchMark from './PerchMark'
import './ErrorBoundary.css'

// window.onerror (see errorReporter.js) never sees a React render
// crash -- React swallows it before it reaches the window. This is
// the other half: catches it, reports it, and shows something better
// than a blank screen instead of the app just disappearing.
export default class ErrorBoundary extends Component {
  state = { hasError: false }

  static getDerivedStateFromError() {
    return { hasError: true }
  }

  componentDidCatch(error, info) {
    reportError(error?.message, error?.stack || info?.componentStack)
  }

  render() {
    if (!this.state.hasError) return this.props.children
    return (
      <div className="error-boundary">
        <PerchMark size={26} state="idle" />
        <h1>Something went wrong.</h1>
        <p>This has been reported. Reloading usually fixes it.</p>
        <button type="button" onClick={() => window.location.reload()}>Reload</button>
      </div>
    )
  }
}
