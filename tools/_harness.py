"""验收／取证脚本的公共底座：临时目录、空闲端口、静态服务、无头 Chrome、CDP。

为什么要有它：`tools/` 下十几个脚本各自抄过同一段「起临时库 + 起端口 +
起无头 Chrome + 等它 ready」。抄的代价不是多几行，而是三处会各自漂：
端口写死（8012 与 `production_scenario_check.py` 撞车）、Chrome 路径写死
macOS 的 `.app`（CI 的 ubuntu runner 直接跑不了）、临时目录写死
`/tmp/<name>`（容器里可能删不掉，本机重启后还留着上一轮的库）。

边界：这里只管「把进程／端口／浏览器准备好」，不碰任何业务断言。
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "app" / "backend"
FRONTEND = ROOT / "app" / "frontend"


def free_port() -> int:
    """让内核分配空闲端口：写死端口会撞上上一轮残留的服务或 Chrome。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def chrome_binary() -> str:
    """先认 CHROME_BIN，再看常见安装位置；找不到就明确说怎么办。"""
    env = os.environ.get("CHROME_BIN")
    if env:
        return env
    for path in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ):
        if Path(path).exists():
            return path
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit("找不到 Chrome/Chromium；装一个，或用 CHROME_BIN 指向可执行文件")


# 本次进程建过的临时目录。**建了就要收**（见 `_cleanup_temp_dirs`）：
# 一个 Chrome profile 是 50–110MB，浏览器验收跑一轮建一个，从不回收的话
# `.pytest-tmp/` 一个月就能长到 1.6G——那不是磁盘太小，是这里少了一口气。
_TEMP_DIRS: list[Path] = []
_cleanup_hooked = False


def _cleanup_temp_dirs() -> None:
    """进程退出时把自己建的临时目录删掉。

    三条边界，缺一条就会把这件事做坏：
    1. **只删自己建的**：`_TEMP_DIRS` 里是本次进程 `mkdtemp` 出来的路径，
       不按前缀扫目录——按前缀扫会把并行的另一轮验收正在用的目录一起删掉。
    2. **失败不炸**：进程已经在退出了，删不掉（被占用、权限）只该留个念想，
       不该让一个跑完的验收变成非零退出。删不掉的下次 `--prune` 会收。
    3. **`FASTVIDEO_KEEP_TMP=1` 时整个跳过**：调试验收脚本时经常需要回头看
       库副本与 Chrome profile，那时候"清理"反而是帮倒忙。
    """
    if os.environ.get("FASTVIDEO_KEEP_TMP") == "1":
        return
    while _TEMP_DIRS:
        path = _TEMP_DIRS.pop()
        # Chrome 被 terminate 之后还会短暂持有 profile 里的文件（写到一半的
        # `Local State`、`Variations`），`rmtree` 在这种时刻会半途失败。
        # 不重试的结果就是"每次都留下一个 12K 的残壳"——体积没关系，
        # 但 `ls` 里永远多一条，人就会以为清理根本没生效。
        for attempt in range(3):
            shutil.rmtree(path, ignore_errors=True)
            if not path.exists():
                break
            time.sleep(0.2 * (attempt + 1))


def temp_dir(prefix: str = "fv-check-") -> Path:
    """建一个本次运行独占的临时目录，并登记进退出清理。

    `FASTVIDEO_CHECK_TMP` 可把它指到工作区内（沙箱里 `/tmp` 的删除可能被拦）；
    不设就退回系统临时目录。目录名带随机后缀，不会和上一轮撞。

    返回的目录在进程正常退出时会被删掉；要留着看就把 `FASTVIDEO_KEEP_TMP=1`。
    """
    global _cleanup_hooked
    base = os.environ.get("FASTVIDEO_CHECK_TMP")
    if base:
        Path(base).mkdir(parents=True, exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix=prefix, dir=base))
    else:
        path = Path(tempfile.mkdtemp(prefix=prefix))
    _TEMP_DIRS.append(path)
    if not _cleanup_hooked:
        # 用 atexit 而不是让每个脚本自己 finally：九个脚本里已经有六个
        # 只 terminate 进程、忘了删目录（`.pytest-tmp` 里 110 个 fv-* 就是这么来的）。
        # 收在一处，新脚本什么都不用做就自动带上这一条。
        atexit.register(_cleanup_temp_dirs)
        _cleanup_hooked = True
    return path


def prune_temp_dirs(base: Path | str | None = None, *, keep_prefix: str = "",
                    dry_run: bool = False) -> tuple[int, int]:
    """清掉历史残留的 `fv-*` 临时目录，返回 (删了几个, 释放多少 MB)。

    `atexit` 只保证**以后**不再漏；已经躺在地上的那一堆还得有人收一次。
    这就是那个"有人"——`python tools/_harness.py --prune` 调它。

    只认 `fv-`／`jscheck-` 前缀（这些是 `temp_dir()` 的默认前缀与脚本传进去的），
    `keep_prefix` 可以再点名保留某一族（比如正在排查某一轮的目录）。
    """
    root = Path(base) if base else Path(os.environ.get(
        "FASTVIDEO_CHECK_TMP", ROOT / ".pytest-tmp"))
    if not root.is_dir():
        return 0, 0
    removed = freed = 0
    for path in root.iterdir():
        if not _is_disposable(path):
            continue
        if keep_prefix and path.name.startswith(keep_prefix):
            continue
        size = (sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
                if path.is_dir() else path.stat().st_size)
        if not dry_run:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        removed += 1
        freed += size
    return removed, freed // (1024 * 1024)


# 这些前缀/名字是"跑一轮就丢"的产物，`prune_temp_dirs` 认它们：
#   fv-*        浏览器验收的库副本与 Chrome profile（`temp_dir()` 建的）
#   jscheck-*   `js_load_check.py` 的 Chrome profile
#   test_*      pytest 的 basetemp 用例目录（--basetemp 指到这儿时留下的）
#   bt／chk     basetemp 跑多轮后 pytest 自己的分桶目录
#   diag_*.py   一次性取证脚本——跑完就该没，留在仓库里只会被下次提交带上
_DISPOSABLE_PREFIXES = (
    "fv-", "fvtest-",           # `temp_dir()` 与 conftest 的测试库副本
    "jscheck-",                 # js_load_check 的 Chrome profile
    "test_",                    # pytest basetemp 的用例目录
    "bt", "chk",                # basetemp 跑多轮后 pytest 自己的分桶
    "full", "findfail",         # 手动排查轮留下的分桶（同 basetemp 形状）
)
_DISPOSABLE_SUFFIXES = (".py",)     # 仅配合 diag_ 前缀，见下


def _is_disposable(path: Path) -> bool:
    """这一条是不是"跑一轮就丢"的临时产物。

    为什么要有判据而不是"清空目录"：`FASTVIDEO_CHECK_TMP` 可能被人指到一个
    还放着别的东西的目录（比如整个 `/tmp`）。清空目录是破坏性的，
    认名字才敢删——名字不认识的原样留着，删不干净好过删错。
    """
    name = path.name
    if name.startswith("diag_") and path.is_file() and name.endswith(_DISPOSABLE_SUFFIXES):
        return True
    return path.is_dir() and name.startswith(_DISPOSABLE_PREFIXES)


def serve_static(root: Path = FRONTEND, port: int | None = None
                 ) -> tuple[ThreadingHTTPServer, int]:
    """起一个只读静态服务；返回 (httpd, port)，调用方负责 shutdown。"""
    port = port or free_port()
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def launch_chrome(debug_port: int, profile: Path | None = None,
                  window: str = "1440,1000") -> subprocess.Popen:
    profile = profile or temp_dir("fv-chrome-")
    return subprocess.Popen(
        [chrome_binary(), "--headless=new", "--disable-gpu", "--no-sandbox",
         "--no-first-run", "--no-default-browser-check",
         f"--user-data-dir={profile}", f"--remote-debugging-port={debug_port}",
         f"--window-size={window}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_http(url: str, timeout: float = 30.0, expect: int = 200) -> None:
    """轮询一个 HTTP 端点直到它给出 `expect`；超时就明确点名，不静默继续。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == expect:
                    return
        except Exception:
            time.sleep(0.25)
    raise SystemExit(f"{url} 在 {timeout:.0f}s 内没起来")


def wait_cdp(debug_port: int, timeout: float = 30.0) -> str:
    """等 Chrome 的调试端口，返回第一个 page 的 WebSocket URL。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{debug_port}/json", timeout=1).read())
            for tab in data:
                if tab.get("type") == "page":
                    return tab["webSocketDebuggerUrl"]
        except Exception:
            time.sleep(0.2)
    raise SystemExit(f"Chrome 调试端口 {debug_port} 没有可用页面")


def backend_python(backend: Path = BACKEND) -> Path:
    """后端该用哪个解释器：有 venv 用 venv，CI 的干净 runner 退回当前解释器。

    2026-11-09 体检：`start_service()` 里这段回退当初就是为了"写死 .venv/bin/python
    的脚本在 CI 里 FileNotFoundError"加的，但另有六个脚本（model_center_browser_check、
    production_browser_check、generate_scene_check、model_center_shots、
    production_scenario_check、production_shots）各自拼字符串起 uvicorn/worker，
    回退漏了它们。凡是要起后端进程的，都从这里取解释器，不要再拼 `.venv`。
    """
    venv = backend / ".venv" / "bin" / "python"
    return venv if venv.exists() else Path(sys.executable)


def start_service(port: int, python: Path | None = None,
                  backend: Path = BACKEND) -> subprocess.Popen:
    """用后端自己的 venv 起 uvicorn；没有 venv（CI 的干净 runner）就退回当前解释器。

    原来写死 `.venv/bin/python`，CI 里 `pip install -r requirements.txt` 装的是
    系统解释器，于是三套浏览器验收永远进不了流水线。
    """
    if python is None:
        python = backend_python(backend)
    return subprocess.Popen(
        [str(python), "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(backend), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def prepare_backend_db(dst: Path, backend: Path = BACKEND) -> None:
    """准备一份可用的测试库：有现役库就复制，没有就从迁移起一个并建 admin。

    这一步是"浏览器验收能不能进 CI"的关键：原来无条件复制
    `backend/data/platform.db`，干净 runner 上根本没有这个文件。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    live = backend / "data" / "platform.db"
    if live.exists():
        shutil.copy(live, dst)
        for suffix in ("-wal", "-shm"):
            src = backend / "data" / f"platform.db{suffix}"
            if src.exists():
                shutil.copy(src, dst.with_name(dst.name + suffix))
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    from migrate import migrate
    migrate(verbose=False)
    if not live.exists():
        from bootstrap import ensure_admin
        ensure_admin(verbose=False)


def stop(proc, grace: float = 5.0) -> None:
    """先 SIGTERM，等不到再 SIGKILL。所有脚本的 finally 都用这一个。"""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()


def _main(argv: list[str]) -> int:
    """`python tools/_harness.py --prune [--dry-run] [--keep PREFIX]`

    为什么收在这里：这些目录是这个模块建的，清理逻辑也该与它同住。
    另起一个 `clean_tmp.py` 会让"谁负责回收"变成两个人都不管的事。
    """
    if "--prune" not in argv:
        print("用法：python tools/_harness.py --prune [--dry-run] [--keep 前缀]")
        print("  清掉 .pytest-tmp 下历史残留的 fv-*／jscheck-* 临时目录。")
        print("  当前进程建的目录由 atexit 自动删；这条只收历史遗留。")
        return 2
    dry = "--dry-run" in argv
    keep = ""
    if "--keep" in argv:
        idx = argv.index("--keep")
        if idx + 1 < len(argv):
            keep = argv[idx + 1]
    root = Path(os.environ.get("FASTVIDEO_CHECK_TMP", ROOT / ".pytest-tmp"))
    if not dry:
        # 先把自己这一轮会建的也登记上，免得 prune 时正连着别的东西
        _cleanup_temp_dirs()
    removed, freed = prune_temp_dirs(root, keep_prefix=keep, dry_run=dry)
    verb = "将删除" if dry else "已删除"
    print(f"{verb} {removed} 个临时目录，约 {freed} MB（{root}）"
          + (f"；保留前缀 {keep}*" if keep else ""))
    if dry:
        print("（--dry-run：什么都没动）")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
