"""FrameGuard -- Build Version Validator

Checks that every place a release version ships agrees with
frameguard/__init__.py:

- frameguard.spec: APP_VERSION, the Windows filevers/prodvers tuples and
  every x.y.z[.w] string literal (FileVersion/ProductVersion StringStructs)
- installer.iss: AppVersion and every x.y.z[.w] literal
- version.json: version, and the changelog's first entry header
  ("v<version> - <title>"), which release_notes.py publishes
- installer_info.txt: the title line and the "WHAT'S NEW IN v<version>"
  heading shown on the Setup information page

Called by build_installer.bat, build_mac.sh, the pre-push gate and the
release workflow before anything is built. Exits 1 and lists every
mismatch it finds.
"""
import json
import re
import sys

_LITERAL_RE = re.compile(r"\d+\.\d+\.\d+(?:\.\d+)?")
_TUPLE_RE = re.compile(r"\b(filevers|prodvers)\s*=\s*\(([^)]*)\)")


def _read_text(filepath, errors):
    try:
        with open(filepath, "r", encoding="utf-8-sig") as f:
            return f.read()
    except OSError as e:
        errors.append(f"cannot read {filepath}: {e}")
        return None


def read_version(filepath, pattern):
    """Read a version string from a file using a regex pattern."""
    try:
        with open(filepath, "r", encoding="utf-8-sig") as f:
            content = f.read()
        match = re.search(pattern, content, re.MULTILINE)
        if match:
            return match.group(1).strip()
    except OSError as e:
        print(f"  ERROR: Cannot read {filepath}: {e}")
        sys.exit(1)
    return None


def read_release_data(filepath):
    """Parse version.json (the release metadata) or exit."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print(f"  ERROR: Cannot read {filepath}: {e}")
        sys.exit(1)
    return data if isinstance(data, dict) else {}


def read_release_version(filepath):
    """Read the version field from version.json."""
    version = read_release_data(filepath).get("version", "")
    return version.strip() if isinstance(version, str) else None


def spec_version_tuples(content):
    """{name: tuple-of-ints-or-None} for filevers/prodvers in the spec."""
    found = {}
    for name, body in _TUPLE_RE.findall(content):
        parts = [p.strip() for p in body.split(",") if p.strip()]
        try:
            found.setdefault(name, []).append(tuple(int(p) for p in parts))
        except ValueError:
            found.setdefault(name, []).append(None)
    return found


def check_spec_tuples(content, version, errors):
    expected = tuple(int(p) for p in version.split(".")) + (0,)
    found = spec_version_tuples(content)
    for name in ("filevers", "prodvers"):
        values = found.get(name)
        if not values:
            errors.append(f"frameguard.spec has no {name}=(...) tuple")
            continue
        for value in values:
            if value != expected:
                errors.append(
                    f"frameguard.spec {name}={value} != {expected}")


def check_changelog(data, version, errors):
    changelog = data.get("changelog", "")
    if not isinstance(changelog, str) or not changelog.startswith(f"v{version} - "):
        first = (changelog.splitlines() or [""])[0] if isinstance(changelog, str) else ""
        errors.append(
            f"version.json changelog must start with 'v{version} - <title>' "
            f"(first line is {first[:60]!r})")


def check_installer_info(content, version, errors):
    if content is None:
        return
    first = (content.splitlines() or [""])[0]
    if first != f"FrameGuard Desktop v{version}":
        errors.append(
            f"installer_info.txt title is {first[:60]!r}, expected "
            f"'FrameGuard Desktop v{version}'")
    if f"WHAT'S NEW IN v{version}" not in content:
        errors.append(
            f"installer_info.txt has no \"WHAT'S NEW IN v{version}\" heading")


def main():
    v_init = read_version("frameguard/__init__.py", r'__version__\s*=\s*"([^"]+)"')
    v_spec = read_version("frameguard.spec", r'APP_VERSION\s*=\s*"([^"]+)"')
    v_iss = read_version("installer.iss", r"^AppVersion=(.+)$")
    data = read_release_data("version.json")
    v_json = data.get("version", "")
    v_json = v_json.strip() if isinstance(v_json, str) else None

    if not all([v_init, v_spec, v_iss, v_json]):
        print("  ERROR: Could not read version from one or more files.")
        print(f"    __init__.py: {v_init}")
        print(f"    frameguard.spec: {v_spec}")
        print(f"    installer.iss: {v_iss}")
        print(f"    version.json: {v_json}")
        sys.exit(1)

    errors = []
    if not re.fullmatch(r"\d+\.\d+\.\d+", v_init):
        errors.append(f"__init__.py version {v_init!r} is not x.y.z")
    if v_init != v_spec:
        errors.append(f"__init__.py={v_init} != frameguard.spec={v_spec}")
    if v_init != v_iss:
        errors.append(f"__init__.py={v_init} != installer.iss={v_iss}")
    if v_init != v_json:
        errors.append(f"__init__.py={v_init} != version.json={v_json}")

    # The headline fields above are not the only places a version ships.
    # installer.iss also carried VersionInfoVersion, the registry "Version"
    # value and the welcome/finished labels — all of which sat at 0.5.4
    # through the 0.5.5 release while this validator printed "consistent",
    # because it only ever read AppVersion=. Every x.y.z[.w] literal in the
    # installer script and the spec must be the release version (or its
    # four-part form); anything else is drift that end users can see.
    allowed = {v_init, v_init + ".0"}
    spec_text = None
    for path in ("installer.iss", "frameguard.spec"):
        content = _read_text(path, errors)
        if content is None:
            continue
        if path == "frameguard.spec":
            spec_text = content
        for literal in sorted(set(_LITERAL_RE.findall(content))):
            if literal not in allowed:
                errors.append(f"{path} contains stale version literal {literal}")

    if re.fullmatch(r"\d+\.\d+\.\d+", v_init):
        # The Windows file-version tuples are ints, invisible to the literal
        # scan above; prodvers once stayed stale while filevers was bumped.
        if spec_text is not None:
            check_spec_tuples(spec_text, v_init, errors)
        check_changelog(data, v_init, errors)
        check_installer_info(_read_text("installer_info.txt", errors), v_init, errors)

    if errors:
        print("  ERROR: Version mismatch!")
        for e in errors:
            print(f"    - {e}")
        print("\n  All files must have the same version number.")
        sys.exit(1)

    print(f"       Versions consistent: {v_init}")


if __name__ == "__main__":
    main()
