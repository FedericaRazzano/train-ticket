"""
test_collector.py

Quick smoke-test for data_collector.py.

Calls collect_snapshot(), pretty-prints a human-readable summary, and saves
the full result to data/snapshot_test.json.

Usage:
    python test_collector.py
    python test_collector.py --jaeger http://localhost:16686 --prometheus http://localhost:30090
"""

import argparse
import json
import sys
import os

# Allow running from any working directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data_collector import collect_snapshot, save_snapshot, JAEGER_DEFAULT, PROMETHEUS_DEFAULT


def print_summary(snapshot: dict) -> None:
    print()
    print(f"  collected_at : {snapshot['collected_at']}")
    print(f"  nodes        : {len(snapshot['nodes'])}")
    print(f"  edges        : {len(snapshot['edges'])}")

    if snapshot["nodes"]:
        print()
        print("  NODES")
        print(f"  {'service':<35} {'calls':>6}  {'p99 (ms)':>10}  {'error%':>8}")
        print("  " + "-" * 65)
        for node in snapshot["nodes"]:
            p99  = f"{node['p99_latency_ms']:.1f}" if node["p99_latency_ms"] is not None else "n/a"
            err  = f"{node['error_rate']*100:.2f}" if node["error_rate"] is not None else "n/a"
            print(f"  {node['service']:<35} {node['call_count']:>6}  {p99:>10}  {err:>8}")

    if snapshot["edges"]:
        print()
        print("  EDGES")
        print(f"  {'source':<30} {'target':<30} {'calls':>6}  {'avg lat (ms)':>12}")
        print("  " + "-" * 85)
        for edge in snapshot["edges"]:
            print(
                f"  {edge['source']:<30} {edge['target']:<30}"
                f" {edge['call_count']:>6}  {edge['avg_latency_ms']:>12.3f}"
            )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test for data_collector")
    parser.add_argument("--jaeger",      default=JAEGER_DEFAULT,      help="Jaeger base URL")
    parser.add_argument("--prometheus",  default=PROMETHEUS_DEFAULT,  help="Prometheus base URL")
    args = parser.parse_args()

    print(f"Jaeger     : {args.jaeger}")
    print(f"Prometheus : {args.prometheus}")
    print()

    snapshot = collect_snapshot(jaeger_url=args.jaeger, prometheus_url=args.prometheus)
    print_summary(snapshot)

    path = save_snapshot(snapshot, "snapshot_test.json")
    print(f"Full JSON saved to: {path}")


if __name__ == "__main__":
    main()
