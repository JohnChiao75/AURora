#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AURora — 轻量级 AUR 包维护 CLI。

子命令：
    init      初始化包工程（git clone + .gitignore）
    edit      编辑 PKGBUILD
    generate  生成 .SRCINFO
    clean     清理构建产物
    build     构建（-i 安装 / -c 清理 / -h 用 AUR 助手）
    publish   补 .SRCINFO → commit → push
    bump      更新 pkgver / pkgrel
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

__version__ = "0.1.0"

PKGBUILD = "PKGBUILD"
SRCINFO = ".SRCINFO"

AUR_SSH = "ssh://aur@aur.archlinux.org/{name}.git"
HELPERS = ("paru", "yay", "pikaur", "trizen")

NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9@._+-]*$")
PKGVER_RE = re.compile(r"^[A-Za-z0-9._+]+$")
VCS_SUFFIX = ("-git", "-svn", "-hg", "-bzr", "-cvs", "-nightly")

SAFE_DIRS = ("pkg", "src")
SAFE_PATTERNS = (
    "*.pkg.tar", "*.pkg.tar.*", "*.src.tar.*",
    "*.tar", "*.tar.gz", "*.tar.bz2", "*.tar.xz", "*.tar.zst",
    "*.tgz", "*.zip", "*.log",
)
KEEP_ALWAYS = {PKGBUILD, SRCINFO, ".gitignore", ".git"}
KEEP_SUFFIXES = (".install", ".patch", ".diff", ".service", ".hook", ".preset", ".sh")
KEEP_PREFIXES = ("README", "LICENSE", "COPYING", "NEWS", "CHANGELOG")


# --------------------------------------------------------------------- 输出

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def info(msg: str) -> None:
    print(f"{_paint('1;36', '::')} {msg}")


def step(msg: str) -> None:
    print(f"{_paint('1;34', '==>')} {msg}")


def ok(msg: str) -> None:
    print(f"{_paint('1;32', '==>')} {msg}")


def warn(msg: str) -> None:
    print(f"{_paint('1;33', 'warning:')} {msg}", file=sys.stderr)


def err(msg: str) -> None:
    print(f"{_paint('1;31', 'error:')} {msg}", file=sys.stderr)


def die(msg: str, code: int = 1) -> None:
    err(msg)
    raise SystemExit(code)


def confirm(prompt: str, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        warn("非交互环境，默认不确认（需要的话加 -y）")
        return False
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ------------------------------------------------------------------ 执行外部命令

def run(cmd, cwd=None, capture: bool = False, check: bool = True):
    """跑一条外部命令。capture=True 时返回带 stdout/stderr 的 CompletedProcess。"""
    argv = [str(c) for c in cmd]
    if os.environ.get("AURORA_VERBOSE"):
        info("$ " + " ".join(shlex.quote(a) for a in argv))
    kwargs = {"cwd": str(cwd) if cwd else None}
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    proc = subprocess.run(argv, **kwargs)
    if check and proc.returncode != 0:
        die("命令失败（{}）：{}".format(
            proc.returncode, " ".join(shlex.quote(a) for a in argv)))
    return proc


def git(*args, cwd=None, check: bool = True, capture: bool = False):
    return run(["git", *args], cwd=cwd, check=check, capture=capture)


def git_config(key: str) -> str:
    if shutil.which("git") is None:
        return ""
    proc = git("config", "--get", key, check=False, capture=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


# ------------------------------------------------------------------ PKGBUILD 解析

def project_root(start=None) -> Path:
    """从 start 向上找含 PKGBUILD 的目录。"""
    base = Path(start or os.getcwd()).resolve()
    for d in (base, *base.parents):
        if (d / PKGBUILD).is_file():
            return d
    die("这里（以及上层目录）都没有 PKGBUILD —— 先 cd 进包目录吧")
    raise AssertionError  # pragma: no cover


def get_var(text: str, name: str):
    """读取 PKGBUILD 里 name= 的值（去掉引号和行尾注释）。"""
    pat = re.compile(
        r"^[ \t]*(?:export[ \t]+)?" + re.escape(name) + r"[ \t]*=[ \t]*(.*?)[ \t]*$",
        re.MULTILINE)
    m = pat.search(text)
    if not m:
        return None
    val = m.group(1).strip()
    if val[:1] not in ("'", '"') and " #" in val:
        val = val.split(" #", 1)[0].strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
        val = val[1:-1]
    return val


def set_var(text: str, name: str, value: str):
    """替换 name= 那一行，返回 (新文本, 是否成功)。"""
    quoted = '"' if re.search(r"""[\s$'"\\]""", value) else ""
    line = f"{name}={quoted}{value}{quoted}"
    pat = re.compile(
        r"^[ \t]*(?:export[ \t]+)?" + re.escape(name) + r"[ \t]*=.*$", re.MULTILINE)
    if not pat.search(text):
        return text, False
    return pat.sub(lambda _m: line, text, count=1), True


def insert_after_var(text: str, anchor: str, line: str):
    """在 anchor= 那行之后插入一行。"""
    pat = re.compile(
        r"^[ \t]*(?:export[ \t]+)?" + re.escape(anchor) + r"[ \t]*=.*$", re.MULTILINE)
    m = pat.search(text)
    if not m:
        return text, False
    return text[:m.end()] + "\n" + line + text[m.end():], True


_REF_RE = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?$")


def deref(text: str, value):
    """把 $var / ${var} 解一层，返回 (真实值, 变量名或 None)。"""
    if value is None:
        return None, None
    m = _REF_RE.match(value.strip())
    if not m:
        return value, None
    name = m.group(1)
    target = get_var(text, name)
    return (target, name) if target is not None else (value, name)


@dataclass
class Meta:
    pkgname: str
    pkgver: str
    pkgrel: str
    pkgver_var: str      # 真正承载版本号的变量名（通常是 pkgver）
    pkgrel_var: str
    text: str

    @property
    def tag(self) -> str:
        return f"{self.pkgver}-{self.pkgrel}"


def load_meta(root: Path) -> Meta:
    path = root / PKGBUILD
    if not path.is_file():
        die(f"{path} 不存在")
    text = path.read_text(encoding="utf-8", errors="replace")

    raw_name = get_var(text, "pkgname") or ""
    name = raw_name.strip("()").split()[0] if raw_name else root.name

    raw_ver = get_var(text, "pkgver")
    if raw_ver is None:
        die("PKGBUILD 里找不到 pkgver")
    ver, ver_var = deref(text, raw_ver)

    raw_rel = get_var(text, "pkgrel")
    rel, rel_var = deref(text, raw_rel if raw_rel is not None else "1")

    return Meta(name, ver, rel or "1", ver_var or "pkgver", rel_var or "pkgrel", text)


def bump_last_number(value: str) -> str:
    """把字符串里最后一个数字段 +1。"""
    matches = list(re.finditer(r"\d+", value))
    if not matches:
        return value + ".1"
    m = matches[-1]
    return f"{value[:m.start()]}{int(m.group()) + 1}{value[m.end():]}"


# ------------------------------------------------------------------ 模板

TEMPLATE_GITIGNORE = """\
# ---- 由 AURora 生成 ----
# 构建产物：不要提交
pkg/
src/
*.pkg.tar
*.pkg.tar.*
*.src.tar.*
*.tar
*.tar.gz
*.tgz
*.zip
*.log

# 编辑器 / 临时文件
*.swp
*.swo
*~
.DS_Store

# 注意：.SRCINFO 必须提交，永远不要写进这里
"""

TEMPLATE_PKGBUILD = """\
# Maintainer: {maintainer}
pkgname={name}
pkgver=0.1.0
pkgrel=1
pkgdesc=""
arch=('any')
url=""
license=('MIT')
depends=()
makedepends=()
source=()
sha256sums=()

package() {{
	cd "$srcdir/$pkgname-$pkgver"
	install -Dm755 "$pkgname" "$pkgdir/usr/bin/$pkgname"
}}
"""


# ------------------------------------------------------------------ 命令：init

def cmd_init(args) -> None:
    name = args.name
    if not NAME_RE.match(name):
        die("包名不合法：只能是字母数字和 @ . _ + -，且不能以 - 或 . 开头")
    if shutil.which("git") is None:
        die("找不到 git（pacman -S git）")

    dest = Path.cwd() / name
    if dest.exists():
        die(f"{dest} 已经存在了")

    url = AUR_SSH.format(name=name)
    step(f"克隆 AUR 仓库：{url}")
    proc = run(["git", "clone", "--quiet", url, str(dest)], check=False)

    fresh_repo = False
    if proc.returncode != 0:
        warn("克隆失败——仓库还不存在，或者 AUR 账户里没登记这台机器的 SSH 公钥")
        info("改为本地初始化；第一次 push 时 AUR 会自动创建仓库")
        dest.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "--quiet"], cwd=dest)
        run(["git", "remote", "add", "origin", url], cwd=dest)
        fresh_repo = True
    else:
        fresh_repo = not [p for p in dest.iterdir() if p.name != ".git"]

    gitignore = dest / ".gitignore"
    if gitignore.exists() and not args.force:
        info(".gitignore 已存在，保持原样（--force 可覆盖）")
    else:
        gitignore.write_text(TEMPLATE_GITIGNORE, encoding="utf-8")
        ok("写入 .gitignore（排除构建产物，保留 .SRCINFO）")

    pkgbuild = dest / PKGBUILD
    if args.no_template:
        pass
    elif pkgbuild.exists():
        info("PKGBUILD 已存在，保持原样")
    else:
        who = git_config("user.name") or "Unknown Maintainer"
        mail = git_config("user.email")
        maintainer = f"{who} <{mail}>" if mail else who
        pkgbuild.write_text(
            TEMPLATE_PKGBUILD.format(name=name, maintainer=maintainer),
            encoding="utf-8")
        ok("写入 PKGBUILD 模板")

    print()
    info("下一步：")
    print(f"    cd {name}")
    print("    aur edit          # 填 pkgdesc / url / source / depends")
    print("    aur generate      # 生成 .SRCINFO")
    print("    aur build -ich    # 构建、安装、清理")
    print("    aur publish       # 提交到 AUR")
    if fresh_repo:
        warn("记得先在 AUR 账户设置页登记 SSH 公钥，否则 publish 会失败")


# ------------------------------------------------------------------ 命令：edit

def pick_editor() -> list:
    for var in ("AURORA_EDITOR", "VISUAL", "EDITOR"):
        val = os.environ.get(var)
        if val:
            return shlex.split(val)
    for cand in ("nano", "vim", "vi", "micro", "emacs", "nvim"):
        if shutil.which(cand):
            return [cand]
    die("找不到编辑器：设置 $EDITOR，或者装个 nano")
    raise AssertionError  # pragma: no cover


def cmd_edit(args) -> None:
    root = project_root()
    if args.file:
        target = Path(args.file)
        if not target.is_absolute():
            target = root / target
    else:
        target = root / PKGBUILD
    if not target.is_file():
        die(f"{target} 不存在")

    editor = pick_editor()
    rel = target.relative_to(root) if root in target.parents else target
    info(f"打开 {rel}（{shlex.join(editor)}）")
    run([*editor, str(target)])

    if target.name == PKGBUILD and (root / SRCINFO).is_file():
        warn(".SRCINFO 可能已经过期：跑 aur generate，或直接 aur publish 会自动补")


# ------------------------------------------------------------------ 命令：generate

def cmd_generate(args=None) -> None:
    root = project_root()
    if is_root():
        die("makepkg 拒绝以 root 运行，请用普通用户")
    if shutil.which("makepkg") is None:
        die("找不到 makepkg（pacman -S base-devel）")

    step("makepkg --printsrcinfo")
    proc = run(["makepkg", "--printsrcinfo"], cwd=root, capture=True, check=False)
    out = proc.stdout or ""
    if proc.returncode != 0 or not out.strip():
        detail = (proc.stderr or out or "").strip()
        die("生成 .SRCINFO 失败：\n" + detail)

    if not out.endswith("\n"):
        out += "\n"
    (root / SRCINFO).write_text(out, encoding="utf-8")
    meta = load_meta(root)
    ok(f"{SRCINFO} 已更新（{meta.tag}）")


# ------------------------------------------------------------------ 命令：clean

def _du(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GiB"


def _tracked_top_level(root: Path) -> set:
    if shutil.which("git") is None:
        return set()
    proc = run(["git", "ls-files", "-z"], cwd=root, check=False, capture=True)
    if proc.returncode != 0:
        return set()
    return {item.split("/", 1)[0] for item in (proc.stdout or "").split("\0") if item}


def _clean_targets(root: Path, aggressive: bool) -> list:
    targets = []
    for entry in sorted(root.iterdir()):
        name = entry.name
        if name in KEEP_ALWAYS:
            continue
        if entry.is_dir():
            if aggressive or name in SAFE_DIRS:
                targets.append(entry)
            continue
        if aggressive:
            if any(name.endswith(s) for s in KEEP_SUFFIXES):
                continue
            if name.startswith(KEEP_PREFIXES):
                continue
            targets.append(entry)
        elif any(fnmatch.fnmatch(name, p) for p in SAFE_PATTERNS):
            targets.append(entry)

    if aggressive and targets:
        tracked = _tracked_top_level(root)
        targets = [t for t in targets if t.name not in tracked]
    return targets


def _delete(targets) -> int:
    freed = 0
    for t in targets:
        size = _du(t)
        if t.is_dir():
            shutil.rmtree(t, ignore_errors=True)
        else:
            try:
                t.unlink()
            except OSError as exc:
                warn(f"{t.name}: {exc}")
        freed += size
    return freed


def cmd_clean(args) -> None:
    root = project_root()
    aggressive = args.all

    if aggressive and not args.yes:
        warn("--all 会删掉 PKGBUILD/.SRCINFO/.gitignore 之外所有未跟踪文件（包括没提交的源码）")

    targets = _clean_targets(root, aggressive)
    if not targets:
        ok("已经很干净了")
        return

    freed = 0
    for t in targets:
        size = _du(t)
        freed += size
        kind = "dir " if t.is_dir() else "file"
        print(f"  {'would rm' if args.dry_run else 'rm      '} {kind}  {t.name}  ({_human(size)})")

    if args.dry_run:
        info(f"dry-run：共 {len(targets)} 项，约 {_human(freed)}，什么都没动")
        return

    if aggressive and not confirm(f"确认删除以上 {len(targets)} 项？", args.yes):
        die("已取消")

    freed = _delete(targets)
    ok(f"清理完成，释放约 {_human(freed)}")


# ------------------------------------------------------------------ 命令：build

_BUILD_SUPPORT: dict = {}


def detect_helper():
    for h in HELPERS:
        if shutil.which(h):
            return h
    return None


def helper_supports_build(helper: str) -> bool:
    """探测助手是否支持 -B（从 --help 输出里找）。"""
    if helper in _BUILD_SUPPORT:
        return _BUILD_SUPPORT[helper]
    proc = run([helper, "--help"], capture=True, check=False)
    blob = (proc.stdout or "") + (proc.stderr or "")
    supports = bool(re.search(r"(?:^|[\s,(])-B(?:[\s,)\]]|$)", blob)) or "--build" in blob
    _BUILD_SUPPORT[helper] = supports
    return supports


def _package_files(root: Path) -> list:
    return [p for p in sorted(root.glob("*.pkg.tar*")) if not p.name.endswith(".sig")]


def _report_artifacts(root: Path) -> None:
    pkgs = _package_files(root)
    if not pkgs:
        warn("没看到构建产物，makepkg 可能把它输出到别处了")
        return
    for p in pkgs:
        ok(f"产物：{p.name}（{_human(p.stat().st_size)}）")


def makepkg_cmd(install: bool, clean: bool, noconfirm: bool) -> list:
    cmd = ["makepkg", "-s", "-f"]
    if install:
        cmd.append("-i")
    if clean:
        cmd.append("-c")
    if noconfirm:
        cmd.append("--noconfirm")
    return cmd


def cmd_build(args) -> None:
    root = project_root()
    if is_root():
        die("别用 root 跑构建（AUR 助手自己会调 sudo）")

    if args.helper:
        helper = detect_helper()
        if helper is None:
            die("没检测到 AUR 助手（paru/yay/pikaur/trizen）；去掉 -h 用 makepkg 构建")
        info(f"AUR 助手：{helper}")

        if helper_supports_build(helper):
            cmd = [helper, "-B"]
            if args.install:
                cmd.append("-i")
            cmd.append("--noconfirm")
            step(" ".join(cmd))
            proc = run(cmd, cwd=root, check=False)
            if proc.returncode != 0:
                die(f"{helper} 构建失败（退出码 {proc.returncode}）")
        else:
            warn(f"{helper} 不支持 -B，回退：makepkg 构建 + {helper} -U 安装")
            proc = run(makepkg_cmd(False, False, True), cwd=root, check=False)
            if proc.returncode != 0:
                die("makepkg 构建失败")
            if args.install:
                pkgs = _package_files(root)
                if not pkgs:
                    die("没有找到构建产物，无法安装")
                run([helper, "-U", "--noconfirm", *map(str, pkgs)])

        if args.clean:
            _delete(_clean_targets(root, False))
            ok("构建文件已清理")
        _report_artifacts(root)
        return

    if shutil.which("makepkg") is None:
        die("找不到 makepkg（pacman -S base-devel）")

    cmd = makepkg_cmd(args.install, args.clean, not args.ask)
    step(" ".join(cmd))
    proc = run(cmd, cwd=root, check=False)
    if proc.returncode != 0:
        die(f"makepkg 失败（退出码 {proc.returncode}）")
    _report_artifacts(root)


# ------------------------------------------------------------------ 命令：publish

def _srcinfo_versions(root: Path):
    path = root / SRCINFO
    if not path.is_file():
        return None, None
    ver = rel = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^pkgver\s*=\s*(\S+)", line)
        if m and ver is None:
            ver = m.group(1)
        m = re.match(r"^pkgrel\s*=\s*(\S+)", line)
        if m and rel is None:
            rel = m.group(1)
    return ver, rel


def cmd_publish(args) -> None:
    root = project_root()
    if shutil.which("git") is None:
        die("找不到 git")
    if not (root / ".git").exists():
        die("这里不是 git 仓库（先用 aur init 初始化）")

    meta = load_meta(root)

    # 1) .SRCINFO 必须和 PKGBUILD 对得上
    if not (root / SRCINFO).is_file():
        info(".SRCINFO 不存在，先补一个")
        cmd_generate()
    else:
        ver, rel = _srcinfo_versions(root)
        if (ver, rel) != (meta.pkgver, meta.pkgrel):
            info(f".SRCINFO 还是 {ver}-{rel}，PKGBUILD 已经是 {meta.tag}，重新生成")
            cmd_generate()
        else:
            ok(f".SRCINFO 与 PKGBUILD 一致（{meta.tag}）")

    # 2) 一点点 lint
    if "SKIP" in meta.text and not meta.pkgname.endswith(VCS_SUFFIX):
        warn("sha256sums 里出现了 SKIP —— 非 VCS 包应该给出真实校验和")

    # 3) 有没有东西要提交
    status = git("status", "--porcelain", cwd=root, capture=True).stdout
    if not status.strip() and not args.force:
        ok("工作区没有改动，不需要发布")
        return

    if args.dry_run:
        info("dry-run：将要提交的内容")
        print(status.rstrip() or "  (无)")
        return

    git("add", "-A", cwd=root)
    for name in (PKGBUILD, SRCINFO):
        if (root / name).exists():
            # 防止被写坏的 .gitignore 吞掉这两个关键文件
            git("add", "--force", name, cwd=root, check=False)

    message = args.message or f"upgpkg: {meta.pkgname} {meta.tag}"
    proc = run(["git", "commit", "-m", message], cwd=root, check=False)
    if proc.returncode != 0:
        die("git commit 失败（检查 git user.name / user.email 是否配好）")
    ok(f"已提交：{message}")

    if args.no_push:
        info("--no-push：跳过推送")
        return

    remote = git("remote", "get-url", "origin", cwd=root,
                 check=False, capture=True).stdout.strip()
    if not remote:
        die("没有 origin 远端")
    if "aur.archlinux.org" not in remote:
        warn(f"origin 看起来不是 AUR 仓库：{remote}")
        if not confirm("还是要 push 吗？", args.yes):
            die("已取消")

    refspec = f"HEAD:{args.branch}"
    step(f"git push origin {refspec}")
    proc = run(["git", "push", "origin", refspec], cwd=root, check=False)
    if proc.returncode != 0:
        die("push 失败（SSH key / 权限 / 远端有更新，先 pull 再试）")
    ok(f"已发布 {meta.pkgname} {meta.tag} 到 AUR")


# ------------------------------------------------------------------ 命令：bump

KEEP_VER = (None, "", "-", ".")


def cmd_bump(args) -> None:
    root = project_root()
    meta = load_meta(root)

    if args.ver in KEEP_VER:
        new_ver = meta.pkgver if args.rel is not None else bump_last_number(meta.pkgver)
    else:
        new_ver = args.ver
    new_rel = args.rel or "1"

    if not PKGVER_RE.match(new_ver):
        die(f"pkgver 不合法：{new_ver!r}（允许字母数字和 . _ +）")
    if not new_rel.isdigit():
        die(f"pkgrel 应该是数字：{new_rel!r}")

    if new_ver == meta.pkgver and new_rel == meta.pkgrel:
        ok(f"版本没变（{meta.tag}）")
        return

    if args.dry_run:
        info(f"dry-run：{meta.pkgver_var} {meta.pkgver} -> {new_ver}，"
             f"{meta.pkgrel_var} {meta.pkgrel} -> {new_rel}")
        return

    text = meta.text
    for name, value in {meta.pkgver_var: new_ver, meta.pkgrel_var: new_rel}.items():
        text, done = set_var(text, name, value)
        if not done and name == "pkgrel":
            text, done = insert_after_var(text, meta.pkgver_var, f"pkgrel={value}")
        if not done:
            die(f"没能在 PKGBUILD 里更新 {name}，请 aur edit 手动改")

    (root / PKGBUILD).write_text(text, encoding="utf-8")
    ok(f"{meta.pkgname}: {meta.tag} -> {new_ver}-{new_rel}")

    if (root / SRCINFO).is_file():
        info(".SRCINFO 现在过期了：aur generate，或者直接 aur publish 会自动重生成")


# ------------------------------------------------------------------ CLI

EPILOG = """\
常用流程：
  新包   aur init mypkg && cd mypkg && aur edit && aur generate && aur build -ich && aur publish
  更新   aur bump && aur build -i && aur publish

注意：
  aur build 里的 -h 是「用 AUR 助手构建」，在那里看帮助请用 aur build --help。
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aur",
        description="AURora — 轻量级 AUR 包维护工具",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-V", "--version", action="version", version=f"AURora {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<命令>")

    sp = sub.add_parser("init", help="初始化 AUR 包工程（git clone + .gitignore）")
    sp.add_argument("name", help="包名，例如 mypkg")
    sp.add_argument("--force", action="store_true", help="覆盖已存在的 .gitignore")
    sp.add_argument("--no-template", action="store_true", help="空仓库时不生成 PKGBUILD 模板")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("edit", help="用 $EDITOR 打开 PKGBUILD")
    sp.add_argument("file", nargs="?", help="要编辑的文件，默认 PKGBUILD")
    sp.set_defaults(func=cmd_edit)

    sp = sub.add_parser("generate", help="生成 .SRCINFO")
    sp.set_defaults(func=cmd_generate)

    sp = sub.add_parser("clean", help="清理构建产物")
    sp.add_argument("-a", "--all", action="store_true",
                    help="连未跟踪的其他文件一起删（危险）")
    sp.add_argument("-n", "--dry-run", action="store_true", help="只列出要删的东西")
    sp.add_argument("-y", "--yes", action="store_true", help="不询问，直接删")
    sp.set_defaults(func=cmd_clean)

    sp = sub.add_parser("build", help="构建（-i 安装 / -c 清理 / -h 用助手）",
                        add_help=False)
    sp.add_argument("-i", "--install", action="store_true", help="构建后安装")
    sp.add_argument("-c", "--clean", action="store_true", help="构建后清理构建文件")
    sp.add_argument("-h", "--helper", action="store_true",
                    help="用 AUR 助手（paru/yay/...）而不是 makepkg")
    sp.add_argument("--ask", action="store_true", help="不加 --noconfirm")
    sp.add_argument("--help", action="help",
                    help="显示帮助（-h 已被 --helper 占用）")
    sp.set_defaults(func=cmd_build)

    sp = sub.add_parser("publish", help="补 .SRCINFO → commit → push 到 AUR")
    sp.add_argument("-m", "--message", help="自定义 commit message")
    sp.add_argument("-n", "--no-push", action="store_true", help="只本地提交，不 push")
    sp.add_argument("-f", "--force", action="store_true", help="即使没有改动也继续")
    sp.add_argument("--branch", default="master", help="推送目标分支（默认 master）")
    sp.add_argument("-y", "--yes", action="store_true", help="交互确认一律 yes")
    sp.add_argument("--dry-run", action="store_true", help="只展示，不动 git")
    sp.set_defaults(func=cmd_publish)

    sp = sub.add_parser("bump", help="更新 pkgver / pkgrel")
    sp.add_argument("ver", nargs="?",
                    help="新版本号；留空则末尾数字 +1，'-' 表示版本不变")
    sp.add_argument("rel", nargs="?", help="新 pkgrel，默认 1")
    sp.add_argument("-n", "--dry-run", action="store_true", help="只显示结果，不写入")
    sp.set_defaults(func=cmd_bump)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", None) is None:
        parser.print_help()
        return 0
    try:
        args.func(args)
    except KeyboardInterrupt:
        warn("被 Ctrl-C 打断了")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
