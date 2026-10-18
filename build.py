"""Packages the addon directory into controllerShutup-<version>.nvda-addon."""

import pathlib
import re
import zipfile

root = pathlib.Path(__file__).resolve().parent
addonDir = root / "addon"
manifest = (addonDir / "manifest.ini").read_text(encoding="utf-8")
version = re.search(r"^version\s*=\s*(\S+)", manifest, re.M).group(1)
outPath = root / f"controllerShutup-{version}.nvda-addon"

with zipfile.ZipFile(outPath, "w", zipfile.ZIP_DEFLATED) as zf:
	for path in sorted(addonDir.rglob("*")):
		if path.is_file() and "__pycache__" not in path.parts:
			zf.write(path, path.relative_to(addonDir).as_posix())

print(f"Built {outPath}")
