"""
inject_anomaly.py

Injects / removes a 3-second network delay on ts-order-service using Chaos Mesh.

Chaos Mesh (chaos-mesh.org) is the chaos engineering tool already configured
in this repo (see manifests/monitoring/install_chaos.sh and chaos_rbac.yaml).
It works by applying a Kubernetes custom resource (NetworkChaos) that instructs
the Chaos Daemon (a DaemonSet running on every node) to add tc/netem rules to
the target pod's network interface — no code changes needed.

Target:  pods with label  app=ts-order-service  in namespace  ts
Effect:  3 000 ms extra latency on every packet leaving ts-order-service
         (i.e. callers wait 3 s longer for every response)

Prerequisites:
  - kubectl configured and pointing at the right cluster
  - Chaos Mesh installed  (helm install chaos-mesh chaos-mesh/chaos-mesh ...)
  - Train Ticket deployed in namespace  ts

Usage:
  python inject_anomaly.py inject   # add the delay
  python inject_anomaly.py remove   # remove the delay
  python inject_anomaly.py status   # show whether the chaos resource exists
"""

import argparse        # parses command-line arguments (inject / remove / status)
import subprocess      # runs kubectl as a child process
import sys             # used to exit with a non-zero code on error
import textwrap        # dedents the YAML so indentation in this file stays clean

# ---------------------------------------------------------------------------
# The Chaos Mesh manifest we will apply / delete.
# NetworkChaos is a Kubernetes CRD provided by Chaos Mesh.
# ---------------------------------------------------------------------------

# Name of the NetworkChaos resource — used both when creating and deleting it.
CHAOS_NAME = "ts-order-delay"

# Kubernetes namespace where Train Ticket runs (set by  make deploy NS=ts).
TARGET_NAMESPACE = "ts"

# The YAML that describes the fault to inject.
# We keep it as a Python string so the script is self-contained (no extra files).
CHAOS_MANIFEST = textwrap.dedent(f"""
    apiVersion: chaos-mesh.org/v1alpha1   # CRD API group provided by Chaos Mesh
    kind: NetworkChaos                    # resource type: network-level fault
    metadata:
      name: {CHAOS_NAME}                  # how we'll refer to this resource later
      namespace: {TARGET_NAMESPACE}       # same namespace as the target pods
    spec:
      action: delay                       # inject latency (other options: loss, duplicate, corrupt)
      mode: all                           # apply to ALL matching pods (not just one)
      selector:
        namespaces:
          - {TARGET_NAMESPACE}            # only look at pods in the  ts  namespace
        labelSelectors:
          app: ts-order-service           # pods deployed by Helm have  app: <svc.name>
                                          # (see manifests/helm/trainticket/templates/deployment.yaml line 8)
      delay:
        latency: "3000ms"                 # 3-second extra delay on every packet
        jitter: "0ms"                     # no random variation around the 3 s value
        correlation: "0"                  # each packet's delay is independent
      direction: from                     # delay packets going FROM ts-order-service
                                          # → callers receive responses 3 s late
""")

# ---------------------------------------------------------------------------
# Helper: run a kubectl command and return (stdout, stderr, returncode).
# ---------------------------------------------------------------------------

def _kubectl(*args: str) -> tuple[str, str, int]:
    """Run  kubectl <args>  and return (stdout, stderr, returncode).

    We use subprocess.run with capture_output=True so the output does not
    appear on screen until we decide what to do with it.
    """
    cmd = ["kubectl", *args]          # build the full command list
    print(f"  Running: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        capture_output=True,          # capture both stdout and stderr
        text=True,                    # decode bytes → str automatically
    )
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def _check_prerequisites() -> bool:
    """Verify kubectl is reachable and Chaos Mesh CRDs are installed."""

    # --- check kubectl itself ---
    _, _, rc = _kubectl("version", "--client", "--short")
    if rc != 0:
        print("ERROR: kubectl not found or not working.")
        print("  Install kubectl and make sure it is on your PATH.")
        return False

    # --- check cluster is reachable ---
    _, err, rc = _kubectl("cluster-info", "--request-timeout=5s")
    if rc != 0:
        print("ERROR: cannot reach the Kubernetes cluster.")
        print(f"  {err}")
        print("  Check your kubeconfig:  kubectl config current-context")
        return False

    # --- check Chaos Mesh CRD is installed ---
    # If NetworkChaos CRD does not exist, 'kubectl get networkchaos' returns non-zero.
    _, err, rc = _kubectl(
        "get", "crd", "networkchaos.chaos-mesh.org",
        "--ignore-not-found",
    )
    if rc != 0:
        print("ERROR: Chaos Mesh is not installed (NetworkChaos CRD not found).")
        print("  Install it with:  bash manifests/monitoring/install_chaos.sh")
        return False

    # --- check the target namespace exists ---
    _, _, rc = _kubectl("get", "namespace", TARGET_NAMESPACE, "--ignore-not-found")
    if rc != 0:
        print(f"ERROR: namespace '{TARGET_NAMESPACE}' not found.")
        print(f"  Deploy Train Ticket first:  make deploy NS={TARGET_NAMESPACE}")
        return False

    return True  # all checks passed


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_inject() -> None:
    """Apply the NetworkChaos manifest to start injecting the delay."""

    print(f"\n[inject] Adding 3 s delay to ts-order-service responses...")

    if not _check_prerequisites():
        sys.exit(1)

    # Check whether the resource already exists to avoid double-injection.
    out, _, rc = _kubectl(
        "get", "networkchaos", CHAOS_NAME,
        "-n", TARGET_NAMESPACE,
        "--ignore-not-found",
    )
    if out:
        # 'out' is non-empty → resource already exists, nothing to do.
        print(f"  WARN: '{CHAOS_NAME}' already exists — delay is already active.")
        print(f"  Run  python inject_anomaly.py status  to inspect it.")
        return

    # Pipe the YAML string into  kubectl apply -f -
    # The  -f -  flag tells kubectl to read the manifest from stdin.
    print("  Applying NetworkChaos manifest...")
    result = subprocess.run(
        ["kubectl", "apply", "-f", "-"],   # read manifest from stdin
        input=CHAOS_MANIFEST,              # pass the YAML string as stdin
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        print(f"  OK: {result.stdout.strip()}")
        print(f"\n  Delay is now ACTIVE on ts-order-service.")
        print(f"  Every response from ts-order-service will arrive ~3 s late.")
        print(f"\n  To verify with Jaeger: look for spans on ts-order-service > 3 000 ms")
        print(f"  To remove the delay:   python inject_anomaly.py remove")
    else:
        print(f"  ERROR applying manifest:\n{result.stderr.strip()}")
        sys.exit(1)


def cmd_remove() -> None:
    """Delete the NetworkChaos resource to stop the delay immediately."""

    print(f"\n[remove] Removing delay from ts-order-service...")

    if not _check_prerequisites():
        sys.exit(1)

    # Delete the named NetworkChaos resource.
    # --ignore-not-found prevents an error if the resource was already removed.
    out, err, rc = _kubectl(
        "delete", "networkchaos", CHAOS_NAME,
        "-n", TARGET_NAMESPACE,
        "--ignore-not-found",
    )

    if rc == 0:
        if "deleted" in out:
            print(f"  OK: {out}")
            print(f"\n  Delay REMOVED. ts-order-service is back to normal.")
        else:
            # --ignore-not-found returns rc=0 even when nothing was deleted.
            print(f"  WARN: '{CHAOS_NAME}' was not found — nothing to remove.")
    else:
        print(f"  ERROR:\n{err}")
        sys.exit(1)


def cmd_status() -> None:
    """Show whether the delay is currently active."""

    print(f"\n[status] Checking chaos injection on ts-order-service...")

    # We intentionally skip the full prerequisite check here so  status  works
    # even when Chaos Mesh is not installed (it will just say 'not active').
    out, err, rc = _kubectl(
        "get", "networkchaos", CHAOS_NAME,
        "-n", TARGET_NAMESPACE,
        "-o", "wide",
        "--ignore-not-found",
    )

    if rc != 0:
        print(f"  Could not query cluster: {err}")
        return

    if out:
        # Resource exists → delay is active. Print the kubectl table output.
        print(f"\n  ACTIVE — delay is currently injected:\n")
        for line in out.splitlines():
            print(f"    {line}")
        print(f"\n  Remove it with:  python inject_anomaly.py remove")
    else:
        print(f"  NOT ACTIVE — no delay injected (resource '{CHAOS_NAME}' not found).")
        print(f"  Inject it with:  python inject_anomaly.py inject")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inject / remove a 3 s delay on ts-order-service via Chaos Mesh"
    )
    # Positional argument: one of inject / remove / status
    parser.add_argument(
        "command",
        choices=["inject", "remove", "status"],
        help="inject=add delay  remove=restore normal  status=check current state",
    )
    args = parser.parse_args()

    # Dispatch to the right function based on the command argument.
    if args.command == "inject":
        cmd_inject()
    elif args.command == "remove":
        cmd_remove()
    elif args.command == "status":
        cmd_status()


if __name__ == "__main__":
    main()
