#!/usr/bin/env -S uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "tomlkit",
# ]
# ///

from argparse import ArgumentParser, Namespace
from os import walk
from os.path import join
from re import match

import tomlkit


def set_version_in_file(file_path: str, new_version: str) -> tuple[bool, str | None]:
    """
    Update the 'project.version' key in a single pyproject.toml file.
    Returns a tuple (updated, module_name) where updated is True if version was updated,
    and module_name is the value of [project].name (if present).
    """
    with open(file_path, "r+", encoding="utf-8") as f:
        doc = tomlkit.load(f)

        if "project" in doc and "version" in doc["project"]:
            doc["project"]["version"] = new_version
            module_name = doc["project"].get("name", None)
            f.seek(0)
            f.write(tomlkit.dumps(doc))
            f.truncate()
            return True, module_name
        else:
            return False, None


def recursively_update_pyprojects(args: Namespace) -> dict:
    """
    First pass: Walk through `dir` recursively, find all pyproject.toml files, and update the version.
    Returns a dictionary mapping module names (from [project].name) to the new version.
    """
    module_to_version = {}
    for current_path, dirs, files in walk(args.dir):
        if "pyproject.toml" in files:
            pyproject_path = join(current_path, "pyproject.toml")
            print(f"Found pyproject.toml: {pyproject_path}")
            updated, module_name = set_version_in_file(pyproject_path, args.version)
            if updated:
                if module_name:
                    module_to_version[module_name] = args.version
                print(f"  Updated version to {args.version}")
            else:
                print(f"  Skipped (no 'project.version' in {pyproject_path})")
    return module_to_version


def update_dependency_string(dep: str, module_to_version: dict) -> str:
    """
    If the dependency string references a module present in module_version_map,
    update its version specifier to '==<new_version>'.

    This function handles dependency strings that may include extras or environment markers.
    """
    # Split out any environment markers (after a ';')
    parts = dep.split(";", 1)
    main_part = parts[0].strip()
    marker = ";" + parts[1].strip() if len(parts) > 1 else ""

    # Regex to capture the package name, optional extras, and any existing version specifiers.
    result = match(r"^([A-Za-z0-9_\-]+)(\[[^]]+])?\s*(.*)$", main_part)
    if result:
        name = result.group(1)
        extras = result.group(2) or ""
        if name in module_to_version:
            new_version = module_to_version[name]
            # Replace any existing version specifier with an exact version constraint.
            new_main_part = f"{name}{extras}>={new_version}"
            return new_main_part + marker
    return dep


def update_dependency_versions_in_file(file_path: str, module_to_version: dict) -> bool:
    """
    Update the dependencies in a single pyproject.toml file so that any dependency
    referencing a module in module_version_map gets its version specifier updated.
    Returns True if any dependency was updated.
    """
    updated = False
    with open(file_path, "r+", encoding="utf-8") as f:
        doc = tomlkit.load(f)
        if "project" in doc and "dependencies" in doc["project"]:
            deps = doc["project"]["dependencies"]
            new_deps = []
            for dep in deps:
                if isinstance(dep, str):
                    new_dep = update_dependency_string(dep, module_to_version)
                    if new_dep != dep:
                        updated = True
                    new_deps.append(new_dep)
                else:
                    new_deps.append(dep)
            if updated:
                doc["project"]["dependencies"] = new_deps
                f.seek(0)
                f.write(tomlkit.dumps(doc))
                f.truncate()
    return updated


def recursively_update_dependencies(args: Namespace, module_to_version: dict) -> int:
    """
    Second pass: Walk through `dir` recursively, find all pyproject.toml files, and update
    dependency declarations referencing modules in module_version_map.
    Returns the number of files that were updated.
    """
    updated_count = 0
    for current_path, dirs, files in walk(args.dir):
        if "pyproject.toml" in files:
            pyproject_path = join(current_path, "pyproject.toml")
            print(f"Checking dependencies in: {pyproject_path}")
            was_updated = update_dependency_versions_in_file(pyproject_path, module_to_version)
            if was_updated:
                updated_count += 1
                print(f"  Updated dependencies in {pyproject_path}")
    return updated_count


if __name__ == "__main__":
    parser = ArgumentParser(
        prog="set-version",
        description="Set the version in all pyproject.toml files and update dependency references.",
        epilog="To specify the value for a flag, you can either use --name=[VALUE] or --name [VALUE]",
    )
    parser.add_argument(
        "version",
        help="New version to set in all pyproject.toml files.",
    )
    parser.add_argument(
        "--dir",
        help="Root directory to start searching for pyproject.toml files. Defaults to current directory.",
        default=".",
    )
    arguments = parser.parse_args()

    print(f"Recursively searching for pyproject.toml under '{arguments.dir}'...")
    # First pass: update project version and build a mapping of module names to new version.
    module_version_map = recursively_update_pyprojects(arguments)
    print(f"\nFirst pass done. Modules updated: {module_version_map}")

    # Second pass: update dependency declarations referencing the updated modules.
    print("\nStarting second pass to update dependency versions...")
    dep_updates = recursively_update_dependencies(arguments, module_version_map)
    print(f"\nDone. Updated {dep_updates} pyproject.toml file(s) for dependencies.")
