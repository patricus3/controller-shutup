"""Packages the addon directory into controllerShutup-<version>.nvda-addon.

The current version's section of CHANGELOG.md is written into the packaged manifest's
changelog key, which the Add-on Store shows on the add-on's entry.
"""

import pathlib
import re
import zipfile

root = pathlib.Path(__file__).resolve().parent
addonDir = root / "addon"
manifest = (addonDir / "manifest.ini").read_text(encoding="utf-8")
version = re.search(r"^version\s*=\s*(\S+)", manifest, re.M).group(1)

changelogMatch = re.search(
	rf"^## {re.escape(version)}\s*\n(.*?)(?=^## |\Z)",
	(root / "CHANGELOG.md").read_text(encoding="utf-8"),
	re.M | re.S,
)
if not changelogMatch:
	raise SystemExit(f"CHANGELOG.md has no section for version {version}")
changelog = changelogMatch.group(1).strip()
if '"""' in changelog:
	raise SystemExit('The changelog section must not contain """')
manifest = manifest.rstrip("\n") + f'\nchangelog = """{changelog}"""\n'

outPath = root / f"controllerShutup-{version}.nvda-addon"
with zipfile.ZipFile(outPath, "w", zipfile.ZIP_DEFLATED) as zf:
	zf.writestr("manifest.ini", manifest)
	for path in sorted(addonDir.rglob("*")):
		if path.is_file() and path.name != "manifest.ini" and "__pycache__" not in path.parts:
			zf.write(path, path.relative_to(addonDir).as_posix())

print(f"Built {outPath}")
