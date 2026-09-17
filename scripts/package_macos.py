"""构建 AT小PP 的 macOS 发行包（.dmg 直接含真身 AT小PP.app，无安装/卸载向导）。

流程：
  1. 由 PNG 生成 build_assets/app_icon.icns（仅 macOS 可运行）。
  2. pyinstaller build.spec            -> dist/AT小PP.app（真身应用，无控制台黑窗）
  3. 裁剪无用 Qt 翻译 / QtPdf 以减小体积
  4. 对 .app 做免费自签名（ad-hoc codesign），消除「App 已损坏」类拦截
  5. hdiutil 打包为 release/AT小PP-macos.dmg（含 AT小PP.app 与 Applications 快捷方式）

使用方式（类比 Windows「安装到本地」）：
  - 挂载 .dmg 后，把里面的 AT小PP.app 拖进 /Applications（或 ~/Applications）即可。
  - 卸载 = 把 AT小PP.app 拖进废纸篓。无需任何向导。

仅在 macOS 上运行；依赖 requirements-macos.txt 与 PyInstaller。
全程零成本：无需 Apple 付费开发者账号（自签名为本地 ad-hoc，免费）。

兼容性说明（兼容性优先方案）：
  - 通过环境变量 ATPP_TARGET_ARCH=x86_64 产出 x86_64 包：
      Intel Mac 原生运行；Apple Silicon Mac 经 Rosetta 2 运行。
      单个 .dmg 即可覆盖几乎所有 Mac，系统兼容性最强（这也是默认 CI 方案）。
  - 加 --universal 可尝试产出 universal2（Intel+Apple Silicon 单包通吃），
    需宿主机 Python 为 universal2 且依赖含通用切片；否则自动回退宿主机架构。
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
    # build_assets/ 被 .gitignore 排除（CI 上不存在），必须先建目录
    icns.parent.mkdir(parents=True, exist_ok=True)
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


def _qt6_root(app_dir: Path) -> Path | None:
    """定位 bundle 内的 PyQt6/Qt6 目录。

    注意：macOS .app 的 PyInstaller one-folder 实际位于 Contents/Frameworks
    （sys._MEIPASS），不是 Contents/MacOS —— 不能写死路径，必须全包搜索。
    """
    hits = [p for p in app_dir.rglob("PyQt6/Qt6") if p.is_dir()]
    return hits[0] if hits else None


def _prune_app(app_dir: Path) -> None:
    """删除无用的 Qt 翻译与 QtPdf，显著减小 .app 体积。

    注意：PyQt6/Qt6 在 bundle 内可能有多份（Frameworks/Resources，可能互为软链）。
    只删其中一份会让另一份变悬空软链 → 后续 copytree 崩溃。必须全量清理。
    """
    for translations in list(app_dir.rglob("PyQt6/Qt6/translations")):
        try:
            if translations.is_symlink():
                translations.unlink()
            elif translations.is_dir():
                shutil.rmtree(translations)
            print(f"removed Qt translations: {translations}")
        except FileNotFoundError:
            pass  # 已随软链目标一起消失
    qt_pdf = app_dir / "Contents" / "Frameworks" / "QtPdf.framework"
    if qt_pdf.exists():
        shutil.rmtree(qt_pdf)
        print(f"removed QtPdf framework: {qt_pdf}")


def _ensure_qt_conf(app_dir: Path) -> None:
    """显式写入 qt.conf，强制 Qt 用文件系统路径解析 Qt 库/插件，而非查询主 bundle。

    关键修复：PyQt6/Qt 6.x 在 .so 静态初始化期会调用 QLibraryInfo::path()
    -> CFBundleCopyBundleURL()，若 qt.conf 缺失则该调用在 CFBundleGetMainBundle()
    返回 NULL 时直接 SIGSEGV（EXC_BAD_ACCESS）。显式给出 Prefix 可彻底绕开 bundle 查询。
    """
    macos_dir = app_dir / "Contents" / "MacOS"
    qt6_dir = _qt6_root(app_dir)
    if qt6_dir is None:
        print("警告：未找到 PyQt6/Qt6 目录，跳过 qt.conf 写入")
        return
    # Prefix 为相对于 qt.conf 所在目录（Contents/MacOS）的路径
    prefix = os.path.relpath(qt6_dir, macos_dir)
    conf = (
        "[Paths]\n"
        f"Prefix = {prefix}\n"
        f"Libraries = {prefix}/lib\n"
        f"Plugins = {prefix}/plugins\n"
        f"Imports = {prefix}/imports\n"
        f"Qml2Imports = {prefix}/qml\n"
        f"ArchData = {prefix}\n"
        f"Data = {prefix}\n"
        f"Translations = {prefix}/translations\n"
    )
    # Qt 查找顺序：可执行文件同目录 -> ../Resources；两者都写，确保命中
    targets = [macos_dir / "qt.conf"]
    resources_dir = app_dir / "Contents" / "Resources"
    if resources_dir.exists():
        targets.append(resources_dir / "qt.conf")
    for t in targets:
        t.write_text(conf, encoding="utf-8")
        print(f"wrote qt.conf -> {t} (Prefix={prefix})")


def _verify_multimedia_plugins(app_dir: Path) -> None:
    """校验 QtMultimedia 后端插件已打进包（缺失 = QMediaPlayer 静默无声）。"""
    qt6 = _qt6_root(app_dir)
    plugins_root = qt6 / "plugins" if qt6 is not None else None
    multimedia = plugins_root / "multimedia" if plugins_root else None
    if not multimedia or not multimedia.is_dir() or not any(multimedia.iterdir()):
        raise RuntimeError(
            "QtMultimedia 后端插件（plugins/multimedia/，AVFoundation 后端）未打进 .app！"
            "这将导致音乐播放彻底无声。请检查 PyInstaller 的 hook-PyQt6.QtMultimedia 是否生效。"
        )
    names = [f.name for f in sorted(multimedia.iterdir())]
    print(f"multimedia 后端插件已就位：{names}")


def build_app() -> Path:
    _pyinstaller(ROOT / "build.spec", True)
    app = DIST / "AT小PP.app"
    if not app.exists():
        raise FileNotFoundError(f"未生成应用包：{app}")
    _prune_app(app)
    _ensure_qt_conf(app)
    _verify_multimedia_plugins(app)
    _codesign(app)
    return app


def build_dmg(app_dir: Path) -> Path:
    """把真正的 AT小PP.app 直接打进 .dmg（拖进 /Applications 即用，无安装向导）。"""
    RELEASE.mkdir(parents=True, exist_ok=True)
    stage = BUILD / "dmg_stage"
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    app_dest = stage / "AT小PP.app"
    # ignore_dangling_symlinks=True：Qt 目录可能残留悬空软链，不兜底会整包崩溃
    shutil.copytree(app_dir, app_dest, ignore_dangling_symlinks=True)

    # Applications 快捷方式（拖放安装入口：把 .app 拖进这里即装到 /Applications）
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
    dmg = build_dmg(app)
    print(f"\nmacOS 发行包构建完成：{dmg}")
    print("使用：挂载 .dmg 后把 AT小PP.app 拖进 /Applications（或 ~/Applications）即可；卸载 = 拖进废纸篓。")
    if args.universal:
        print("（注：universal2 实际成败取决于 PyQt6/Qt 等依赖是否提供通用切片；"
              "若 .app 仅含单架构，请用 Intel 宿主机构建以获得最广覆盖。）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
