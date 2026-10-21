"""Where releases come from.

A release is always the same shape regardless of transport:

    <channel>/manifest.json     SHA-256 of every file in the build
    <channel>/files/...         the full tree

Two optional transports are supported behind one interface. FrameGuard's
zero-configuration updater uses full installers from GitHub Releases instead:
the bundled Windows FFmpeg files exceed GitHub's per-object Git limit and the
macOS payload is too large for a repository branch. A team can explicitly
configure one of these transports when it has a suitable payload host.

LocalSource  - any filesystem path: a local test mirror, a UNC share, a
               mapped drive. Costs nothing to operate and needs no server.
HttpSource   - an internal static host or artifact store that serves a
               directory-shaped tree over HTTP(S).

NOTE ON GOOGLE DRIVE (the retired channel)
    Drive hosted this channel until it moved to the repository. As an
    HTTP host Drive cannot serve this layout at all — files are
    addressed by opaque fileId rather than path, large downloads are
    interrupted by a virus-scan interstitial, and unauthenticated
    access is throttled — so the old channel silently depended on every
    machine running Drive for Desktop against the same shared folder.
    The repository needs none of that.
"""

import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional


def _https_context():
    """Trust what the OS trusts (see frameguard.updater.https_context)."""
    from frameguard.updater import https_context
    return https_context()

from frameguard import manifest as M

_TIMEOUT = 30
_USER_AGENT = "FrameGuard-Updater/1.0"


class UpdateSourceError(Exception):
    """A release could not be read from the configured channel."""


class BaseSource:
    """Common interface: read the manifest, fetch one file at a time."""

    def describe(self) -> str:
        raise NotImplementedError

    def read_manifest(self) -> dict:
        raise NotImplementedError

    def fetch(self, rel_path: str, dest_path: str):
        raise NotImplementedError

    def is_reachable(self) -> bool:
        try:
            self.read_manifest()
            return True
        except UpdateSourceError:
            return False


class LocalSource(BaseSource):
    """A release directory on a filesystem path."""

    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expandvars(os.path.expanduser(root)))

    def describe(self) -> str:
        return self.root

    def _payload_dir(self) -> str:
        nested = os.path.join(self.root, M.PAYLOAD_DIRNAME)
        return nested if os.path.isdir(nested) else self.root

    def read_manifest(self) -> dict:
        path = os.path.join(self.root, M.MANIFEST_FILENAME)
        data = M.load_manifest(path)
        if data is None:
            raise UpdateSourceError(f"No usable manifest at:\n{path}")
        return data

    def fetch(self, rel_path: str, dest_path: str):
        src = os.path.join(self._payload_dir(), rel_path.replace("/", os.sep))
        if not os.path.isfile(src):
            raise UpdateSourceError(f"Release is missing a file: {rel_path}")
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        shutil.copy2(src, dest_path)


class HttpSource(BaseSource):
    """A release served over HTTP(S) from a directory-shaped URL."""

    def __init__(self, base_url: str, token: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def describe(self) -> str:
        return self.base_url

    def _request(self, url: str) -> urllib.request.Request:
        headers = {"User-Agent": _USER_AGENT}
        if self.token:
            # Works for GitHub Enterprise and most internal artifact stores.
            headers["Authorization"] = f"Bearer {self.token}"
        return urllib.request.Request(url, headers=headers)

    def read_manifest(self) -> dict:
        url = f"{self.base_url}/{M.MANIFEST_FILENAME}"
        try:
            with urllib.request.urlopen(self._request(url), timeout=_TIMEOUT,
                                        context=_https_context()) as r:
                data = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError) as e:
            raise UpdateSourceError(f"Could not reach the update channel:\n{url}\n{e}")
        except ValueError as e:
            raise UpdateSourceError(f"The manifest at {url} is not valid JSON: {e}")

        if not isinstance(data, dict) or "files" not in data:
            raise UpdateSourceError(f"The manifest at {url} is not a release manifest.")
        if not M.manifest_paths_are_safe(data):
            raise UpdateSourceError(
                f"The manifest at {url} contains a file path that escapes "
                f"the release tree; refusing it.")
        if data.get("manifest_version", 0) > M.MANIFEST_VERSION:
            raise UpdateSourceError(
                "This release was published by a newer version of the build "
                "tooling. Update FrameGuard from the installer instead.")
        return data

    def fetch(self, rel_path: str, dest_path: str):
        quoted = "/".join(urllib.parse.quote(part) for part in rel_path.split("/"))
        url = f"{self.base_url}/{M.PAYLOAD_DIRNAME}/{quoted}"
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        try:
            with urllib.request.urlopen(self._request(url), timeout=_TIMEOUT,
                                        context=_https_context()) as r, \
                    open(dest_path, "wb") as f:
                shutil.copyfileobj(r, f, length=1024 * 256)
        except (urllib.error.URLError, OSError) as e:
            raise UpdateSourceError(f"Could not download {rel_path}:\n{e}")


def make_source(channel: str, token: Optional[str] = None) -> BaseSource:
    """Build the right source for a channel string.

    Accepts an http(s) URL or any filesystem path, so one setting covers a
    Drive-synced folder, a UNC share and a web endpoint.
    """
    if not channel or not channel.strip():
        raise UpdateSourceError("No update channel is configured.")
    channel = channel.strip()
    if channel.lower().startswith(("http://", "https://")):
        return HttpSource(channel, token=token)
    return LocalSource(channel)
