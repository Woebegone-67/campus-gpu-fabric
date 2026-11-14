import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const apiTarget = loadEnv(mode, '.', 'FABRIC_').FABRIC_API_TARGET || 'http://127.0.0.1:8080';
  return {
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/healthz': apiTarget,
      '/v1': apiTarget,
      '/x/v1': apiTarget,
    },
  },
  };
});
