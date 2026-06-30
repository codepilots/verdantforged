# tee-broker-deploy/skill_library/config.py
import os
from dataclasses import dataclass

@dataclass(frozen=True)
class Config:
    db_path: str
    files_dir: str
    api_key: str
    port: int
    efs_mount: str  # e.g. /mnt/broker — used to print deploy hints
    broker_base_url: str  # for /sync-to-broker
    broker_skills_api_key: str  # for /sync-to-broker

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            db_path=os.environ.get(
                "SKILL_LIBRARY_DB",
                "/mnt/broker/logs/skill_library.db",
            ),
            files_dir=os.environ.get(
                "SKILL_LIBRARY_FILES_DIR",
                "/mnt/broker/skill-library/files",
            ),
            api_key=os.environ.get("SKILL_LIBRARY_API_KEY", ""),
            port=int(os.environ.get("SKILL_LIBRARY_PORT", "8091")),
            efs_mount=os.environ.get("BROKER_EFS_MOUNT", "/mnt/broker"),
            broker_base_url=os.environ.get(
                "BROKER_BASE_URL",
                "https://verdant.codepilots.co.uk",
            ),
            broker_skills_api_key=os.environ.get("BROKER_SKILLS_API_KEY", ""),
        )
