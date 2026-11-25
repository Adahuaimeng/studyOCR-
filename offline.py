"""Prepare or install a Windows OCR dependency bundle."""

from argparse import ArgumentParser
from importlib.util import find_spec
from pathlib import Path
from shutil import copy2
import subprocess
import sys
from venv import EnvBuilder


PROJECT = Path(__file__).resolve().parent
MODELS = (
    "ch_PP-OCRv5_det_mobile.onnx",
    "ch_PP-OCRv5_rec_mobile.onnx",
    "ch_PP-OCRv5_rec_server.onnx",
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
)


def model_directory(python=None):
    if python is None:
        package = find_spec("rapidocr")
        if package is None:
            raise ValueError("请先在联网机器上安装 requirements.txt，再准备离线依赖。")
        return Path(package.origin).parent / "models"
    command = "from importlib.util import find_spec; print(find_spec('rapidocr').origin)"
    location = subprocess.check_output([str(python), "-X", "utf8", "-c", command], encoding="utf-8")
    return Path(location.strip()).parent / "models"


def copy_models(source, target):
    missing = [name for name in MODELS if not (source / name).is_file()]
    if missing:
        raise ValueError("找不到 OCR 模型：" + "、".join(missing))
    target.mkdir(parents=True, exist_ok=True)
    for name in MODELS:
        copy2(source / name, target / name)


def prepare(bundle):
    from ocr_core import LocalOcr

    source = model_directory()
    if any(not (source / name).is_file() for name in MODELS):
        ocr = LocalOcr()
        ocr.engine
        ocr.handwriting_engine
    copy_models(source, bundle / "models")
    manifest = copy2(PROJECT / "requirements.txt", bundle / "requirements.txt")
    subprocess.run([sys.executable, "-m", "pip", "download", "--only-binary=:all:",
                    "--dest", str(bundle / "wheels"), "-r", manifest], check=True)


def install(bundle, environment):
    manifest = bundle / "requirements.txt"
    wheels = bundle / "wheels"
    models = bundle / "models"
    if not manifest.is_file() or not any(wheels.glob("*.whl")) or any(
        not (models / name).is_file() for name in MODELS
    ):
        raise ValueError("离线依赖包不完整，需要 requirements.txt、wheels 和 models。")
    EnvBuilder(with_pip=True).create(environment)
    python = environment / "Scripts" / "python.exe"
    subprocess.run([str(python), "-m", "pip", "--disable-pip-version-check", "install", "--no-index",
                    "--find-links", str(wheels),
                    "-r", str(manifest)], check=True)
    copy_models(models, model_directory(python))


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "install"))
    parser.add_argument("--bundle", type=Path, default=PROJECT / "离线依赖")
    parser.add_argument("--venv", type=Path, default=PROJECT / ".venv")
    args = parser.parse_args()
    if sys.platform != "win32" or sys.version_info[:2] != (3, 13) or sys.maxsize <= 2**32:
        parser.error("请使用 Windows 64 位 Python 3.13。")
    try:
        if args.action == "prepare":
            prepare(args.bundle.resolve())
        else:
            install(args.bundle.resolve(), args.venv.resolve())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
