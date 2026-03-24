# AI Anomaly Detector — Demo Guide

Complete walkthrough from a cold system to anomaly results in the notebook.

Two variants are documented:

| | Lite Variant | Full Variant |
|---|---|---|
| **Kubernetes cluster** | not required | Docker Desktop + kind |
| **Recommended RAM** | 4 GB | 16 GB |
| **Setup time** | ~5 minutes | ~25 minutes |
| **Data source** | synthetic (`simulate_traces.py`) | real (Jaeger + Prometheus) |
| **Final result** | identical | identical |

> If you haven't touched this project in a while, start with the **Lite Variant**.
> It always works and does not depend on the cluster state.

---

## Common Prerequisites

### Python and dependencies

```bash
# Check version (requires 3.8+)
python --version
```

> **Windows note:** if `python` and `pip` are not recognised, use the full path:
> `C:\Users\<you>\AppData\Local\Programs\Python\Python3x\python.exe`
> Or add them to the PATH, or set a session alias in PowerShell:
> ```powershell
> Set-Alias python "C:\Users\<you>\AppData\Local\Programs\Python\Python3x\python.exe"
> function pip { python -m pip @args }
> ```

```bash
# Upgrade pip first (version 19.x ships with some Python 3.8 installers and
# is too old to resolve modern packages correctly)
python -m pip install --upgrade pip

# Install Python dependencies (once)
# jinja2 is required by pandas .style (used in the notebook results table)
python -m pip install requests scikit-learn matplotlib pandas networkx jinja2
```

### Opening the notebook

**Recommended on Windows: use VS Code.**
The standard `jupyter` package fails to install on Windows with Python 3.8 due
to a MAX\_PATH limit triggered by webpack bundle filenames inside `jupyterlab`.

1. Open VS Code
2. Open `ai-anomaly-detector/notebooks/demo_anomaly_detection.ipynb`
3. VS Code will prompt you to install the **Jupyter** extension — accept
4. Select the Python 3.x interpreter when asked for a kernel

If you prefer the classic browser interface, install only the lightweight
classic notebook (avoids the MAX\_PATH issue):

```bash
python -m pip install "notebook<7" ipykernel
python -m jupyter notebook ai-anomaly-detector/notebooks/demo_anomaly_detection.ipynb
```

### Working directory

All commands below assume the **repo root** as the working directory:

```
TRAIN-TICKET/
└── train-ticket/          ← working directory
    └── ai-anomaly-detector/
        ├── src/
        ├── notebooks/
        └── data/
```

```bash
cd train-ticket   # run everything from here
```

---

## Lite Variant — Simulated Data (no cluster required)

Best for resource-constrained machines or whenever a live cluster is not needed.
Everything runs locally with synthetic data that replicates the real snapshot format.

### Step 1 — Generate simulated data

```bash
python ai-anomaly-detector/src/simulate_traces.py --normal 20 --anomaly 10
```

Expected output:

```
Generated 30 snapshot(s): 20 normal, 10 anomalous.
Saved to: .../ai-anomaly-detector/data
```

Files `sim_000_normal.json` … `sim_019_normal.json` contain normal traffic.
Files `sim_020_anomaly.json` … `sim_029_anomaly.json` contain a 3 s delay
injected on `ts-order-service`.

### Step 2 — Run the detector from the command line (optional)

```bash
python ai-anomaly-detector/src/anomaly_detector.py \
  --data-dir ai-anomaly-detector/data \
  --train-size 20 \
  --report
```

Expected output (excerpt):

```
  Loaded 30 snapshot(s)
  Feature space: 36 features
  Training set : 20 snapshot(s)

  [AnomalyDetector] Fitted on ~240 samples from 20 snapshot(s).

  [normal]   2026-03-24T19:00:00  score=  0.3412
  ...
  [ANOMALY]  2026-03-24T20:40:00  score=  0.7891
       ↳ edge.ts-preserve-service→ts-order-service.avg_latency_ms: 3024.3  (z=18.4)
       ↳ edge.ts-payment-service→ts-order-service.avg_latency_ms: 3021.7  (z=17.9)
  ...
  Accuracy : 100.0%  (30/30 correct)
```

### Step 3 — Open the notebook

**In VS Code** (recommended): open `ai-anomaly-detector/notebooks/demo_anomaly_detection.ipynb` directly.

**In the browser** (if classic notebook is installed):

```bash
cd ai-anomaly-detector/notebooks
python -m jupyter notebook demo_anomaly_detection.ipynb
```

Run each cell in order with **Shift+Enter**.

The notebook:
1. Generates 20 normal snapshots + 1 anomalous snapshot internally (no external data needed)
2. Trains the Isolation Forest on the normal data
3. Draws the service dependency graph (green = normal, red = anomalous)
4. Shows a styled table with the top-3 most anomalous services

> Steps 1–2 do not need to be run beforehand — the notebook generates its own
> data independently.

---

## Full Variant — Real Cluster

Uses real data collected from Jaeger and Prometheus while Train Ticket runs on
Kubernetes.

> **Cluster state warning**: the last known deployment had several issues
> documented in the [Known Issues](#known-issues) section at the bottom of
> this guide. Read that section before starting.

### Step 1 — Start Docker Desktop

1. Open **Docker Desktop**
2. Go to *Settings → Kubernetes* and make sure **Enable Kubernetes** is checked
3. Wait until both status indicators in the bottom-left corner are green:
   **Docker running** and **Kubernetes running**

Verify from a terminal:

```bash
kubectl cluster-info
# expected: "Kubernetes control plane is running at https://127.0.0.1:..."
```

### Step 2 — Deploy Train Ticket

> Use **PowerShell** for this step — `helm` is not in the bash PATH on Windows.

```powershell
# From the repo root (train-ticket/)
helm dependency build manifests/helm/generic_service

helm install ts manifests/helm/generic_service `
  -n ts --create-namespace `
  -f manifests/helm/trainticket/values-local.yaml `
  --set global.image.tag=v1.2.7 `
  --set global.security.allowInsecureImages=true `
  --set global.monitoring=opentelemetry `
  --set opentelemetry.enabled=true `
  --set skywalking.enabled=false
```

> The `--set opentelemetry.enabled=true` flag overrides the `false` default in
> `values-local.yaml` and enables trace export to Jaeger.
> **Without this flag, Jaeger will be empty.**

Wait for all pods to be Running (10–15 minutes):

```bash
# Linux / macOS
watch kubectl get pods -n ts

# Windows (no watch available — run repeatedly)
kubectl get pods -n ts
```

Proceed when: **at least 45 pods are in Running state** and none has been in
`CrashLoopBackOff` for more than 5 minutes.

> If some pods remain in `CrashLoopBackOff`, see [Known Issues](#known-issues).

### Step 3 — Start Jaeger and Prometheus (standalone Docker containers)

```bash
# Jaeger  (UI at http://localhost:16686)
docker run -d --name jaeger \
  -p 16686:16686 \
  -p 14268:14268 \
  jaegertracing/all-in-one:latest

# Prometheus — needs a minimal config file
cat > /tmp/prometheus.yml << 'EOF'
global:
  scrape_interval: 15s
scrape_configs:
  - job_name: 'trainticket'
    static_configs:
      - targets: ['host.docker.internal:8080']
EOF

docker run -d --name prometheus \
  -p 30090:9090 \
  -v /tmp/prometheus.yml:/etc/prometheus/prometheus.yml \
  prom/prometheus:latest
```

Verify both are reachable:

```bash
python ai-anomaly-detector/src/check_connections.py
```

Expected output:

```
Jaeger     http://localhost:16686  ...  OK  (N services found)
Prometheus http://localhost:30090  ...  OK  (N metrics found)
```

### Step 4 — Port-forward the services

> The gateway has a bug in its `application.yml` (routes registered under
> `spring.application` instead of `spring.cloud`) and returns 404 for
> everything. Bypass it with direct port-forwards.

Open **4 separate terminals** and keep them open for the entire demo session:

```bash
# Terminal 1
kubectl port-forward svc/ts-auth-service -n ts 8081:8080

# Terminal 2
kubectl port-forward svc/ts-travel-service -n ts 8082:8080

# Terminal 3
kubectl port-forward svc/ts-contacts-service -n ts 8083:8080

# Terminal 4
kubectl port-forward svc/ts-preserve-service -n ts 8084:8080
```

### Step 5 — Generate real traffic

```bash
# 10 booking simulation cycles
python ai-anomaly-detector/src/generate_traffic.py --loops 10
```

Expected output per cycle:

```
[1/10] login          ... OK  (token: eyJ...)
[1/10] search trains  ... OK  (3 trips found)
[1/10] trip detail    ... OK
[1/10] get contacts   ... OK
[1/10] book ticket    ... OK / FAILED  (see Known Issues)
```

After generating traffic, open Jaeger at `http://localhost:16686` and confirm
that services appear in the *Service* drop-down.

### Step 6 — Collect baseline snapshots

```bash
python ai-anomaly-detector/src/test_collector.py
# saves to ai-anomaly-detector/data/snapshot_test.json
```

Collect at least 20 snapshots spaced a few minutes apart, ideally while
`generate_traffic.py` is running in another terminal:

```bash
for i in $(seq 1 20); do
  python ai-anomaly-detector/src/data_collector.py \
    --out "snapshot_normal_$(printf '%03d' $i).json"
  sleep 60
done
```

### Step 7 — Inject the anomaly

```bash
# Add a 3 s delay to ts-order-service
python ai-anomaly-detector/src/inject_anomaly.py inject

# Confirm the ChaosExperiment is active
python ai-anomaly-detector/src/inject_anomaly.py status
```

Collect 5–10 snapshots with the anomaly active:

```bash
for i in $(seq 1 10); do
  python ai-anomaly-detector/src/data_collector.py \
    --out "snapshot_anomaly_$(printf '%03d' $i).json"
  sleep 60
done
```

### Step 8 — Remove the anomaly

```bash
python ai-anomaly-detector/src/inject_anomaly.py remove
```

### Step 9 — Run the detector

```bash
python ai-anomaly-detector/src/anomaly_detector.py \
  --data-dir ai-anomaly-detector/data \
  --train-size 20 \
  --report
```

### Step 10 — Open the notebook

Before launching the notebook, edit the **Baseline** cell to load the real
files instead of generating synthetic data:

```python
# Replace the generate_dataset(...) call with:
import glob, json
paths = sorted(glob.glob('../data/snapshot_normal_*.json'))
normal_snapshots = [json.load(open(p)) for p in paths]
baseline = normal_snapshots[-1]
```

```bash
cd ai-anomaly-detector/notebooks
jupyter notebook demo_anomaly_detection.ipynb
```

### Step 11 — Shut everything down

```bash
# Remove the ts namespace (stops all pods)
kubectl delete namespace ts

# Stop and remove the standalone Docker containers
docker stop jaeger prometheus
docker rm jaeger prometheus
```

---

## Generated File Structure

```
ai-anomaly-detector/
├── data/
│   ├── sim_000_normal.json       ← simulated snapshots (Lite Variant)
│   ├── sim_020_anomaly.json
│   ├── snapshot_normal_001.json  ← real snapshots (Full Variant)
│   ├── snapshot_anomaly_001.json
│   └── snapshot_test.json        ← quick smoke-test snapshot
├── notebooks/
│   └── demo_anomaly_detection.ipynb
└── src/
    ├── simulate_traces.py        ← generates synthetic data
    ├── anomaly_detector.py       ← Isolation Forest + fixed-threshold detector
    ├── data_collector.py         ← collects snapshots from Jaeger + Prometheus
    ├── test_collector.py         ← smoke-test for the collector
    ├── generate_traffic.py       ← simulates a user booking tickets
    ├── inject_anomaly.py         ← injects a delay via Chaos Mesh
    └── check_connections.py      ← verifies connectivity to Jaeger + Prometheus
```

---

## Known Issues (Full Variant)

| Issue | Symptom | Fix |
|---|---|---|
| **Gateway 404** | All requests to the gateway return 404 | Use direct port-forwards (Step 4) |
| **Travel service 403** | `generate_traffic.py` prints `403 Forbidden` on search trains | The booking step still partially works; traces are generated even with a 403 |
| **Jaeger empty** | No services appear in the Jaeger UI | Ensure `--set opentelemetry.enabled=true` was passed at deploy time |
| **MySQL CrashLoopBackOff** | `mysql-0` pod fails to start | See fix below |
| **Port-forward drops** | `Connection refused` after a few minutes | Reopen the terminal and re-run the port-forward command |
| **Kubernetes API timeouts** | `kubectl` is slow or times out | Normal under load; wait and retry |

### MySQL CrashLoopBackOff

If the MySQL pod fails to start with an authentication error:

```bash
# Patch the ConfigMap to use the legacy auth plugin compatible with the JDBC driver
kubectl patch configmap mysql-config -n ts --type merge -p \
  '{"data":{"mysql.conf":"[mysqld]\ndefault-authentication-plugin=mysql_native_password\n"}}'

# Force pod recreation
kubectl delete pod mysql-0 -n ts
```

### Cluster unresponsive after Docker Desktop restart

If Docker Desktop is closed and reopened, the cluster may be in an inconsistent
state:

```bash
# Check node status
kubectl get nodes

# If the node is not Ready, switch context
kubectl config use-context docker-desktop
```

If the problem persists, use Docker Desktop: *Troubleshoot → Reset Kubernetes cluster*.
The reset deletes all deployments — you will need to repeat Step 2.

---

## Quick Command Reference

```bash
# ── Lite Variant (3 commands) ─────────────────────────────────────────────
python ai-anomaly-detector/src/simulate_traces.py --normal 20 --anomaly 10
python ai-anomaly-detector/src/anomaly_detector.py --data-dir ai-anomaly-detector/data --train-size 20 --report
# open the notebook in VS Code, or:
python -m jupyter notebook ai-anomaly-detector/notebooks/demo_anomaly_detection.ipynb

# ── Full Variant ──────────────────────────────────────────────────────────
python ai-anomaly-detector/src/check_connections.py             # 1. verify connectivity
python ai-anomaly-detector/src/generate_traffic.py --loops 10  # 2. generate traffic
python ai-anomaly-detector/src/test_collector.py                # 3. collect baseline snapshot
python ai-anomaly-detector/src/inject_anomaly.py inject         # 4. inject anomaly
python ai-anomaly-detector/src/test_collector.py                # 5. collect anomalous snapshot
python ai-anomaly-detector/src/inject_anomaly.py remove         # 6. remove anomaly
python ai-anomaly-detector/src/anomaly_detector.py --data-dir ai-anomaly-detector/data --train-size 20 --report
jupyter notebook ai-anomaly-detector/notebooks/demo_anomaly_detection.ipynb
```
