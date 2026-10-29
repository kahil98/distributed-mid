"""Create a source submission ZIP without local credentials or caches."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT.parent / f"{ROOT.name}-submission.zip"
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "build-wheels", "dist"}

def excluded(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in SKIP_DIRS for part in rel.parts):
        return True
    if path.name == ".env" or (path.name.startswith(".env.") and path.name not in {".env.example"}):
        return True
    if path.suffix in {".pyc", ".pyo"}:
        return True
    return False

files = sorted(path for path in ROOT.rglob("*") if path.is_file() and not excluded(path))
with ZipFile(OUTPUT, "w", ZIP_DEFLATED, compresslevel=6) as archive:
    for path in files:
        archive.write(path, Path(ROOT.name) / path.relative_to(ROOT))

with ZipFile(OUTPUT) as archive:
    names = archive.namelist()
    assert any(name.endswith("SUBMISSION.md") for name in names)
    assert any("reports/workloads.json" in name for name in names)
    assert any("tests/test_storage_node.py" in name for name in names)
    assert not any(name.endswith("/.env") or name.endswith("/.env.local") for name in names)
print(f"Created {OUTPUT} with {len(files)} files ({OUTPUT.stat().st_size:,} bytes)")
