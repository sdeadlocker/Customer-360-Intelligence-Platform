# Observability backend portability (task 10.9, requirement 18.13)

The platform's telemetry is backend-portable by construction: the application only ever speaks
OTLP to a collector, and the collector decides where signals land. Moving from the local
development backend (Jaeger for traces, Prometheus for metrics) to AWS (X-Ray for traces,
CloudWatch for metrics) is a **collector-config swap with no application change**.

## Why no application change is needed

The application's entire coupling to a telemetry backend is one setting:

- `OTEL_EXPORTER_OTLP_ENDPOINT` — where the OTLP exporter sends spans and metrics.

Everything else — the span-attribute allowlist, the metric-label allowlist, the resource
attributes, the metric names, the salted customer hash — is produced in-process and is identical
regardless of the backend. The collector receives the same OTLP stream either way; only its
`exporters` and `service.pipelines` differ.

This is deliberate (design §13.7, §18.13): the allowlist that keeps PII, monetary values and prompt
content out of telemetry lives in the application, not the collector, so it cannot be lost by
deploying a differently-configured collector. The collector's `attributes/scrub` processor is
belt-and-braces, present in both configs.

## The two collector configs

| Backend | File | Traces exporter | Metrics exporter |
|---|---|---|---|
| Local dev | `docker/otel-collector.yaml` | `otlp/jaeger` | `prometheus` (:8889) |
| AWS | `docker/otel-collector.aws.yaml` | `awsxray` | `awsemf` (CloudWatch) |

The two files share the same `receivers` (OTLP gRPC :4317 / HTTP :4318), the same `batch` processor
and the same `attributes/scrub` processor. Only `exporters` and `service.pipelines` change.

## Performing the swap

1. Run the collector with the AWS config instead of the local one. With the compose stack, point
   the collector's `command` and volume at `otel-collector.aws.yaml`; on ECS/EC2, ship the same
   file as the collector's config.
2. Provide AWS credentials to the collector via the default credential chain (instance role, task
   role, or environment) and set `AWS_REGION`. This is the same credential model the application
   uses for Bedrock, so no new secret is introduced.
3. Leave the application untouched. `OTEL_EXPORTER_OTLP_ENDPOINT` still points at the collector; the
   app does not know X-Ray or CloudWatch exist.

No rebuild, no redeploy of the application, no code change. The swap is reversible by pointing the
collector back at `otel-collector.yaml`.

## What lands where on AWS

- **Traces → X-Ray.** OTLP spans become X-Ray segments. The correlation id and the salted customer
  hash the app sets as span attributes ride along, so a support request maps to a segment the same
  way it maps to a Jaeger trace locally.
- **Metrics → CloudWatch (EMF).** The metric names (`c360.*`, `gen_ai.*`) and their allowlisted
  dimensions (`role`, `route`, `agent`, `model`, `outcome`, …) are emitted verbatim as CloudWatch
  metrics in the `C360` namespace. The Grafana dashboards' PromQL does not port directly, but the
  underlying metric names and dimensions do, so equivalent CloudWatch dashboards query the same
  series.

## Verification

`backend/tests/test_backend_portability.py` asserts the invariants this document depends on:

- both collector configs share identical `receivers` and the `attributes/scrub` processor;
- the AWS config exports traces to `awsxray` and metrics to `awsemf`;
- the application exposes exactly one backend-coupling setting (`OTEL_EXPORTER_OTLP_ENDPOINT`) and
  no X-Ray/CloudWatch/Jaeger/Prometheus identifier appears anywhere in the application source.
