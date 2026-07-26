"""Provider-specific statistics adapters."""

from app.stats.providers.baseball_reference import (
    BASEBALL_REFERENCE_MINIMUM_INTERVAL_SECONDS,
    BaseballReferenceProvider,
    BaseballReferenceTable,
)
from app.stats.providers.retrosheet import (
    MLB_REGULAR_SEASON_SCOPE,
    RETROSHEET_ATTRIBUTION_TEXT,
    RETROSHEET_ATTRIBUTION_VERSION,
    RETROSHEET_SEVEN_MEMBERS,
    RetrosheetCsvMember,
    RetrosheetDataset,
    RetrosheetProvider,
)
from app.stats.providers.pybaseball_parity import (
    PYBASEBALL_REQUIRED_VERSION,
    HttpsSessionPatch,
    HttpsUrlPatch,
    PybaseballFunctions,
    PybaseballIdentityError,
    PybaseballParityAdapter,
    PybaseballParityError,
    PybaseballParityResult,
    PybaseballProvenance,
    PybaseballVersionError,
)
from app.stats.providers.pybaseball_compatibility import (
    PYBASEBALL_COMPATIBILITY_CONTRACT_VERSION,
    PybaseballCompatibilityError,
    PybaseballCompatibilityResult,
    run_installed_pybaseball_compatibility_probe,
)
from app.stats.providers.player_register import (
    PYBASEBALL_REGISTER_ADAPTER_VERSION,
    PYBASEBALL_REGISTER_COMMIT_SHA,
    PYBASEBALL_REGISTER_ENDPOINT_CATEGORY,
    PYBASEBALL_REGISTER_URL,
    PlayerRegisterDataset,
    PlayerRegisterMapping,
    PybaseballPlayerRegisterProvider,
)
from app.stats.providers.statcast import StatcastDataset, StatcastProvider

__all__ = [
    "BASEBALL_REFERENCE_MINIMUM_INTERVAL_SECONDS",
    "BaseballReferenceProvider",
    "BaseballReferenceTable",
    "MLB_REGULAR_SEASON_SCOPE",
    "PYBASEBALL_REQUIRED_VERSION",
    "PYBASEBALL_COMPATIBILITY_CONTRACT_VERSION",
    "PYBASEBALL_REGISTER_ADAPTER_VERSION",
    "PYBASEBALL_REGISTER_COMMIT_SHA",
    "PYBASEBALL_REGISTER_ENDPOINT_CATEGORY",
    "PYBASEBALL_REGISTER_URL",
    "HttpsSessionPatch",
    "HttpsUrlPatch",
    "PybaseballFunctions",
    "PybaseballCompatibilityError",
    "PybaseballCompatibilityResult",
    "PybaseballIdentityError",
    "PybaseballParityAdapter",
    "PybaseballParityError",
    "PybaseballParityResult",
    "PybaseballPlayerRegisterProvider",
    "PybaseballProvenance",
    "PybaseballVersionError",
    "PlayerRegisterDataset",
    "PlayerRegisterMapping",
    "RETROSHEET_ATTRIBUTION_TEXT",
    "RETROSHEET_ATTRIBUTION_VERSION",
    "RETROSHEET_SEVEN_MEMBERS",
    "RetrosheetCsvMember",
    "RetrosheetDataset",
    "RetrosheetProvider",
    "StatcastDataset",
    "StatcastProvider",
    "run_installed_pybaseball_compatibility_probe",
]
