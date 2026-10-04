import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path
    busy_timeout_ms: int = 5000
    max_request_bytes: int = 262_144

    @classmethod
    def from_environment(cls) -> "Settings":
        volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
        on_railway = bool(os.getenv("RAILWAY_ENVIRONMENT_ID"))
        if on_railway and not volume:
            raise RuntimeError("Railway requires a persistent volume mounted at /data.")
        default = str(Path(volume) / "sync.db") if volume else "./data/sync.db"
        path = Path(os.getenv("SYNC_DATABASE_PATH", default)).resolve()
        if on_railway and not path.is_relative_to(Path(volume).resolve()):
            raise RuntimeError("SYNC_DATABASE_PATH must be inside the Railway volume.")
        return cls(database_path=path)
