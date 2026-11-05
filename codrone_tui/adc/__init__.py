"""Aerial Drone Competition 2026-27 "Fast Track" support: scoring, match clock, field, routines.

Write user routines with ``from codrone_tui.adc.routine import routine``. The decorator is not
re-exported here on purpose: a package attribute called ``routine`` would shadow the
``codrone_tui.adc.routine`` module.
"""

from .field import Element, FieldConfig, default_field, load_field
from .match import AttemptResult, MatchController, MatchError
from .preflight import ITEMS as PREFLIGHT_ITEMS
from .preflight import PreflightItem, PreflightResult, preflight_ok, run_preflight
from .routine import (
    DryRunReport,
    Routine,
    RoutineContext,
    RoutineError,
    builtin_routines,
    dry_run,
    load_routines,
)
from .rules import (
    LANDINGS,
    MATCH_SECONDS,
    MAX_ATTEMPTS,
    PILOTING_PHASES,
    TASKS,
    CreditEvent,
    CreditResult,
    PilotingPhase,
    Row,
    Scorecard,
    TaskDef,
    max_score,
)

__all__ = [
    "AttemptResult",
    "CreditEvent",
    "CreditResult",
    "DryRunReport",
    "Element",
    "FieldConfig",
    "LANDINGS",
    "MATCH_SECONDS",
    "MAX_ATTEMPTS",
    "MatchController",
    "MatchError",
    "PILOTING_PHASES",
    "PREFLIGHT_ITEMS",
    "PilotingPhase",
    "PreflightItem",
    "PreflightResult",
    "Routine",
    "RoutineContext",
    "RoutineError",
    "Row",
    "Scorecard",
    "TASKS",
    "TaskDef",
    "builtin_routines",
    "default_field",
    "dry_run",
    "load_field",
    "load_routines",
    "max_score",
    "preflight_ok",
    "run_preflight",
]
