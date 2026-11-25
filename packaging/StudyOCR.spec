from importlib.util import find_spec
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata


project = Path(SPECPATH).resolve().parent
packaging = project / "packaging"
assets = packaging / "assets"
sys.path.insert(0, str(project))

from offline import MODELS


rapidocr = Path(find_spec("rapidocr").origin).parent
models = [(rapidocr / "models" / name, "rapidocr/models") for name in MODELS]
datas = [
    (project / "外部AI调用说明.md", "."),
    (assets / "StudyOCR.ico", "assets"),
    (assets / "NotoSansSC.ttf", "assets"),
    *models,
]
for file, _ in datas:
    if not file.is_file():
        raise SystemExit(f"Build input missing: {file}")

datas = [(str(file), destination) for file, destination in datas]
datas += collect_data_files("rapidocr", includes=["config.yaml", "default_models.yaml", "**/*.txt"])
datas += collect_data_files("pypdfium2_raw", includes=["version.json"])
datas += copy_metadata("rapidocr")
datas += copy_metadata("onnxruntime")

runtime_binaries = collect_dynamic_libs("pypdfium2_raw")
hidden_ocr = [
    "rapidocr.main", "rapidocr.inference_engine.onnxruntime.main",
    *collect_submodules("numpy._core", filter=lambda name: ".tests" not in name),
]

desktop = Analysis(
    [str(project / "app.py")],
    pathex=[str(project)],
    binaries=runtime_binaries,
    datas=datas,
    hiddenimports=hidden_ocr,
    runtime_hooks=[str(packaging / "windowed_streams.py")],
)
agent = Analysis(
    [str(project / "ocr_cli.py")],
    pathex=[str(project)],
    hiddenimports=hidden_ocr,
)

app = EXE(
    PYZ(desktop.pure), desktop.scripts, [],
    exclude_binaries=True, name="StudyOCR",
    console=False, icon=str(assets / "StudyOCR.ico"), upx=False,
)
cli = EXE(
    PYZ(agent.pure), agent.scripts, [],
    exclude_binaries=True, name="ocr-cli",
    console=True, icon=str(assets / "StudyOCR.ico"), upx=False,
)
bundle = COLLECT(
    app, cli, desktop.binaries, agent.binaries, desktop.datas, agent.datas,
    upx=False, name="StudyOCR",
)
