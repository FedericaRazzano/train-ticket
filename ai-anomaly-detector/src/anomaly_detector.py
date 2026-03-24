"""
anomaly_detector.py

Detects per-service anomalies in Train Ticket service-mesh snapshots.

The detector works at the *service* level: for every snapshot it produces one
result per service with an anomaly score and a binary label.

Features used per service
--------------------------
- avg_latency_in_ms  : mean latency of all inbound calls (edges where target == service)
- calls_in           : total number of inbound calls
- calls_out          : total number of outbound calls
- error_rate         : fraction of 5xx responses (from Prometheus; 0 if unavailable)

Two detection modes
--------------------
fit() / detect()      : Isolation Forest (scikit-learn).  Requires training on a
                        set of normal snapshots before scoring new ones.
detect_simple()       : Fixed thresholds.  No training required; useful for
                        quick sanity checks when no training data is available.

Usage
-----
    from anomaly_detector import AnomalyDetector
    from data_collector   import collect_snapshot          # or simulate_traces

    detector = AnomalyDetector()
    detector.fit(normal_snapshots)                         # list of snapshot dicts
    results  = detector.detect(new_snapshot)               # list of per-service dicts

    # --- without training ---
    results = AnomalyDetector().detect_simple(new_snapshot)
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

try:
    import numpy as np
    from sklearn.ensemble import IsolationForest
    from sklearn.preprocessing import StandardScaler
    _SKLEARN_AVAILABLE = True
except ImportError:
    _SKLEARN_AVAILABLE = False


# ---------------------------------------------------------------------------
# Fixed thresholds used by detect_simple()
# ---------------------------------------------------------------------------

THRESHOLD_LATENCY_MS = 500.0   # flag if avg inbound latency exceeds 500 ms
THRESHOLD_ERROR_RATE = 0.05    # flag if error rate exceeds 5 %


# ---------------------------------------------------------------------------
# Feature extraction helpers
# ---------------------------------------------------------------------------

def _extract_service_features(
    snapshot: Dict[str, Any],
) -> Dict[str, Dict[str, float]]:
    """
    Compute a feature vector for every service present in ``snapshot``.

    Returns
    -------
    dict mapping service name → feature dict:
        {
            "avg_latency_in_ms": float,   # mean latency of inbound edges
            "calls_in":          float,   # total inbound call count
            "calls_out":         float,   # total outbound call count
            "error_rate":        float,   # Prometheus error rate (0 if missing)
        }
    """
    # Collect inbound-edge data per service
    latency_in: Dict[str, List[float]] = {}
    calls_in:   Dict[str, float]       = {}
    calls_out:  Dict[str, float]       = {}

    for edge in snapshot.get("edges", []):
        src = edge["source"]
        dst = edge["target"]
        cnt = float(edge.get("call_count", 0))
        lat = float(edge.get("avg_latency_ms", 0.0))

        # Accumulate inbound latency samples for the target service
        if dst not in latency_in:
            latency_in[dst] = []
        # Approximate individual samples from the average and call count
        latency_in[dst].append(lat)
        calls_in[dst]  = calls_in.get(dst,  0.0) + cnt
        calls_out[src] = calls_out.get(src, 0.0) + cnt

    # Collect error rates and ensure every node-listed service is present
    error_rates: Dict[str, float] = {}
    for node in snapshot.get("nodes", []):
        svc = node["service"]
        er  = node.get("error_rate")
        error_rates[svc] = float(er) if er is not None else 0.0

    # Union of all services
    all_services = (
        set(latency_in.keys())
        | set(calls_out.keys())
        | set(error_rates.keys())
    )

    features: Dict[str, Dict[str, float]] = {}
    for svc in all_services:
        lats = latency_in.get(svc, [])
        avg_lat = sum(lats) / len(lats) if lats else 0.0
        features[svc] = {
            "avg_latency_in_ms": avg_lat,
            "calls_in":          calls_in.get(svc,  0.0),
            "calls_out":         calls_out.get(svc, 0.0),
            "error_rate":        error_rates.get(svc, 0.0),
        }

    return features


# Fixed column order — must be consistent between fit() and detect()
_FEATURE_COLUMNS: List[str] = [
    "avg_latency_in_ms",
    "calls_in",
    "calls_out",
    "error_rate",
]


def _features_to_row(feat: Dict[str, float]) -> List[float]:
    """Convert a feature dict to a fixed-order float list."""
    return [feat.get(col, 0.0) for col in _FEATURE_COLUMNS]


# ---------------------------------------------------------------------------
# Normalisation helper for anomaly scores
# ---------------------------------------------------------------------------

def _decision_to_score(raw_decision: float) -> float:
    """
    Map an IsolationForest decision_function value to a [0, 1] anomaly score.

    decision_function returns values roughly in [-0.5, 0.5]:
        - positive  → inlier  (normal)
        - negative  → outlier (anomalous)

    We invert and clip so that score = 1 means maximally anomalous and
    score = 0 means normal.
    """
    # Shift and scale: 0.5 → 0.0 (normal), -0.5 → 1.0 (anomalous)
    score = 0.5 - raw_decision
    # Clip to [0, 1] in case the model produces values outside [-0.5, 0.5]
    return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------
# AnomalyDetector
# ---------------------------------------------------------------------------

class AnomalyDetector:
    """
    Per-service anomaly detector for Train Ticket service-mesh snapshots.

    Attributes
    ----------
    contamination : float
        Expected fraction of anomalies in the training set.  Keep this small
        (0.05 – 0.10) when training exclusively on normal snapshots.
    score_threshold : float
        Anomaly score (0–1) above which a service is labelled anomalous.
        Applies to detect().  Automatically derived from contamination if
        not provided explicitly.
    random_state : int
        Random seed for reproducibility.
    """

    def __init__(
        self,
        contamination: float = 0.05,
        score_threshold: Optional[float] = None,
        random_state: int = 42,
    ) -> None:
        if not _SKLEARN_AVAILABLE:
            raise ImportError(
                "scikit-learn is required for AnomalyDetector.fit() / detect(). "
                "Run: pip install scikit-learn\n"
                "Alternatively, use detect_simple() which has no dependencies."
            )
        self.contamination  = contamination
        # Default threshold: map the IF decision boundary (0.0) to score space
        self.score_threshold = score_threshold if score_threshold is not None else 0.5
        self.random_state   = random_state

        self._model:   Optional[IsolationForest] = None
        self._scaler:  Optional[StandardScaler]  = None
        self._is_fitted: bool = False

    # ------------------------------------------------------------------
    # fit
    # ------------------------------------------------------------------

    def fit(self, snapshots: List[Dict[str, Any]]) -> "AnomalyDetector":
        """
        Train the Isolation Forest on a list of *normal* snapshots.

        Each (snapshot, service) pair becomes one training row.  Using all
        services across all snapshots gives enough rows even when only 20
        snapshots are available.

        Parameters
        ----------
        snapshots : list of snapshot dicts (output of collect_snapshot() or
                    simulate_traces.simulate_snapshot())

        Returns
        -------
        self  (for chaining)
        """
        rows: List[List[float]] = []

        for snap in snapshots:
            per_service = _extract_service_features(snap)
            for feat in per_service.values():
                rows.append(_features_to_row(feat))

        if not rows:
            raise ValueError("No feature rows extracted — snapshots may be empty.")

        X = np.array(rows, dtype=float)

        # Standardise features so that IsolationForest treats each dimension
        # equally regardless of magnitude (latency is in ms, rate is 0–1, etc.)
        self._scaler = StandardScaler()
        X_scaled = self._scaler.fit_transform(X)

        self._model = IsolationForest(
            contamination=self.contamination,
            n_estimators=100,
            random_state=self.random_state,
        )
        self._model.fit(X_scaled)
        self._is_fitted = True

        print(f"  [AnomalyDetector] Fitted on {len(rows)} samples "
              f"from {len(snapshots)} snapshot(s).")
        return self

    # ------------------------------------------------------------------
    # detect  (requires fit)
    # ------------------------------------------------------------------

    def detect(
        self,
        snapshot: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        Score every service in ``snapshot`` using the trained Isolation Forest.

        Parameters
        ----------
        snapshot : dict — one snapshot from collect_snapshot() or simulate_snapshot()

        Returns
        -------
        List of per-service result dicts, sorted by anomaly_score descending:
            {
                "service":       str,
                "anomaly_score": float,   # 0 (normal) → 1 (very anomalous)
                "is_anomaly":    bool,
                "features":      dict,    # raw feature values used for scoring
            }

        Raises
        ------
        RuntimeError if fit() has not been called yet.
        """
        if not self._is_fitted:
            raise RuntimeError("Call fit() before detect().")

        per_service = _extract_service_features(snapshot)
        if not per_service:
            return []

        service_names = list(per_service.keys())
        rows  = [_features_to_row(per_service[svc]) for svc in service_names]
        X     = np.array(rows, dtype=float)
        X_sc  = self._scaler.transform(X)

        raw_scores = self._model.decision_function(X_sc)  # higher = more normal

        results = []
        for svc, raw, feat_row in zip(service_names, raw_scores, rows):
            score = _decision_to_score(float(raw))
            results.append({
                "service":       svc,
                "anomaly_score": round(score, 4),
                "is_anomaly":    score >= self.score_threshold,
                "features": {
                    col: round(feat_row[i], 4)
                    for i, col in enumerate(_FEATURE_COLUMNS)
                },
            })

        # Sort: most anomalous first
        results.sort(key=lambda r: r["anomaly_score"], reverse=True)
        return results

    # ------------------------------------------------------------------
    # detect_simple  (no training required)
    # ------------------------------------------------------------------

    @staticmethod
    def detect_simple(
        snapshot: Dict[str, Any],
        latency_threshold_ms: float = THRESHOLD_LATENCY_MS,
        error_rate_threshold: float = THRESHOLD_ERROR_RATE,
    ) -> List[Dict[str, Any]]:
        """
        Rule-based anomaly detection using fixed thresholds.

        A service is flagged as anomalous when *either* of these conditions
        holds:
            - avg inbound latency  > latency_threshold_ms  (default 500 ms)
            - error rate           > error_rate_threshold  (default 5 %)

        The anomaly_score is a simple 0/1 value (1 = anomalous).

        Parameters
        ----------
        snapshot             : snapshot dict
        latency_threshold_ms : latency threshold in milliseconds
        error_rate_threshold : error rate threshold as a fraction (0–1)

        Returns
        -------
        Same structure as detect():
            [{ "service", "anomaly_score", "is_anomaly", "features" }, ...]
        sorted by anomaly_score descending, then by avg_latency_in_ms descending.
        """
        per_service = _extract_service_features(snapshot)
        results = []

        for svc, feat in per_service.items():
            lat_flag = feat["avg_latency_in_ms"] > latency_threshold_ms
            err_flag = feat["error_rate"]        > error_rate_threshold
            is_anom  = lat_flag or err_flag

            # Score: fraction of thresholds exceeded, as a rough 0–1 value
            lat_ratio = feat["avg_latency_in_ms"] / latency_threshold_ms
            err_ratio = feat["error_rate"]        / error_rate_threshold
            # Cap individual ratios and combine; normal operation gives < 0.5
            score = min(1.0, max(lat_ratio, err_ratio) / 2.0)

            results.append({
                "service":       svc,
                "anomaly_score": round(score, 4),
                "is_anomaly":    is_anom,
                "features": {
                    col: round(feat[col], 4)
                    for col in _FEATURE_COLUMNS
                },
            })

        results.sort(
            key=lambda r: (r["anomaly_score"], r["features"]["avg_latency_in_ms"]),
            reverse=True,
        )
        return results
