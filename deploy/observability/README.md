# Tracing deployment

The OpenTelemetry Collector receives OTLP traces from the controller and forwards them to the in-cluster Jaeger all-in-one instance. The Jaeger data is intentionally ephemeral; controller JSONL and load-test artifacts remain the source of record.

After deployment, view traces only through a local port-forward:

```powershell
kubectl -n scheduler-system port-forward service/jaeger 16686:16686
```

No public LoadBalancer or Ingress is created for Jaeger.
