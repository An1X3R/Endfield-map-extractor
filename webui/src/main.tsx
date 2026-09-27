import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './styles.css'

const root = createRoot(document.getElementById('root')!)

if (import.meta.env.DEV && new URLSearchParams(window.location.search).get('review') === 'completion') {
  void import('./components/completion/CompletionReview').then(({ CompletionReview }) => {
    root.render(<StrictMode><CompletionReview /></StrictMode>)
  })
} else {
  root.render(<StrictMode><App /></StrictMode>)
}
