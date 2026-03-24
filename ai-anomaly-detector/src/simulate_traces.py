"""
simulate_traces.py

Generates synthetic service-mesh snapshots that mimic the structure returned
by collect_snapshot() — without needing a live Jaeger / Prometheus stack.

Two regimes
-----------
normal  — latencies drawn from realistic log-normal distributions, near-zero
          error rates.
anomaly — a 3 000 ms network delay is injected on ts-order-service (simulates
          Chaos Mesh NetworkChaos with direction: from).  Every call that
          terminates at ts-order-service sees its latency increase by ~3 000 ms.

Usage
-----
    # 20 normal + 10 anomalous snapshots, saved to ai-anomaly-detector/data/
    python simulate_traces.py --normal 20 --anomaly 10

    # 1 snapshot printed to stdout (no files written)
    python simulate_traces.py --normal 1 --print

    # Custom output directory
    python simulate_traces.py --normal 20 --anomaly 10 --out-dir /tmp/snapshots
"""

from __future__ import annotations

import json
import os
import random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Service topology  (source, target, base_mean_ms, base_std_ms, base_call_rate)
#
# base_call_rate is the expected number of calls per snapshot window (~5 min).
# Drawn from a Poisson distribution so each snapshot looks slightly different.
# ---------------------------------------------------------------------------

TOPOLOGY: List[Tuple[str, str, float, float, int]] = [
    # source                     target                    mean  std  calls
    ("ts-preserve-service",  "ts-order-service",           25.0, 8.0,  40),
    ("ts-preserve-service",  "ts-seat-service",            15.0, 4.0,  38),
    ("ts-preserve-service",  "ts-travel-service",          20.0, 6.0,  42),
    ("ts-preserve-service",  "ts-contacts-service",        12.0, 3.0,  40),
    ("ts-order-service",     "ts-price-service",           18.0, 5.0,  35),
    ("ts-order-service",     "ts-station-service",         10.0, 3.0,  32),
    ("ts-travel-service",    "ts-station-service",          8.0, 2.0,  60),
    ("ts-auth-service",      "ts-user-service",            14.0, 4.0,  85),
    ("ts-payment-service",   "ts-order-service",           22.0, 7.0,  20),
    ("ts-route-service",     "ts-station-service",          9.0, 2.5,  28),
    ("ts-travel-service",    "ts-route-service",           11.0, 3.0,  55),
    ("ts-preserve-service",  "ts-route-service",           13.0, 4.0,  38),
]

# Name of the service where the anomaly is injected
ANOMALY_SERVICE = "ts-order-service"

# Extra latency (ms) added to every call terminating at ANOMALY_SERVICE
ANOMALY_DELAY_MS = 3_000.0

# Typical base error rate per service in normal operation (fraction 0-1)
BASE_ERROR_RATE = 0.005

# Error rate for ANOMALY_SERVICE when anomaly is active
ANOMALY_ERROR_RATE = 0.12


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _all_services() -> List[str]:
    """Return the sorted list of unique service names in the topology."""
    services: set = set()
    for src, dst, *_ in TOPOLOGY:
        services.add(src)
        services.add(dst)
    return sorted(services)


def _lognormal_sample(mean_ms: float, std_ms: float, rng: random.Random) -> float:
    """
    Draw one latency sample (ms) from a log-normal distribution.

    The log-normal is a good model for response times: always positive, right-
    skewed, and bounded away from zero.

    mean_ms, std_ms refer to the *underlying* normal (mu, sigma) parameters,
    not to the mean/std of the resulting log-normal, which is fine for our
    purposes (we just want a plausible spread around the given baseline).
    """
    if std_ms <= 0:
        return mean_ms
    # Convert (mean, std) of the normal to log-normal parameters
    # mu_ln = ln(mean^2 / sqrt(mean^2 + std^2))
    import math
    variance = std_ms ** 2
    mu_ln = math.log(mean_ms ** 2 / math.sqrt(mean_ms ** 2 + variance))
    sigma_ln = math.sqrt(math.log(1 + variance / (mean_ms ** 2)))
    return rng.lognormvariate(mu_ln, sigma_ln)


def _simulate_edge(
    src: str,
    dst: str,
    mean_ms: float,
    std_ms: float,
    base_calls: int,
    anomaly: bool,
    rng: random.Random,
) -> Dict[str, Any]:
    """
    Simulate one edge in the service graph for a single snapshot window.

    Returns a dict matching the edge schema of collect_snapshot():
        { source, target, call_count, avg_latency_ms }
    """
    # Poisson-distributed call count
    call_count = max(1, rng.randint(
        int(base_calls * 0.7),
        int(base_calls * 1.3),
    ))

    # Inject delay for calls that terminate at the anomaly service
    extra_ms = ANOMALY_DELAY_MS if (anomaly and dst == ANOMALY_SERVICE) else 0.0

    # Average latency: mean of N log-normal samples + constant extra delay
    total_latency = sum(
        _lognormal_sample(mean_ms, std_ms, rng) + extra_ms
        for _ in range(call_count)
    )
    avg_latency = total_latency / call_count

    return {
        "source": src,
        "target": dst,
        "call_count": call_count,
        "avg_latency_ms": round(avg_latency, 3),
    }


def _simulate_node(
    service: str,
    call_count: int,
    anomaly: bool,
    rng: random.Random,
) -> Dict[str, Any]:
    """
    Simulate per-service Prometheus metrics for a single snapshot window.

    Returns a dict matching the node schema of collect_snapshot():
        { service, call_count, p99_latency_ms, error_rate }
    """
    is_target = anomaly and service == ANOMALY_SERVICE

    # p99 latency: roughly 2-3× the average edge latency arriving at this node
    base_p99 = rng.uniform(40.0, 90.0)
    p99 = base_p99 + (ANOMALY_DELAY_MS * rng.uniform(0.95, 1.05)) if is_target else base_p99

    error_rate = rng.uniform(ANOMALY_ERROR_RATE * 0.8, ANOMALY_ERROR_RATE * 1.2) \
        if is_target else rng.uniform(0.0, BASE_ERROR_RATE * 2)

    return {
        "service": service,
        "call_count": call_count,
        "p99_latency_ms": round(p99, 3),
        "error_rate": round(error_rate, 6),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def simulate_snapshot(
    anomaly: bool = False,
    timestamp: Optional[datetime] = None,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Generate one synthetic snapshot of the service mesh.

    Parameters
    ----------
    anomaly:
        If True, inject a 3 000 ms delay on ts-order-service.
    timestamp:
        The ``collected_at`` value.  Defaults to ``datetime.utcnow()``.
    seed:
        Random seed for reproducibility.

    Returns
    -------
    dict
        Same schema as ``collect_snapshot()``:
        ``{ collected_at, nodes, edges, _meta }``
        The extra ``_meta`` key carries ground-truth labels for later
        evaluation:  ``{ anomaly: bool, anomaly_service: str | None }``.
    """
    rng = random.Random(seed)
    ts = timestamp or datetime.utcnow()
    collected_at = ts.strftime("%Y-%m-%dT%H:%M:%S")

    # --- edges ---
    edges: List[Dict[str, Any]] = []
    service_call_totals: Dict[str, int] = {svc: 0 for svc in _all_services()}

    for src, dst, mean_ms, std_ms, base_calls in TOPOLOGY:
        edge = _simulate_edge(src, dst, mean_ms, std_ms, base_calls, anomaly, rng)
        edges.append(edge)
        # accumulate total call count per service (as a rough proxy for span count)
        service_call_totals[src] = service_call_totals.get(src, 0) + edge["call_count"]
        service_call_totals[dst] = service_call_totals.get(dst, 0) + edge["call_count"]

    # --- nodes ---
    nodes: List[Dict[str, Any]] = []
    for svc in _all_services():
        node = _simulate_node(
            svc,
            call_count=service_call_totals.get(svc, 0),
            anomaly=anomaly,
            rng=rng,
        )
        nodes.append(node)

    return {
        "collected_at": collected_at,
        "nodes": nodes,
        "edges": edges,
        "_meta": {
            "anomaly": anomaly,
            "anomaly_service": ANOMALY_SERVICE if anomaly else None,
        },
    }


def generate_dataset(
    n_normal: int = 20,
    n_anomaly: int = 10,
    start_time: Optional[datetime] = None,
    interval_minutes: int = 5,
    seed: Optional[int] = 42,
) -> List[Dict[str, Any]]:
    """
    Generate a time-ordered list of snapshots (normal + anomalous).

    The anomalous snapshots are placed *after* the normal ones, simulating a
    fault that appears partway through the observation window.

    Parameters
    ----------
    n_normal:
        Number of normal snapshots.
    n_anomaly:
        Number of anomalous snapshots.
    start_time:
        Timestamp of the first snapshot.  Defaults to now.
    interval_minutes:
        Minutes between consecutive snapshots.
    seed:
        Base random seed.  Each snapshot gets seed + i for reproducibility.

    Returns
    -------
    list of snapshot dicts, ordered by ``collected_at``.
    """
    base_seed = seed if seed is not None else random.randint(0, 10_000)
    t = start_time or datetime.utcnow()
    snapshots: List[Dict[str, Any]] = []

    for i in range(n_normal):
        snapshots.append(
            simulate_snapshot(
                anomaly=False,
                timestamp=t + timedelta(minutes=interval_minutes * i),
                seed=base_seed + i,
            )
        )

    for j in range(n_anomaly):
        idx = n_normal + j
        snapshots.append(
            simulate_snapshot(
                anomaly=True,
                timestamp=t + timedelta(minutes=interval_minutes * idx),
                seed=base_seed + idx,
            )
        )

    return snapshots


def save_dataset(
    snapshots: List[Dict[str, Any]],
    out_dir: Optional[str] = None,
    prefix: str = "sim",
) -> List[str]:
    """
    Save each snapshot as a separate JSON file.

    Parameters
    ----------
    snapshots:
        List returned by ``generate_dataset()``.
    out_dir:
        Directory to write files into.  Defaults to
        ``ai-anomaly-detector/data/``.
    prefix:
        Filename prefix (e.g. ``"sim"`` → ``sim_000_normal.json``).

    Returns
    -------
    List of absolute file paths written.
    """
    if out_dir is None:
        out_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "data"
        )
    os.makedirs(out_dir, exist_ok=True)

    paths: List[str] = []
    for i, snap in enumerate(snapshots):
        label = "anomaly" if snap["_meta"]["anomaly"] else "normal"
        filename = f"{prefix}_{i:03d}_{label}.json"
        path = os.path.join(out_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=2)
        paths.append(path)

    return paths


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate synthetic Train Ticket service-mesh snapshots."
    )
    parser.add_argument(
        "--normal",
        type=int,
        default=20,
        metavar="N",
        help="Number of normal snapshots to generate (default: 20).",
    )
    parser.add_argument(
        "--anomaly",
        type=int,
        default=10,
        metavar="N",
        help="Number of anomalous snapshots to generate (default: 10).",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=5,
        metavar="MINUTES",
        help="Minutes between consecutive snapshots (default: 5).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducibility (default: 42).",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        metavar="DIR",
        help="Output directory (default: ai-anomaly-detector/data/).",
    )
    parser.add_argument(
        "--prefix",
        default="sim",
        help="Filename prefix (default: sim).",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        dest="print_only",
        help="Print the first snapshot to stdout instead of writing files.",
    )
    args = parser.parse_args()

    dataset = generate_dataset(
        n_normal=args.normal,
        n_anomaly=args.anomaly,
        interval_minutes=args.interval,
        seed=args.seed,
    )

    if args.print_only:
        print(json.dumps(dataset[0], indent=2))
    else:
        saved = save_dataset(dataset, out_dir=args.out_dir, prefix=args.prefix)
        total = len(saved)
        n_norm = sum(1 for s in dataset if not s["_meta"]["anomaly"])
        n_anom = total - n_norm
        print(f"Generated {total} snapshot(s): {n_norm} normal, {n_anom} anomalous.")
        print(f"Saved to: {os.path.dirname(saved[0])}")
