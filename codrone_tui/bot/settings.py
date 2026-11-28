# Tunables for the bot. Every field can be overridden in config.toml under [bot].

from __future__ import annotations

from dataclasses import dataclass, fields

LLM_MODES = ("ollama", "anthropic", "openai", "off")
MOTIONS = ("creep", "steps")


@dataclass
class BotSettings:
    # obstacle guard (front range sensor)
    margin_cm: float = 30.0  # never fly closer than this to something the front sensor sees
    sensor_max_cm: float = 150.0  # a front reading past this means "nothing within range"
    cone_deg: float = 10.0  # half-angle of the sensor beam, used when marking the map
    unknown_step_cm: float = 25.0  # forward step when the sensor gives no reading at all
    none_is_clear: bool = False  # true once you've seen your drone report "no reading" for open space
    max_step_cm: float = 60.0  # re-measure at least this often while flying forward
    min_move_cm: float = 5.0  # dead reckoning isn't more precise than this
    samples: int = 3  # range readings per measurement (the median is used)
    settle_s: float = 0.25  # pause after a turn before measuring

    # how forward flight is flown
    motion: str = "creep"  # "creep": steady slow stick flight, a reading every creep_tick_cm, stops the moment
    #                        something is in the way; "steps": separate move commands of up to max_step_cm
    creep_tick_cm: float = 5.0  # read the sensor after every this many cm (a 100 cm move = 20 ticks)
    creep_cm_s: float = 25.0  # creep speed; slower stops shorter
    creep_brake_s: float = 0.3  # sticks centred this long after a creep, so the drone stops before what's next

    # flight limits
    max_alt_cm: float = 150.0
    min_alt_cm: float = 20.0
    geofence_cm: float = 600.0  # max distance from the takeoff point
    blind_warn_cm: float = 50.0  # sideways / backward moves longer than this get a warning

    # map + route planner
    cell_cm: float = 10.0
    drone_radius_cm: float = 10.0
    prior_clearance_cm: float = 10.0  # extra room kept around field.toml elements
    arrive_cm: float = 10.0
    max_replans: int = 15
    max_legs: int = 40
    side_look_deg: float = 20.0  # glance this far left and right before each nav leg (0 = don't; faster, riskier)
    scan_angles: tuple[float, ...] = (-90.0, -45.0, 0.0, 45.0, 90.0)
    field_obstacles: bool = True  # treat field.toml elements as known obstacles when routing
    guard_missions: bool = False  # start with `avoid on` (missions get the obstacle guard)

    # language model: `ask` plans and `live` guidance
    llm: str = "ollama"  # "ollama" (local), "anthropic" or "openai" (API key), or "off" (phrase parser only)
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3.5:9b"  # the Ollama model
    timeout_s: float = 180.0  # the first request also loads the model, which can take a while
    think: bool = False  # let reasoning models think first (slower, sometimes better)
    api_model: str = ""  # model for anthropic/openai; empty = the provider's default (anthropic: a fast Haiku)
    api_url: str = ""  # empty = the provider's own; any OpenAI-compatible server works for "openai"
    api_key_env: str = ""  # environment variable with the key; empty = ANTHROPIC_API_KEY / OPENAI_API_KEY

    # live mode: the model writes guidance once, the flight algorithm flies it
    camera_url: str = ""  # a phone/IP camera watching the flight; live mode saves stills while flying ("" = none)
    camera_timeout_s: float = 3.0
    camera_every_s: float = 2.0  # during `live`, a still this often (and one at every step)
    live_record: bool = True  # keep the guidance and what happened in logs/bot-live-*.jsonl

    @classmethod
    def from_config(cls, section: object) -> tuple["BotSettings", list[str]]:
        # Settings from the [bot] table plus a list of problems. Bad entries keep the default.
        settings = cls()
        problems: list[str] = []
        if not section:
            return settings, problems
        if not isinstance(section, dict):
            return settings, ["[bot] must be a table"]
        known = {f.name for f in fields(cls)}
        for key, raw in section.items():
            if key not in known:
                problems.append(f"unknown setting '{key}' ignored")
                continue
            current = getattr(settings, key)
            try:
                setattr(settings, key, _coerce(raw, current))
            except (TypeError, ValueError) as exc:
                problems.append(f"{key}: {exc}; keeping {current!r}")
        problems += settings.repair()
        return settings, problems

    def repair(self) -> list[str]:
        # Put out-of-range values back to their defaults so a typo can't make the guard unsafe.
        default = type(self)()
        limits = {
            "margin_cm": (10, 150), "sensor_max_cm": (40, 400), "cone_deg": (0, 30), "unknown_step_cm": (5, 60),
            "max_step_cm": (10, 200), "min_move_cm": (1, 20), "samples": (1, 9), "settle_s": (0, 3),
            "max_alt_cm": (30, 300), "min_alt_cm": (10, 100), "geofence_cm": (100, 3000), "blind_warn_cm": (0, 500),
            "cell_cm": (5, 25), "drone_radius_cm": (5, 30), "prior_clearance_cm": (0, 50), "arrive_cm": (3, 50),
            "max_replans": (0, 100), "max_legs": (1, 200), "side_look_deg": (0, 45), "timeout_s": (5, 1800),
            "camera_timeout_s": (0.5, 30), "camera_every_s": (0.5, 30),
            "creep_tick_cm": (2, 20), "creep_cm_s": (5, 60), "creep_brake_s": (0, 2),
        }
        problems = []
        for key, (lo, hi) in limits.items():
            value = getattr(self, key)
            if not lo <= value <= hi:
                problems.append(f"{key} = {value!r} is outside {lo}..{hi}; using {getattr(default, key)!r}")
                setattr(self, key, getattr(default, key))
        if self.min_alt_cm >= self.max_alt_cm:
            problems.append("min_alt_cm must be below max_alt_cm; using the defaults")
            self.min_alt_cm, self.max_alt_cm = default.min_alt_cm, default.max_alt_cm
        if self.motion not in MOTIONS:
            problems.append(f"motion must be one of {', '.join(MOTIONS)}; using {default.motion!r}")
            self.motion = default.motion
        if self.llm not in LLM_MODES:
            problems.append(f"llm must be one of {', '.join(LLM_MODES)}; using {default.llm!r}")
            self.llm = default.llm
        if not self.scan_angles:
            self.scan_angles = default.scan_angles
        return problems


def _coerce(raw: object, current: object) -> object:
    if isinstance(current, bool):
        if not isinstance(raw, bool):
            raise TypeError("expected true or false")
        return raw
    if isinstance(current, int):
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise TypeError("expected a whole number")
        return raw
    if isinstance(current, float):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise TypeError("expected a number")
        return float(raw)
    if isinstance(current, str):
        if not isinstance(raw, str):
            raise TypeError("expected text")
        return raw.strip()
    if isinstance(current, tuple):
        if not isinstance(raw, (list, tuple)) or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in raw):
            raise TypeError("expected a list of numbers")
        return tuple(float(v) for v in raw)
    raise TypeError("unsupported setting")
