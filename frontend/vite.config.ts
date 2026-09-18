import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

/**
 * The dev server proxies API paths to the backend rather than the app calling
 * `http://127.0.0.1:8000` directly. Same-origin requests in development keep CORS out of the
 * picture and mean production builds need no base-URL switch.
 */
const BACKEND = 'http://127.0.0.1:8000';
// The OpenTelemetry Collector's OTLP/HTTP receiver (docker/otel-collector.yaml, :4318). RUM posts
// browser metrics to `/v1/metrics`, proxied here in development.
const OTEL_COLLECTOR_HTTP = 'http://127.0.0.1:4318';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      // Every backend path the app calls is proxied so the browser makes same-origin requests and
      // CORS stays out of the picture in development. These mirror the routers mounted in
      // `backend/src/c360/main.py`.
      '/api': { target: BACKEND, changeOrigin: true },
      '/health': { target: BACKEND, changeOrigin: true },
      '/ready': { target: BACKEND, changeOrigin: true },
      '/auth': { target: BACKEND, changeOrigin: true },
      '/me': { target: BACKEND, changeOrigin: true },
      '/customers': { target: BACKEND, changeOrigin: true },
      // The cross-customer Ask AI endpoint on the search landing page (task 9.6).
      '/ask': { target: BACKEND, changeOrigin: true },
      // The revenue plays: the bank-wide priced pipeline and the cross-book money-in-motion feed
      // (Phase 22). Per-customer revenue reads hang off `/customers`, already proxied above.
      '/revenue': { target: BACKEND, changeOrigin: true },
      '/knowledge': { target: BACKEND, changeOrigin: true },
      '/admin': { target: BACKEND, changeOrigin: true },
      // RUM (task 10.5) posts OTLP/HTTP metrics here; proxied to the collector's HTTP receiver so
      // the browser makes a same-origin request and CORS stays out of the picture in development.
      '/v1/metrics': { target: OTEL_COLLECTOR_HTTP, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
  test: {
    environment: 'jsdom',
    globals: false,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'lcov'],
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/main.tsx',
        'src/test/**',
        'src/**/*.test.{ts,tsx}',
        'src/**/*.d.ts',
        // Machine-generated API types carry no logic to cover (task 12.1).
        'src/api/generated/**',
      ],
    },
  },
});
