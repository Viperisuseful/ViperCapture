import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { Toaster } from '@/components/ui/sonner'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
    <Toaster
      richColors
      position="bottom-right"
      mobileOffset={{ bottom: "calc(1rem + env(safe-area-inset-bottom, 0px))", left: 16, right: 16 }}
    />
  </StrictMode>,
)
