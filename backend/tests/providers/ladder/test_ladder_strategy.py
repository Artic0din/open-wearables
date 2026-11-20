from app.schemas.enums import ProviderName, SeriesType
from app.services.providers.base_strategy import BaseProviderStrategy
from app.services.providers.factory import ProviderFactory
from app.services.providers.ladder.strategy import LadderStrategy
from app.services.providers.ladder.workouts import LadderWorkouts


class TestLadderStrategy:
    def test_factory_resolves_ladder(self) -> None:
        strategy = ProviderFactory().get_provider(ProviderName.LADDER.value)
        assert isinstance(strategy, LadderStrategy)

    def test_identity_and_capabilities(self) -> None:
        strategy = LadderStrategy()
        assert isinstance(strategy, BaseProviderStrategy)
        assert strategy.name == "ladder"
        assert strategy.display_name == "Ladder"
        assert strategy.api_base_url == "https://rocky-api.prod.ltdev.io"
        assert strategy.capabilities.rest_pull is True
        assert strategy.oauth is None
        assert strategy.has_cloud_api is False
        assert strategy.credentials_configured is True
        assert strategy.data_247 is None
        assert isinstance(strategy.workouts, LadderWorkouts)
        assert strategy.icon_url == "/static/provider-icons/ladder.svg"

    def test_coverage(self) -> None:
        cov = LadderStrategy().coverage
        assert SeriesType.heart_rate in cov.timeseries
        assert "heart_rate_avg" in cov.workout_fields
        assert "energy_burned" in cov.workout_fields
        assert "moving_time_seconds" in cov.workout_fields
