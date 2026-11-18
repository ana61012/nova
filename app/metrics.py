from prometheus_client import Counter, Gauge, Histogram

RESERVATIONS = Counter("drop_reservations_total", "Reservation attempts", ["result"])
WEBHOOKS = Counter("drop_webhooks_total", "Payment webhook outcomes", ["outcome"])
QUEUE_WAITING = Gauge("drop_queue_waiting", "People waiting in the queue")
QUEUE_ADMITTED = Gauge("drop_queue_admitted", "People currently admitted")
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP latency", ["method", "route", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
