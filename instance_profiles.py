"""Safe metadata store for named VOIDEX game instances.

Game files are deliberately kept separate from the shared Mojang cache. This
module only manages profile metadata and directory identity; launch wiring is
performed by the UI/launch worker in a later integration step.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import uuid
from pathlib import Path

_PROFILE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_PROFILE_NAME_MAX = 48
_PROFILE_VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")
_ALLOWED_LOADERS = {"vanilla", "fabric", "forge", "neoforge", "quilt"}
_PROFILE_SCHEMA = 1


def _safe_root(root) -> Path:
    path = Path(root).expanduser()
    if path.is_symlink():
        raise ValueError("Profile root must not be a symbolic link.")
    path.mkdir(parents=True, exist_ok=True)
    resolved = path.resolve()
    if not resolved.is_dir():
        raise ValueError("Profile root is not a directory.")
    return resolved


def _validate_profile_id(profile_id: str) -> str:
    if not isinstance(profile_id, str) or not _PROFILE_ID_RE.fullmatch(profile_id):
        raise ValueError("Invalid instance profile ID.")
    return profile_id


def _validate_profile_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("Instance profile name must be text.")
    normalized = " ".join(name.split())
    if not normalized or len(normalized) > _PROFILE_NAME_MAX:
        raise ValueError(f"Instance profile name must contain 1–{_PROFILE_NAME_MAX} characters.")
    if any(ord(char) < 32 or char in '<>:"/\\\\|?*' for char in normalized):
        raise ValueError("Instance profile name contains unsupported characters.")
    if normalized.endswith((".", " ")):
        raise ValueError("Instance profile name cannot end with a dot or space.")
    return normalized


def _validate_version(version: str) -> str:
    if not isinstance(version, str) or not _PROFILE_VERSION_RE.fullmatch(version):
        raise ValueError("Instance profile requires a concrete Minecraft version.")
    return version


def _profile_directory(root: Path, profile_id: str) -> Path:
    directory = root / profile_id
    if directory.is_symlink():
        raise ValueError("Instance profile directory must not be a symbolic link.")
    resolved = directory.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("Instance profile directory escapes the profile root.") from exc
    return directory


def create_instance_profile(root, name, game_version, loader="vanilla") -> dict:
    """Create an isolated named instance and atomically persist its metadata."""
    root_path = _safe_root(root)
    name = _validate_profile_name(name)
    game_version = _validate_version(game_version)
    if loader not in _ALLOWED_LOADERS:
        raise ValueError("Unsupported instance profile loader.")
    profile_id = uuid.uuid4().hex
    directory = _profile_directory(root_path, profile_id)
    directory.mkdir()
    game_directory = directory / "game"
    game_directory.mkdir()
    profile = {
        "schema": _PROFILE_SCHEMA,
        "id": profile_id,
        "name": name,
        "game_version": game_version,
        "loader": loader,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "game_directory": "game",
    }
    target = directory / "profile.json"
    temporary = directory / "profile.json.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(profile, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        try:
            game_directory.rmdir()
            directory.rmdir()
        except OSError:
            pass
        raise
    return {**profile, "directory": str(directory), "game_directory_path": str(game_directory)}


def list_instance_profiles(root) -> list[dict]:
    """Read valid profiles in stable name order; reject unsafe profile entries."""
    root_path = _safe_root(root)
    profiles = []
    for directory in root_path.iterdir():
        if directory.is_symlink():
            raise ValueError("Profile root contains a symbolic-link entry.")
        if not directory.is_dir() or not _PROFILE_ID_RE.fullmatch(directory.name):
            continue
        profile_file = directory / "profile.json"
        if profile_file.is_symlink() or not profile_file.is_file():
            continue
        try:
            with profile_file.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not read instance profile {directory.name}: {exc}") from exc
        if (not isinstance(data, dict) or data.get("schema") != _PROFILE_SCHEMA
                or data.get("id") != directory.name):
            raise ValueError(f"Instance profile metadata is invalid: {directory.name}")
        data["name"] = _validate_profile_name(data.get("name"))
        data["game_version"] = _validate_version(data.get("game_version"))
        if data.get("loader") not in _ALLOWED_LOADERS:
            raise ValueError(f"Instance profile loader is invalid: {directory.name}")
        game_rel = data.get("game_directory")
        if game_rel != "game":
            raise ValueError(f"Instance profile game directory is invalid: {directory.name}")
        game_dir = directory / "game"
        if game_dir.is_symlink():
            raise ValueError(f"Instance game directory is a symbolic link: {directory.name}")
        data["directory"] = str(directory)
        data["game_directory_path"] = str(game_dir)
        profiles.append(data)
    return sorted(profiles, key=lambda item: (item["name"].casefold(), item["id"]))



def rename_instance_profile(root, profile_id, new_name) -> dict:
    """Rename a profile without moving or modifying its isolated game directory."""
    root_path = _safe_root(root)
    profile_id = _validate_profile_id(profile_id)
    new_name = _validate_profile_name(new_name)
    directory = _profile_directory(root_path, profile_id)
    metadata = directory / "profile.json"
    if not directory.is_dir() or metadata.is_symlink() or not metadata.is_file():
        raise ValueError("Instance profile does not exist or is unsafe.")
    with metadata.open("r", encoding="utf-8") as stream:
        data = json.load(stream)
    if (not isinstance(data, dict) or data.get("schema") != _PROFILE_SCHEMA
            or data.get("id") != profile_id):
        raise ValueError("Instance profile metadata does not match its directory.")
    for existing in list_instance_profiles(root_path):
        if existing["id"] != profile_id and existing["name"].casefold() == new_name.casefold():
            raise ValueError("An instance with this name already exists.")
    data["name"] = new_name
    temporary = directory / "profile.json.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, metadata)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {**data, "directory": str(directory),
            "game_directory_path": str(directory / "game")}

def delete_instance_profile(root, profile_id, *, delete_game_data=False) -> dict:
    """Remove profile metadata; game data is preserved unless explicitly requested."""
    root_path = _safe_root(root)
    profile_id = _validate_profile_id(profile_id)
    directory = _profile_directory(root_path, profile_id)
    metadata = directory / "profile.json"
    if not directory.is_dir() or metadata.is_symlink() or not metadata.is_file():
        raise ValueError("Instance profile does not exist or is unsafe.")
    with metadata.open("r", encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict) or data.get("id") != profile_id:
        raise ValueError("Instance profile metadata does not match its directory.")
    if delete_game_data:
        import shutil
        shutil.rmtree(directory)
        return {"id": profile_id, "deleted": True, "game_data_deleted": True}
    metadata.unlink()
    try:
        (directory / "game").rmdir()
        directory.rmdir()
    except OSError:
        pass
    return {"id": profile_id, "deleted": True, "game_data_deleted": False,
            "preserved_directory": str(directory) if directory.exists() else None}


def resolve_instance_profile(root, profile_id, expected_game_version=None):
    """Resolve a selected profile to its isolated game directory safely."""
    root_path = _safe_root(root)
    profile_id = _validate_profile_id(profile_id)
    directory = _profile_directory(root_path, profile_id)
    metadata = directory / "profile.json"
    if metadata.is_symlink() or not metadata.is_file():
        raise ValueError("Instance profile does not exist or is unsafe.")
    with metadata.open("r", encoding="utf-8") as stream:
        data = json.load(stream)
    if (not isinstance(data, dict) or data.get("schema") != _PROFILE_SCHEMA
            or data.get("id") != profile_id):
        raise ValueError("Instance profile metadata is invalid.")
    version = _validate_version(data.get("game_version"))
    if expected_game_version is not None and version != expected_game_version:
        raise ValueError(
            f"Instance uses Minecraft {version}, not selected version {expected_game_version}."
        )
    if data.get("loader") not in _ALLOWED_LOADERS or data.get("game_directory") != "game":
        raise ValueError("Instance profile settings are invalid.")
    game_directory = directory / "game"
    if game_directory.is_symlink() or not game_directory.is_dir():
        raise ValueError("Instance game directory is missing or unsafe.")
    resolved = game_directory.resolve()
    try:
        resolved.relative_to(directory.resolve())
    except ValueError as exc:
        raise ValueError("Instance game directory escapes its profile.") from exc
    return {
        "id": profile_id,
        "name": _validate_profile_name(data.get("name")),
        "game_version": version,
        "loader": data["loader"],
        "game_directory_path": str(resolved),
    }
