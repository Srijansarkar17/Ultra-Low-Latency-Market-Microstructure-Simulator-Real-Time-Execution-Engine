"""
volatility_predictor.py
========================
XGBoost-based short-term BTCUSDT volatility predictor — v2 (improved).

Consumes features.csv produced by FeatureLogger (see feature_logger.py).
Columns expected: timestamp, mid, spread, spread_change, imbalance, trade_count

Key improvements over v1
------------------------
* DataLoader resamples raw event-driven CSV to 1-second bars so every row
  represents a genuine time interval (fixes the 98.99 % zero-return problem).
* FeatureEngineer adds richer microstructure features: log-transformed volume,
  price velocity, EWM momentum / volatility, extended lags, multi-scale
  realized volatility.
* Target is scaled by 1e6 before fitting and back-transformed for display
  (improves XGBoost numerical stability on near-zero floating-point targets).
* Improved XGBoost params: early stopping, L1/L2 regularisation, gamma.
* Extended evaluation: Pearson correlation + directional accuracy.

Usage
-----
Train + evaluate + predict (one-shot):
    python volatility_predictor.py

Force re-training:
    python volatility_predictor.py --retrain

Live monitoring mode (refresh every 5 s):
    python volatility_predictor.py --live

Author : Market Microstructure Simulator project
"""

from __future__ import annotations

import argparse
import logging
import os
import pickle
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import TimeSeriesSplit

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("VolatilityPredictor")


# ---------------------------------------------------------------------------
# Constants / default paths
# ---------------------------------------------------------------------------
_HERE = Path(__file__).parent
_DEFAULT_CSV   = _HERE.parent / "features.csv"          # backend/src/features.csv
_DEFAULT_MODEL = _HERE / "volatility_model.json"         # saved next to this file
_DEFAULT_COLS  = _HERE / "feature_columns.pkl"           # saved next to this file

# Target scaling factor — improves XGBoost numerical stability when the raw
# target values are in the ~1e-6 range (near-zero floats cause poor splits).
TARGET_SCALE: float = 1_000_000.0

XGBOOST_PARAMS: dict = {
    "n_estimators": 400,
    "max_depth": 3,              # shallow trees: better generalisation on small datasets
    "learning_rate": 0.04,
    "subsample": 0.7,
    "colsample_bytree": 0.7,
    "min_child_weight": 5,      # prevents overfitting on sparse signal
    "gamma": 0.1,               # minimum loss reduction for a split
    "reg_alpha": 0.1,           # L1 regularisation
    "reg_lambda": 2.0,          # L2 regularisation
    "random_state": 42,
    "objective": "reg:squarederror",
    "tree_method": "hist",      # fast & memory-efficient
    "early_stopping_rounds": 40,
}


# ---------------------------------------------------------------------------
# DataLoader
# ---------------------------------------------------------------------------
class DataLoader:
    """
    Loads and cleans features.csv produced by FeatureLogger, then resamples
    the raw event-driven rows into 1-second time bars.

    Root Cause Fix B (partially): raw data logged per-WebSocket-event has
    ~86 rows/second; log returns are 0.0 in ~99 % of rows. Resampling to
    1-second bars collapses these into single rows whose mid-price
    differences represent genuine 1-second price moves, making log returns
    and rolling statistics meaningful for the model.

    Root Cause Fix C (partially): trade_count is *summed* within each 1-second
    bar rather than taking the last value, recovering the volume information
    that was silently zeroed by the event-driven reset bug in the original
    data_feed.py.

    Parameters
    ----------
    csv_path : str | Path
        Path to features.csv.
    bar_seconds : float
        Resampling interval in seconds. Default = 1.0.
    """

    REQUIRED_COLUMNS: Tuple[str, ...] = (
        "timestamp", "mid", "spread", "spread_change", "imbalance", "trade_count"
    )

    def __init__(
        self,
        csv_path: str | Path = _DEFAULT_CSV,
        bar_seconds: float = 1.0,
    ) -> None:
        self.csv_path   = Path(csv_path)
        self.bar_seconds = bar_seconds

    def load(self) -> pd.DataFrame:
        """
        Read, validate, resample to 1-second bars, and return a clean
        DataFrame sorted chronologically.

        Returns
        -------
        pd.DataFrame
            Resampled DataFrame with one row per second.

        Raises
        ------
        FileNotFoundError
            If the CSV file is missing.
        ValueError
            If required columns are absent.
        """
        if not self.csv_path.exists():
            raise FileNotFoundError(
                f"features.csv not found at: {self.csv_path}\n"
                "Make sure the simulator has run at least once."
            )

        logger.info("Loading data from %s", self.csv_path)
        df = pd.read_csv(self.csv_path)

        # ── Validate columns ──────────────────────────────────────────
        missing = set(self.REQUIRED_COLUMNS) - set(df.columns)
        if missing:
            raise ValueError(f"features.csv is missing columns: {missing}")

        # ── Coerce numeric types ──────────────────────────────────────
        for col in self.REQUIRED_COLUMNS:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # ── Sort chronologically ──────────────────────────────────────
        df.sort_values("timestamp", inplace=True)
        df.drop_duplicates(subset=["timestamp"], inplace=True)
        df.dropna(inplace=True)
        df.reset_index(drop=True, inplace=True)

        raw_rows = len(df)
        logger.info("Raw rows loaded: %d", raw_rows)

        # ── Resample to 1-second bars (FIX A + C) ────────────────────
        #
        # timestamp is in *microseconds* → convert to integer seconds
        df["ts_sec"] = (df["timestamp"] / 1_000_000).astype(int)

        resampled = (
            df.groupby("ts_sec", sort=True)
            .agg(
                timestamp  = ("timestamp",   "last"),   # representative ts
                mid        = ("mid",         "last"),   # closing price of bar
                spread     = ("spread",      "mean"),   # average spread
                spread_change = ("spread_change", "last"),
                imbalance  = ("imbalance",   "mean"),   # average imbalance
                trade_count = ("trade_count", "sum"),   # FIX C: aggregate!
            )
            .reset_index(drop=True)
        )

        resampled.dropna(inplace=True)
        resampled.reset_index(drop=True, inplace=True)

        logger.info(
            "Resampled %d raw rows → %d 1-second bars (%.1f%% reduction)",
            raw_rows,
            len(resampled),
            100.0 * (1 - len(resampled) / max(1, raw_rows)),
        )

        return resampled


# ---------------------------------------------------------------------------
# FeatureEngineer
# ---------------------------------------------------------------------------
class FeatureEngineer:
    """
    Transforms a resampled (1-second bar) DataFrame into a fully-engineered
    design matrix suitable for the XGBoost model.

    Improvements over v1
    --------------------
    * trade_count_log: log1p transform to handle heavy right skew.
    * price_velocity: return_1 / spread — microstructure signal.
    * volume_imbalance_interaction: imbalance × log1p(trade_count).
    * realized_volatility_10 and _20: multi-scale rolling volatility context.
    * Extended lags to 5 periods for all base features.
    * ewm_return and ewm_vol: exponential moving average of returns and
      volatility for momentum and regime detection.

    Parameters
    ----------
    prediction_horizon : int
        Number of future 1-second bars used to compute the rolling-std
        volatility target.  Default = 30 (= 30 seconds ahead).
    """

    def __init__(self, prediction_horizon: int = 30) -> None:
        self.prediction_horizon = prediction_horizon

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Full pipeline: resampled 1-second bar DataFrame → engineered design
        matrix with target column.

        Steps
        -----
        1. Compute 1-second log returns.
        2. Compute forward-looking volatility target (scaled by TARGET_SCALE).
        3. Log-transform trade_count.
        4. Build lag features (lags 1–5 for spread, imbalance, trade_count_log,
           spread_change).
        5. Build rolling statistics over 5-bar and 10-bar windows.
        6. Build multi-scale realized volatility (5, 10, 20 bars).
        7. Build EWM momentum and volatility features.
        8. Build interaction terms (price_velocity, volume_imbalance).
        9. Drop rows with NaN and rows where future_volatility == 0
           (zero-target rows where all next-N returns are flat provide no
           learning signal and distort MSE minimisation).

        Returns
        -------
        pd.DataFrame
            DataFrame with all feature columns and 'future_volatility' target
            (already scaled by TARGET_SCALE).
        """
        df = df.copy()

        # ── 1. Log returns (1-second) ──────────────────────────────────
        df["return_1"] = np.log(df["mid"] / df["mid"].shift(1))
        df["return_5"] = np.log(df["mid"] / df["mid"].shift(5))

        # ── 2. Future-volatility target (FIX B) ──────────────────────
        #
        # With 1-second bars, rolling(N) now means the next N *seconds*
        # of real time — a genuinely meaningful volatility window.
        # Scale by TARGET_SCALE so the tree can make useful splits on a
        # value of ~1.0 instead of ~0.000001.
        df["future_volatility"] = (
            df["return_1"]
            .shift(-1)
            .rolling(window=self.prediction_horizon,
                     min_periods=self.prediction_horizon)
            .std()
            .shift(-(self.prediction_horizon - 1))
            * TARGET_SCALE
        )

        # ── 3. Log-transform trade_count (FIX: heavy skew) ───────────
        df["trade_count_log"] = np.log1p(df["trade_count"])

        # ── 4. Extended lag features (lags 1–5) ──────────────────────
        for col in ("spread", "imbalance", "trade_count_log", "spread_change"):
            for lag in range(1, 6):
                df[f"{col}_lag{lag}"] = df[col].shift(lag)

        # ── 5. Rolling statistics (5-bar and 10-bar windows) ─────────
        for col in ("spread", "imbalance", "trade_count_log"):
            for w in (5, 10):
                df[f"rolling_mean_{col}_{w}"] = (
                    df[col].rolling(window=w, min_periods=1).mean()
                )
                df[f"rolling_std_{col}_{w}"] = (
                    df[col].rolling(window=w, min_periods=2).std()
                )

        # ── 6. Multi-scale realized volatility ───────────────────────
        for w in (5, 10, 20):
            df[f"realized_volatility_{w}"] = (
                df["return_1"].rolling(window=w, min_periods=2).std()
                * TARGET_SCALE  # scale consistent with target
            )

        # ── 7. EWM momentum and volatility ───────────────────────────
        df["ewm_return"] = df["return_1"].ewm(alpha=0.3, adjust=False).mean()
        df["ewm_vol"]    = (
            df["return_1"].ewm(alpha=0.3, adjust=False).std()
            * TARGET_SCALE
        )

        # ── 8. Interaction / derived features ─────────────────────────
        # price_velocity: how much price moved per unit of spread.
        # Captures whether price moves are large relative to the bid-ask cost.
        df["price_velocity"] = (
            df["return_1"] / df["spread"].replace(0, np.nan)
        ).fillna(0.0)

        # volume_imbalance_interaction: directional pressure weighted by volume
        df["volume_imbalance"] = df["imbalance"] * df["trade_count_log"]

        # ── 9. Drop NaN rows + zero-target rows ──────────────────────
        feature_cols = self.feature_columns(df)
        df.dropna(subset=["future_volatility"] + feature_cols, inplace=True)
        # Filter out rows where future_volatility == 0 (flat return windows).
        # These rows have no learning signal and would push the model toward
        # predicting 0 for every low-volatility state.
        df = df[df["future_volatility"] > 0].copy()
        df.reset_index(drop=True, inplace=True)

        logger.info(
            "Feature engineering complete: %d rows, %d features",
            len(df), len(feature_cols),
        )
        return df

    @staticmethod
    def feature_columns(df: pd.DataFrame) -> List[str]:
        """
        Return the list of feature column names present in *df*.

        Canonical feature set (v2):
            spread_lag1..5, imbalance_lag1..5,
            trade_count_log_lag1..5, spread_change_lag1..5,
            rolling_mean/std_{spread,imbalance,trade_count_log}_{5,10},
            realized_volatility_{5,10,20},
            ewm_return, ewm_vol,
            return_1, return_5,
            price_velocity, volume_imbalance,
            trade_count_log
        """
        candidates = (
            [f"spread_lag{i}"           for i in range(1, 6)]
            + [f"imbalance_lag{i}"      for i in range(1, 6)]
            + [f"trade_count_log_lag{i}" for i in range(1, 6)]
            + [f"spread_change_lag{i}"  for i in range(1, 6)]
            + [f"rolling_mean_spread_{w}" for w in (5, 10)]
            + [f"rolling_std_spread_{w}"  for w in (5, 10)]
            + [f"rolling_mean_imbalance_{w}" for w in (5, 10)]
            + [f"rolling_std_imbalance_{w}"  for w in (5, 10)]
            + [f"rolling_mean_trade_count_log_{w}" for w in (5, 10)]
            + [f"rolling_std_trade_count_log_{w}"  for w in (5, 10)]
            + [f"realized_volatility_{w}" for w in (5, 10, 20)]
            + ["ewm_return", "ewm_vol"]
            + ["return_1", "return_5"]
            + ["price_velocity", "volume_imbalance", "trade_count_log"]
        )
        return [c for c in candidates if c in df.columns]


# ---------------------------------------------------------------------------
# VolatilityModel
# ---------------------------------------------------------------------------
class VolatilityModel:
    """
    XGBoost regressor wrapper for short-term volatility prediction.

    The model is trained on targets that have been scaled by TARGET_SCALE
    and will return raw (scaled) predictions; callers divide by TARGET_SCALE
    to obtain the true volatility value.

    Parameters
    ----------
    model_path : str | Path
        Where to save / load the XGBoost model (JSON format).
    cols_path : str | Path
        Where to save / load the feature column list (pickle).
    xgb_params : dict, optional
        XGBoost parameters. Defaults to ``XGBOOST_PARAMS``.
    """

    def __init__(
        self,
        model_path: str | Path = _DEFAULT_MODEL,
        cols_path: str | Path  = _DEFAULT_COLS,
        xgb_params: Optional[dict] = None,
    ) -> None:
        self.model_path = Path(model_path)
        self.cols_path  = Path(cols_path)
        self.params     = {k: v for k, v in (xgb_params or XGBOOST_PARAMS).items()
                           if k != "early_stopping_rounds"}
        self._early_stopping_rounds: int = (xgb_params or XGBOOST_PARAMS).get(
            "early_stopping_rounds", 40
        )
        self._model: Optional[xgb.XGBRegressor] = None
        self._feature_columns: Optional[List[str]] = None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self) -> None:
        """Persist the trained model and feature column list to disk."""
        if self._model is None:
            raise RuntimeError("No trained model to save. Call train() first.")

        self._model.save_model(str(self.model_path))
        with open(self.cols_path, "wb") as fh:
            pickle.dump(self._feature_columns, fh)

        logger.info("Model saved to %s", self.model_path)
        logger.info("Feature columns saved to %s", self.cols_path)

    def load(self) -> None:
        """
        Load a previously saved model and feature column list from disk.

        Raises
        ------
        FileNotFoundError
            If either artefact is missing.
        """
        if not self.model_path.exists():
            raise FileNotFoundError(
                f"No saved model found at {self.model_path}. "
                "Run without --live first to train."
            )
        if not self.cols_path.exists():
            raise FileNotFoundError(
                f"No feature column list found at {self.cols_path}."
            )

        self._model = xgb.XGBRegressor(**self.params)
        self._model.load_model(str(self.model_path))

        with open(self.cols_path, "rb") as fh:
            self._feature_columns = pickle.load(fh)

        logger.info("Model loaded from %s", self.model_path)

    @property
    def is_trained(self) -> bool:
        """True if a model is resident in memory (trained or loaded)."""
        return self._model is not None

    @property
    def is_saved(self) -> bool:
        """True if both model artefacts exist on disk."""
        return self.model_path.exists() and self.cols_path.exists()

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train(self, df: pd.DataFrame, feature_cols: List[str]) -> None:
        """
        Chronological 80/20 train-test split + XGBoost fit with early stopping.

        The target ('future_volatility') is expected to already be scaled by
        TARGET_SCALE (done in FeatureEngineer.build).

        Parameters
        ----------
        df : pd.DataFrame
            Fully-engineered DataFrame including 'future_volatility'.
        feature_cols : list[str]
            Column names to use as model features.
        """
        self._feature_columns = feature_cols
        split_idx = int(len(df) * 0.8)

        X_train = df.loc[:split_idx - 1, feature_cols]
        y_train = df.loc[:split_idx - 1, "future_volatility"]
        X_test  = df.loc[split_idx:, feature_cols]
        y_test  = df.loc[split_idx:, "future_volatility"]

        logger.info(
            "Training on %d rows, validating on %d rows  "
            "(target scale = ×%g)",
            len(X_train), len(X_test), TARGET_SCALE,
        )

        model_params = dict(self.params)
        model_params["early_stopping_rounds"] = self._early_stopping_rounds

        self._model = xgb.XGBRegressor(**model_params)
        self._model.fit(
            X_train,
            y_train,
            eval_set=[(X_test, y_test)],
            verbose=False,
        )

        # ── Evaluation ────────────────────────────────────────────────
        y_pred = self._model.predict(X_test)
        self._print_metrics(y_test.values, y_pred)

    @staticmethod
    def _print_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> None:
        """Print evaluation metrics including correlation and directional accuracy."""
        # Clamp negatives (volatility can't be negative)
        y_pred_c = np.clip(y_pred, 0.0, None)

        rmse = np.sqrt(mean_squared_error(y_true, y_pred_c))
        mae  = mean_absolute_error(y_true, y_pred_c)
        r2   = r2_score(y_true, y_pred_c)

        # Pearson correlation (captures monotonic relationship)
        if np.std(y_pred_c) > 0 and np.std(y_true) > 0:
            corr = float(np.corrcoef(y_true, y_pred_c)[0, 1])
        else:
            corr = 0.0

        # Directional accuracy: does the model correctly predict high vs. low
        # relative to the median?
        med = np.median(y_true)
        dir_acc = np.mean((y_pred_c >= med) == (y_true >= med))

        # Scale back for display
        scale = 1.0 / TARGET_SCALE
        separator = "─" * 55
        print(f"\n{separator}")
        print("  Model Evaluation Metrics  (raw units = ×1e-6 volatility)")
        print(separator)
        print(f"  RMSE             : {rmse * scale:.8f}")
        print(f"  MAE              : {mae  * scale:.8f}")
        print(f"  R²               : {r2:.6f}")
        print(f"  Pearson r        : {corr:.6f}")
        print(f"  Directional Acc  : {dir_acc * 100:.2f}%")
        print(f"{separator}\n")

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(self, X: pd.DataFrame) -> float:
        """
        Run inference on the most recent row and return the predicted future
        volatility in *raw units* (not scaled by TARGET_SCALE — caller divides).

        Parameters
        ----------
        X : pd.DataFrame
            DataFrame that contains exactly ``self._feature_columns``.

        Returns
        -------
        float
            Predicted future volatility (divided by TARGET_SCALE).
        """
        if self._model is None or self._feature_columns is None:
            raise RuntimeError("Model is not loaded. Call load() or train() first.")

        X_aligned = X[self._feature_columns].tail(1)
        pred_scaled = float(self._model.predict(X_aligned)[0])
        return max(0.0, pred_scaled) / TARGET_SCALE  # back-transform + clamp

    # ------------------------------------------------------------------
    # Feature importance
    # ------------------------------------------------------------------

    def print_feature_importance(self) -> None:
        """Print ranked feature importances to stdout."""
        if self._model is None or self._feature_columns is None:
            logger.warning("No model loaded – cannot display feature importance.")
            return

        scores = self._model.feature_importances_
        ranked = sorted(
            zip(self._feature_columns, scores),
            key=lambda x: x[1],
            reverse=True,
        )

        separator = "─" * 60
        print(f"\n{separator}")
        print("  Feature Importance Ranking (XGBoost normalised gain)")
        print(separator)
        for rank, (feat, score) in enumerate(ranked, start=1):
            bar = "█" * max(1, int(score * 400))
            print(f"  {rank:>2}. {feat:<45} {score:.6f}  {bar}")
        print(f"{separator}\n")


# ---------------------------------------------------------------------------
# VolatilityPredictor  (orchestration layer)
# ---------------------------------------------------------------------------
class VolatilityPredictor:
    """
    High-level orchestrator that wires DataLoader, FeatureEngineer, and
    VolatilityModel together.

    Parameters
    ----------
    csv_path : str | Path
        Path to features.csv.
    model_path : str | Path
        Where the XGBoost model JSON is saved/loaded.
    cols_path : str | Path
        Where the feature column pickle is saved/loaded.
    prediction_horizon : int
        Rolling-std window for the volatility target in 1-second bars
        (default 30 = 30 seconds ahead).
    bar_seconds : float
        Resampling interval for the DataLoader (default 1.0 second).
    """

    def __init__(
        self,
        csv_path: str | Path        = _DEFAULT_CSV,
        model_path: str | Path      = _DEFAULT_MODEL,
        cols_path: str | Path       = _DEFAULT_COLS,
        prediction_horizon: int     = 10,
        bar_seconds: float          = 1.0,
    ) -> None:
        self.loader   = DataLoader(csv_path=csv_path, bar_seconds=bar_seconds)
        self.engineer = FeatureEngineer(prediction_horizon=prediction_horizon)
        self.model    = VolatilityModel(model_path=model_path, cols_path=cols_path)

    # ------------------------------------------------------------------
    # Train
    # ------------------------------------------------------------------

    def train_and_save(self) -> None:
        """
        End-to-end training pipeline:
        1. Load + resample CSV → 2. Engineer features → 3. Train XGBoost →
        4. Walk-forward CV evaluation → 5. Save.
        """
        raw_df = self.loader.load()
        eng_df = self.engineer.build(raw_df)

        if len(eng_df) < 50:
            logger.warning(
                "Only %d usable rows after engineering. "
                "Predictions may be unreliable — collect more data.",
                len(eng_df),
            )

        feature_cols = FeatureEngineer.feature_columns(eng_df)
        logger.info("Feature set (%d columns): %s", len(feature_cols), feature_cols)

        self.model.train(eng_df, feature_cols)

        # ── Walk-forward cross-validation ─────────────────────────────
        self._run_walk_forward_cv(eng_df, feature_cols)

        self.model.save()

    @staticmethod
    def _run_walk_forward_cv(df: pd.DataFrame, feature_cols: List[str]) -> None:
        """
        Run 5-fold walk-forward time-series cross-validation and print a
        summary. This is more informative than a single train/test split
        on a small dataset — it shows how the model generalises across
        different volatility regimes in the data.
        """
        from sklearn.model_selection import TimeSeriesSplit

        tscv = TimeSeriesSplit(n_splits=5, gap=3)
        r2_scores, corr_scores, dir_scores = [], [], []

        params = {k: v for k, v in XGBOOST_PARAMS.items()
                  if k not in ("early_stopping_rounds",)}

        separator = "─" * 65
        print(f"\n{separator}")
        print("  Walk-Forward Cross-Validation  (5 folds, gap=3 bars)")
        print(separator)

        for fold, (tr_idx, te_idx) in enumerate(tscv.split(df), start=1):
            X_tr = df.iloc[tr_idx][feature_cols]
            y_tr = df.iloc[tr_idx]["future_volatility"]
            X_te = df.iloc[te_idx][feature_cols]
            y_te = df.iloc[te_idx]["future_volatility"]

            if len(X_te) < 5:
                continue

            fold_model = xgb.XGBRegressor(
                **params,
                early_stopping_rounds=20,
            )
            fold_model.fit(X_tr, y_tr, eval_set=[(X_te, y_te)], verbose=False)
            y_pred = np.clip(fold_model.predict(X_te), 0.0, None)

            r2 = r2_score(y_te, y_pred)
            corr = (float(np.corrcoef(y_te, y_pred)[0, 1])
                    if np.std(y_pred) > 0 else 0.0)
            dir_acc = float(np.mean(
                (y_pred >= np.median(y_te)) == (y_te >= np.median(y_te))
            ))

            r2_scores.append(r2)
            corr_scores.append(corr)
            dir_scores.append(dir_acc)

            scale = 1.0 / TARGET_SCALE
            print(
                f"  Fold {fold}: n_train={len(tr_idx):>3}, n_test={len(te_idx):>3} │ "
                f"R²={r2:+.4f}  corr={corr:+.4f}  DirAcc={dir_acc * 100:.1f}%"
            )

        if r2_scores:
            print(separator)
            print(
                f"  Mean: R²={np.mean(r2_scores):+.4f} (±{np.std(r2_scores):.4f})"
                f"  Pearson={np.mean(corr_scores):+.4f}"
                f"  DirAcc={np.mean(dir_scores) * 100:.1f}%"
            )
            print(f"{separator}\n")

    # ------------------------------------------------------------------
    # Predict latest
    # ------------------------------------------------------------------

    def predict_latest_volatility(self) -> dict:
        """
        Load the latest features.csv, resample, recompute all features,
        load (or reuse) the saved model, and return the prediction for the
        most recent 1-second bar.

        Returns
        -------
        dict
            {
              "predicted_volatility": float,   # true scale (not scaled)
              "current_mid": float,
              "current_spread": float,
              "current_imbalance": float,
              "current_trade_count": int,
            }
        """
        if not self.model.is_trained:
            if not self.model.is_saved:
                raise RuntimeError(
                    "No trained model found. Run without --live first to train."
                )
            self.model.load()

        raw_df = self.loader.load()
        eng_df = self.engineer.build(raw_df)

        feature_cols = FeatureEngineer.feature_columns(eng_df)
        predicted    = self.model.predict(eng_df[feature_cols])

        latest_raw = raw_df.iloc[-1]
        return {
            "predicted_volatility": predicted,
            "current_mid":          float(latest_raw["mid"]),
            "current_spread":       float(latest_raw["spread"]),
            "current_imbalance":    float(latest_raw["imbalance"]),
            "current_trade_count":  int(latest_raw["trade_count"]),
        }

    # ------------------------------------------------------------------
    # Live monitoring
    # ------------------------------------------------------------------

    def run_live(self, interval_seconds: float = 5.0) -> None:
        """
        Continuously poll features.csv and print volatility predictions.

        Parameters
        ----------
        interval_seconds : float
            Seconds to sleep between prediction cycles.
        """
        separator = "═" * 56
        logger.info("Starting live monitoring (interval = %.1f s)", interval_seconds)
        logger.info("Press Ctrl+C to stop.")

        if not self.model.is_trained:
            if self.model.is_saved:
                self.model.load()
            else:
                logger.info("No saved model found – training now …")
                self.train_and_save()

        while True:
            try:
                result = self.predict_latest_volatility()

                print(f"\n{separator}")
                print(f"  ⏱  {time.strftime('%Y-%m-%d %H:%M:%S')}")
                print(separator)
                print(f"  Current Mid Price          : {result['current_mid']:.4f}")
                print(f"  Current Spread             : {result['current_spread']:.8f}")
                print(f"  Current Imbalance          : {result['current_imbalance']:.6f}")
                print(f"  Current Trade Count (1s)   : {result['current_trade_count']}")
                print(f"  Predicted Future Volatility: {result['predicted_volatility']:.8f}")
                print(f"{separator}")

            except Exception as exc:
                logger.error("Prediction cycle failed: %s", exc, exc_info=True)

            time.sleep(interval_seconds)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "BTCUSDT Short-Term Volatility Predictor (v2 — improved)\n"
            "Trains an XGBoost model on features.csv resampled to 1-second bars."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Run in live-monitoring mode (poll every --interval seconds).",
    )
    parser.add_argument(
        "--csv",
        type=str,
        default=str(_DEFAULT_CSV),
        help=f"Path to features.csv (default: {_DEFAULT_CSV})",
    )
    parser.add_argument(
        "--horizon",
        type=int,
        default=10,
        help="Prediction horizon in 1-second bars (default: 10).",
    )
    parser.add_argument(
        "--retrain",
        action="store_true",
        default=False,
        help="Force re-training even if a saved model already exists.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Live-mode polling interval in seconds (default: 5).",
    )
    return parser


def main() -> None:
    """CLI entry point."""
    parser = _build_arg_parser()
    args   = parser.parse_args()

    predictor = VolatilityPredictor(
        csv_path           = args.csv,
        prediction_horizon = args.horizon,
    )

    # ── Live monitoring mode ──────────────────────────────────────────
    if args.live:
        predictor.run_live(interval_seconds=args.interval)
        return

    # ── One-shot mode ─────────────────────────────────────────────────
    # Step 1: Train if no saved model, or if --retrain is specified
    if args.retrain or not predictor.model.is_saved:
        logger.info("Step 1 – Training model …")
        predictor.train_and_save()
    else:
        logger.info(
            "Step 1 – Saved model found at %s. Loading …",
            predictor.model.model_path,
        )
        predictor.model.load()

    # Step 2: Evaluate on held-out 20%
    if not args.retrain and predictor.model.is_saved:
        logger.info("Step 2 – Running evaluation on held-out 20%% …")
        raw_df       = predictor.loader.load()
        eng_df       = predictor.engineer.build(raw_df)
        feature_cols = FeatureEngineer.feature_columns(eng_df)
        split_idx    = int(len(eng_df) * 0.8)
        X_test       = eng_df.loc[split_idx:, feature_cols]
        y_test       = eng_df.loc[split_idx:, "future_volatility"].values
        y_pred       = predictor.model._model.predict(X_test)
        VolatilityModel._print_metrics(y_test, y_pred)

    # Step 3: Predict latest
    logger.info("Step 3 – Predicting latest volatility …")
    result = predictor.predict_latest_volatility()

    print("\n" + "═" * 56)
    print("  BTCUSDT Volatility Prediction  (v2 — improved)")
    print("═" * 56)
    print(f"  Current Mid Price          : {result['current_mid']:.4f}")
    print(f"  Current Spread             : {result['current_spread']:.8f}")
    print(f"  Current Imbalance          : {result['current_imbalance']:.6f}")
    print(f"  Current Trade Count (1s)   : {result['current_trade_count']}")
    print(f"  Predicted Future Volatility: {result['predicted_volatility']:.8f}")
    print("═" * 56 + "\n")

    # Step 4: Feature importance
    logger.info("Step 4 – Feature importance ranking …")
    predictor.model.print_feature_importance()


if __name__ == "__main__":
    main()
