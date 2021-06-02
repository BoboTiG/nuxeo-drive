import importlib
from typing import Any, Dict

__migrations_list = ["0004_initial_migration"]  # Keep sorted


def import_migrations() -> Dict[str, Any]:
    """Dynamically load all the migrations from the module."""
    migrations = {}

    for migration_name in __migrations_list:
        module = getattr(
            importlib.import_module(
                f".{migration_name}", package="nxdrive.engine.dao.migrations.manager"
            ),
            "migration",
        )
        migrations[migration_name] = module
    return migrations


manager_migrations = import_migrations()
