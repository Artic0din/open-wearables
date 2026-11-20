"""Ladder coverage declaration — workouts + in-session heart rate only."""

from app.schemas.enums import SeriesType
from app.schemas.enums.health_score_category import HealthScoreCategory

TIMESERIES: frozenset[SeriesType] = frozenset({SeriesType.heart_rate})

WORKOUT_FIELDS: frozenset[str] = frozenset(
    {
        "heart_rate_avg",
        "heart_rate_max",
        "heart_rate_min",
        "energy_burned",
        "moving_time_seconds",
    }
)

SLEEP_FIELDS: frozenset[str] = frozenset()
HEALTH_SCORES: frozenset[HealthScoreCategory] = frozenset()
