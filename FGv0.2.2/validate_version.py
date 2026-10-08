"""FrameGuard -- Build Version Validator
Checks that __init__.py, frameguard.spec, installer.iss and version.json all
agree on the version. Called by build_installer.bat and build_mac.sh before
PyInstaller runs.
"""
import json
import re
import sys

def read_version(filepath, pattern):
    """Read a version string from a file using a regex pattern."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        match = re.search(pattern, content)
        if match:
            return match.group(1).strip()
    except (OSError, IOError) as e:
        print(f"  ERROR: Cannot read {filepath}: {e}")
        sys.exit(1)
    return None

def read_release_version(filepath):
    """Read the version field from version.json (the update manifest)."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        version = data.get("version", "")
        return version.strip() if isinstance(version, str) else None
    except (OSError, ValueError) as e:
        print(f"  ERROR: Cannot read {filepath}: {e}")
        sys.exit(1)

def main():
    v_init = read_version("frameguard/__init__.py", r'__version__\s*=\s*"([^"]+)"')
    v_spec = read_version("frameguard.spec", r'APP_VERSION\s*=\s*"([^"]+)"')
    v_iss = read_version("installer.iss", r"AppVersion=(.+)")
    v_json = read_release_version("version.json")

    if not all([v_init, v_spec, v_iss, v_json]):
        print("  ERROR: Could not read version from one or more files.")
        print(f"    __init__.py: {v_init}")
        print(f"    frameguard.spec: {v_spec}")
        print(f"    installer.iss: {v_iss}")
        print(f"    version.json: {v_json}")
        sys.exit(1)

    errors = []
    if v_init != v_spec:
        errors.append(f"__init__.py={v_init} != frameguard.spec={v_spec}")
    if v_init != v_iss:
        errors.append(f"__init__.py={v_init} != installer.iss={v_iss}")
    if v_init != v_json:
        errors.append(f"__init__.py={v_init} != version.json={v_json}")

    if errors:
        print("  ERROR: Version mismatch!")
        for e in errors:
            print(f"    - {e}")
        print("\n  All files must have the same version number.")
        sys.exit(1)

    print(f"       Versions consistent: {v_init}")

if __name__ == "__main__":
    main()
