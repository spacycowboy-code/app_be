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
"""
from __future__ import annotations

import io
import re
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
class Declared:
    """包对自己的描述。三项都可能是空的,空就是"要问上传者"。"""

    launch: str | None = None
    runtime: str | None = None
    keep: list[str] = field(default_factory=list)
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
        src = {}
        if launch:
            src["launch"] = f"{SIDECAR_NAME}"
        if runtime:
            src["runtime"] = f"{SIDECAR_NAME}"
        if keep:
            src["keep"] = f"{SIDECAR_NAME}"
        return Declared(
            launch=launch,
            runtime=runtime if runtime in RUNTIMES else None,
            keep=[str(k) for k in keep if str(k).strip()],
            sources=src,
        )
    return Declared()


def inspect(root: Path, entries: list[FileEntry]) -> Inspection:
    """把解开的目录看一遍,得出确认屏和审核页要显示的全部事实。"""
    readme_path, readme_text = _read_readme(root, entries)
    declared = read_sidecar(root, entries)
    candidates = launch_candidates(readme_text)
    if declared.launch is None and candidates:
        declared.launch = candidates[0]
        declared.sources["launch"] = (
            f"从 {readme_path} 猜的候选,请核对" if readme_path else "候选"
        )
    return Inspection(
        files=entries,
        total_bytes=sum(e.size for e in entries),
        top_dir=_common_top_dir(entries),
        readme_path=readme_path,
        readme_text=readme_text,
        declared=declared,
        launch_candidates=candidates,
        env_files=[e.path for e in entries if PurePosixPath(e.path).name in (".env", ".env.local")],
        has_git_dir=any(p.path.startswith(".git/") or "/.git/" in p.path for p in entries),
    )


def launch_target_exists(launch: str, entries: list[FileEntry]) -> bool:
    """启动命令里提到的脚本/程序,是不是真的在包里。

    这是整套流程里唯一的自动可用性检查,也是最容易错的一项:命令抄错一个字,用户粘贴后
    看到的是"找不到文件",而那行命令看起来完全正常。找不到不代表一定错(可能是 pip 装出来
    的 entry point),所以它只是一条提示,不拦。
    """
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
    return "\n".join(lines) + "\n"


def json_safe(value: str) -> str:
    """YAML 里安全的标量写法。命令里常有引号、冒号、反斜杠,一律走双引号转义。"""
    import json

    return json.dumps(value, ensure_ascii=False)
