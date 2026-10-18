import os
import json
import numpy as np
from typing import Dict, List, Tuple, Optional, Any
import joblib

from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import Ridge
from sklearn.model_selection import GroupKFold


class GazeCalibration:
    """
    9-Point Grid Calibration with Ridge Regression and Group-Aware LOPO-CV.
    Evaluates out-of-sample error split across Center, Edge, and Corner targets.
    """

    TARGET_GRID_9 = [
        # (x_norm, y_norm, type)
        (0.1, 0.1, "corner"),  # 0: Top-Left
        (0.5, 0.1, "edge"),    # 1: Top-Center
        (0.9, 0.1, "corner"),  # 2: Top-Right
        (0.1, 0.5, "edge"),    # 3: Mid-Left
        (0.5, 0.5, "center"),  # 4: Center
        (0.9, 0.5, "edge"),    # 5: Mid-Right
        (0.1, 0.9, "corner"),  # 6: Bottom-Left
        (0.5, 0.9, "edge"),    # 7: Bottom-Center
        (0.9, 0.9, "corner"),  # 8: Bottom-Right
    ]

    def __init__(self, screen_w: int = 1920, screen_h: int = 1080):
        self.screen_w = screen_w
        self.screen_h = screen_h

        # Raw collected data: list of dicts with target_id, target_type, features, target_px
        self.samples: List[Dict[str, Any]] = []

        # Production models (fitted on all points)
        self.model_x: Optional[Pipeline] = None
        self.model_y: Optional[Pipeline] = None
        self.best_alpha: float = 1.0

        # Calibration diagnostics
        self.diagnostics: Optional[Dict[str, Any]] = None

    def add_target_samples(
        self, target_id: int, feature_rows: List[np.ndarray]
    ):
        """Add multiple frames collected during dwell on a specific target."""
        if target_id < 0 or target_id >= len(self.TARGET_GRID_9):
            raise ValueError(f"Invalid target_id: {target_id}")

        tx_norm, ty_norm, t_type = self.TARGET_GRID_9[target_id]
        target_px_x = tx_norm * self.screen_w
        target_px_y = ty_norm * self.screen_h

        for feat in feature_rows:
            self.samples.append({
                "target_id": target_id,
                "target_type": t_type,
                "features": np.asarray(feat, dtype=np.float64),
                "target_x": target_px_x,
                "target_y": target_px_y,
            })

    def run_lopo_cross_validation(
        self, alpha_candidates: Optional[np.ndarray] = None
    ) -> Dict[str, Any]:
        """
        Runs rigorous Leave-One-Point-Out Cross Validation across all 9 targets.
        Selects best alpha and computes Center vs Edge vs Corner errors without data leakage.
        """
        if len(self.samples) < 9 * 5:
            raise ValueError("Insufficient calibration samples (need samples from all 9 targets).")

        if alpha_candidates is None:
            alpha_candidates = np.logspace(-2, 4, 25)

        X = np.array([s["features"] for s in self.samples])
        y_x = np.array([s["target_x"] for s in self.samples])
        y_y = np.array([s["target_y"] for s in self.samples])
        groups = np.array([s["target_id"] for s in self.samples])
        types = np.array([s["target_type"] for s in self.samples])

        gkf = GroupKFold(n_splits=9)

        best_alpha = None
        lowest_overall_error = float("inf")
        best_eval_breakdown = {}

        for alpha in alpha_candidates:
            fold_errors = []
            predictions_by_target = {}

            for train_idx, test_idx in gkf.split(X, y_x, groups=groups):
                # Clean pipeline refitted strictly inside fold to prevent scaler leakage
                pipe_x = Pipeline([
                    ("scaler", StandardScaler()),
                    ("ridge", Ridge(alpha=alpha, fit_intercept=True))
                ])
                pipe_y = Pipeline([
                    ("scaler", StandardScaler()),
                    ("ridge", Ridge(alpha=alpha, fit_intercept=True))
                ])

                pipe_x.fit(X[train_idx], y_x[train_idx])
                pipe_y.fit(X[train_idx], y_y[train_idx])

                pred_x = pipe_x.predict(X[test_idx])
                pred_y = pipe_y.predict(X[test_idx])

                held_out_id = groups[test_idx][0]
                held_out_type = types[test_idx][0]

                # Mean Euclidean error for this held-out target point
                true_x = y_x[test_idx]
                true_y = y_y[test_idx]
                euclidean_err = np.mean(np.hypot(pred_x - true_x, pred_y - true_y))

                fold_errors.append((held_out_id, held_out_type, euclidean_err))

            overall_mean_err = float(np.mean([e[2] for e in fold_errors]))
            if overall_mean_err < lowest_overall_error:
                lowest_overall_error = overall_mean_err
                best_alpha = alpha

                # Categorize errors
                center_errs = [e[2] for e in fold_errors if e[1] == "center"]
                edge_errs = [e[2] for e in fold_errors if e[1] == "edge"]
                corner_errs = [e[2] for e in fold_errors if e[1] == "corner"]

                best_eval_breakdown = {
                    "overall_mean_px": overall_mean_err,
                    "overall_std_px": float(np.std([e[2] for e in fold_errors])),
                    "center_mean_px": float(np.mean(center_errs)) if center_errs else 0.0,
                    "edge_mean_px": float(np.mean(edge_errs)) if edge_errs else 0.0,
                    "corner_mean_px": float(np.mean(corner_errs)) if corner_errs else 0.0,
                    "target_errors": {int(e[0]): float(e[2]) for e in fold_errors},
                    "best_alpha": float(best_alpha),
                }

        self.best_alpha = float(best_alpha)
        self.diagnostics = best_eval_breakdown

        # Refit final production pipelines on ALL data into local variables first
        new_model_x = Pipeline([
            ("scaler", StandardScaler()),
            ("ridge", Ridge(alpha=self.best_alpha, fit_intercept=True))
        ])
        new_model_y = Pipeline([
            ("scaler", StandardScaler()),
            ("ridge", Ridge(alpha=self.best_alpha, fit_intercept=True))
        ])
        new_model_x.fit(X, y_x)
        new_model_y.fit(X, y_y)

        # Atomic assignment to prevent vision thread from reading an unfitted Pipeline
        self.model_x = new_model_x
        self.model_y = new_model_y

        return self.diagnostics

    def predict(self, feature_vector: np.ndarray) -> Tuple[float, float]:
        """Maps a single [yaw, pitch, iris_x, iris_y] feature vector to (screen_x, screen_y)."""
        mx, my = self.model_x, self.model_y
        if mx is None or my is None:
            raise RuntimeError("Model is not calibrated yet. Run cross-validation first.")
        feat = feature_vector.reshape(1, -1)
        px_x = float(mx.predict(feat)[0])
        px_y = float(my.predict(feat)[0])
        # Clamp to screen bounds
        px_x = max(0.0, min(float(self.screen_w), px_x))
        px_y = max(0.0, min(float(self.screen_h), px_y))
        return px_x, px_y

    def save(self, filepath: str):
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        joblib.dump({
            "model_x": self.model_x,
            "model_y": self.model_y,
            "best_alpha": self.best_alpha,
            "screen_w": self.screen_w,
            "screen_h": self.screen_h,
            "diagnostics": self.diagnostics,
        }, filepath)

    def load(self, filepath: str):
        data = joblib.load(filepath)
        model_x = data["model_x"]
        model_y = data["model_y"]
        # Reject saved models trained before tracker vectors gained pose translation.
        expected_features = 7
        if (getattr(model_x, "n_features_in_", None) != expected_features or
                getattr(model_y, "n_features_in_", None) != expected_features):
            self.model_x = None
            self.model_y = None
            raise ValueError("Calibration model uses an outdated feature format; recalibration is required.")
        self.model_x = model_x
        self.model_y = model_y
        self.best_alpha = data["best_alpha"]
        self.screen_w = data["screen_w"]
        self.screen_h = data["screen_h"]
        self.diagnostics = data.get("diagnostics")
