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
               directory-shaped tree over HTTPS. Plain http:// (and any
               other URL scheme) is refused: the manifest carries the hashes
               every file is verified against, and an optional bearer token
               rides on every request, so neither may cross the network in
               clear text. Redirects must stay on HTTPS, and the token is
               dropped when a redirect leaves the configured host.

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


def _is_https_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url or "")
    return parsed.scheme.lower() == "https" and bool(parsed.hostname)


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to HTTPS, and never carry the channel's bearer
    token to a different host (urllib copies it onto every redirect)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_https_url(newurl):
            raise urllib.error.HTTPError(
                newurl, code,
                "Update channel redirected to a non-HTTPS location; refused.",
                headers, fp)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old_host = (urllib.parse.urlsplit(req.full_url).hostname or "").lower()
            new_host = (urllib.parse.urlsplit(newurl).hostname or "").lower()
            if old_host != new_host:
                new.remove_header("Authorization")
        return new


def _open_https(request, timeout: float):
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_https_context()),
        _HttpsOnlyRedirectHandler())
    return opener.open(request, timeout=timeout)


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
        if not _is_https_url(base_url):
            raise UpdateSourceError(
                "Update channels must use https://. A plain-HTTP channel "
                "would expose the release manifest (and any access token) "
                "to the network; refusing it.")
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
            with _open_https(self._request(url), _TIMEOUT) as r:
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
        if not M.manifest_hashes_are_valid(data):
            raise UpdateSourceError(
                f"The manifest at {url} lists a file without a valid SHA-256; "
                f"refusing it.")
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
            with _open_https(self._request(url), _TIMEOUT) as r, \
                    open(dest_path, "wb") as f:
                shutil.copyfileobj(r, f, length=1024 * 256)
        except (urllib.error.URLError, OSError) as e:
            raise UpdateSourceError(f"Could not download {rel_path}:\n{e}")


def make_source(channel: str, token: Optional[str] = None) -> BaseSource:
    """Build the right source for a channel string.

    Accepts an https:// URL or any filesystem path, so one setting covers a
    local mirror, a UNC share and a web endpoint. Any other URL scheme,
    plain http:// included, raises UpdateSourceError.
    """
    if not channel or not channel.strip():
        raise UpdateSourceError("No update channel is configured.")
    channel = channel.strip()
    if "://" in channel:
        if not _is_https_url(channel):
            raise UpdateSourceError(
                "Update channels must be an https:// URL or a folder path; "
                f"refusing {channel.split('://', 1)[0]}:// .")
        return HttpSource(channel, token=token)
    return LocalSource(channel)
