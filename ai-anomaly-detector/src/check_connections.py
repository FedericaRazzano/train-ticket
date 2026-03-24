"""
check_connections.py

Checks connectivity to Jaeger and Prometheus.
Prints the services found in Jaeger and HTTP-related metrics from Prometheus.

Default URLs (from manifests/monitoring/):
  Jaeger:     http://localhost:16686   (standard Jaeger UI/API port)
              Access: kubectl port-forward -n monitoring svc/jaeger-query 16686:16686
  Prometheus: http://localhost:30090   (NodePort configured in prometheus-stack.yaml)
              Alternative: kubectl port-forward -n monitoring svc/prometheus-stack-kube-prom-prometheus 9090:9090

Usage:
  python check_connections.py
  python check_connections.py --jaeger http://localhost:16686 --prometheus http://localhost:30090
"""

import argparse
import sys

try:
    import requests
except ImportError:
    print("ERROR: the 'requests' package is not installed.")
    print("  Run: pip install requests")
    sys.exit(1)

JAEGER_DEFAULT = "http://localhost:16686"
PROMETHEUS_DEFAULT = "http://localhost:30090"


def check_jaeger(base_url: str) -> None:
    url = f"{base_url}/api/services"
    print(f"\n--- Jaeger ({url}) ---")
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        services = data.get("data", [])
        if services:
            print(f"Found {len(services)} service(s):")
            for svc in sorted(services):
                print(f"  - {svc}")
        else:
            print("No services found (the system may not have generated any traces yet).")
    except requests.exceptions.ConnectionError:
        print(f"ERROR: could not connect to Jaeger at {base_url}")
        print("  Make sure the port-forward is running:")
        print("  kubectl port-forward -n monitoring svc/jaeger-query 16686:16686")
    except requests.exceptions.Timeout:
        print(f"ERROR: connection to Jaeger at {base_url} timed out")
    except requests.exceptions.HTTPError as e:
        print(f"HTTP ERROR from Jaeger: {e}")
    except Exception as e:
        print(f"Unexpected error with Jaeger: {e}")


def check_prometheus(base_url: str) -> None:
    url = f"{base_url}/api/v1/label/__name__/values"
    print(f"\n--- Prometheus ({url}) ---")
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        all_metrics = data.get("data", [])
        http_metrics = [m for m in all_metrics if "http" in m.lower()]
        if http_metrics:
            print(f"Found {len(http_metrics)} metric(s) containing 'http' (out of {len(all_metrics)} total):")
            for metric in sorted(http_metrics):
                print(f"  - {metric}")
        else:
            print(f"No metrics containing 'http' found (total metrics: {len(all_metrics)}).")
    except requests.exceptions.ConnectionError:
        print(f"ERROR: could not connect to Prometheus at {base_url}")
        print("  Check that NodePort 30090 is reachable, or start a port-forward:")
        print("  kubectl port-forward -n monitoring svc/prometheus-stack-kube-prom-prometheus 9090:9090")
        print("  then use --prometheus http://localhost:9090")
    except requests.exceptions.Timeout:
        print(f"ERROR: connection to Prometheus at {base_url} timed out")
    except requests.exceptions.HTTPError as e:
        print(f"HTTP ERROR from Prometheus: {e}")
    except Exception as e:
        print(f"Unexpected error with Prometheus: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Check connectivity to Jaeger and Prometheus")
    parser.add_argument("--jaeger", default=JAEGER_DEFAULT, help=f"Jaeger base URL (default: {JAEGER_DEFAULT})")
    parser.add_argument("--prometheus", default=PROMETHEUS_DEFAULT, help=f"Prometheus base URL (default: {PROMETHEUS_DEFAULT})")
    args = parser.parse_args()

    print("=== Observability connectivity check ===")
    check_jaeger(args.jaeger)
    check_prometheus(args.prometheus)
    print("\nDone.")


if __name__ == "__main__":
    main()
