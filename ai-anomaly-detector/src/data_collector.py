"""
data_collector.py

Collects a snapshot of the Train Ticket system's observability data from
Jaeger (distributed traces) and Prometheus (metrics), then structures it
as a service dependency graph ready for anomaly detection.

Usage:
    from data_collector import collect_snapshot, save_snapshot

    data = collect_snapshot()
    save_snapshot(data, "snapshot_20260324.json")

Default endpoints (matching the port-forwards used in this project):
    Jaeger:     http://localhost:16686
    Prometheus: http://localhost:30090
"""

import json
import os
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    raise ImportError("Run: pip install requests")

# ---------------------------------------------------------------------------
# Default endpoints
# ---------------------------------------------------------------------------

JAEGER_DEFAULT = "http://localhost:16686"
PROMETHEUS_DEFAULT = "http://localhost:30090"

# How far back to look for traces (Jaeger lookback parameter)
TRACE_LOOKBACK = "5m"

# Maximum number of traces to fetch per service
TRACES_LIMIT = 200

# Directory (relative to this file) where snapshots are saved
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")


# ---------------------------------------------------------------------------
# Jaeger helpers
# ---------------------------------------------------------------------------

def _get_services(jaeger_url: str) -> List[str]:
    """
    Fetch the list of service names known to Jaeger.

    GET /api/services  ->  {"data": ["ts-auth-service", "ts-order-service", ...]}
    """
    resp = requests.get(f"{jaeger_url}/api/services", timeout=5)
    resp.raise_for_status()
    return resp.json().get("data", [])


def _get_traces(jaeger_url: str, service: str) -> List[dict]:
    """
    Fetch recent traces for a single service.

    GET /api/traces?service=<name>&limit=<n>&lookback=<duration>

    Each trace contains a list of spans and a processes map:
        processes: { "p1": { "serviceName": "ts-order-service" }, ... }
        spans: [
            {
                "spanID":        "abc123",
                "processID":     "p1",         # key into processes map
                "duration":      45000,         # microseconds
                "references":    [{"refType": "CHILD_OF", "spanID": "parent123"}],
                ...
            },
            ...
        ]
    """
    params = {
        "service": service,
        "limit": TRACES_LIMIT,
        "lookback": TRACE_LOOKBACK,
    }
    resp = requests.get(f"{jaeger_url}/api/traces", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json().get("data", [])


def _build_span_index(trace: dict) -> Dict[str, dict]:
    """
    Build a flat spanID -> span_info dict for one trace.

    span_info = {
        "service":    str,   # resolved from trace["processes"]
        "duration_ms": float,
        "parent_id":  str | None,
    }

    The span's service name lives in trace["processes"][span["processID"]]["serviceName"].
    """
    processes = trace.get("processes", {})
    index: Dict[str, dict] = {}

    for span in trace.get("spans", []):
        span_id = span.get("spanID")
        if not span_id:
            continue

        # Resolve service name via processID
        process = processes.get(span.get("processID", ""), {})
        service = process.get("serviceName", "unknown")

        # Duration stored in microseconds → convert to milliseconds
        duration_ms = span.get("duration", 0) / 1000.0

        # Find parent span ID (only CHILD_OF references count)
        parent_id = None
        for ref in span.get("references", []):
            if ref.get("refType") == "CHILD_OF":
                parent_id = ref.get("spanID")
                break

        index[span_id] = {
            "service": service,
            "duration_ms": duration_ms,
            "parent_id": parent_id,
        }

    return index


def _extract_edges_from_trace(
    trace: dict,
) -> List[Tuple[str, str, float]]:
    """
    Extract inter-service calls from one trace as (source, target, latency_ms) tuples.

    A call from service A to service B is identified when a span belonging to B
    has a parent span belonging to A (cross-service CHILD_OF reference).
    The latency used is the child span's duration (time spent inside service B).
    """
    index = _build_span_index(trace)
    edges: List[Tuple[str, str, float]] = []

    for span_id, span in index.items():
        parent_id = span["parent_id"]
        if parent_id and parent_id in index:
            parent_service = index[parent_id]["service"]
            child_service = span["service"]
            # Only record if it's a genuine cross-service call
            if parent_service != child_service:
                edges.append((parent_service, child_service, span["duration_ms"]))

    return edges


def collect_jaeger_data(
    jaeger_url: str,
) -> Tuple[Dict[str, int], Dict[Tuple[str, str], List[float]]]:
    """
    Query Jaeger for all services and build:
      - node_calls:  { service_name: total_span_count }
      - edge_latencies: { (source, target): [latency_ms, ...] }

    Returns (node_calls, edge_latencies).
    On connection error, returns empty dicts and prints a warning.
    """
    node_calls: Dict[str, int] = defaultdict(int)
    edge_latencies: Dict[Tuple[str, str], List[float]] = defaultdict(list)

    try:
        services = _get_services(jaeger_url)
    except requests.exceptions.ConnectionError:
        print(f"  [Jaeger] Cannot connect to {jaeger_url}. Skipping trace data.")
        return node_calls, edge_latencies
    except Exception as e:
        print(f"  [Jaeger] Error fetching services: {e}")
        return node_calls, edge_latencies

    print(f"  [Jaeger] Found {len(services)} service(s). Fetching traces...")

    for service in services:
        try:
            traces = _get_traces(jaeger_url, service)
        except Exception as e:
            print(f"  [Jaeger] Error fetching traces for {service}: {e}")
            continue

        for trace in traces:
            # Count spans per service (node activity)
            for span in trace.get("spans", []):
                process = trace["processes"].get(span.get("processID", ""), {})
                svc = process.get("serviceName", "unknown")
                node_calls[svc] += 1

            # Collect inter-service edges
            for src, dst, latency_ms in _extract_edges_from_trace(trace):
                edge_latencies[(src, dst)].append(latency_ms)

    return node_calls, edge_latencies


# ---------------------------------------------------------------------------
# Prometheus helpers
# ---------------------------------------------------------------------------

def _prom_query(prometheus_url: str, promql: str) -> List[dict]:
    """
    Execute an instant PromQL query.

    GET /api/v1/query?query=<promql>  ->  {"data": {"result": [...]}}

    Each result item has:
        { "metric": {"__name__": ..., "service": ...}, "value": [timestamp, "42.3"] }
    """
    resp = requests.get(
        f"{prometheus_url}/api/v1/query",
        params={"query": promql},
        timeout=5,
    )
    resp.raise_for_status()
    return resp.json().get("data", {}).get("result", [])


def collect_prometheus_data(
    prometheus_url: str,
) -> Dict[str, Dict[str, Optional[float]]]:
    """
    Query Prometheus for per-service p99 latency and error rate.

    Tries the OTel standard metric names used by the Java auto-instrumentation agent:
      - http_server_request_duration_milliseconds_bucket  (histogram)
      - http_server_requests_errors_total                 (counter)
      - http_server_requests_total                        (counter)

    Returns:
        {
            "ts-order-service": {
                "p99_latency_ms": 120.5,   # None if not available
                "error_rate":     0.03,    # None if not available
            },
            ...
        }

    On connection error, returns an empty dict and prints a warning.
    """
    metrics: Dict[str, Dict[str, Optional[float]]] = defaultdict(
        lambda: {"p99_latency_ms": None, "error_rate": None}
    )

    try:
        # --- p99 latency ---
        # OTel Java agent histogram metric name (milliseconds variant)
        p99_query = (
            "histogram_quantile(0.99, "
            "sum by (le, service_name) ("
            "rate(http_server_request_duration_milliseconds_bucket[5m])"
            "))"
        )
        results = _prom_query(prometheus_url, p99_query)
        for item in results:
            svc = item["metric"].get("service_name", item["metric"].get("service", ""))
            if svc:
                try:
                    metrics[svc]["p99_latency_ms"] = float(item["value"][1])
                except (IndexError, ValueError):
                    pass

        # --- error rate ---
        # Ratio of 5xx responses over total HTTP requests in the last 5 minutes
        error_rate_query = (
            "sum by (service_name) ("
            "rate(http_server_requests_total{http_response_status_code=~'5..'}[5m])"
            ") / sum by (service_name) ("
            "rate(http_server_requests_total[5m])"
            ")"
        )
        results = _prom_query(prometheus_url, error_rate_query)
        for item in results:
            svc = item["metric"].get("service_name", item["metric"].get("service", ""))
            if svc:
                try:
                    metrics[svc]["error_rate"] = float(item["value"][1])
                except (IndexError, ValueError):
                    pass

        print(f"  [Prometheus] Collected metrics for {len(metrics)} service(s).")

    except requests.exceptions.ConnectionError:
        print(f"  [Prometheus] Cannot connect to {prometheus_url}. Skipping metrics.")
    except Exception as e:
        print(f"  [Prometheus] Error: {e}")

    return metrics


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect_snapshot(
    jaeger_url: str = JAEGER_DEFAULT,
    prometheus_url: str = PROMETHEUS_DEFAULT,
) -> Dict[str, Any]:
    """
    Collect a point-in-time snapshot of the service mesh state.

    Queries Jaeger for traces from the last 5 minutes and Prometheus for
    current metrics, then merges them into a unified structure.

    Returns:
        {
            "collected_at": "2026-03-24T19:00:00",   # ISO timestamp
            "nodes": [
                {
                    "service":       "ts-order-service",
                    "call_count":    142,              # span count from Jaeger
                    "p99_latency_ms": 85.3,            # from Prometheus (None if unavailable)
                    "error_rate":    0.01,             # from Prometheus (None if unavailable)
                },
                ...
            ],
            "edges": [
                {
                    "source":        "ts-preserve-service",
                    "target":        "ts-order-service",
                    "call_count":    45,
                    "avg_latency_ms": 22.7,
                },
                ...
            ],
        }
    """
    print("Collecting snapshot...")
    collected_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    # --- Jaeger ---
    node_calls, edge_latencies = collect_jaeger_data(jaeger_url)

    # --- Prometheus ---
    prom_metrics = collect_prometheus_data(prometheus_url)

    # --- Merge nodes ---
    # Union of services seen in Jaeger and Prometheus
    all_services = set(node_calls.keys()) | set(prom_metrics.keys())
    nodes = []
    for svc in sorted(all_services):
        prom = prom_metrics.get(svc, {})
        nodes.append(
            {
                "service": svc,
                "call_count": node_calls.get(svc, 0),
                "p99_latency_ms": prom.get("p99_latency_ms"),
                "error_rate": prom.get("error_rate"),
            }
        )

    # --- Build edges ---
    edges = []
    for (src, dst), latencies in sorted(edge_latencies.items()):
        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
        edges.append(
            {
                "source": src,
                "target": dst,
                "call_count": len(latencies),
                "avg_latency_ms": round(avg_latency, 3),
            }
        )

    snapshot = {
        "collected_at": collected_at,
        "nodes": nodes,
        "edges": edges,
    }

    print(f"  Snapshot ready: {len(nodes)} node(s), {len(edges)} edge(s).")
    return snapshot


def save_snapshot(data: Dict[str, Any], filename: str) -> str:
    """
    Save a snapshot dict as a JSON file in the data/ directory.

    Args:
        data:     The dict returned by collect_snapshot().
        filename: File name (e.g. "snapshot_20260324.json").
                  If it doesn't end in .json, the extension is added automatically.

    Returns:
        The absolute path of the saved file.
    """
    if not filename.endswith(".json"):
        filename += ".json"

    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, filename)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"  Snapshot saved to: {path}")
    return path


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Collect a service mesh snapshot")
    parser.add_argument("--jaeger",      default=JAEGER_DEFAULT,      help="Jaeger base URL")
    parser.add_argument("--prometheus",  default=PROMETHEUS_DEFAULT,  help="Prometheus base URL")
    parser.add_argument("--out",         default=None,                help="Output filename (default: snapshot_<timestamp>.json)")
    args = parser.parse_args()

    snapshot = collect_snapshot(jaeger_url=args.jaeger, prometheus_url=args.prometheus)

    filename = args.out or f"snapshot_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    save_snapshot(snapshot, filename)
