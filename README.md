# AURora

> 一个轻量的 AUR 包维护 CLI：把 init / edit / generate / clean / build / publish / bump
> 这七件事收进一条命令里，不需要背 makepkg 和 git 的参数。

写给长期维护 AUR 包的人（以及未来的我）。
纯 Python 标准库实现，零第三方依赖，单文件即可运行。

---

## 1. 依赖

| 用途 | 需要 |
| --- | --- |
| 基本运行 | `python` (>= 3.9)、`git` |
| `generate` / `build` | `base-devel`（提供 `makepkg`） |
| `build -h` | `paru` / `yay` / `pikaur` / `trizen` 任选其一 |
| `publish` | AUR 账户已登记 SSH 公钥 |

`makepkg` 拒绝以 root 运行，AURora 也会提前拦住你（助手模式另说，助手自己会调 sudo）。

---

## 2. 安装

### 从 AUR

```bash
paru -S aurora        # 或 yay -S aurora
```

包内会安装两个入口：

- `/usr/bin/aurora`
- `/usr/bin/aur`（便捷别名，与 `aurutils` 冲突，PKGBUILD 里已声明 `conflicts`）

> 如果机器上装了 `aurutils`，二选一，别硬凑。

### 手动（开发时）

```bash
install -Dm755 main.py ~/.local/bin/aurora
ln -sf ~/.local/bin/aurora ~/.local/bin/aur
```

---

## 3. 命令总览

| 命令 | 作用 |
| --- | --- |
| `aur init <name>` | 初始化包工程（git clone + .gitignore + 空仓库时补 PKGBUILD 模板） |
| `aur edit [file]` | 用 `$EDITOR` 打开 PKGBUILD（也可打开 `.install`、补丁等） |
| `aur generate` | 生成 `.SRCINFO` |
| `aur clean` | 清除构建产物（除 PKGBUILD 以外的垃圾） |
| `aur build [-ich]` | 构建（`-i` 安装、`-c` 清理、`-h` 用助手） |
| `aur publish` | 补 `.SRCINFO` → commit → push 到 AUR |
| `aur bump [ver] [rel]` | 更新版本，默认末尾数字 +1、`pkgrel=1` |

所有命令都会**从当前目录向上找 `PKGBUILD`**，所以在包的子目录里也能直接跑。

### 3.1 init

```bash
aur init mypkg
cd mypkg
```

- `git clone ssh://aur@aur.archlinux.org/mypkg.git`
- 克隆失败（仓库还不存在 / SSH 没配好）时，自动退化为本地 `git init` + `git remote add origin`，push 时 AUR 会创建仓库
- 写入 `.gitignore`（排除构建产物，**保留 `.SRCINFO`**）
- 仓库为空时写一份 PKGBUILD 模板，`# Maintainer:` 从 `git config user.name/user.email` 取

| 参数 | 说明 |
| --- | --- |
| `--force` | 覆盖已有的 `.gitignore` |
| `--no-template` | 不生成 PKGBUILD 模板 |

### 3.2 edit

```bash
aur edit              # 打开 PKGBUILD
aur edit mypkg.install
```

编辑器优先级：`$AURORA_EDITOR` → `$VISUAL` → `$EDITOR` → `nano`/`vim`/`vi`/`micro`/`nvim`。
编辑完 PKGBUILD 后如果 `.SRCINFO` 存在，会提醒你它可能已经过期。

### 3.3 generate

```bash
aur generate
```

等价于 `makepkg --printsrcinfo > .SRCINFO`，但会检查输出合法性、剥掉 stderr 噪音、报告版本号。

⚠️ `makepkg --printsrcinfo` 会**执行** PKGBUILD（这是 makepkg 的固有行为）。
只对自己信任的 PKGBUILD 运行。

### 3.4 clean

```bash
aur clean             # 安全模式：只删构建产物
aur clean -n          # dry-run，只列出要删的东西
aur clean -a -y       # 激进模式：删掉除白名单外所有未跟踪文件
```

安全模式删除：`pkg/`、`src/`、`*.pkg.tar*`、`*.src.tar.*`、`*.tar.*`、`*.zip`、`*.log`。

激进模式（`-a`）保留：`PKGBUILD`、`.SRCINFO`、`.gitignore`、`.git/`、git 已跟踪的文件、
`*.install` / `*.patch` / `*.diff` / `*.service` / `*.hook` / `*.preset` / `*.sh`、
`README*` / `LICENSE*` / `COPYING*` / `NEWS*` / `CHANGELOG*`。

> `-a` 有删掉未提交源码的风险，默认要交互确认；管道里跑请显式加 `-y`。

### 3.5 build

```bash
aur build              # makepkg -s -f
aur build -i           # 构建并安装
aur build -c           # 构建后清理构建文件
aur build -h           # 用 AUR 助手构建（自动检测 paru/yay/...）
aur build -ich         # 三个一起上
aur build --ask        # 不自动加 --noconfirm
```

助手模式：优先 `<helper> -B [-i] --noconfirm`；
如果该助手的 `--help` 里没有 `-B`（比如某些老版本），自动回退为
`makepkg` 构建 + `<helper> -U <包文件>` 安装，不会静默失败。

> ⚠️ 在 `build` 子命令里 **`-h` 是 `--helper`**，看帮助请用 `aur build --help`。

### 3.6 publish

```bash
aur publish                 # 默认提交信息：upgpkg: mypkg 1.2.3-1
aur publish -m "custom msg"
aur publish -n              # 只本地 commit，不 push
aur publish --dry-run       # 只展示将要提交的内容
```

流程：

1. 检查 `.SRCINFO`：不存在 → 生成；与 PKGBUILD 版本不一致 → 重新生成
2. 顺带 lint：非 VCS 包的 `sha256sums` 里出现 `SKIP` 会警告
3. `git add -A`（并且对 `.SRCINFO`/`PKGBUILD` 用 `--force`，防止被错误的 .gitignore 吞掉）
4. `git commit -m "upgpkg: <pkgname> <pkgver>-<pkgrel>"`
5. `git push origin HEAD:<branch>`（默认 `master`，AUR 只从 master 读）

| 参数 | 说明 |
| --- | --- |
| `-m, --message` | 自定义提交信息 |
| `-n, --no-push` | 只提交不推送 |
| `-f, --force` | 工作区没改动也继续 |
| `--branch` | 推送目标分支，默认 `master` |
| `-y, --yes` | 跳过交互确认 |
| `--dry-run` | 只展示，不动 git |

### 3.7 bump

```bash
aur bump                # pkgver 末尾数字 +1，pkgrel 归 1
aur bump 2.1.0          # pkgver=2.1.0，pkgrel=1
aur bump 2.1.0 2        # pkgver=2.1.0，pkgrel=2
aur bump "" 3           # 版本不动，只把 pkgrel 设成 3（"-" / "." 同理）
aur bump -n             # dry-run
```

版本规则：

| 原值 | 结果 |
| --- | --- |
| `1.2.3` | `1.2.4` |
| `1.2` | `1.3` |
| `20260101` | `20260102` |
| `1.2.3.r4.gabc1234` | `1.2.3.r5.gabc1234` |

如果 PKGBUILD 写的是 `pkgver=$_ver` + `_ver=1.2.3`，AURora 会**改 `_ver`**，
而不是粗暴地把 `pkgver` 拍成字面量——保持你原本的写法。

---

## 4. 典型工作流

**新包**

```bash
aur init mypkg && cd mypkg
aur edit                    # 填 pkgdesc / url / source / depends
aur generate
aur build -ich              # 构建、装一次、顺手清理
aur publish                 # 上传到 AUR
```

**日常更新**

```bash
cd mypkg
aur edit                    # 改 sha256sums / 版本相关
aur bump                    # 1.2.3-2 → 1.2.4-1
aur build -i                # 本地验证
aur publish                 # 自动重生成 .SRCINFO 并推送
```

**别人报告构建失败**

```bash
aur clean -n                # 先看看有没有脏东西
aur clean
aur build                   # 干净环境下重放
```

---

## 5. 关于 `.gitignore` 与 `.SRCINFO`（重要）

AUR 的规矩和普通仓库不一样：

- `.SRCINFO` **必须提交**，它是 AUR 网页和 `paru`/`yay` 解析包元数据的唯一来源
- 构建产物（`pkg/`、`src/`、`*.pkg.tar.*`、源码压缩包）**必须不提交**

AURora 生成的 `.gitignore` 就是按这个原则写的。
如果你从别处抄来一份 `*` + `!PKGBUILD` 的 whitelist 式 .gitignore，
它会连 `.SRCINFO` 一起忽略掉——`aur publish` 会 `git add --force` 帮你兜底，但还是建议改掉。

---

## 6. 环境变量

| 变量 | 作用 |
| --- | --- |
| `AURORA_EDITOR` / `VISUAL` / `EDITOR` | 指定编辑器 |
| `NO_COLOR` | 关掉颜色输出 |
| `AURORA_VERBOSE` | 打印每条要执行的外部命令 |

退出码：`0` 成功、`1` 错误、`130` 被 Ctrl-C 打断。

---

## 7. 设计笔记

- **不用 `source PKGBUILD` 读变量。** PKGBUILD 是会被执行的 shell，为了读 `pkgver`
  就把它 source 进来，等于给每个包开了个后门。AURora 用正则读、用正则写。
  代价是「高度动态计算出来的 pkgver」改不了——这时它会明确报错让你手动改。
- **`generate` 才允许执行 PKGBUILD**（`makepkg --printsrcinfo` 无法避免），
  所以这一步单独成命令，不带任何隐式调用。
- **`publish` 重建 `.SRCINFO` 是强制的**，因为版本号对不上的 `.SRCINFO` 是 AUR 上
  最常见的「我明明改了为什么还是旧包」事故来源。
- **push 用 `HEAD:master`**，AUR 只从 master 分支读取包定义。
- **危险操作（`clean -a`）默认交互确认**，`-n` 永远只读。

---

## 8. Roadmap

- [ ] `aur check` —— 一个轻量 PKGBUILD linter（依赖缺失、缺 `sha256sums`、URL 检查）
- [ ] bash / zsh / fish 补全
- [ ] `aur log` —— 只看某个包最近的 upgpkg 记录
- [ ] 多包（`pkgname=(a b)`）工程的定向 bump

## License

MIT
