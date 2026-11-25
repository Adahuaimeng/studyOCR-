"""Build a relocatable Windows x64 folder and its ZIP archive."""

from argparse import ArgumentParser
from importlib.metadata import distributions
from pathlib import Path
from shutil import copy2
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile


PACKAGING = Path(__file__).resolve().parent
PROJECT = PACKAGING.parent
RELEASE = "StudyOCR-1.2.0-win-x64"


def collect_notices(destination):
    for package in distributions():
        name = package.metadata["Name"]
        if not name or not name.replace("-", "").replace("_", "").replace(".", "").isalnum():
            continue
        for member in package.files or ():
            path = Path(member)
            if path.is_absolute() or ".." in path.parts or not path.name.lower().startswith(
                ("license", "licence", "copying", "notice", "thirdpartynotices")
            ):
                continue
            source = package.locate_file(member)
            if source.is_file():
                target = destination / name / path
                target.parent.mkdir(parents=True, exist_ok=True)
                copy2(source, target)
    python = Path(sys.base_prefix)
    for source in (python / "LICENSE.txt", *(python / "tcl").glob("*/license.terms")):
        if source.is_file():
            copy2(source, destination / (source.parent.name + "-" + source.name))
    copy2(PACKAGING / "assets" / "NotoSansSC-OFL.txt", destination / "NotoSansSC-OFL.txt")


def archive_release(bundle, archive):
    with ZipFile(archive, "w", compression=ZIP_DEFLATED, compresslevel=5) as zip_file:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                zip_file.write(path, Path(RELEASE) / path.relative_to(bundle))


def build(output):
    output.mkdir(parents=True, exist_ok=True)
    release = output / RELEASE
    archive = output / f"{RELEASE}.zip"
    if release.exists() or archive.exists():
        raise FileExistsError(f"发布文件已存在，请选择新目录：{output}")
    with TemporaryDirectory(prefix=".studyocr-build-", dir=output, ignore_cleanup_errors=True) as folder:
        staging = Path(folder)
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--distpath", str(staging / "dist"),
            "--workpath", str(staging / "work"), str(PACKAGING / "StudyOCR.spec"),
        ], cwd=PROJECT, check=True)
        bundle = staging / "dist" / "StudyOCR"
        subprocess.run([str(bundle / "ocr-cli.exe"), "--help"], cwd=staging, check=True,
                       stdout=subprocess.DEVNULL, timeout=30)
        copy2(PACKAGING / "便携版使用说明.txt", bundle / "使用说明.txt")
        copy2(PROJECT / "外部AI调用说明.md", bundle / "外部AI调用说明.md")
        notices = bundle / "licenses"
        notices.mkdir()
        collect_notices(notices)
        pending_zip = staging / archive.name
        archive_release(bundle, pending_zip)
        bundle.rename(release)
        pending_zip.rename(archive)
    print(release)
    print(archive)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT / "发布")
    args = parser.parse_args()
    if sys.platform != "win32" or sys.version_info[:2] != (3, 13) or sys.maxsize <= 2**32:
        parser.error("请使用 Windows x64 上的 64 位 Python 3.13 构建。")
    build(args.output.resolve())


if __name__ == "__main__":
    main()
