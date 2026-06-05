"""
volatility_predictor.py
========================
XGBoost-based short-term BTCUSDT volatility predictor.

Consumes features.csv produced by FeatureLogger (see feature_logger.py).
Columns expected: timestamp, mid, spread, spread_change, imbalance, trade_count

Usage
-----
Train + evaluate + predict (one-shot):
    python volatility_predictor.py

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
_DEFAULT_CSV = _HERE.parent / "features.csv"          # backend/src/features.csv
_DEFAULT_MODEL = _HERE / "volatility_model.json"       # saved next to this file
_DEFAULT_COLS = _HERE / "feature_columns.pkl"          # saved next to this file

XGBOOST_PARAMS: dict = {
    "n_estimators": 300,
    "max_depth": 6,
    "learning_rate": 0.05,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "random_state": 42,
    "objective": "reg:squarederror",
    "tree_method": "hist",          # fast, memory-efficient
}


# ---------------------------------------------------------------------------
# FeatureEngineer
# ---------------------------------------------------------------------------
class FeatureEngineer:
    """
    Transforms a raw features DataFrame (from FeatureLogger's CSV) into a
    fully-engineered design matrix suitable for the XGBoost model.

    Parameters
    ----------
    prediction_horizon : int
        Number of future rows used to compute the rolling-std volatility target.
        Default = 30.
    """

    def __init__(self, prediction_horizon: int = 30) -> None:
        self.prediction_horizon = prediction_horizon

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Full pipeline: raw CSV data → engineered DataFrame with target column.

        Steps
        -----
        1. Compute log returns.
        2. Compute forward-looking volatility target.
        3. Build lag features (spreads, imbalance, trade_count, spread_change).
        4. Build rolling statistics (mean + std over 5-row window).
        5. Build return-based features.
        6. Drop rows that still contain NaN.

        Returns
        -------
        pd.DataFrame
            DataFrame with all feature columns and 'future_volatility' target.
            The original raw columns are retained for reference.
        """
        df = df.copy()

        # ── 1. Log returns ────────────────────────────────────────────
        df["return_1"] = np.log(df["mid"] / df["mid"].shift(1))

        # ── 2. Future-volatility target ───────────────────────────────
        df["future_volatility"] = (
            df["return_1"]
            .shift(-1)                            # align: we want FUTURE returns
            .rolling(window=self.prediction_horizon, min_periods=self.prediction_horizon)
            .std()
            .shift(-(self.prediction_horizon - 1))  # re-align to current row
        )

        # ── 3. Lag features ───────────────────────────────────────────
        for col in ("spread", "imbalance", "trade_count", "spread_change"):
            for lag in (1, 2, 3):
                df[f"{col}_lag{lag}"] = df[col].shift(lag)

        # ── 4. Rolling statistics (window = 5) ───────────────────────
        for col in ("spread", "imbalance", "trade_count"):
            df[f"rolling_mean_{col}_5"] = (
                df[col].rolling(window=5, min_periods=1).mean()
            )
            df[f"rolling_std_{col}_5"] = (
                df[col].rolling(window=5, min_periods=2).std()
            )

        # ── 5. Return-based features ──────────────────────────────────
        # return_1 already computed above
        df["return_5"] = np.log(df["mid"] / df["mid"].shift(5))
        df["realized_volatility_5"] = (
            df["return_1"].rolling(window=5, min_periods=2).std()
        )

        # ── 6. Drop rows that can't be fully computed ─────────────────
        df.dropna(subset=["future_volatility"] + self.feature_columns(df), inplace=True)
        df.reset_index(drop=True, inplace=True)

        return df

    @staticmethod
    def feature_columns(df: pd.DataFrame) -> List[str]:
        """
        Return the list of feature column names present in *df*.

        The canonical list is:
            spread_lag1/2/3, imbalance_lag1/2/3, trade_count_lag1/2/3,
            spread_change_lag1/2/3,
            rolling_mean/std_{spread,imbalance,trade_count}_5,
            return_1, return_5, realized_volatility_5
        """
        candidates = (
            [f"spread_lag{i}" for i in (1, 2, 3)]
            + [f"imbalance_lag{i}" for i in (1, 2, 3)]
            + [f"trade_count_lag{i}" for i in (1, 2, 3)]
            + [f"spread_change_lag{i}" for i in (1, 2, 3)]
            + [f"rolling_mean_spread_5", "rolling_std_spread_5"]
            + [f"rolling_mean_imbalance_5", "rolling_std_imbalance_5"]
            + [f"rolling_mean_trade_count_5", "rolling_std_trade_count_5"]
            + ["return_1", "return_5", "realized_volatility_5"]
        )
        return [c for c in candidates if c in df.columns]


# ---------------------------------------------------------------------------
# DataLoader
# ---------------------------------------------------------------------------
class DataLoader:
    """
    Loads and cleans features.csv produced by FeatureLogger.

    Parameters
    ----------
    csv_path : str | Path
        Path to features.csv.
    """

    REQUIRED_COLUMNS: Tuple[str, ...] = (
        "timestamp", "mid", "spread", "spread_change", "imbalance", "trade_count"
    )

    def __init__(self, csv_path: str | Path = _DEFAULT_CSV) -> None:
        self.csv_path = Path(csv_path)

    def load(self) -> pd.DataFrame:
        """
        Read, validate, clean, and sort the CSV.

        Returns
        -------
        pd.DataFrame
            Clean DataFrame sorted by timestamp, numeric types enforced.

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

        logger.info(
            "Loaded %d rows after cleaning  (%.0f%% retained)",
            len(df),
            100.0 * len(df) / max(1, len(pd.read_csv(self.csv_path))),
        )
        return df


# ---------------------------------------------------------------------------
# VolatilityModel
# ---------------------------------------------------------------------------
class VolatilityModel:
    """
    XGBoost regressor wrapper for short-term volatility prediction.

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
        cols_path: str | Path = _DEFAULT_COLS,
        xgb_params: Optional[dict] = None,
    ) -> None:
        self.model_path = Path(model_path)
        self.cols_path = Path(cols_path)
        self.params = xgb_params or XGBOOST_PARAMS
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
        Chronological 80/20 train-test split + XGBoost fit.

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
        X_test = df.loc[split_idx:, feature_cols]
        y_test = df.loc[split_idx:, "future_volatility"]

        logger.info(
            "Training on %d rows, testing on %d rows", len(X_train), len(X_test)
        )

        self._model = xgb.XGBRegressor(**self.params)
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
        rmse = np.sqrt(mean_squared_error(y_true, y_pred))
        mae = mean_absolute_error(y_true, y_pred)
        r2 = r2_score(y_true, y_pred)

        separator = "─" * 45
        print(f"\n{separator}")
        print("  Model Evaluation Metrics")
        print(separator)
        print(f"  RMSE  : {rmse:.8f}")
        print(f"  MAE   : {mae:.8f}")
        print(f"  R²    : {r2:.6f}")
        print(f"{separator}\n")

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(self, X: pd.DataFrame) -> float:
        """
        Run inference on a single row (or small batch) and return the
        predicted future volatility for the *last* row.

        Parameters
        ----------
        X : pd.DataFrame
            DataFrame that contains exactly ``self._feature_columns``.

        Returns
        -------
        float
            Predicted future volatility.
        """
        if self._model is None or self._feature_columns is None:
            raise RuntimeError("Model is not loaded. Call load() or train() first.")

        X_aligned = X[self._feature_columns].tail(1)
        pred = float(self._model.predict(X_aligned)[0])
        return pred

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

        separator = "─" * 55
        print(f"\n{separator}")
        print("  Feature Importance Ranking (XGBoost gain)")
        print(separator)
        for rank, (feat, score) in enumerate(ranked, start=1):
            bar = "█" * int(score * 500)  # visual bar scaled to 500 chars max
            print(f"  {rank:>2}. {feat:<40} {score:.6f}  {bar}")
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
        Rolling-std window for the volatility target (default 30).
    """

    def __init__(
        self,
        csv_path: str | Path = _DEFAULT_CSV,
        model_path: str | Path = _DEFAULT_MODEL,
        cols_path: str | Path = _DEFAULT_COLS,
        prediction_horizon: int = 30,
    ) -> None:
        self.loader = DataLoader(csv_path=csv_path)
        self.engineer = FeatureEngineer(prediction_horizon=prediction_horizon)
        self.model = VolatilityModel(model_path=model_path, cols_path=cols_path)

    # ------------------------------------------------------------------
    # Train
    # ------------------------------------------------------------------

    def train_and_save(self) -> None:
        """
        End-to-end training pipeline:
        1. Load CSV → 2. Engineer features → 3. Train XGBoost → 4. Save artefacts.
        """
        raw_df = self.loader.load()
        eng_df = self.engineer.build(raw_df)

        if len(eng_df) < 100:
            logger.warning(
                "Only %d usable rows after engineering. "
                "Predictions may be unreliable.",
                len(eng_df),
            )

        feature_cols = FeatureEngineer.feature_columns(eng_df)
        logger.info("Feature set (%d columns): %s", len(feature_cols), feature_cols)

        self.model.train(eng_df, feature_cols)
        self.model.save()

    # ------------------------------------------------------------------
    # Predict latest
    # ------------------------------------------------------------------

    def predict_latest_volatility(self) -> dict:
        """
        Load the latest features.csv, recompute all engineered features,
        load (or reuse) the saved model, and return the prediction for the
        most recent observation.

        Returns
        -------
        dict
            {
              "predicted_volatility": float,
              "current_mid": float,
              "current_spread": float,
              "current_imbalance": float,
            }

        Raises
        ------
        RuntimeError
            If the model is not yet trained / saved.
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
        predicted = self.model.predict(eng_df[feature_cols])

        latest_raw = raw_df.iloc[-1]
        return {
            "predicted_volatility": predicted,
            "current_mid": float(latest_raw["mid"]),
            "current_spread": float(latest_raw["spread"]),
            "current_imbalance": float(latest_raw["imbalance"]),
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
        separator = "═" * 52
        logger.info("Starting live monitoring (interval = %.1f s)", interval_seconds)
        logger.info("Press Ctrl+C to stop.")

        # Ensure model is available before entering the loop
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
                print(f"  Current Mid Price         : {result['current_mid']:.4f}")
                print(f"  Current Spread            : {result['current_spread']:.8f}")
                print(f"  Current Imbalance         : {result['current_imbalance']:.6f}")
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
            "BTCUSDT Short-Term Volatility Predictor\n"
            "Trains an XGBoost model on features.csv and predicts future volatility."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Run in live-monitoring mode (poll every 5 s).",
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
        default=30,
        help="Prediction horizon in rows (default: 30).",
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
    args = parser.parse_args()

    predictor = VolatilityPredictor(
        csv_path=args.csv,
        prediction_horizon=args.horizon,
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
            "Step 1 – Saved model found at %s. Loading …", predictor.model.model_path
        )
        predictor.model.load()

    # Step 2: Evaluate (already printed inside train_and_save)
    # If we just loaded (not retrained), run a quick evaluation pass
    if not args.retrain and predictor.model.is_saved:
        logger.info("Step 2 – Running evaluation on held-out 20%% …")
        raw_df = predictor.loader.load()
        eng_df = predictor.engineer.build(raw_df)
        feature_cols = FeatureEngineer.feature_columns(eng_df)
        split_idx = int(len(eng_df) * 0.8)
        X_test = eng_df.loc[split_idx:, feature_cols]
        y_test = eng_df.loc[split_idx:, "future_volatility"].values
        y_pred = predictor.model._model.predict(X_test)
        VolatilityModel._print_metrics(y_test, y_pred)

    # Step 3: Predict latest
    logger.info("Step 3 – Predicting latest volatility …")
    result = predictor.predict_latest_volatility()

    print("\n" + "═" * 52)
    print("  BTCUSDT Volatility Prediction")
    print("═" * 52)
    print(f"  Current Mid Price          : {result['current_mid']:.4f}")
    print(f"  Current Spread             : {result['current_spread']:.8f}")
    print(f"  Current Imbalance          : {result['current_imbalance']:.6f}")
    print(f"  Predicted Future Volatility: {result['predicted_volatility']:.8f}")
    print("═" * 52 + "\n")

    # Step 4: Feature importance
    logger.info("Step 4 – Feature importance ranking …")
    predictor.model.print_feature_importance()


if __name__ == "__main__":
    main()
