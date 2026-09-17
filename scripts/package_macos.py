"""构建 AT小PP 的 macOS 发行包（自定义向导安装器 + .dmg）。

流程：
  1. 由 PNG 生成 build_assets/app_icon.icns（仅 macOS 可运行）。
  2. pyinstaller build.spec            -> dist/AT小PP.app
  3. 复制 dist/AT小PP.app -> release/AT小PP.app（作为安装器 payload）
  4. pyinstaller bootstrap.spec        -> dist/AT小PP Installer.app（内嵌 payload）
  5. 裁剪无用 Qt 翻译 / QtPdf 以减小体积
  6. hdiutil 打包为 release/AT小PP-macos.dmg（含「安装器」与 Applications 快捷方式）

仅在 macOS 上运行；依赖 requirements-macos.txt 与 PyInstaller。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
RELEASE = ROOT / "release"
BUILD = ROOT / "build"


def _run(args: list[str | os.PathLike[str]], cwd: Path = ROOT) -> None:
    printable = " ".join(str(a) for a in args)
    print(f"> {printable}")
    subprocess.run([str(a) for a in args], cwd=str(cwd), check=True)


def ensure_icns() -> None:
    icns = ROOT / "build_assets" / "app_icon.icns"
    if icns.exists():
        print(f"icns 已存在，跳过生成：{icns}")
        return
    png = ROOT / "assets" / "app_icon.png"
    if not png.exists():
        print("缺少 assets/app_icon.png，无法生成 icns；请手动放置 build_assets/app_icon.icns")
        return
    tmp = tempfile.mkdtemp(prefix="atpp_iconset_")
    iconset = Path(tmp) / "app.iconset"
    iconset.mkdir()
    sizes = [16, 32, 64, 128, 256, 512, 1024]
    for s in sizes:
        out = iconset / f"icon_{s}x{s}.png"
        _run(["sips", "-z", str(s), str(s), str(png), "--out", str(out)])
        if s * 2 <= 1024:
            out2 = iconset / f"icon_{s}x{s}@2x.png"
            _run(["sips", "-z", str(s * 2), str(s * 2), str(png), "--out", str(out2)])
    _run(["iconutil", "--convert", "icns", "--output", str(icns), str(iconset)])
    print(f"已生成：{icns}")


def _pyinstaller(spec: Path, clean: bool) -> None:
    args = [sys.executable, "-m", "PyInstaller", str(spec), "--noconfirm"]
    if clean:
        args.append("--clean")
    _run(args)


def _prune_app(app_dir: Path) -> None:
    """删除无用的 Qt 翻译与 QtPdf，显著减小 .app 体积。"""
    translations = app_dir / "Contents" / "MacOS" / "PyQt6" / "Qt6" / "translations"
    if translations.exists():
        shutil.rmtree(translations)
        print(f"removed Qt translations: {translations}")
    qt_pdf = app_dir / "Contents" / "Frameworks" / "QtPdf.framework"
    if qt_pdf.exists():
        shutil.rmtree(qt_pdf)
        print(f"removed QtPdf framework: {qt_pdf}")


def build_app() -> Path:
    _pyinstaller(ROOT / "build.spec", True)
    app = DIST / "AT小PP.app"
    if not app.exists():
        raise FileNotFoundError(f"未生成应用包：{app}")
    _prune_app(app)
    return app


def build_installer(payload_app: Path) -> Path:
    RELEASE.mkdir(exist_ok=True)
    # bootstrap.spec 将 release/AT小PP.app 作为 payload 内嵌
    dest = RELEASE / "AT小PP.app"
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(payload_app, dest)
    print(f"payload 已就位：{dest}")
    _pyinstaller(ROOT / "bootstrap.spec", True)
    installer = DIST / "AT小PP Installer.app"
    if not installer.exists():
        raise FileNotFoundError(f"未生成安装器：{installer}")
    _prune_app(installer)
    return installer


def build_dmg(installer_app: Path) -> Path:
    stage = BUILD / "dmg_stage"
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    app_dest = stage / "AT小PP Installer.app"
    shutil.copytree(installer_app, app_dest)

    # 便捷卸载脚本（打开已安装应用的卸载向导）
    uninstall_sh = stage / "uninstall.command"
    uninstall_sh.write_text(
        "#!/bin/bash\n"
        'open -a "AT小PP" --args --uninstall\n',
        encoding="utf-8",
    )
    uninstall_sh.chmod(0o755)

    # Applications 快捷方式（拖放安装入口）
    os.symlink("/Applications", str(stage / "Applications"))

    dmg = RELEASE / "AT小PP-macos.dmg"
    if dmg.exists():
        dmg.unlink()
    _run([
        "hdiutil", "create",
        "-volname", "AT小PP",
        "-srcfolder", str(stage),
        "-ov", "-format", "UDZO",
        str(dmg),
    ])
    print(f"已生成安装包：{dmg}")
    return dmg


def main() -> int:
    parser = argparse.ArgumentParser(description="Build AT小PP macOS installer (.dmg).")
    parser.add_argument("--clean", action="store_true", help="强制完整重建")
    parser.add_argument("--skip-assets", action="store_true", help="复用已有 app_icon.icns")
    args = parser.parse_args()

    if sys.platform != "darwin":
        print("错误：本脚本只能在 macOS 上运行（PyInstaller 无法跨平台编译 .app/.dmg）。", file=sys.stderr)
        print("提示：可在 macOS 本机执行，或用仓库内 .github/workflows/build-macos.yml 在 GitHub macOS Runner 上自动产出 .dmg。", file=sys.stderr)
        return 2

    if not args.skip_assets:
        ensure_icns()

    app = build_app()
    installer = build_installer(app)
    dmg = build_dmg(installer)
    print(f"\nmacOS 发行包构建完成：{dmg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
