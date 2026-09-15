/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * OTLP/HTTP metrics endpoint the RUM module posts Web Vitals and custom marks to (task 10.5).
   * Empty or unset disables RUM. In development this is proxied to the collector via `/v1/metrics`;
   * in production it is set to the collector's public OTLP/HTTP metrics URL.
   */
  readonly VITE_OTLP_METRICS_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
