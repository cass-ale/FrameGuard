# FrameGuard release channel — Windows

This folder is the macOS half of FrameGuard's update channel. The app
checks it on its 24-hour cadence via
`https://raw.githubusercontent.com/cass-ale/FrameGuard/releases/RELEASES-macos/manifest.json`
and downloads only the files that differ from the installed build.

Layout once a release is published:

    manifest.json   SHA-256 of every file in the build
    files/          the full build tree

Publish with `FGv0.2.2/publish_release.py --publish-to <this folder in a
checkout of the releases branch>`, then commit and push this branch. The
publisher copies the payload first and the manifest last, so a client
reading mid-publish never sees a manifest describing files that have
not arrived.

There is intentionally no manifest.json yet: until the first release is
published here, the updater treats the channel as unreachable and stays
quiet. Do not hand-write a manifest — publish_release.py generates it.
