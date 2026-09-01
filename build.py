"""Pack the add-on into an installable extension zip.

Usage: python build.py
Output: dist/<id>-<version>.zip
(zip 根目录必须直接包含 blender_manifest.toml 和 __init__.py)
"""
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
manifest = (ROOT / "blender_manifest.toml").read_text(encoding="utf-8")
ext_id = re.search(r'^id\s*=\s*"([^"]+)"', manifest, re.M).group(1)
version = re.search(r'^version\s*=\s*"([^"]+)"', manifest, re.M).group(1)

out = ROOT / "dist" / f"{ext_id}-{version}.zip"
out.parent.mkdir(exist_ok=True)

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
    for name in ("blender_manifest.toml", "__init__.py"):
        zf.write(ROOT / name, name)

print(f"Built {out}")
