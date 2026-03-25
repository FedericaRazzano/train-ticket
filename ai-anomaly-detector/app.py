"""
app.py -- Flask REST API for the AI anomaly detector microservice.

Endpoints
---------
GET  /health                          health check
GET  /metrics                         Prometheus metrics
POST /api/v1/anomalydetector/analyze  analyze a snapshot with the trained model
POST /api/v1/anomalydetector/train    retrain the model with new snapshots
POST /api/v1/anomalydetector/scan     collect a live snapshot and analyze it
"""

import logging
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from anomaly_detector import AnomalyDetector
from data_collector import collect_snapshot
from flask import Flask, jsonify, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from simulate_traces import generate_dataset, simulate_snapshot

_scans_total     = Counter('ai_anomaly_scans_total', 'Total scans run')
_anomalies_gauge = Gauge('ai_anomaly_anomalies_found', 'Anomalies found in last scan')
_service_score   = Gauge('ai_anomaly_service_score', 'Per-service anomaly score', ['service'])

JAEGER_URL        = os.environ.get("JAEGER_URL",        "http://jaeger-query:16686")
PROMETHEUS_URL    = os.environ.get("PROMETHEUS_URL",    "http://prometheus:9090")
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

_detector = AnomalyDetector()
_model_trained = False


def _auto_train() -> None:
    """Bootstrap the model at startup using 20 simulated normal snapshots."""
    global _model_trained
    snapshots = generate_dataset(n_normal=20, n_anomaly=0, seed=42)
    _detector.fit(snapshots)
    _model_trained = True
    logger.info("Auto-trained Isolation Forest on 20 simulated normal snapshots.")


_auto_train()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "model_trained": _model_trained})


@app.route("/metrics", methods=["GET"])
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


@app.route("/api/v1/anomalydetector/analyze", methods=["POST"])
def analyze():
    body = request.get_json(force=True, silent=True)
    if not body or "snapshot" not in body:
        return jsonify({"status": "error", "message": "Missing 'snapshot' field"}), 400

    snapshot = body["snapshot"]

    if _model_trained:
        results = _detector.detect(snapshot)
        model_used = "isolation_forest"
    else:
        results = AnomalyDetector.detect_simple(snapshot)
        model_used = "simple_threshold"

    return jsonify({"status": "ok", "model": model_used, "results": results})


@app.route("/api/v1/anomalydetector/train", methods=["POST"])
def train():
    global _model_trained
    body = request.get_json(force=True, silent=True)
    if not body or "snapshots" not in body:
        return jsonify({"status": "error", "message": "Missing 'snapshots' field"}), 400

    snapshots = body["snapshots"]
    if not isinstance(snapshots, list) or len(snapshots) == 0:
        return jsonify({"status": "error", "message": "'snapshots' must be a non-empty list"}), 400

    try:
        _detector.fit(snapshots)
        _model_trained = True
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500

    return jsonify({"status": "ok", "message": f"Model trained on {len(snapshots)} snapshot(s)."})


@app.route("/api/v1/anomalydetector/scan", methods=["POST"])
def scan():
    snapshot = collect_snapshot(jaeger_url=JAEGER_URL, prometheus_url=PROMETHEUS_URL)

    source = "live"
    if not snapshot.get("nodes") and not snapshot.get("edges"):
        logger.warning("No data from Jaeger/Prometheus — falling back to simulated snapshot.")
        snapshot = simulate_snapshot(anomaly=random.random() < 0.3, seed=random.randint(0, 9999))
        source = "simulated"

    if _model_trained:
        results = _detector.detect(snapshot)
        model_used = "isolation_forest"
    else:
        results = AnomalyDetector.detect_simple(snapshot)
        model_used = "simple_threshold"

    anomalies = [r for r in results if r["is_anomaly"]]
    logger.info("Scan complete: source=%s anomalies=%d/%d", source, len(anomalies), len(results))

    _scans_total.inc()
    _anomalies_gauge.set(len(anomalies))
    for r in results:
        _service_score.labels(service=r["service"]).set(r["anomaly_score"])

    if anomalies and ALERT_WEBHOOK_URL:
        payload = {
            "source": "ts-ai-anomaly-detector",
            "collected_at": snapshot.get("collected_at"),
            "anomalies_found": len(anomalies),
            "anomalies": anomalies,
        }
        try:
            import requests as _requests
            _requests.post(ALERT_WEBHOOK_URL, json=payload, timeout=5)
            logger.info("Alert sent to webhook: %d anomaly/anomalies", len(anomalies))
        except Exception as exc:
            logger.warning("Alert webhook failed: %s", exc)

    return jsonify({
        "status": "ok",
        "collected_at": snapshot.get("collected_at"),
        "source": source,
        "model": model_used,
        "anomalies_found": len(anomalies),
        "results": results,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
