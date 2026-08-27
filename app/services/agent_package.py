"""Agent 包(zip)的解包、体检与自描述文件。

这个模块只回答"这个 zip 里是什么",不做任何判断题式的裁决 —— 审计结论归引擎,是否放行归
人。它管三类**事实**:

1. **能不能安全解开。** zip 里的成员路径可以写成 `../../etc/x` 或者一个指向 `/etc/passwd`
   的符号链接。照抄 `extractall` 就会写到目标目录外面(zip slip)。这不是对上传者的判断,
   是我们自己的漏洞,所以这类包直接退回。
2. **GitLab 会不会拒收。** 包里带明文凭据时,GitLab 的 pre-receive 钩子必定拒绝这次
   push。我们不做这项检查也不会"放过"它,只会把失败推到两分钟后、报错变成 GitLab 的原文,
   而且组里已经留下一个建了一半的空项目。所以提前检出、提前说明。
3. **提示词需要的那几项在不在。** 启动命令、是否常驻、升级要保留哪些文件。找得到就作为
   候选预填给上传者核对,找不到就让他填 —— 这三项没有一项可以由我们猜了算,猜错的表现是
   用户粘贴后看到"未知参数",而那行命令看起来完全正常。

第 3 项有两种来源,区别是**错的时候看不看得出来**,所以它们不能混在一起说:

**结构化推导**读的是本来就为机器写的文件 —— `.kiro/agents/*.json` 的 agent 名、
`pyproject.toml` 的 `[project.scripts]`、`.gitignore` 的忽略项。这些是解析,不是理解:
解析不出来会返回空(于是提示词退回"请填"),不会返回一个像样但错的答案。

**README 正则**是猜。它读的是给人看的散文,猜错时给出的是一行语法完美、看起来完全正常、
却跑不起来的命令 —— 而那正是这个模块要消除的失败。所以它排在所有结构化来源之后,
且它的结果一律标成"待核对"。
"""
from __future__ import annotations

import io
import json
import re
import tomllib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

#: 包根目录的自描述文件。名字里带 portal 是故意的:它是门户加进去的,不是上传者的 README
#: 的一部分 —— 我们从不改写上传者的文件。
SIDECAR_NAME = "portal-agent.yaml"

#: 常驻/一次性。常驻的才提示"会弹浏览器窗口、别关掉";一次性的提示这句就是错的。
RUNTIMES = ("long-running", "oneshot")

#: 明文凭据。和引擎里那份是同一批模式(引擎已拆到 rp-ai-miao/skill-audit,门户这侧要在
#: push 之前自己看一眼,所以这里留一份最小集)。宁少勿滥:这一项会拦上传,误报的代价是
#: 有人上传不了一个本来没问题的包。
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("gitlab_pat", re.compile(r"glpat-[A-Za-z0-9_\-]{20,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("openai_key", re.compile(r"sk-(?:proj-)?(?!YOUR)[A-Za-z0-9_\-]{20,}")),
    ("tavily_key", re.compile(r"tvly-(?!YOUR)[A-Za-z0-9]{16,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
)

#: 会被当成"可执行入口"展示给审核人。不是黑名单,是"请你看一眼这些"。
_EXECUTABLE_SUFFIXES = frozenset(
    {".ps1", ".bat", ".cmd", ".sh", ".exe", ".msi", ".py", ".js", ".mjs", ".rb", ".pl"}
)

#: 明显是二进制或不可能藏文本凭据的格式,读都不用读。**不是允许列表**:凭据最常出现在
#: `.pem`/`.key` 这类没有通用扩展名的文件里,所以判断方式是"读一段看是不是文本",而不是
#: 按后缀挑 —— 之前那版按后缀挑,恰好漏掉了 `.pem`。
_BINARY_SUFFIXES = frozenset(
    {".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".whl", ".pyc",
     ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svgz", ".pdf",
     ".exe", ".dll", ".so", ".dylib", ".bin", ".onnx", ".pt", ".pth", ".safetensors",
     ".mp4", ".mp3", ".wav", ".woff", ".woff2", ".ttf", ".eot", ".db", ".sqlite"}
)

_README_NAMES = ("readme.md", "readme.markdown", "readme.txt", "readme")

#: 一行里出现这些词的代码块,更可能是启动命令而不是安装步骤。
_LAUNCH_HEADING = re.compile(
    r"^#{1,6}\s*.*(启动|运行|使用方法|快速开始|run|usage|start|quick\s*start|getting\s*started)",
    re.IGNORECASE | re.MULTILINE,
)
_INSTALLISH = re.compile(
    r"^\s*(pip|pip3|npm|yarn|poetry|conda|apt|apt-get|brew|choco|winget|git\s+clone|"
    r"virtualenv|python\s+-m\s+venv|uv\s+(pip|venv))\b",
    re.IGNORECASE,
)

#: 一行里出现这些就不是可执行的命令,而是一句让人自己填的模板。
#:
#: 这一条是从一个真实的错误里来的:report-manhour 的 README 里有
#: `python -m timesheet --input "..." --force-rewrite`,它不装依赖、也在"启动"标题下面,
#: 于是被排到候选第一位并预填进确认屏。粘贴出去的结果是找不到名叫 `...` 的文件 ——
#: 而那行命令看起来完全正常。
_PLACEHOLDERISH = re.compile(
    r"\.\.\."                        # "..." 省略号占位
    r"|<[A-Za-z_\u4e00-\u9fff][^>]*>"  # <启动命令> / <your-path>;不误伤 shell 的 `< file`
    r"|\{\{|\$\{\{"                  # {{ }} 模板
    r"|\bYOUR_[A-Z_]+\b",            # YOUR_TOKEN 这类
)

#: kiro 的 agent 配置。这是整套推导里唯一**确定**的来源:这个文件本来就是为
#: `kiro-cli chat --agent <名字>` 写的,不存在"它想说什么"的问题。
_KIRO_AGENT_DIR = ".kiro/agents"

#: agent 名里允许出现的字符。**这不是洁癖,是这条推导唯一的安全边界**:推出来的命令会被
#: 标成"已确定、无需核对"预填进确认屏,提示词再把它原样交给使用者的 agent 去执行。一个叫
#: `x; whoami.json` 的文件会推出 `kiro-cli chat --agent x; whoami` —— 两条命令。
#:
#: 上传者本来就能在表单里手填任何启动命令,所以这不是新增的权限;新增的是"这条不用核对"
#: 这个信号,而那正是不能给出去的东西。名字不合规就整条不出,不降级成候选:一个名字里带
#: shell 元字符的 agent 配置,我们没有任何理由替它拼出一条命令。
#:
#: 字符集与 `_KIRO_AGENT_ARG` 一致 —— 两边不一致会让 `launch_target_exists` 对自己推出来
#: 的命令判 False,也就是确认屏一边说"已确定"、一边说"包里找不到"。
_KIRO_AGENT_NAME = re.compile(r"^[\w.-]+$")

#: 常驻运行的强信号。只认这几个:它们都会开一个真实的浏览器进程并停在那里。
#: 认得越少越好 —— `runtime` 猜错的表现是对一个两秒就退出的脚本说"别关掉那个窗口",
#: 而一处明显不对的提醒会让整段提示词都不被相信。
_LONG_RUNNING_DEPS = ("playwright", "selenium", "puppeteer", "pyppeteer")

#: `.gitignore` 里这些是构建产物或工具缓存,不是"只存在于这台机器上的状态"。
#: 升级时不搬它们不会丢任何东西 —— 下次构建会重新生成。
_DERIVED_IGNORES = frozenset({
    "__pycache__", ".venv", "venv", "env", "build", "dist", "node_modules",
    ".pytest_cache", ".mypy_cache", ".hypothesis", ".ruff_cache", ".tox",
    ".idea", ".vscode", ".DS_Store", "htmlcov", ".coverage", "coverage",
    "target", ".next", ".nuxt", ".parcel-cache", ".gradle", ".eggs",
})

#: keep 候选最多给这么多。再多上传者就不会逐条看了,而这个清单的价值全在他逐条看。
_KEEP_LIMIT = 8

#: 一体式安装脚本。发现它就只出这一条 —— 这类脚本的约定是"整个安装都归我"
#: (report-manhour 的 setup.ps1 自己查 Python、装依赖、装浏览器、自检),再并列几条我们
#: 猜出来的步骤会和它抢同一件事,而冲突的表现是脚本跑一半失败、报错指向别处。
_SETUP_SCRIPTS = ("setup.ps1",)

#: `requires-python = ">=3.12"` 里的那个次版本号。
_PY_FLOOR = re.compile(r">=\s*3\.(\d+)")


class PackageRejected(Exception):
    """这个 zip 我们处理不了,或者 GitLab 一定会拒收。

    和"审计说它可疑"是两回事:那个只记录不拦,这个是事实性的死路,越早说越好。
    """

    def __init__(self, detail: str, *, findings: list[dict] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.findings = findings or []


@dataclass
class FileEntry:
    path: str
    size: int

    @property
    def is_executable_ish(self) -> bool:
        return PurePosixPath(self.path).suffix.lower() in _EXECUTABLE_SUFFIXES


@dataclass
class LaunchGuess:
    """一条启动命令候选,连同它是哪来的、能不能直接用。

    `certain` 区分的不是"我们有多自信",而是**错的时候看不看得出来**:结构化来源解析不出
    就是空,解析出来就是对的;README 猜出来的那条可能语法完美却指向另一个程序。确认屏据此
    决定要不要替上传者预填 —— 一个自己填好的值,他会倾向直接点下一步。
    """

    command: str
    source: str
    certain: bool = False


@dataclass
class InstallStep:
    """一条安装命令,以及它为什么需要。

    `note` 不是装饰。读提示词的是使用者的 agent,它遇到阻碍会自己发明变通做法,而这类步骤
    的变通做法恰好都是错的(改机器的执行策略而不是单次绕过、跳过 `playwright install`
    因为 pip 已经"装好了" playwright)。把理由写出来,它就不必猜。
    """

    command: str
    note: str | None = None


@dataclass
class Declared:
    """包对自己的描述。每项都可能是空的,空就是"要问上传者"。"""

    launch: str | None = None
    runtime: str | None = None
    keep: list[str] = field(default_factory=list)
    #: 开始装之前使用者必须知道的事:独占资源、要先登出的会话、要先申请的权限。
    #: **不推导** —— 这类知识不在代码里(report-manhour 的"SAP 工号是排他锁,先 Exit +
    #: Log off,否则锁挂 30 分钟到 2 小时"只以散文形式存在于 README 和 agent 的 prompt 里),
    #: 只能来自 sidecar 或上传者手填。
    prerequisites: list[str] = field(default_factory=list)
    #: 每一项从哪来:sidecar / readme / form。确认屏要把来源写给上传者看 —— 一个自己填好
    #: 的值,如果看不出是哪来的、能不能改,比空着更让人不安。
    sources: dict[str, str] = field(default_factory=dict)


@dataclass
class Inspection:
    files: list[FileEntry]
    total_bytes: int
    top_dir: str | None
    readme_path: str | None
    readme_text: str | None
    declared: Declared
    launch_candidates: list[str]
    env_files: list[str]
    #: 包里带了 .git —— 上传者自己的历史,推的时候会被丢掉,得让他知道。
    has_git_dir: bool
    #: 候选的完整形态:每条带来源和"错了看不看得出来"。`launch_candidates` 是它的命令列表,
    #: 保留是因为确认屏和审核页都在读那个形状。
    launch_guesses: list[LaunchGuess] = field(default_factory=list)
    #: 装依赖那几步。提示词第 4 步原来只有一句泛泛的"按 README 安装",于是使用者的 agent
    #: 要自己读 README 再自己决定怎么装 —— 而 Windows 上那几步每一步都有一个坑。
    install_steps: list[InstallStep] = field(default_factory=list)

    @property
    def executables(self) -> list[str]:
        return [f.path for f in self.files if f.is_executable_ish]


# --- 解包 ------------------------------------------------------------------


def _unsafe_reason(name: str) -> str | None:
    """成员路径能不能安全落地。返回 None 表示可以。"""
    if not name or name.endswith("/"):
        return None
    p = PurePosixPath(name)
    if p.is_absolute() or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return "绝对路径"
    if ".." in p.parts:
        return "路径里有 .."
    if "\\" in name:
        # zip 规范里分隔符是 /;反斜杠在 Windows 上会被当成目录分隔,绕过上面的检查。
        return "路径里有反斜杠"
    return None


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    # 高 16 位是 Unix mode;0xA000 是 S_IFLNK。符号链接解出来能指向目录外的任何位置。
    return (info.external_attr >> 16) & 0xF000 == 0xA000


def extract_safely(data: bytes, dest: Path, *, max_bytes: int) -> list[FileEntry]:
    """把 zip 解到 `dest`,拒绝任何会写到 `dest` 外面的东西。

    返回文件清单。目录项不计入 —— 审核人要看的是文件。
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PackageRejected("这不是一个有效的 zip 文件。") from exc

    entries: list[FileEntry] = []
    total = 0
    with zf:
        for info in zf.infolist():
            if _is_symlink(info):
                raise PackageRejected(
                    f"包里有符号链接({info.filename}),解压时它可以指向目标目录之外,"
                    "所以这个包不能被接收。请去掉链接、放实际文件。"
                )
            reason = _unsafe_reason(info.filename)
            if reason is not None:
                raise PackageRejected(
                    f"包里有不安全的路径({info.filename}:{reason}),解压时会写到目标目录"
                    "之外。请重新打包,只包含相对路径。"
                )
            if info.is_dir():
                continue
            total += info.file_size
            if total > max_bytes:
                raise PackageRejected(
                    f"解压后的总大小超过 {max_bytes // (1024 * 1024)} MB 上限。"
                )
            entries.append(FileEntry(path=info.filename, size=info.file_size))

        dest.mkdir(parents=True, exist_ok=True)
        for info in zf.infolist():
            if info.is_dir():
                (dest / info.filename).mkdir(parents=True, exist_ok=True)
                continue
            target = (dest / info.filename).resolve()
            # 双保险:上面按名字判过,这里按落地后的真实路径再判一次。名字检查可能被
            # 某种编码绕过,而 resolve() 之后的比较不会。
            if not str(target).startswith(str(dest.resolve()) + "/"):
                raise PackageRejected(f"包里有不安全的路径({info.filename})。")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                out.write(src.read())
    return entries


# --- 体检 ------------------------------------------------------------------


def _common_top_dir(entries: list[FileEntry]) -> str | None:
    """所有文件是否都在同一个顶层目录下。

    安装提示词的第 2 步假设"包里已经带了这一层目录"。这里只是**观察**有没有;打包下载时
    由门户自己补齐,不要求上传者弄对。
    """
    tops = {PurePosixPath(e.path).parts[0] for e in entries if PurePosixPath(e.path).parts}
    if len(tops) == 1:
        only = next(iter(tops))
        if any(PurePosixPath(e.path).parts[1:] for e in entries):
            return only
    return None


def scan_secrets(root: Path, entries: list[FileEntry]) -> list[dict]:
    """在文本文件里找明文凭据。命中的证据只留模式名和位置,不回显原文。

    判断"是不是文本"靠读一段看有没有 NUL 字节,不靠扩展名:私钥文件常叫 `id_rsa`、
    `server.key`、`*.pem`,按后缀挑会正好漏掉它们。
    """
    found: list[dict] = []
    for entry in entries:
        if PurePosixPath(entry.path).suffix.lower() in _BINARY_SUFFIXES:
            continue
        path = root / entry.path
        if not path.is_file() or entry.size > 2 * 1024 * 1024:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue
        text = raw.decode("utf-8", errors="ignore")
        for rule, pattern in _SECRET_PATTERNS:
            m = pattern.search(text)
            if m:
                line = text.count("\n", 0, m.start()) + 1
                found.append({"rule": rule, "file": entry.path, "line": line})
    return found


def _read_readme(root: Path, entries: list[FileEntry]) -> tuple[str | None, str | None]:
    """找 README:优先包根,其次唯一顶层目录下。"""
    top = _common_top_dir(entries)
    prefixes = [""] if top is None else [f"{top}/", ""]
    by_lower = {e.path.lower(): e.path for e in entries}
    for prefix in prefixes:
        for name in _README_NAMES:
            actual = by_lower.get(f"{prefix}{name}")
            if actual:
                path = root / actual
                try:
                    return actual, path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    return actual, None
    return None, None


def _prefixes(entries: list[FileEntry]) -> list[str]:
    """在哪些前缀下找包根的文件:唯一顶层目录下,其次真正的根。

    和 `_read_readme` 用的是同一条规则。抽出来是因为下面每个结构化来源都要它,而写错这个
    的表现是"包里明明有 .kiro/agents/ 却推不出启动命令"。
    """
    top = _common_top_dir(entries)
    return [""] if top is None else [f"{top}/", ""]


def _read_text(root: Path, entries: list[FileEntry], rel: str) -> str | None:
    """按 `_prefixes` 的规则读一个包根文件。找不到或读不了都返回 None。"""
    by_path = {e.path for e in entries}
    for prefix in _prefixes(entries):
        candidate = f"{prefix}{rel}"
        if candidate not in by_path:
            continue
        try:
            return (root / candidate).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
    return None


def kiro_agent_launches(root: Path, entries: list[FileEntry]) -> list[LaunchGuess]:
    """`.kiro/agents/*.json` → `kiro-cli chat --agent <名字>`。

    这是唯一可以标 `certain` 的来源:那个文件存在的全部目的就是被 `--agent` 引用。

    两处故意保守:

    **用文件名 stem 而不是 json 里的 `name`。** 两者约定上一致(本机 5 个配置都一致),但
    "kiro-cli 到底认哪个"我们没有权威依据。取 stem 并要求 `name == stem`,就不需要知道
    答案 —— 不一致时降级成待核对,并把不一致本身写进来源文案。赌错的表现是
    "agent not found",看起来像根本没装上。

    **包里有多个 agent 时一条都不预填。** 替人选一个的代价是他点下一步时不会发现选错了;
    全列出来的代价只是多点一下。
    """
    found: list[tuple[str, str]] = []           # (显示路径, stem)
    mismatched: list[tuple[str, str, str]] = []  # (显示路径, stem, json 里的 name)
    for prefix in _prefixes(entries):
        want = f"{prefix}{_KIRO_AGENT_DIR}/"
        for entry in entries:
            if not entry.path.startswith(want) or not entry.path.endswith(".json"):
                continue
            rel = entry.path[len(want):]
            if "/" in rel:          # agents/ 下的子目录不是 agent 配置
                continue
            stem = rel[: -len(".json")]
            if not stem or not _KIRO_AGENT_NAME.match(stem):
                # 见 _KIRO_AGENT_NAME:名字不合规就整条不出。
                continue
            try:
                data = json.loads((root / entry.path).read_text(encoding="utf-8", errors="ignore"))
            except (OSError, ValueError):
                # 坏掉的 json 当作没有。它可能根本不是 agent 配置,而按文件名硬推出一条
                # 命令,错的时候看不出来。
                continue
            if not isinstance(data, dict):
                continue
            declared_name = str(data.get("name") or "").strip()
            if declared_name and declared_name != stem:
                mismatched.append((entry.path, stem, declared_name))
            else:
                found.append((entry.path, stem))
        if found or mismatched:
            break

    out: list[LaunchGuess] = []
    sole = len(found) == 1 and not mismatched
    for path, stem in found:
        out.append(LaunchGuess(
            command=f"kiro-cli chat --agent {stem}",
            source=(
                f"{path}"
                if sole
                else f"{path}(包里有多个 agent 配置,请选一个)"
            ),
            certain=sole,
        ))
    for path, stem, declared_name in mismatched:
        out.append(LaunchGuess(
            command=f"kiro-cli chat --agent {stem}",
            source=(
                f"{path}:文件名是 {stem},但里面写的 name 是 {declared_name} —— "
                "两者不一致,请确认该用哪个"
            ),
            certain=False,
        ))
    return out


def pyproject_launches(root: Path, entries: list[FileEntry]) -> list[LaunchGuess]:
    """`pyproject.toml` 的 `[project.scripts]` → 那个命令名。

    装上之后它就在 PATH 上,所以命令名本身就是启动方式。标 `certain` 是安全的:这个表是
    给打包工具读的,不是散文。
    """
    text = _read_text(root, entries, "pyproject.toml")
    if not text:
        return []
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    # 每一层都要 isinstance:这是上传者的文件,`project = "x"` 是合法 TOML,而
    # `(data.get("project") or {}).get(...)` 对一个字符串会抛 AttributeError ——
    # 体检端点 500,上传者看到的是"服务器错误",不是"你的 pyproject 有问题"。
    project = data.get("project")
    scripts = project.get("scripts") if isinstance(project, dict) else None
    if not isinstance(scripts, dict):
        return []
    names = [str(k).strip() for k in scripts if str(k).strip()]
    return [
        LaunchGuess(
            command=name,
            source=(
                "pyproject.toml 的 [project.scripts]"
                if len(names) == 1
                else f"pyproject.toml 的 [project.scripts](共 {len(names)} 个,请选一个)"
            ),
            certain=len(names) == 1,
        )
        for name in names
    ]


def package_json_launches(root: Path, entries: list[FileEntry]) -> list[LaunchGuess]:
    """`package.json` 的 `scripts.start` / `bin` → `npm start` 或那个命令名。"""
    text = _read_text(root, entries, "package.json")
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    out: list[LaunchGuess] = []
    scripts = data.get("scripts")
    if isinstance(scripts, dict) and str(scripts.get("start") or "").strip():
        out.append(LaunchGuess(
            command="npm start",
            source="package.json 的 scripts.start",
            certain=True,
        ))
    bins = data.get("bin")
    names = (
        [str(k) for k in bins if str(k).strip()] if isinstance(bins, dict)
        else [str(data.get("name") or "").strip()] if isinstance(bins, str)
        else []
    )
    for name in [n for n in names if n]:
        out.append(LaunchGuess(
            command=name,
            source="package.json 的 bin",
            certain=False,   # bin 要先全局装上才在 PATH 上,不一定是这个包的启动方式
        ))
    return out


def derive_launch(
    root: Path, entries: list[FileEntry], readme: str | None
) -> list[LaunchGuess]:
    """按"错的时候看不看得出来"排序的候选清单,第一条用于预填。

    结构化来源在前,README 在最后。顺序是这里唯一重要的东西,举个真实的例子:
    report-manhour 的 `pyproject.toml` 里有 `timesheet = "timesheet.cli:main"`,而它的
    `.kiro/agents/timesheet.json` 说的是 `kiro-cli chat --agent timesheet`。两条都"对",
    但前者是给这个工具的开发者用的底层 CLI,后者才是市场里的人要的那个交互式会话。
    """
    out: list[LaunchGuess] = []
    seen: set[str] = set()
    for guess in (
        *kiro_agent_launches(root, entries),
        *pyproject_launches(root, entries),
        *package_json_launches(root, entries),
        *(
            LaunchGuess(command=c, source="从 README 猜的候选,请核对", certain=False)
            for c in launch_candidates(readme)
        ),
    ):
        if guess.command in seen:
            continue
        seen.add(guess.command)
        out.append(guess)
    return out


def _runtime_deps(root: Path, entries: list[FileEntry]) -> str:
    """只把**运行时**依赖拼成一段文本。dev / 测试依赖不算。

    这个区分是必要的,不是讲究:一个用 playwright 做端到端测试的普通 web 应用,它的
    `pytest-playwright` 只在 dev extra 里,而它跑起来根本不开浏览器。把它判成常驻,提示词
    就会对一个两秒退出的脚本说"别关掉那个窗口" —— 而一处明显不对的提醒会让人连带不信
    别的提醒。

    `requirements.txt` 整个文件都算运行时:它没有分组,而 dev 依赖的惯例是另一个文件
    (`requirements-dev.txt`),那个文件不在这里读。
    """
    parts: list[str] = [_read_text(root, entries, "requirements.txt") or ""]

    toml_text = _read_text(root, entries, "pyproject.toml")
    if toml_text:
        try:
            data = tomllib.loads(toml_text)
        except tomllib.TOMLDecodeError:
            data = {}
        project = data.get("project")
        deps = project.get("dependencies") if isinstance(project, dict) else None
        if isinstance(deps, list):
            parts += [str(d) for d in deps]

    pkg_text = _read_text(root, entries, "package.json")
    if pkg_text:
        try:
            data = json.loads(pkg_text)
        except ValueError:
            data = {}
        deps = data.get("dependencies") if isinstance(data, dict) else None
        if isinstance(deps, dict):
            parts += [str(k) for k in deps]

    return "\n".join(parts).lower()


def derive_runtime(root: Path, entries: list[FileEntry]) -> str | None:
    """运行时依赖里有浏览器自动化 → `long-running`。推不出来返回 None,让上传者选。

    只在有强信号时才答。别的形态(常驻服务、定时任务)也可能是 long-running,但从依赖看不
    出来,而这一项猜错会让提示词出现一句明显不对的提醒。
    """
    blob = _runtime_deps(root, entries)
    return "long-running" if any(dep in blob for dep in _LONG_RUNNING_DEPS) else None


def _has(entries: list[FileEntry], rel: str) -> bool:
    """包根有没有这个文件(按 `_prefixes` 的规则)。"""
    by_path = {e.path for e in entries}
    return any(f"{prefix}{rel}" in by_path for prefix in _prefixes(entries))


def _python_floor(root: Path, entries: list[FileEntry]) -> str | None:
    """`requires-python` 声明的最低次版本,例如 "3.12"。声明不出来返回 None。

    有这个值才敢出 `winget install Python.Python.3.X` 那一步:版本号猜错的表现不是装不上,
    而是装了一个能用但不是作者测过的解释器。
    """
    text = _read_text(root, entries, "pyproject.toml")
    if not text:
        return None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    project = data.get("project")
    spec = project.get("requires-python") if isinstance(project, dict) else None
    if not isinstance(spec, str):
        return None
    m = _PY_FLOOR.search(spec)
    return f"3.{m.group(1)}" if m else None


def derive_install_steps(root: Path, entries: list[FileEntry]) -> list[InstallStep]:
    """按包里实际有什么,给出装依赖的精确命令。

    这一步存在的理由是提示词第 4 步原来只写"按仓库里的 README 安装(若有 setup.ps1 就跑
    它)"。那句话把判断推给了使用者的 agent,而 Windows 上这几步每一步都有一个只有 Windows
    用户会踩的坑:默认执行策略拦下 `.ps1`;`winget` 装完 Python 后当前终端的 PATH 还是旧的;
    `python` 与 `py -3.12` 两种入口取决于安装时勾了什么。照文档敲会一个个踩,而这些坑
    可以直接从包里的文件推出来、提前写进命令里。

    推不出来就返回空,提示词退回原来那句 —— 那是一句诚实的兜底,不是失败。
    """
    for script in _SETUP_SCRIPTS:
        if _has(entries, script):
            return [InstallStep(
                command=f"powershell -ExecutionPolicy Bypass -File .\\{script}",
                note=(
                    f"`-ExecutionPolicy Bypass` 不能省:Windows 客户端默认策略是 Restricted,"
                    f"直接敲 `.\\{script}` 会报 \"running scripts is disabled on this "
                    f"system\"。这里用的是单次调用的绕过,**不要**去改机器的策略设置。"
                    f"这个脚本负责整个安装(查 Python、装依赖、自检),所以不要再另外装一遍;"
                    f"它报错就把原文告诉我,不要绕过它自己想办法。"
                ),
            )]

    steps: list[InstallStep] = []
    floor = _python_floor(root, entries)
    py = f"py -{floor}" if floor else "py -3"
    needs_python = _has(entries, "requirements.txt") or _has(entries, "pyproject.toml")

    if needs_python and floor:
        steps.append(InstallStep(
            command=f"winget install Python.Python.{floor}",
            note=(
                f"只在还没装 Python {floor} 时需要。**装完必须开一个新终端**:PATH 是终端"
                f"启动时读的,当前窗口里 `python` 仍然找不到 —— 这一点每个人都会踩一次,"
                f"而它看起来像\"装失败了\"。"
            ),
        ))

    if _has(entries, "requirements.txt"):
        steps.append(InstallStep(
            command=f"{py} -m pip install -r requirements.txt",
            note=(
                "用 `py` 启动器而不是 `python`:python.org 的安装包两者都给,但 PATH 上有"
                "哪个取决于安装时勾了什么,而那个勾选框默认不勾。"
            ),
        ))
    elif _has(entries, "pyproject.toml"):
        steps.append(InstallStep(command=f"{py} -m pip install -e .", note=None))
    elif _has(entries, "package.json"):
        steps.append(InstallStep(
            command="npm ci",
            note="用 `ci` 而不是 `install`:它照 lock 文件装,装出来和作者测过的是同一套版本。",
        ))

    if any(dep in _runtime_deps(root, entries) for dep in ("playwright", "pyppeteer")):
        steps.append(InstallStep(
            command="playwright install chromium",
            note=(
                "pip 装的只是 playwright 这个库,浏览器本身要另外下。**不做这一步不会在"
                "安装时报错**,而是等程序真的去开浏览器时才失败,那时的报错指向别处。"
            ),
        ))

    return steps


def derive_keep(root: Path, entries: list[FileEntry]) -> list[str]:
    """`.gitignore` 里被忽略的**本机状态** → 升级时要从 .bak 搬过来的候选。

    为什么 `.gitignore` 是对的地方:被它忽略的东西正好就是"代码包里没有、远端也没有、
    只存在于这台机器上"的那一批 —— 而这恰是提示词第 3 步要搬的定义。report-manhour 的
    `.gitignore` 头两条 `.browser-profile/`(登录态)和 `.timesheet-runs/`(审计记录)
    就是这么来的,它自己的注释写着"绝不入库"。

    两类要排掉:

    - **构建产物与工具缓存**(`__pycache__/`、`.venv/`、`dist/`)。不搬不丢东西。
    - **裸扩展名通配**(`*.xlsx`、`*.png`)。它们匹配得太宽,列进清单等于让人去搬一整类
      文件;而提示词第 3 步本来就写了"凡是只存在于 .bak 的都要搬",已经覆盖了它们。

    宁多勿少:这是**候选**,上传者在确认屏里逐条看、多的删掉。多一条的代价是他删一次;
    少一条的代价是登录态或审计记录被删掉,而那些东西任何地方都恢复不回来。
    """
    text = _read_text(root, entries, ".gitignore")
    if not text:
        return []
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        bare = line.rstrip("/")
        if not bare or bare in _DERIVED_IGNORES:
            continue
        if bare.startswith("*.") and "/" not in bare:
            continue                              # 裸扩展名通配,见上
        if PurePosixPath(bare).name in _DERIVED_IGNORES:
            continue                              # `foo/build/` 这类
        if bare not in out:
            out.append(bare)
        if len(out) >= _KEEP_LIMIT:
            break
    return out


def launch_candidates(readme: str | None) -> list[str]:
    """从 README 里挑出可能是启动命令的行。

    只作为**候选**给上传者核对,永远不直接当成启动命令用。装依赖那类行(pip/npm/...)
    先排掉,它们最常出现在同一个代码块里,而选错了会让人在装依赖的那一步以为已经启动。
    """
    if not readme:
        return []
    blocks: list[str] = []
    # 先取"启动/运行/usage"标题之后的代码块,再退回全文的代码块。
    positions = [m.end() for m in _LAUNCH_HEADING.finditer(readme)]
    fences = list(re.finditer(r"```[^\n]*\n(.*?)```", readme, re.DOTALL))
    for after in positions:
        for f in fences:
            if f.start() >= after:
                blocks.append(f.group(1))
                break
    if not blocks:
        blocks = [f.group(1) for f in fences]

    out: list[str] = []
    for block in blocks:
        for raw in block.splitlines():
            line = raw.strip().lstrip("$>").strip()
            if not line or line.startswith("#") or _INSTALLISH.match(line):
                continue
            if len(line) > 200:
                continue
            if _PLACEHOLDERISH.search(line):
                # 模板行不是命令。留着它最坏的结果不是"没用",而是它排到第一位被预填。
                continue
            if line not in out:
                out.append(line)
        if len(out) >= 5:
            break
    return out[:5]


def read_sidecar(root: Path, entries: list[FileEntry]) -> Declared:
    """读包里的 portal-agent.yaml(如果上传者已经带了一个)。"""
    top = _common_top_dir(entries)
    for prefix in ([f"{top}/", ""] if top else [""]):
        path = root / f"{prefix}{SIDECAR_NAME}"
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8", errors="ignore")) or {}
        except yaml.YAMLError:
            # 格式坏了就当没有,并把这件事作为"来源"留痕 —— 静默忽略会让上传者以为
            # 他写的值生效了。
            return Declared(sources={"sidecar": "portal-agent.yaml 解析失败,已忽略"})
        if not isinstance(data, dict):
            return Declared(sources={"sidecar": "portal-agent.yaml 内容不是键值对,已忽略"})
        launch = (data.get("launch") or "").strip() or None
        runtime = (data.get("runtime") or "").strip() or None
        keep = data.get("keep") or []
        if isinstance(keep, str):
            keep = [keep]
        prereq = data.get("prerequisites") or []
        if isinstance(prereq, str):
            prereq = [prereq]
        src = {}
        if launch:
            src["launch"] = f"{SIDECAR_NAME}"
        if runtime:
            src["runtime"] = f"{SIDECAR_NAME}"
        if keep:
            src["keep"] = f"{SIDECAR_NAME}"
        if prereq:
            src["prerequisites"] = f"{SIDECAR_NAME}"
        return Declared(
            launch=launch,
            runtime=runtime if runtime in RUNTIMES else None,
            keep=[str(k) for k in keep if str(k).strip()],
            prerequisites=[str(p).strip() for p in prereq if str(p).strip()],
            sources=src,
        )
    return Declared()


def inspect(root: Path, entries: list[FileEntry]) -> Inspection:
    """把解开的目录看一遍,得出确认屏和审核页要显示的全部事实。

    sidecar 永远优先:上传者已经明确写过的东西,不该被我们推导出来的值盖掉。剩下三项各自
    独立回退到推导 —— 一个包可能带了 sidecar 只写 launch,那 runtime 和 keep 仍然该推。
    """
    readme_path, readme_text = _read_readme(root, entries)
    declared = read_sidecar(root, entries)
    guesses = derive_launch(root, entries, readme_text)
    candidates = [g.command for g in guesses]

    if declared.launch is None and guesses:
        first = guesses[0]
        declared.launch = first.command
        declared.sources["launch"] = (
            first.source if first.certain else f"{first.source} —— 待核对"
        )
    if declared.runtime is None:
        runtime = derive_runtime(root, entries)
        if runtime is not None:
            declared.runtime = runtime
            declared.sources["runtime"] = "依赖里有浏览器自动化,推断为常驻运行 —— 待核对"
    if not declared.keep:
        keep = derive_keep(root, entries)
        if keep:
            declared.keep = keep
            declared.sources["keep"] = ".gitignore 里的本机状态 —— 请删掉不需要搬的"

    return Inspection(
        files=entries,
        total_bytes=sum(e.size for e in entries),
        top_dir=_common_top_dir(entries),
        readme_path=readme_path,
        readme_text=readme_text,
        declared=declared,
        launch_candidates=candidates,
        launch_guesses=guesses,
        install_steps=derive_install_steps(root, entries),
        env_files=[e.path for e in entries if PurePosixPath(e.path).name in (".env", ".env.local")],
        has_git_dir=any(p.path.startswith(".git/") or "/.git/" in p.path for p in entries),
    )


#: `kiro-cli chat --agent <名字>` 里的那个名字。
_KIRO_AGENT_ARG = re.compile(r"--agent[=\s]+([\w.-]+)")


def launch_target_exists(launch: str, entries: list[FileEntry]) -> bool:
    """启动命令里提到的脚本/程序,是不是真的在包里。

    这是整套流程里唯一的自动可用性检查,也是最容易错的一项:命令抄错一个字,用户粘贴后
    看到的是"找不到文件",而那行命令看起来完全正常。找不到不代表一定错(可能是 pip 装出来
    的 entry point),所以它只是一条提示,不拦。

    `kiro-cli chat --agent X` 单独处理:这条命令里一个词都不是包内文件,按下面的通用规则
    一定判 False,于是最常见、也最有把握的那种包每次都会亮一条"包里找不到这个文件"的警告。
    一处明显不对的提醒会让人连带不信别的提醒,所以这里去查它真正指向的东西 ——
    `.kiro/agents/X.json`。这样检查仍然有意义:agent 名写错了照样判 False。
    """
    agent = _KIRO_AGENT_ARG.search(launch)
    if agent and "kiro-cli" in launch:
        want = f"{_KIRO_AGENT_DIR}/{agent.group(1)}.json"
        return any(e.path == want or e.path.endswith(f"/{want}") for e in entries)

    names = {PurePosixPath(e.path).name for e in entries}
    stems = {PurePosixPath(e.path).as_posix() for e in entries}
    for token in re.findall(r"[\w./\\-]+", launch):
        base = PurePosixPath(token.replace("\\", "/")).name
        if base in names or token.lstrip("./") in stems:
            return True
    return False


def render_sidecar(declared: Declared, *, name: str, version: str) -> str:
    """生成要写进包里的 portal-agent.yaml。

    只**新增**这个文件,从不改上传者的 README:改了守不住(他下一版从自己仓库打包,我们的
    修改被无声覆盖),而且发布出去的包就不是审计过的那一个了。
    """
    keep = declared.keep or []
    prereq = declared.prerequisites or []
    lines = [
        "# 由 RP AI Portal 生成:安装提示词按这里的字段生成,不再去 README 里猜。",
        "# 这一份是给拿着 zip 离开门户的人看的;门户自己读的是数据库里那份(两者同时写入)。",
        f"name: {json_safe(name)}",
        f"version: {json_safe(version)}",
        f"launch: {json_safe(declared.launch or '')}",
        f"runtime: {json_safe(declared.runtime or 'oneshot')}",
        "keep:" if keep else "keep: []",
    ]
    lines += [f"  - {json_safe(k)}" for k in keep]
    lines.append("prerequisites:" if prereq else "prerequisites: []")
    lines += [f"  - {json_safe(p)}" for p in prereq]
    return "\n".join(lines) + "\n"


def json_safe(value: str) -> str:
    """YAML 里安全的标量写法。命令里常有引号、冒号、反斜杠,一律走双引号转义。"""
    return json.dumps(value, ensure_ascii=False)
