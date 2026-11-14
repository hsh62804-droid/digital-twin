"""
FastAPI Backend - T1D Digital Twin Research Application.

Persistent Invariant (AGENTS.md Rule 7):
  "Research prototype. Not a medical device. Not for clinical decisions."
  Zero dosing or insulin recommendations provided.
  All data labeled with explicit origin: synthetic | simulated | estimated.
"""
from __future__ import annotations

import glob
import json
import logging
import sqlite3
import sys
import uuid
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="T1D Digital Twin - Research API",
    description="Research prototype. Not a medical device. Not for clinical decisions.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:4173",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:4173",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

DISCLAIMER = "Research prototype. Not a medical device. Not for clinical decisions."
USER_MEALS_PATH = REPO_ROOT / "data" / "processed" / "user_meals.json"

# ---------------------------------------------------------------------------
# SQLite patient profile database
# ---------------------------------------------------------------------------
DB_PATH = REPO_ROOT / "data" / "processed" / "patients.db"


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    """Idempotent schema initialisation — safe to call on every startup."""
    conn = _get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS patients (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                age INTEGER,
                weight_kg REAL,
                height_cm REAL,
                sex TEXT,
                category TEXT DEFAULT 'Custom',
                notes TEXT DEFAULT '',
                baseline_glucose_mgdL REAL,
                is_synthetic INTEGER DEFAULT 0,
                source TEXT DEFAULT 'user_created',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()
        logger.info("DB initialised at %s", DB_PATH)
    finally:
        conn.close()


_init_db()


def _bmi(weight_kg: float | None, height_cm: float | None) -> float | None:
    if weight_kg and height_cm and height_cm > 0:
        h_m = height_cm / 100.0
        return round(weight_kg / (h_m * h_m), 1)
    return None


def _row_to_patient_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["is_synthetic"] = bool(d.get("is_synthetic", 0))
    d["bmi"] = _bmi(d.get("weight_kg"), d.get("height_cm"))
    return d

PATIENT_CALIBRATED_PARAMS: dict[str, dict[str, float]] = {
    "synthetic_000": {
        "p1": 0.104949, "p2": 0.0124484, "p3": 0.000335955, "n": 0.111931,
        "Gb": 100.0, "Ib": 10.0, "Vg": 117.0, "Vi": 12.0, "rmse_calibrated": 5.42,
    },
    "synthetic_001": {
        "p1": 0.0316127, "p2": 0.0163347, "p3": 0.000104347, "n": 0.152273,
        "Gb": 100.0, "Ib": 10.0, "Vg": 117.0, "Vi": 12.0, "rmse_calibrated": 42.94,
    },
    "synthetic_002": {
        "p1": 0.0280004, "p2": 0.0279998, "p3": 0.0000499994, "n": 0.15,
        "Gb": 100.0, "Ib": 10.0, "Vg": 117.0, "Vi": 12.0, "rmse_calibrated": 50.42,
    },
    "synthetic_003": {
        "p1": 0.112159, "p2": 0.00451762, "p3": 0.000189795, "n": 0.137152,
        "Gb": 100.0, "Ib": 10.0, "Vg": 117.0, "Vi": 12.0, "rmse_calibrated": 11.62,
    },
    "synthetic_004": {
        "p1": 0.0316127, "p2": 0.0163347, "p3": 0.000104347, "n": 0.152273,
        "Gb": 100.0, "Ib": 10.0, "Vg": 117.0, "Vi": 12.0, "rmse_calibrated": 46.98,
    },
}

# Descriptive labels for synthetic benchmark patients
SYNTHETIC_LABELS: dict[str, dict[str, str]] = {
    "synthetic_000": {"display_name": "Synthetic Patient 000", "category": "Synthetic Cohort", "notes": "Well-calibrated benchmark (RMSE 5.4 mg/dL)."},
    "synthetic_001": {"display_name": "Synthetic Patient 001", "category": "Synthetic Cohort", "notes": "High glycaemic variability pattern."},
    "synthetic_002": {"display_name": "Synthetic Patient 002", "category": "Synthetic Cohort", "notes": "Nominal Bergman parameters; baseline synthetic trace."},
    "synthetic_003": {"display_name": "Synthetic Patient 003", "category": "Synthetic Cohort", "notes": "Elevated p1 (glucose effectiveness)."},
    "synthetic_004": {"display_name": "Synthetic Patient 004 (Hold-out)", "category": "Hold-out Test", "notes": "Held-out test patient; never used in model training."},
}


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------
class SimulationRequest(BaseModel):
    duration_hours: float = Field(default=24.0, ge=1.0, le=72.0)
    meal_cho_g: list[float] = Field(default=[40.0, 60.0, 45.0])
    meal_times_h: list[float] = Field(default=[7.0, 12.5, 18.5])
    basal_insulin_mU_per_min: float = Field(default=15.0, ge=0.0, le=100.0)
    seed: int = Field(default=42)


class WhatIfRequest(BaseModel):
    scenario_name: str = Field(default="Scenario")
    duration_hours: float = Field(default=4.0, ge=0.5, le=24.0)
    meal_cho_g: float = Field(default=40.0, ge=0.0, le=200.0)
    meal_time_h: float = Field(default=1.0, ge=0.0, le=24.0)
    basal_insulin_mU_per_min: float = Field(default=15.0, ge=0.0, le=100.0)
    bolus_insulin_mU: float = Field(default=0.0, ge=0.0, le=2000.0)
    seed: int = Field(default=42)


class MealInput(BaseModel):
    name: str = Field(default="Meal", min_length=1, max_length=80)
    timestamp: str = Field(..., description="ISO 8601 timestamp string")
    cho_g: float = Field(..., ge=0.0, le=300.0, description="Carbohydrates in grams")
    category: str = Field(default="Lunch", description="Meal category e.g. Breakfast, Lunch, Dinner, Snack")
    notes: str = Field(default="", max_length=250)


class LiveSimulationRequest(BaseModel):
    patient_id: str
    duration_hours: float = Field(default=12.0, ge=1.0, le=72.0)
    step_minutes: float = Field(default=5.0, ge=1.0, le=15.0)
    basal_insulin_mU_per_min: float = Field(default=15.0, ge=0.0, le=100.0)
    include_user_meals: bool = True
    initial_glucose_mgdL: float | None = None


class CreatePatientRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=100)
    age: int | None = Field(default=None, ge=1, le=120)
    weight_kg: float | None = Field(default=None, ge=1.0, le=500.0)
    height_cm: float | None = Field(default=None, ge=30.0, le=300.0)
    sex: str | None = Field(default=None, max_length=20)
    category: str = Field(default="Custom", max_length=60)
    notes: str = Field(default="", max_length=500)
    baseline_glucose_mgdL: float | None = Field(default=None, ge=20.0, le=600.0)


class UpdatePatientRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    age: int | None = Field(default=None, ge=1, le=120)
    weight_kg: float | None = Field(default=None, ge=1.0, le=500.0)
    height_cm: float | None = Field(default=None, ge=30.0, le=300.0)
    sex: str | None = Field(default=None, max_length=20)
    category: str | None = Field(default=None, max_length=60)
    notes: str | None = Field(default=None, max_length=500)
    baseline_glucose_mgdL: float | None = Field(default=None, ge=20.0, le=600.0)


# ---------------------------------------------------------------------------
# Storage Helpers
# ---------------------------------------------------------------------------
def _load_all_user_meals() -> dict[str, list[dict]]:
    if not USER_MEALS_PATH.exists():
        return {}
    try:
        import json
        with open(USER_MEALS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Failed to load user meals: %s", e)
        return {}


def _save_all_user_meals(data: dict[str, list[dict]]) -> None:
    try:
        import json
        USER_MEALS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(USER_MEALS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error("Failed to persist user meals: %s", e)


def _get_patient_user_meals(patient_id: str) -> list[dict]:
    all_meals = _load_all_user_meals()
    return all_meals.get(patient_id, [])


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _compute_trend_arrow(glucose_series: np.ndarray) -> str:
    """Simple 15-min trend from last 3 CGM points (5-min intervals)."""
    if len(glucose_series) < 3:
        return "→"
    delta = float(glucose_series[-1] - glucose_series[-3])
    if delta > 2.0:
        return "↑↑" if delta > 6.0 else "↑"
    if delta < -2.0:
        return "↓↓" if delta < -6.0 else "↓"
    return "→"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health_check():
    """
    Detailed system health check.
    Returns API status, dataset readiness, mechanistic model availability, and experiment artifacts.
    """
    processed_dir = REPO_ROOT / "data" / "processed"
    patient_files = sorted(processed_dir.glob("patient_*.parquet"))
    dataset_ready = len(patient_files) > 0

    # Test mechanistic model import
    mechanistic_ok = False
    try:
        from src.models.mechanistic import simulate_bergman, DEFAULT_BERGMAN_PARAMS  # noqa: F401
        mechanistic_ok = True
    except Exception:
        pass

    # Check experiment artifacts
    exp_dir = REPO_ROOT / "experiments"
    exp_plots = list(exp_dir.glob("phase*.png"))
    exp_reports = list(exp_dir.glob("phase*.md")) + list(exp_dir.glob("phase*.txt"))

    return {
        "status": "ok",
        "api": "online",
        "dataset_ready": dataset_ready,
        "patient_count": len(patient_files),
        "mechanistic_model": "available" if mechanistic_ok else "unavailable",
        "experiment_plots": len(exp_plots),
        "experiment_reports": len(exp_reports),
        "disclaimer": DISCLAIMER,
        "data_origin": "synthetic",
    }



# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
@app.get("/api/status")
def get_status():
    status_path = REPO_ROOT / "STATUS.md"
    if not status_path.exists():
        raise HTTPException(404, "STATUS.md not found")
    with open(status_path, "r", encoding="utf-8") as f:
        content = f.read()
    return {"status_md": content, "disclaimer": DISCLAIMER, "data_origin": "research_artifacts"}


# ---------------------------------------------------------------------------
# Overview dashboard  (patient-facing)
# ---------------------------------------------------------------------------
@app.get("/api/overview/{patient_id}")
def get_overview(patient_id: str, window_hours: int = Query(default=24, ge=1, le=72)):
    """
    Patient-facing dashboard data:
    - Last window_hours of CGM readings with timestamps
    - Summary metrics (TIR, TBR, TAR, mean, std, current glucose)
    - Trend arrow from last 3 readings
    - Meal event markers
    - 30-min mechanistic forecast from final observed state
    All data origin: synthetic.
    """
    processed_dir = REPO_ROOT / "data" / "processed"
    fpath = processed_dir / f"patient_{patient_id}.parquet"
    if not fpath.exists():
        raise HTTPException(404, f"Patient {patient_id} not found")

    df = pd.read_parquet(fpath)
    df = df.sort_values("timestamp").reset_index(drop=True)

    # Last window_hours
    n_points = int(window_hours * 60 / 5)  # 5-min intervals
    df_window = df.tail(n_points).reset_index(drop=True)

    g = df_window["glucose_mgdL"].values
    ts = df_window["timestamp"]
    meals = df_window["meal_cho_g"].values
    insulin = df_window["insulin_mU_per_min"].values

    # Metrics
    tir = float(np.mean((g >= 70) & (g <= 180)) * 100)
    tbr = float(np.mean(g < 70) * 100)
    tar = float(np.mean(g > 180) * 100)
    mean_g = float(np.mean(g))
    std_g = float(np.std(g))
    current_g = float(g[-1])
    trend = _compute_trend_arrow(g)

    # CGM trace
    cgm_trace = [
        {
            "t": row["timestamp"].isoformat(),
            "glucose_mgdL": round(float(row["glucose_mgdL"]), 1),
            "meal_cho_g": round(float(row["meal_cho_g"]), 1),
            "insulin_mU_per_min": round(float(row["insulin_mU_per_min"]), 2),
        }
        for _, row in df_window.iterrows()
    ]

    # Meal events for markers (both synthetic benchmark & user-logged)
    meal_events = [
        {
            "id": f"benchmark_{i}",
            "t": df_window.loc[i, "timestamp"].isoformat(),
            "cho_g": round(float(meals[i]), 1),
            "name": f"Recorded Intake ({round(float(meals[i]))}g)",
            "category": "Dataset Meal",
            "is_user_logged": False,
        }
        for i in range(len(df_window))
        if meals[i] > 0
    ]

    # Merge user-logged meals for this patient if within time window
    user_meals = _get_patient_user_meals(patient_id)
    if len(df_window) > 0:
        min_ts = df_window["timestamp"].min()
        max_ts = df_window["timestamp"].max()
        for um in user_meals:
            try:
                m_ts = pd.to_datetime(um["timestamp"])
                if min_ts <= m_ts <= max_ts:
                    meal_events.append({
                        "id": um.get("id", "user_meal"),
                        "t": m_ts.isoformat(),
                        "cho_g": round(float(um["cho_g"]), 1),
                        "name": um.get("name", "User Meal"),
                        "category": um.get("category", "User Meal"),
                        "notes": um.get("notes", ""),
                        "is_user_logged": True,
                    })
            except Exception:
                pass
    meal_events = sorted(meal_events, key=lambda x: x["t"])

    # 30-min mechanistic forecast from last observed state
    forecast = []
    try:
        from src.models.mechanistic import simulate_bergman, DEFAULT_BERGMAN_PARAMS

        last_g = float(g[-1])
        last_i = float(insulin[-1])
        params = dict(PATIENT_CALIBRATED_PARAMS.get(patient_id, DEFAULT_BERGMAN_PARAMS))
        u_fn = lambda t: last_i
        ra_fn = lambda t: 0.0  # no meal assumed for short forecast
        initial_state = [last_g, 0.0, params.get("Ib", 10.0)]

        t_fcast = np.arange(0.0, 31.0, 5.0)  # 0..30 min
        G_fcast, _, _ = simulate_bergman(t_fcast, params, u_fn, ra_fn, initial_state)

        last_ts = df_window["timestamp"].iloc[-1]
        for i, (dt_min, gv) in enumerate(zip(t_fcast[1:], G_fcast[1:])):  # skip t=0 (overlap with CGM)
            t_future = last_ts + pd.Timedelta(minutes=float(dt_min))
            forecast.append({
                "t": t_future.isoformat(),
                "glucose_mgdL": round(float(gv), 1),
                "is_forecast": True,
            })
    except Exception as exc:
        logger.warning("Forecast generation failed: %s", exc)

    return {
        "patient_id": patient_id,
        "data_origin": "synthetic",
        "disclaimer": DISCLAIMER,
        "units": "mg/dL",
        "window_hours": window_hours,
        "n_readings": len(cgm_trace),
        "current_glucose_mgdL": round(current_g, 1),
        "trend_arrow": trend,
        "metrics": {
            "mean_glucose_mgdL": round(mean_g, 1),
            "std_glucose_mgdL": round(std_g, 1),
            "tir_pct": round(tir, 1),
            "tbr_pct": round(tbr, 1),
            "tar_pct": round(tar, 1),
        },
        "cgm_trace": cgm_trace,
        "meal_events": meal_events,
        "forecast": forecast,
        "forecast_horizon_min": 30,
        "forecast_model": "Bergman Mechanistic ODE (experimental — synthetic only)",
    }


# ---------------------------------------------------------------------------
# List patients
# ---------------------------------------------------------------------------
@app.get("/api/patients")
def list_patients():
    processed_dir = REPO_ROOT / "data" / "processed"
    files = sorted(glob.glob(str(processed_dir / "patient_*.parquet")))
    patients = []
    # 1. Synthetic benchmark patients from parquet files
    for f in files:
        pid = Path(f).stem.replace("patient_", "")
        df = pd.read_parquet(f)
        g = df["glucose_mgdL"].values
        label_meta = SYNTHETIC_LABELS.get(pid, {})
        patients.append({
            "id": pid,
            "display_name": label_meta.get("display_name", f"Synthetic Patient {pid.split('_')[-1]}"),
            "label": label_meta.get("display_name", f"Synthetic Patient {pid.split('_')[-1]}"),
            "category": label_meta.get("category", "Synthetic Cohort"),
            "notes": label_meta.get("notes", ""),
            "data_origin": "synthetic",
            "source": "benchmark",
            "is_synthetic": True,
            "mean_glucose_mgdL": round(float(np.mean(g)), 1),
            "tir_pct": round(float(np.mean((g >= 70) & (g <= 180)) * 100), 1),
            "n_readings": len(df),
            "age": None, "weight_kg": None, "height_cm": None, "bmi": None,
        })
    # 2. User-created profiles from SQLite
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM patients WHERE is_synthetic = 0 ORDER BY created_at DESC"
        ).fetchall()
        for row in rows:
            d = _row_to_patient_dict(row)
            patients.append({
                "id": d["id"],
                "display_name": d["display_name"],
                "label": d["display_name"],
                "category": d.get("category", "Custom"),
                "notes": d.get("notes", ""),
                "data_origin": "user_created",
                "source": "user",
                "is_synthetic": False,
                "age": d.get("age"),
                "weight_kg": d.get("weight_kg"),
                "height_cm": d.get("height_cm"),
                "bmi": d.get("bmi"),
                "sex": d.get("sex"),
                "baseline_glucose_mgdL": d.get("baseline_glucose_mgdL"),
                "mean_glucose_mgdL": None,
                "tir_pct": None,
                "n_readings": 0,
                "created_at": d.get("created_at"),
            })
    finally:
        conn.close()
    return {
        "patients": patients,
        "count": len(patients),
        "disclaimer": DISCLAIMER,
        "data_origin": "mixed",
    }


# ---------------------------------------------------------------------------
# Patient profile — GET/CREATE/UPDATE/DELETE single patient
# ---------------------------------------------------------------------------
@app.get("/api/patients/{patient_id}/profile")
def get_patient_profile(patient_id: str):
    """Get full profile for a patient (synthetic benchmark or user-created)."""
    processed_dir = REPO_ROOT / "data" / "processed"
    fpath = processed_dir / f"patient_{patient_id}.parquet"
    if fpath.exists():
        label_meta = SYNTHETIC_LABELS.get(patient_id, {})
        params = PATIENT_CALIBRATED_PARAMS.get(patient_id, {})
        return {
            "id": patient_id,
            "display_name": label_meta.get("display_name", f"Synthetic Patient {patient_id}"),
            "category": label_meta.get("category", "Synthetic Cohort"),
            "notes": label_meta.get("notes", ""),
            "data_origin": "synthetic",
            "source": "benchmark",
            "is_synthetic": True,
            "age": None, "weight_kg": None, "height_cm": None, "bmi": None, "sex": None,
            "baseline_glucose_mgdL": float(params.get("Gb", 100.0)) if params else None,
            "disclaimer": DISCLAIMER,
        }
    conn = _get_db()
    try:
        row = conn.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"Patient {patient_id} not found")
        d = _row_to_patient_dict(row)
        d["disclaimer"] = DISCLAIMER
        return d
    finally:
        conn.close()


@app.post("/api/patients", status_code=201)
def create_patient(req: CreatePatientRequest):
    """Create a new user patient profile and persist it in SQLite."""
    patient_id = f"custom_{uuid.uuid4().hex[:10]}"
    now = pd.Timestamp.now().isoformat()
    conn = _get_db()
    try:
        conn.execute(
            """
            INSERT INTO patients
              (id, display_name, age, weight_kg, height_cm, sex, category,
               notes, baseline_glucose_mgdL, is_synthetic, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'user_created', ?, ?)
            """,
            (
                patient_id, req.display_name.strip(), req.age,
                req.weight_kg, req.height_cm,
                req.sex.strip() if req.sex else None,
                req.category.strip() or "Custom",
                req.notes.strip(), req.baseline_glucose_mgdL,
                now, now,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
        d = _row_to_patient_dict(row)
        d["disclaimer"] = DISCLAIMER
        return d
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, f"Patient ID conflict: {exc}") from exc
    finally:
        conn.close()


@app.put("/api/patients/{patient_id}")
def update_patient(patient_id: str, req: UpdatePatientRequest):
    """Update a user-created patient profile."""
    conn = _get_db()
    try:
        row = conn.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"Patient {patient_id} not found.")
        if row["is_synthetic"]:
            raise HTTPException(403, "Cannot modify synthetic benchmark patient profiles.")
        updates: dict = {}
        if req.display_name is not None:
            updates["display_name"] = req.display_name.strip()
        if req.age is not None:
            updates["age"] = req.age
        if req.weight_kg is not None:
            updates["weight_kg"] = req.weight_kg
        if req.height_cm is not None:
            updates["height_cm"] = req.height_cm
        if req.sex is not None:
            updates["sex"] = req.sex.strip()
        if req.category is not None:
            updates["category"] = req.category.strip()
        if req.notes is not None:
            updates["notes"] = req.notes.strip()
        if req.baseline_glucose_mgdL is not None:
            updates["baseline_glucose_mgdL"] = req.baseline_glucose_mgdL
        if not updates:
            d = _row_to_patient_dict(row)
            d["disclaimer"] = DISCLAIMER
            return d
        updates["updated_at"] = pd.Timestamp.now().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [patient_id]
        conn.execute(f"UPDATE patients SET {set_clause} WHERE id = ?", values)
        conn.commit()
        row = conn.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
        d = _row_to_patient_dict(row)
        d["disclaimer"] = DISCLAIMER
        return d
    finally:
        conn.close()


@app.delete("/api/patients/{patient_id}")
def delete_patient(patient_id: str):
    """Delete a user-created patient and their associated meal records."""
    conn = _get_db()
    try:
        row = conn.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"Patient {patient_id} not found.")
        if row["is_synthetic"]:
            raise HTTPException(403, "Cannot delete synthetic benchmark patients.")
        conn.execute("DELETE FROM patients WHERE id = ?", (patient_id,))
        conn.commit()
        all_meals = _load_all_user_meals()
        if patient_id in all_meals:
            del all_meals[patient_id]
            _save_all_user_meals(all_meals)
        return {"message": f"Patient {patient_id} deleted.", "disclaimer": DISCLAIMER}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Patient trace (full)
# ---------------------------------------------------------------------------
@app.get("/api/patients/{patient_id}/trace")
def get_patient_trace(patient_id: str, downsample: int = Query(default=1, ge=1, le=12)):
    processed_dir = REPO_ROOT / "data" / "processed"
    fpath = processed_dir / f"patient_{patient_id}.parquet"
    if not fpath.exists():
        raise HTTPException(404, f"Patient {patient_id} not found")
    df = pd.read_parquet(fpath)
    if downsample > 1:
        df = df.iloc[::downsample].reset_index(drop=True)
    g = df["glucose_mgdL"].values
    records = [
        {
            "t": row["timestamp"].isoformat(),
            "glucose_mgdL": round(float(row["glucose_mgdL"]), 1),
            "meal_cho_g": round(float(row.get("meal_cho_g", 0)), 1),
        }
        for _, row in df.iterrows()
    ]
    return {
        "patient_id": patient_id,
        "data_origin": "synthetic",
        "disclaimer": DISCLAIMER,
        "units": "mg/dL",
        "n_readings": len(records),
        "metrics": {
            "mean_glucose_mgdL": round(float(np.mean(g)), 1),
            "std_glucose_mgdL": round(float(np.std(g)), 1),
            "tir_pct": round(float(np.mean((g >= 70) & (g <= 180)) * 100), 1),
            "tbr_pct": round(float(np.mean(g < 70) * 100), 1),
            "tar_pct": round(float(np.mean(g > 180) * 100), 1),
        },
        "trace": records,
    }


# ---------------------------------------------------------------------------
# Experiments list
# ---------------------------------------------------------------------------
@app.get("/api/experiments")
def list_experiments():
    exp_dir = REPO_ROOT / "experiments"
    phases = [
        {"id": "phase2", "title": "Phase 2: Bergman Minimal Model Fit", "description": "Multi-start least-squares fitting of ODE parameters to CGM data.", "plot": "phase2_fit.png", "report": "phase2_bergman_fit.txt", "data_origin": "synthetic", "status": "done"},
        {"id": "phase3", "title": "Phase 3: Parameter Calibration & Identifiability", "description": "Per-patient calibration with profile-likelihood 95% CIs and sensitivity analysis.", "plot": "phase3_identifiability.png", "report": "phase3_identifiability_report.md", "data_origin": "synthetic", "status": "done"},
        {"id": "phase4", "title": "Phase 4: Hybrid Residual Model Forecasting", "description": "Physics-ML hybrid GRU corrects mechanistic drift at 30/60/120-min horizons.", "plot": "phase4_horizons.png", "report": "phase4_metrics_report.md", "data_origin": "estimated", "status": "done"},
        {"id": "phase5", "title": "Phase 5: Extended Kalman Filter State Estimation", "description": "EKF tracks glucose + insulin-sensitivity in real time from CGM readings.", "plot": "phase5_kalman.png", "report": "phase5_ekf_report.md", "data_origin": "estimated", "status": "done"},
        {"id": "phase6", "title": "Phase 6: UVA/Padova Simulator Validation", "description": "Twin fitted to simglucose benchmark across adult, adolescent, and child cohorts.", "plot": "phase6_cohort_match.png", "report": "phase6_validation_report.md", "data_origin": "simulated", "status": "done"},
        {"id": "phase7", "title": "Phase 7: Model Predictive Controller", "description": "Receding-horizon MPC with SLSQP optimizer and hard hypoglycemia safety floor.", "plot": "phase7_mpc.png", "report": "phase7_mpc_report.md", "data_origin": "simulated", "status": "done"},
        {"id": "phase8", "title": "Phase 8: Reinforcement Learning Baseline", "description": "PPO agent trained in Bergman gym env vs MPC and fixed-schedule baselines.", "plot": "phase8_control_comparison.png", "report": "phase8_control_report.md", "data_origin": "simulated", "status": "done"},
    ]
    for p in phases:
        p["plot_available"] = (exp_dir / p["plot"]).exists()
        p["report_available"] = (exp_dir / p["report"]).exists()
    return {"phases": phases, "disclaimer": DISCLAIMER, "data_origin": "research_artifacts"}


# ---------------------------------------------------------------------------
# Serve experiment image
# ---------------------------------------------------------------------------
@app.get("/api/experiments/{filename}/image")
def get_experiment_image(filename: str):
    safe_name = Path(filename).name
    if not safe_name.endswith(".png"):
        raise HTTPException(400, "Only PNG files served")
    img_path = REPO_ROOT / "experiments" / safe_name
    if not img_path.exists():
        raise HTTPException(404, f"Image {safe_name} not found")
    return FileResponse(str(img_path), media_type="image/png")


# ---------------------------------------------------------------------------
# Serve experiment report
# ---------------------------------------------------------------------------
@app.get("/api/experiments/{filename}/report")
def get_experiment_report(filename: str):
    safe_name = Path(filename).name
    report_path = REPO_ROOT / "experiments" / safe_name
    if not report_path.exists():
        raise HTTPException(404, f"Report {safe_name} not found")
    with open(report_path, "r", encoding="utf-8") as f:
        content = f.read()
    return {"filename": safe_name, "content": content, "disclaimer": DISCLAIMER, "data_origin": "research_artifacts"}


# ---------------------------------------------------------------------------
# Simulator cohort trace
# ---------------------------------------------------------------------------
@app.get("/api/simulator/{cohort}/{patient_name}/trace")
def get_simulator_trace(cohort: str, patient_name: str):
    allowed = {"adult", "adolescent", "child"}
    if cohort not in allowed:
        raise HTTPException(400, f"cohort must be one of {allowed}")
    try:
        from src.data.loaders import SimGlucoseLoader
        from src.data.preprocessor import Preprocessor
        loader = SimGlucoseLoader(seed=42)
        raw_df = loader.load_patient(patient_name, duration_hours=24.0, seed=42)
        preproc = Preprocessor(dt_minutes=5.0)
        df = preproc.transform(raw_df)
        g = df["glucose_mgdL"].values
        ts_col = "timestamp" if "timestamp" in df.columns else df.columns[0]
        records = [
            {"t": str(row[ts_col]), "glucose_mgdL": round(float(row["glucose_mgdL"]), 1)}
            for _, row in df.iterrows()
        ]
        return {
            "patient_name": patient_name, "cohort": cohort,
            "data_origin": "simulated (UVA/Padova - simglucose)",
            "disclaimer": DISCLAIMER, "units": "mg/dL",
            "n_readings": len(records),
            "metrics": {
                "mean_glucose_mgdL": round(float(np.mean(g)), 1),
                "tir_pct": round(float(np.mean((g >= 70) & (g <= 180)) * 100), 1),
                "tbr_pct": round(float(np.mean(g < 70) * 100), 1),
                "tar_pct": round(float(np.mean(g > 180) * 100), 1),
            },
            "trace": records,
        }
    except Exception as exc:
        raise HTTPException(503, f"Simulator unavailable: {exc}") from exc


# ---------------------------------------------------------------------------
# What-If scenarios
# ---------------------------------------------------------------------------
@app.post("/api/whatif")
def run_whatif(req: WhatIfRequest):
    try:
        from src.models.mechanistic import simulate_bergman, DEFAULT_BERGMAN_PARAMS
        dt_min = 5.0
        t_eval = np.arange(0.0, req.duration_hours * 60.0 + 1e-6, dt_min)
        meal_time_min = req.meal_time_h * 60.0
        absorption_peak_min = 30.0

        def ra_fn(t: float) -> float:
            if meal_time_min <= t <= meal_time_min + 2 * absorption_peak_min:
                frac = (t - meal_time_min) / absorption_peak_min
                shape = frac if frac <= 1.0 else 2.0 - frac
                return max(0.0, shape * req.meal_cho_g * 10.0 / absorption_peak_min)
            return 0.0

        def u_fn(t: float) -> float:
            base = req.basal_insulin_mU_per_min
            if meal_time_min <= t < meal_time_min + dt_min and req.bolus_insulin_mU > 0:
                return base + req.bolus_insulin_mU / dt_min
            return base

        params = dict(DEFAULT_BERGMAN_PARAMS)
        G, _, _ = simulate_bergman(t_eval, params, u_fn, ra_fn, [params["Gb"], 0.0, params["Ib"]])
        trace = [{"t_min": round(float(t), 1), "glucose_mgdL": round(float(gv), 1)} for t, gv in zip(t_eval, G)]
        return {
            "scenario_name": req.scenario_name,
            "data_origin": "synthetic",
            "disclaimer": DISCLAIMER,
            "units": "mg/dL",
            "metrics": {
                "tir_pct": round(float(np.mean((G >= 70) & (G <= 180)) * 100), 1),
                "tbr_pct": round(float(np.mean(G < 70) * 100), 1),
                "peak_glucose_mgdL": round(float(np.max(G)), 1),
                "min_glucose_mgdL": round(float(np.min(G)), 1),
                "mean_glucose_mgdL": round(float(np.mean(G)), 1),
            },
            "trace": trace,
            "meal_time_h": req.meal_time_h,
            "meal_cho_g": req.meal_cho_g,
            "bolus_insulin_mU": req.bolus_insulin_mU,
        }
    except Exception as exc:
        logger.error("What-if failed: %s", exc, exc_info=True)
        raise HTTPException(500, f"What-if failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Patient Parameters & Scientific Identifiability (SI / SG)
# ---------------------------------------------------------------------------
@app.get("/api/patients/{patient_id}/parameters")
def get_patient_parameters(patient_id: str):
    """
    Returns the calibrated Bergman Minimal Model parameters,
    identifiability confidence intervals, and plain-English scientific explanations.
    """
    if patient_id not in PATIENT_CALIBRATED_PARAMS:
        raise HTTPException(404, f"Parameters not found for patient {patient_id}")

    p = PATIENT_CALIBRATED_PARAMS[patient_id]
    p1 = p["p1"]
    p2 = p["p2"]
    p3 = p["p3"]
    n = p["n"]
    si = p3 / p2 if p2 > 0 else 0.0
    sg = p1

    return {
        "patient_id": patient_id,
        "calibration_status": "calibrated",
        "data_origin": "synthetic",
        "disclaimer": DISCLAIMER,
        "parameters": {
            "p1_Sg": round(float(p1), 6),
            "p2": round(float(p2), 6),
            "p3": round(float(p3), 8),
            "n": round(float(n), 4),
            "Gb": round(float(p.get("Gb", 100.0)), 1),
            "Ib": round(float(p.get("Ib", 10.0)), 1),
            "Vg": round(float(p.get("Vg", 117.0)), 1),
            "Vi": round(float(p.get("Vi", 12.0)), 1),
            "rmse_calibrated": p.get("rmse_calibrated", 0.0),
        },
        "si_estimate": {
            "value": round(float(si), 6),
            "formatted": f"{si:.2e}" if si < 0.001 else f"{si:.5f}",
            "unit": "L / (mU · min)",
            "symbol": "S_I",
            "name": "Insulin Sensitivity",
            "plain_english": "Model-estimated composite parameter (p₃/p₂). Represents the net effect of insulin action on glucose disposal under Bergman Minimal Model assumptions. Higher values indicate stronger estimated insulin-mediated glucose clearance in this synthetic model — not a direct clinical ISI measurement.",
        },
        "sg_estimate": {
            "value": round(float(sg), 6),
            "formatted": f"{sg:.4f}",
            "unit": "1 / min",
            "symbol": "S_G",
            "name": "Glucose Effectiveness",
            "plain_english": "Model parameter p₁ from the Bergman Minimal Model. Represents the rate at which glucose itself (independent of insulin) promotes its own disposal and suppresses hepatic glucose production. Calibrated to synthetic data — not a measured clinical value.",
        },
        "model_context": (
            "The Bergman Minimal Model uses differential equations to represent glucose and insulin dynamics. "
            "These values are model parameter estimates calibrated from benchmark data, not direct clinical measurements."
        ),
    }


# ---------------------------------------------------------------------------
# User Nutrition Management (Log a Meal)
# ---------------------------------------------------------------------------
@app.get("/api/patients/{patient_id}/meals")
def get_user_meals(patient_id: str):
    """Returns all user-logged meals for a patient."""
    meals = _get_patient_user_meals(patient_id)
    return {
        "patient_id": patient_id,
        "meals": meals,
        "count": len(meals),
        "disclaimer": DISCLAIMER,
    }


@app.post("/api/patients/{patient_id}/meals")
def add_user_meal(patient_id: str, req: MealInput):
    """
    Saves a user-entered meal with validation and links it to the selected patient.
    """
    import uuid
    # Validate timestamp
    try:
        ts = pd.to_datetime(req.timestamp)
    except Exception as e:
        raise HTTPException(400, f"Invalid date/time format: {e}") from e

    meal_id = f"meal_{uuid.uuid4().hex[:8]}"
    new_meal = {
        "id": meal_id,
        "patient_id": patient_id,
        "name": req.name.strip(),
        "timestamp": ts.isoformat(),
        "cho_g": round(float(req.cho_g), 1),
        "category": req.category.strip(),
        "notes": req.notes.strip(),
        "created_at": pd.Timestamp.now().isoformat(),
        "is_user_logged": True,
    }

    all_meals = _load_all_user_meals()
    if patient_id not in all_meals:
        all_meals[patient_id] = []
    all_meals[patient_id].append(new_meal)
    _save_all_user_meals(all_meals)

    return {
        "message": "Meal saved successfully",
        "meal": new_meal,
        "disclaimer": DISCLAIMER,
    }


@app.delete("/api/patients/{patient_id}/meals/{meal_id}")
def delete_user_meal(patient_id: str, meal_id: str):
    """Deletes a user-logged meal."""
    all_meals = _load_all_user_meals()
    patient_meals = all_meals.get(patient_id, [])
    initial_len = len(patient_meals)
    filtered = [m for m in patient_meals if m.get("id") != meal_id]
    if len(filtered) == initial_len:
        raise HTTPException(404, f"Meal {meal_id} not found for patient {patient_id}")

    all_meals[patient_id] = filtered
    _save_all_user_meals(all_meals)
    return {"message": "Meal removed successfully", "meal_id": meal_id, "disclaimer": DISCLAIMER}


# ---------------------------------------------------------------------------
# Live Simulation Execution
# ---------------------------------------------------------------------------
@app.post("/api/simulation/run")
def run_live_simulation(req: LiveSimulationRequest):
    """
    Executes a time-stepped numerical simulation of the patient's calibrated Bergman ODE model.
    Incorporates both benchmark dataset meals and user-logged meals.
    """
    # Validate patient existence BEFORE the broad try/except to preserve 404 status
    processed_dir = REPO_ROOT / "data" / "processed"
    fpath = processed_dir / f"patient_{req.patient_id}.parquet"
    if not fpath.exists():
        raise HTTPException(404, f"Patient {req.patient_id} not found")

    try:
        from src.models.mechanistic import simulate_bergman, DEFAULT_BERGMAN_PARAMS


        df = pd.read_parquet(fpath).sort_values("timestamp").reset_index(drop=True)
        start_ts = df["timestamp"].iloc[0]
        duration_min = float(req.duration_hours * 60.0)
        dt_min = float(req.step_minutes)
        t_eval = np.arange(0.0, duration_min + 1e-6, dt_min)

        # Baseline glucose
        g_0 = float(req.initial_glucose_mgdL) if req.initial_glucose_mgdL is not None else float(df["glucose_mgdL"].iloc[0])

        # Get calibrated parameters
        params = dict(PATIENT_CALIBRATED_PARAMS.get(req.patient_id, DEFAULT_BERGMAN_PARAMS))

        # Collect all meal events
        meal_schedule: list[dict] = []
        # Benchmark dataset meals within window
        for _, row in df.iterrows():
            m_cho = float(row.get("meal_cho_g", 0.0))
            if m_cho > 0:
                rel_min = (row["timestamp"] - start_ts).total_seconds() / 60.0
                if 0 <= rel_min <= duration_min:
                    meal_schedule.append({
                        "t_min": rel_min,
                        "cho_g": m_cho,
                        "name": "Dataset Intake",
                        "is_user_logged": False,
                    })

        # User-logged meals
        if req.include_user_meals:
            user_meals = _get_patient_user_meals(req.patient_id)
            for um in user_meals:
                try:
                    m_dt = pd.to_datetime(um["timestamp"])
                    rel_min = (m_dt - start_ts).total_seconds() / 60.0
                    if 0 <= rel_min <= duration_min:
                        meal_schedule.append({
                            "t_min": rel_min,
                            "cho_g": float(um["cho_g"]),
                            "name": um.get("name", "User Meal"),
                            "is_user_logged": True,
                        })
                except Exception:
                    pass

        # Sort meals by time
        meal_schedule = sorted(meal_schedule, key=lambda x: x["t_min"])

        # Construct meal appearance rate Ra(t)
        # Standard gut absorption triangular curve (peak at 30 min, duration 60 min, 80% bioavailability)
        peak_min = 30.0
        ag_bioavail = 0.8

        def ra_fn(t: float) -> float:
            tot_ra = 0.0
            for m in meal_schedule:
                tm = m["t_min"]
                if tm <= t <= tm + 2 * peak_min:
                    frac = (t - tm) / peak_min
                    shape = frac if frac <= 1.0 else 2.0 - frac
                    tot_ra += max(0.0, shape * (m["cho_g"] * 1000.0 * ag_bioavail) / peak_min)
            return tot_ra

        def u_fn(t: float) -> float:
            return float(req.basal_insulin_mU_per_min)

        initial_state = [g_0, 0.0, float(params.get("Ib", 10.0))]
        G_sim, X_sim, I_sim = simulate_bergman(t_eval, params, u_fn, ra_fn, initial_state)

        # Build step records
        steps = []
        for i, (t, gv) in enumerate(zip(t_eval, G_sim)):
            point_ts = start_ts + pd.Timedelta(minutes=float(t))
            steps.append({
                "step": i,
                "t_min": round(float(t), 1),
                "timestamp": point_ts.isoformat(),
                "glucose_mgdL": round(float(gv), 1),
                "is_simulated": True,
            })

        # Calculate simulation metrics
        g_arr = np.array(G_sim)
        tir = float(np.mean((g_arr >= 70) & (g_arr <= 180)) * 100)
        tbr = float(np.mean(g_arr < 70) * 100)
        tar = float(np.mean(g_arr > 180) * 100)

        si = params["p3"] / params["p2"] if params["p2"] > 0 else 0.0
        sg = params["p1"]

        # Derived metrics
        peak_idx = int(np.argmax(g_arr))
        peak_glucose = float(g_arr[peak_idx])
        time_to_peak_h = float(t_eval[peak_idx]) / 60.0
        baseline_g = float(g_arr[0])
        return_idx = None
        for idx in range(peak_idx + 1, len(g_arr)):
            if abs(g_arr[idx] - baseline_g) <= 5.0:
                return_idx = idx
                break
        time_to_return_h = float(t_eval[return_idx]) / 60.0 if return_idx is not None else None

        return {
            "patient_id": req.patient_id,
            "data_origin": "simulated",
            "disclaimer": DISCLAIMER,
            "units": "mg/dL",
            "duration_hours": req.duration_hours,
            "step_minutes": dt_min,
            "n_steps": len(steps),
            "parameters": {
                "p1_Sg": round(float(sg), 6),
                "p2": round(float(params["p2"]), 6),
                "p3": round(float(params["p3"]), 8),
                "n": round(float(params["n"]), 4),
                "Si": round(float(si), 6),
                "Sg": round(float(sg), 6),
            },
            "summary": {
                "initial_glucose_mgdL": round(float(g_arr[0]), 1),
                "final_glucose_mgdL": round(float(g_arr[-1]), 1),
                "min_glucose_mgdL": round(float(np.min(g_arr)), 1),
                "max_glucose_mgdL": round(float(np.max(g_arr)), 1),
                "mean_glucose_mgdL": round(float(np.mean(g_arr)), 1),
                "std_glucose_mgdL": round(float(np.std(g_arr)), 1),
                "tir_pct": round(tir, 1),
                "tbr_pct": round(tbr, 1),
                "tar_pct": round(tar, 1),
                "peak_glucose_mgdL": round(peak_glucose, 1),
                "time_to_peak_h": round(time_to_peak_h, 2),
                "time_to_return_h": round(time_to_return_h, 2) if time_to_return_h else None,
                "total_meals_ingested": len(meal_schedule),
                "total_carbs_g": round(sum(m["cho_g"] for m in meal_schedule), 1),
            },
            "trace": steps,
            "meals": meal_schedule,
        }
    except Exception as exc:
        logger.error("Live simulation failed: %s", exc, exc_info=True)
        raise HTTPException(500, f"Simulation failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------
@app.get("/api/privacy")
def get_privacy():
    return {
        "summary": "This application runs entirely on your local machine. No data is sent to any external server.",
        "data_stored": [
            "Synthetic patient trajectories (generated locally, not real patient data)",
            "User-created patient profiles (stored in local SQLite database at data/processed/patients.db)",
            "User-logged meal records (stored in local JSON file at data/processed/user_meals.json)",
            "Experiment artifacts (plots and reports generated by local research scripts)",
        ],
        "data_not_stored": [
            "Real patient glucose readings",
            "Personal health information",
            "Insulin dosing decisions",
        ],
        "network": "No external network calls. All computation runs locally.",
        "disclaimer": DISCLAIMER,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=True)
