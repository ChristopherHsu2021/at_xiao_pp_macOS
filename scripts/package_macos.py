"""构建 AT小PP 的 macOS 发行包（自定义向导安装器 + .dmg）。

流程：
  1. 由 PNG 生成 build_assets/app_icon.icns（仅 macOS 可运行）。
  2. pyinstaller build.spec            -> dist/AT小PP.app
  3. 复制 dist/AT小PP.app -> release/AT小PP.app（作为安装器 payload）
  4. pyinstaller bootstrap.spec        -> dist/AT小PP Installer.app（内嵌 payload）
  5. 裁剪无用 Qt 翻译 / QtPdf 以减小体积
  6. 对 .app 做免费自签名（ad-hoc codesign），消除「App 已损坏」类拦截
  7. hdiutil 打包为 release/AT小PP-macos.dmg（含「安装器」与 Applications 快捷方式）

仅在 macOS 上运行；依赖 requirements-macos.txt 与 PyInstaller。
全程零成本：无需 Apple 付费开发者账号（自签名为本地 ad-hoc，免费）。

兼容性说明：
  - 默认产出「宿主机架构」的 .app（Apple Silicon 宿主机 -> arm64，Intel 宿主机 -> x86_64）。
    x86_64 包可在 Apple Silicon 上经 Rosetta 2 运行，因此「在 Intel 宿主机上构建」可覆盖最广。
  - 加 --universal 可产出 universal2（Intel+Apple Silicon 单包通吃），需宿主机 Python 为
    universal2 且依赖含通用切片；否则自动回退宿主机架构，不会构建失败。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import struct
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


def _python_is_universal() -> bool:
    """宿主机 Python 是否为 universal2 构建（可产出通用二进制）。"""
    try:
        out = subprocess.run(
            ["file", sys.executable], capture_output=True, text=True
        ).stdout.lower()
        return "universal" in out
    except Exception:
        return False


def _codesign(path: Path) -> None:
    """免费自签名（ad-hoc）：`codesign --force --deep --sign -`。

    作用：满足 Gatekeeper 对「已签名」的要求，消除「App 已损坏 / 无法验证」类拦截。
    不花钱、无需 Apple 开发者账号。局限：下载自网络的未公证包首次启动仍可能弹「无法确认
    开发者」——此时右键→打开 或 `xattr -dr com.apple.quarantine` 一次即可（本地拷贝无此问题）。
    """
    print(f"codesign (ad-hoc): {path}")
    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(path)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


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
    # Apple 标准 iconset 恰好 10 个成员（多出 icon_64x64/icon_1024x1024 等非标准
    # 文件名会让 iconutil 直接报 "Failed to generate ICNS"，已踩坑验证）。
    # 命名 -> (像素尺寸, 对应 ICNS OSType)
    #   ic07=128 ic08=256 ic09=512 ic10=512@2x ic11=16@2x ic12=32@2x ic13=128@2x ic14=256@2x
    entries = [
        ("icon_16x16.png", 16, None),
        ("icon_16x16@2x.png", 32, "ic11"),
        ("icon_32x32.png", 32, None),
        ("icon_32x32@2x.png", 64, "ic12"),
        ("icon_128x128.png", 128, "ic07"),
        ("icon_128x128@2x.png", 256, "ic13"),
        ("icon_256x256.png", 256, "ic08"),
        ("icon_256x256@2x.png", 512, "ic14"),
        ("icon_512x512.png", 512, "ic09"),
        ("icon_512x512@2x.png", 1024, "ic10"),
    ]
    for name, px, _ostype in entries:
        out = iconset / name
        _run(["sips", "-z", str(px), str(px), str(png), "--out", str(out)])

    # 优先用 iconutil 官方转换；它在部分 runner/环境下会无故报
    # "Failed to generate ICNS"（无 verbose 可查，社区多起同类案例），
    # 因此失败时回退为手工组装 ICNS 容器——现代 .icns 支持直接内嵌 PNG，
    # 此方法结果 100% 确定性，不依赖 iconutil。
    try:
        _run(["iconutil", "--convert", "icns", "--output", str(icns), str(iconset)])
    except subprocess.CalledProcessError:
        print("iconutil 失败，回退为手工组装 ICNS（PNG 内嵌容器）...")
        _write_icns_manual(icns, iconset, entries)
    print(f"已生成：{icns}")


def _write_icns_manual(
    icns: Path,
    iconset: Path,
    entries: list[tuple[str, int, str | None]],
) -> None:
    """手工组装 ICNS：'icns' 魔数 + 总长度 + 若干 (OSType, 长度, PNG 数据) 条目。

    现代 macOS（10.15+）的 .icns 允许 PNG 压缩条目， Finder/Dock 均正常显示。
    """
    blocks: list[bytes] = []
    for name, _px, ostype in entries:
        if not ostype:
            continue  # icon_16x16/icon_32x32 的一倍图可省略（有 @2x 已足够）
        data = (iconset / name).read_bytes()
        blocks.append(ostype.encode("ascii") + struct.pack(">I", len(data) + 8) + data)
    total = 8 + sum(len(b) for b in blocks)
    payload = b"icns" + struct.pack(">I", total) + b"".join(blocks)
    icns.write_bytes(payload)


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
    _codesign(app)
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
    # --deep 会递归签名内嵌的 payload/AT小PP.app，安装后落到 ~/Applications 即为已签名状态
    _codesign(installer)
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
    # 对 .dmg 也做自签名，进一步降低挂载时的拦截
    _codesign(dmg)
    print(f"已生成安装包：{dmg}")
    return dmg


def main() -> int:
    parser = argparse.ArgumentParser(description="Build AT小PP macOS installer (.dmg).")
    parser.add_argument("--clean", action="store_true", help="强制完整重建")
    parser.add_argument("--skip-assets", action="store_true", help="复用已有 app_icon.icns")
    parser.add_argument(
        "--universal", action="store_true",
        help="产出通用二进制 universal2（Intel+Apple Silicon）。需宿主机 Python 为 universal2 且依赖含通用切片。",
    )
    args = parser.parse_args()

    if sys.platform != "darwin":
        print("错误：本脚本只能在 macOS 上运行（PyInstaller 无法跨平台编译 .app/.dmg）。", file=sys.stderr)
        print("提示：请在 macOS 本机（或 macOS 虚拟机）中执行本脚本，全程免费、无需 GitHub 配额。", file=sys.stderr)
        return 2

    if args.universal:
        if not _python_is_universal():
            print(
                "警告：当前 Python 不是 universal2 构建，无法产出通用二进制；"
                "将回退为宿主机架构（仍可用）。若需 universal2，请改用 python.org 的 universal2 安装包。",
                file=sys.stderr,
            )
        else:
            os.environ["ATPP_TARGET_ARCH"] = "universal2"
            print("已启用 universal2 通用二进制构建。")

    if not args.skip_assets:
        ensure_icns()

    app = build_app()
    installer = build_installer(app)
    dmg = build_dmg(installer)
    print(f"\nmacOS 发行包构建完成：{dmg}")
    if args.universal:
        print("（注：universal2 实际成败取决于 PyQt6/Qt 等依赖是否提供通用切片；"
              "若 .app 仅含单架构，请用 Intel 宿主机构建以获得最广覆盖。）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
