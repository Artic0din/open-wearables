from typing import cast

from app.services.providers.base_strategy import BaseProviderStrategy, ProviderCapabilities, ProviderCoverage
from app.services.providers.ladder.auth import LadderAuth
from app.services.providers.ladder.client import LadderClient
from app.services.providers.ladder.coverage import HEALTH_SCORES, SLEEP_FIELDS, TIMESERIES, WORKOUT_FIELDS
from app.services.providers.ladder.workouts import LadderWorkouts
from app.services.providers.templates.base_workouts import BaseWorkoutsTemplate


class LadderStrategy(BaseProviderStrategy):
    """Ladder provider — workout sessions + HR samples via REST pull.

    Auth is a pasted joinladder.session JWT (no OAuth). Connect via
    POST /api/v1/ladder/users/{id}/connect-manual. Sync uses the generic
    sync_vendor_data Celery path through ``self.workouts.load_data``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.oauth = None
        self.client = LadderClient(connection_repo=self.connection_repo)
        self.auth_helper = LadderAuth(connection_repo=self.connection_repo)
        self.workouts = cast(
            BaseWorkoutsTemplate,
            LadderWorkouts(
                client=self.client,
                connection_repo=self.connection_repo,
            ),
        )
        self.data_247 = None

    @property
    def name(self) -> str:
        return "ladder"

    @property
    def display_name(self) -> str:
        return "Ladder"

    @property
    def api_base_url(self) -> str:
        return "https://rocky-api.prod.ltdev.io"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(rest_pull=True)

    @property
    def coverage(self) -> ProviderCoverage:
        return ProviderCoverage(
            timeseries=TIMESERIES,
            workout_fields=WORKOUT_FIELDS,
            sleep_fields=SLEEP_FIELDS,
            health_scores=HEALTH_SCORES,
        )
