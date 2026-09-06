import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    // beui components are written against the shadcn convention and import each
    // other as "@/components/...", "@/lib/utils" - the alias is what lets them be
    // dropped in unmodified, so updating one later is a copy rather than a rewrite.
    alias: { '@': new URL('./src', import.meta.url).pathname },
  },
})
