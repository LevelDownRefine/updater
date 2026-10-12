"""Application-specific release and installation rules, supplied explicitly."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class UpdatePolicy:
    repository: str
    archive_prefix: str
    archives: Mapping[str, str]
    required_files: Mapping[str, frozenset[str]]
    default_frontend: str
    program_path: Callable[[str], bool]
    app_exe: str
    updater_exe: str
    process_exes: tuple[str, ...]
    cli_exes: Mapping[str, tuple[str, ...]]
    parent_required: frozenset[str] = frozenset()
    required_prefixes: tuple[str, ...] = ()
    restart_args: tuple[str, ...] = ()

    def __post_init__(self):
        assert self.default_frontend in self.archives
        assert (
            self.archives.keys() == self.required_files.keys() == self.cli_exes.keys()
        )
        assert self.parent_required <= self.archives.keys()
        assert self.archive_prefix.endswith("/") and not self.archive_prefix.startswith(
            "/"
        )

    @property
    def releases_url(self) -> str:
        return f"https://github.com/{self.repository}/releases"
