"""Snapshot tests for ratings.bias_multiplier.

These exist to lock in the current numerical output before the
sectioning refactor (curve-omission patches / context priors / actor
signals). The refactor must not change any value here.

If a future change INTENTIONALLY alters a multiplier (new research
finding, recalibrated A/E, etc.), update the expected value with a
comment pointing to the source.
"""

import sys
from pathlib import Path

# Make the source tree importable without installing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from sim.ratings import BASELINE_AE, bias_multiplier


def _row(**kwargs) -> dict:
    """Build a bias-row dict; missing keys default to None/0/False."""
    base = {
        "first_time_lasix": False,
        "blinkers_off": False,
        "first_time_blinkers": False,
        "off_turf": False,
        "jockey_switch_type": "SAME",
        "jockey_allowance": 0,
        "surface_switch": False,
        "prev_surface": "",
        "surface": "",
        "class_move": "SAME",
        "claimed_last_race": False,
        "trainer_claim_ae": None,
        "trainer_claim_starts": 0,
        "is_fts": False,
        "trainer_fts_ae": None,
        "trainer_fts_starts": 0,
        "is_layoff": False,
        "trainer_layoff_ae": None,
        "trainer_layoff_starts": 0,
        "trainer_switch_ae": None,
        "trainer_switch_starts": 0,
        "trainer_drop_ae": None,
        "trainer_drop_starts": 0,
    }
    base.update(kwargs)
    return base


def test_baseline_no_signals_returns_one():
    assert bias_multiplier(_row()) == pytest.approx(1.0)


# ─── Curve-omission patches ─────────────────────────────────────────────

def test_off_turf_credit_only_applies_to_favorite():
    row = _row(off_turf=True)
    assert bias_multiplier(row, is_favorite=False) == pytest.approx(1.0)
    assert bias_multiplier(row, is_favorite=True) == pytest.approx(1.075)


def test_surface_switch_synth_to_turf():
    row = _row(surface_switch=True, prev_surface="Synthetic", surface="Turf")
    assert bias_multiplier(row) == pytest.approx(1.075)


def test_surface_switch_turf_to_dirt():
    row = _row(surface_switch=True, prev_surface="Turf", surface="Dirt")
    assert bias_multiplier(row) == pytest.approx(0.969)


def test_surface_switch_unknown_pair_no_lift():
    # Dirt → Turf is not in the switch table; multiplier stays 1.0
    row = _row(surface_switch=True, prev_surface="Dirt", surface="Turf")
    assert bias_multiplier(row) == pytest.approx(1.0)


# ─── Race-day context priors ────────────────────────────────────────────

def test_class_drop_generic():
    assert bias_multiplier(_row(class_move="DROP")) == pytest.approx(1.029)


def test_class_rise_generic():
    assert bias_multiplier(_row(class_move="RISE")) == pytest.approx(0.961)


# ─── Race-day actor signals: equipment / medication ─────────────────────

def test_first_time_lasix():
    assert bias_multiplier(_row(first_time_lasix=True)) == pytest.approx(1.022)


def test_blinkers_off():
    assert bias_multiplier(_row(blinkers_off=True)) == pytest.approx(1.101)


def test_first_time_blinkers():
    assert bias_multiplier(_row(first_time_blinkers=True)) == pytest.approx(0.970)


# ─── Race-day actor signals: jockey ─────────────────────────────────────

def test_jockey_upgrade():
    assert bias_multiplier(_row(jockey_switch_type="UPGRADE")) == pytest.approx(1.051)


def test_jockey_downgrade():
    assert bias_multiplier(_row(jockey_switch_type="DOWNGRADE")) == pytest.approx(0.888)


def test_jockey_5lb_allowance():
    assert bias_multiplier(_row(jockey_allowance=5)) == pytest.approx(1.031)


# ─── Race-day actor signals: trainer ────────────────────────────────────

def test_trainer_claim_with_record_overrides_population():
    # Trainer A/E available with sufficient sample → use it (not the 1.034
    # population fallback).
    row = _row(claimed_last_race=True, trainer_claim_ae=1.4, trainer_claim_starts=20)
    assert bias_multiplier(row) == pytest.approx(1.4 / BASELINE_AE)


def test_trainer_claim_no_record_uses_population_fallback():
    row = _row(claimed_last_race=True, trainer_claim_ae=None, trainer_claim_starts=0)
    assert bias_multiplier(row) == pytest.approx(1.034)


def test_trainer_claim_low_sample_uses_population_fallback():
    # Sample below MIN_TRAINER_SAMPLE (10) → fall back to population.
    row = _row(claimed_last_race=True, trainer_claim_ae=1.4, trainer_claim_starts=5)
    assert bias_multiplier(row) == pytest.approx(1.034)


def test_trainer_fts_with_record():
    row = _row(is_fts=True, trainer_fts_ae=1.2, trainer_fts_starts=15)
    assert bias_multiplier(row) == pytest.approx(1.2 / BASELINE_AE)


def test_trainer_fts_low_sample_uses_population_negative():
    # Population FTS A/E = 0.776 / BASELINE_AE shows FTS overbet
    row = _row(is_fts=True, trainer_fts_starts=5)
    assert bias_multiplier(row) == pytest.approx(0.970)


def test_trainer_layoff_with_record():
    row = _row(is_layoff=True, trainer_layoff_ae=1.3, trainer_layoff_starts=20)
    assert bias_multiplier(row) == pytest.approx(1.3 / BASELINE_AE)


def test_trainer_layoff_low_sample_no_change():
    # Layoff alone has no population fallback — only fires if trainer
    # has a record.
    row = _row(is_layoff=True, trainer_layoff_ae=1.5, trainer_layoff_starts=5)
    assert bias_multiplier(row) == pytest.approx(1.0)


# ─── Override patterns (RDS-T1.4 fix) ───────────────────────────────────

def test_trainer_surface_switch_overrides_generic():
    # Generic Synth→Turf would apply 1.075. Trainer A/E 1.0 with sample
    # ≥10 should REPLACE the generic, not stack on it. End multiplier =
    # 1.0 / BASELINE_AE = 1.0.
    row = _row(
        surface_switch=True, prev_surface="Synthetic", surface="Turf",
        trainer_switch_ae=1.0, trainer_switch_starts=15,
    )
    assert bias_multiplier(row) == pytest.approx(1.0 / BASELINE_AE)


def test_trainer_surface_switch_low_sample_keeps_generic():
    # Sample < 10 → trainer override doesn't fire; keep generic 1.075.
    row = _row(
        surface_switch=True, prev_surface="Synthetic", surface="Turf",
        trainer_switch_ae=1.0, trainer_switch_starts=5,
    )
    assert bias_multiplier(row) == pytest.approx(1.075)


def test_trainer_class_drop_overrides_generic():
    # Generic class drop would apply 1.029. Trainer drop A/E 1.2 with
    # sample ≥10 replaces it: end multiplier = 1.2 / BASELINE_AE.
    row = _row(class_move="DROP", trainer_drop_ae=1.2, trainer_drop_starts=20)
    assert bias_multiplier(row) == pytest.approx(1.2 / BASELINE_AE)


def test_trainer_class_drop_low_sample_keeps_generic():
    row = _row(class_move="DROP", trainer_drop_ae=1.2, trainer_drop_starts=5)
    assert bias_multiplier(row) == pytest.approx(1.029)


# ─── Composition tests (multiple multipliers stacking) ──────────────────

def test_lasix_and_blinkers_off_stack():
    row = _row(first_time_lasix=True, blinkers_off=True)
    assert bias_multiplier(row) == pytest.approx(1.022 * 1.101)


def test_full_stack_independent_signals():
    # FTS new trainer, with trainer record, plus lasix, plus jockey upgrade.
    # All independent signals — should multiply cleanly.
    row = _row(
        first_time_lasix=True,
        jockey_switch_type="UPGRADE",
        is_fts=True,
        trainer_fts_ae=1.5,
        trainer_fts_starts=20,
    )
    expected = 1.022 * 1.051 * (1.5 / BASELINE_AE)
    assert bias_multiplier(row) == pytest.approx(expected)


def test_off_turf_with_class_drop_favorite():
    # Favorite, off-turf, also dropping in class.
    row = _row(off_turf=True, class_move="DROP")
    expected = 1.075 * 1.029
    assert bias_multiplier(row, is_favorite=True) == pytest.approx(expected)
