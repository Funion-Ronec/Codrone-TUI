"""Obstacle-aware navigation and plain-English flight plans (see docs/DEVELOPMENT.md, "Bot").

Written with Claude Code (AI-assisted). Read it, and fly it in --demo first, before trusting it with a real drone.
"""

from .context import BotContext
from .navigator import Blocked, NavError, Navigator, ScanPoint, bearing, describe_scan
from .ollama import OllamaClient, OllamaError
from .phrases import PhraseError, parse_phrases
from .planner import TextPlanner, system_prompt
from .procedure import COLORS, OPS, Plan, PlanError, Step, compile_routine, dry_run_plan, normalize, validate_plan
from .sensing import CLEAR, HIT, UNKNOWN, Reading, SensorError, read_front
from .settings import BotSettings
from .world import Box, World, WorldMock, box, demo_world, element_boxes, field_world

__all__ = [
    "Blocked", "BotContext", "BotSettings", "Box", "CLEAR", "COLORS", "HIT", "NavError", "Navigator", "OPS",
    "OllamaClient", "OllamaError", "PhraseError", "Plan", "PlanError", "Reading", "ScanPoint", "SensorError", "Step",
    "TextPlanner", "UNKNOWN", "World", "WorldMock", "bearing", "box", "compile_routine", "demo_world",
    "describe_scan", "dry_run_plan", "element_boxes", "field_world", "normalize", "parse_phrases", "read_front",
    "system_prompt", "validate_plan",
]
