"""PythonAnywhere one-click deployment for the iCAN Flask app.

The script packages the clean authoritative repository, deploys through the
PythonAnywhere API, restores the previous WSGI file on every exit path, and
prints a machine-readable evidence line.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests

API_BASE = 'https://www.pythonanywhere.com/api/v0/user/{user}'
DEFAULT_USER = 'lxh123'
DEFAULT_DOMAIN = '{user}.pythonanywhere.com'
STATIC_PROBE = '/static/js/decision_cockpit.js'
REMOTE_ZIP = '/home/{user}/ican_latest.zip'


def _remote_wsgi(domain: str) -> str:
    return f'/var/www/{domain.replace(".", "_")}_wsgi.py'


def _find_project_root() -> Path:
    """定位工作区根（含 `iCAN大学生创新创业大赛/src` 的那一层）。

    2026-09-23 修正：本仓已从「`交付物/iCAN-参赛交付物/代码仓库`」旧布局收敛为
    「工作区根 + `iCAN大学生创新创业大赛/{src,部署}`」单仓布局（交付物改为
    `交付物/提交包/`）。旧的第二判据 `交付物/iCAN-参赛交付物` 已不存在，会让一键
    部署在第一步就抛「无法定位项目根目录」；现只以 `iCAN大学生创新创业大赛/src` 为判据。
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / 'iCAN大学生创新创业大赛' / 'src').is_dir():
            return parent
    raise RuntimeError('无法定位项目根目录')


def _run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ['git', '-C', str(repo), *args],
        capture_output=True, text=True, encoding='utf-8', errors='replace')
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return proc.stdout.strip()


def _read_token(args) -> str:
    """读取 PA API Token，并清理「贴进去的杂质」。

    2026-09-23 CI 实测踩坑：密钥经管道写入时可能带 BOM（`\\ufeff`）或尾随换行，
    未清理会让 Authorization 头在 urllib3 里抛
    `UnicodeEncodeError: 'latin-1' codec can't encode character '\\ufeff'`——
    报错点离根因很远。这里统一去空白 + 去 BOM，并对非 ASCII 直接给出可操作提示。
    """
    token = (args.token or os.environ.get('PA_API_TOKEN', '') or '').strip().lstrip('\ufeff')
    if not token:
        token_path = Path(tempfile.gettempdir()) / 'pa_token.txt'
        if token_path.is_file():
            token = token_path.read_text(encoding='utf-8').strip().lstrip('\ufeff')
    if not token:
        raise RuntimeError('缺少 PA_API_TOKEN 或 %TEMP%\\pa_token.txt')
    if not token.isascii():
        raise RuntimeError(
            'PA API Token 含非 ASCII 字符（常见成因：写入密钥时带入了 BOM / 零宽字符）。'
            '请在 PythonAnywhere → Account → API token 重新复制后重设密钥/临时文件。')
    return token


def _api_headers(token: str):
    return {'Authorization': f'Token {token}'}


def _api_file_url(user: str, path: str):
    return f'{API_BASE.format(user=user)}/files/path/{path.lstrip("/")}'


def _upload_file(user: str, token: str, remote_path: str, local_path: Path):
    with local_path.open('rb') as handle:
        response = requests.post(
            _api_file_url(user, remote_path),
            headers=_api_headers(token),
            files={'content': (local_path.name, handle, 'application/octet-stream')},
            timeout=120,
        )
    response.raise_for_status()
    return response


def _download_file(user: str, token: str, remote_path: str) -> bytes:
    response = requests.get(
        _api_file_url(user, remote_path),
        headers=_api_headers(token),
        timeout=60,
    )
    response.raise_for_status()
    return response.content


def _api_dir_listing(user: str, token: str, remote_dir: str) -> dict:
    """列出远端目录：`{名称: {'type': 'file'|'directory', 'url': …}}`（目录须带尾斜杠）。"""
    response = requests.get(
        _api_file_url(user, remote_dir.rstrip('/') + '/'),
        headers=_api_headers(token), timeout=60)
    response.raise_for_status()
    return response.json()


def _walk_remote(user: str, token: str, remote_dir: str, rel_prefix: str):
    """递归遍历远端目录，产出 (远端绝对路径, 包内相对路径)。"""
    for name, meta in _api_dir_listing(user, token, remote_dir).items():
        rel = f'{rel_prefix}/{name}' if rel_prefix else name
        if meta.get('type') == 'directory':
            if name == '__pycache__':
                continue
            yield from _walk_remote(user, token, f'{remote_dir}/{name}', rel)
        elif not name.endswith(('.pyc', '.pyo')):
            yield f'{remote_dir}/{name}', rel


def _snapshot_live(username: str, token: str, output_dir: Path) -> Path:
    """上线前快照**线上现有代码**，作为本次部署真正可用的回滚点。

    为什么需要（2026-09-23 实测教训）：回滚原先只认本地 `output/last_success.json` + 上一成功包，
    一旦 `output/` 被清理（确实发生过）就退化成「无回滚点、只能人工介入」。本函数把回滚点变成
    **自带**：部署前把线上 `ican/src` 拉回来打成与部署包同构的 zip；CI 每次运行也照样成立。

    范围只取代码（`src` + `init_db.py` + `requirements.txt` + `pa_wsgi.py`）：
    刻意**不取 `.env`**（含线上密钥，不该落进本地包）与 `data/`（运行数据库，回滚不涉及）。
    """
    root = f'/home/{username}/ican'
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    target = output_dir / f'pa-live-snapshot-{stamp}.zip'
    items = [(rel, _download_file(username, token, remote))
             for remote, rel in _walk_remote(username, token, f'{root}/src', 'src')]
    for name in ('init_db.py', 'requirements.txt', 'pa_wsgi.py'):
        try:
            items.append((name, _download_file(username, token, f'{root}/{name}')))
        except requests.RequestException:
            print(f'[PA-DEPLOY-WARN] 线上缺 {name}，快照跳过该文件')
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for rel, blob in items:
            archive.writestr(rel, blob)
    print(f'[PA-DEPLOY] 线上代码快照完成：{target.name}（{len(items)} 文件）')
    return target


def _resolve_rollback_point(args, username: str, token: str, output_dir: Path) -> dict:
    """选出本次部署的回滚点，**必须在任何写操作之前**调用。

    优先本地上一成功包；缺失或文件已被清理时回退为「上线前线上代码快照」；
    两者都不可得时明确告警（不复用失效路径，避免真正回滚时才发现下载 404）。
    """
    prev = _load_last_success(output_dir)
    recorded = prev.get('package')
    if recorded and Path(recorded).exists():
        print(f'[PA-DEPLOY] 回滚点（本地上一成功包）：{Path(recorded).name}')
        return prev
    if recorded:
        print(f'[PA-DEPLOY-WARN] 本地记录的成功包已不存在：{recorded}')
    if args.snapshot_live:
        print('[PA-DEPLOY] 无可用本地回滚包 → 先快照线上代码作为回滚点')
        snap = _snapshot_live(username, token, output_dir)
        return {'package': str(snap), 'commit': 'live-snapshot',
                'finished_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    print('[PA-DEPLOY-ALERT] 未启用线上快照且无本地回滚包：本次部署**没有**可自动回滚点')
    return {}


def _reload(user: str, domain: str, token: str):
    url = f'{API_BASE.format(user=user)}/webapps/{domain}/reload/'
    response = requests.post(url, headers=_api_headers(token), timeout=60)
    response.raise_for_status()
    if response.text.strip() != '{"status":"OK"}':
        raise RuntimeError(f'reload 返回异常: {response.text[:200]}')


def _wait_http(url: str, expected=200, timeout=90):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            response = requests.get(url, timeout=30)
            last = response
            if response.status_code == expected:
                return response
        except requests.RequestException:
            pass
        time.sleep(3)
    status = getattr(last, 'status_code', 'no-response')
    raise RuntimeError(f'{url} 未在 {timeout}s 内返回 {expected}，最后状态 {status}')


def _build_package(repo: Path, dev_root: Path, output_dir: Path, commit: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    package = output_dir / f'ican-pa-{commit[:8]}-{stamp}.zip'
    files = []
    for path in (repo / 'src').rglob('*'):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix not in {
                '.pyc', '.pyo'}:
            files.append((path, path.relative_to(repo)))
    for name in ('init_db.py', 'requirements.txt'):
        path = repo / name
        if not path.is_file():
            raise RuntimeError(f'部署文件缺失: {path}')
        files.append((path, Path(name)))
    pa_wsgi = dev_root / '部署' / 'pythonanywhere' / 'pa_wsgi.py'
    if not pa_wsgi.is_file():
        raise RuntimeError(f'PA WSGI 文件缺失: {pa_wsgi}')
    files.append((pa_wsgi, Path('pa_wsgi.py')))
    with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source, relative in files:
            archive.write(source, relative.as_posix())
    return package


def _temp_wsgi_text(user: str) -> str:
    return '''import os
import runpy
import sys
import traceback
import zipfile

ROOT = "/home/__USER__/ican"
ZIP_PATH = "/home/__USER__/ican_latest.zip"
report = []

try:
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass
    with zipfile.ZipFile(ZIP_PATH) as archive:
        names = set(archive.namelist())
        archive.extractall(ROOT)
    report.append("extract=ok")
    # ── 剪枝（2026-09-23）────────────────────────────────────────
    # 上传是「覆盖式解压」：仓库里删掉的文件会永远留在线上。实测线上 src 比仓库多
    # 3 个文件（旧 echarts.min.js 1.0MB + 两个旧哈希资产），属真实漂移。
    # 只清 ROOT/src 下不在包内清单里的文件与空目录，且在**解压成功之后**才执行。
    pruned = 0
    src_root = os.path.join(ROOT, "src")
    for dirpath, dirnames, filenames in os.walk(src_root, topdown=False):
        for fn in filenames:
            full_path = os.path.join(dirpath, fn)
            rel = os.path.relpath(full_path, ROOT).replace(os.sep, "/")
            if rel not in names:
                try:
                    os.remove(full_path)
                    pruned += 1
                except OSError:
                    pass
        for dn in dirnames:
            try:
                os.rmdir(os.path.join(dirpath, dn))
            except OSError:
                pass
    report.append("pruned=%d" % pruned)
    os.environ.setdefault("FLASK_ENV", "production")
    os.environ["ICAN_INIT_CONFIRM"] = "YES"
    os.chdir(ROOT)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    old_argv = sys.argv
    sys.argv = ["init_db.py", "--if-empty"]
    try:
        runpy.run_path(os.path.join(ROOT, "init_db.py"), run_name="__main__")
        report.append("init_db=ok")
    except SystemExit as exc:
        if exc.code in (None, 0):
            report.append("init_db=ok")
        else:
            raise
    finally:
        sys.argv = old_argv
except BaseException:
    report.append("deploy=failed")
    # 堆栈**不进响应体**：这个一次性 WSGI 挂在公网域名上，traceback 会带出
    # /home/<user>/ 绝对路径与依赖栈版本（盲审红线 + 安全红线同源：内部细节不属于响应体）。
    # 改落服务端文件，响应里只留一个可 grep 的指针。
    try:
        with open(os.path.join(ROOT, "deploy_error.log"), "w", encoding="utf-8") as _fh:
            _fh.write(traceback.format_exc())
        report.append("traceback_file=deploy_error.log")
    except OSError:
        report.append("traceback_file=unwritable")


def application(environ, start_response):
    body = "\\n".join(report).encode("utf-8")
    # 失败必须改变状态码：此前无论成败一律 200，部署校验只能靠人读正文，
    # 与第 8 轮「CI 一直绿但快照链断了四轮」同族——判据不落在退出码/状态码上就等于没有判据。
    failed = any(ln == "deploy=failed" or ln.startswith("init_db=fail") for ln in report)
    start_response("503 Service Unavailable" if failed else "200 OK", [
        ("Content-Type", "text/plain; charset=utf-8"),
        ("Content-Length", str(len(body))),
    ])
    return [body]
'''.replace('__USER__', user)


LAST_SUCCESS_FILE = 'last_success.json'


def _load_last_success(output_dir: Path) -> dict:
    """读取上一次成功部署记录（回滚点）；不存在/损坏时返回 {}。"""
    import json
    try:
        return json.loads((output_dir / LAST_SUCCESS_FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _save_last_success(output_dir: Path, payload: dict) -> None:
    import json
    (output_dir / LAST_SUCCESS_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def _rollback(username: str, domain: str, token: str, prev_package: Path,
              wsgi_remote: str) -> bool:
    """回滚到上一成功包：重传 zip → 临时 wsgi 触发解压 → 复验静态资源。

    说明（双向迭代 S1）：PythonAnywhere 无 Cloudflare Pages 那样的部署版本/rollback API，
    故以「本地留存的上一成功包」为回滚点——这是架构前提差异下的等价实现（铁律 F1）。
    回滚成功也必须打印人工告警：止损 ≠ 已恢复正常，需人工复盘。
    """
    try:
        _upload_file(username, token, REMOTE_ZIP.format(user=username), prev_package)
        temp = prev_package.parent / 'pa-rollback-wsgi.py'
        temp.write_text(_temp_wsgi_text(username), encoding='utf-8')
        _upload_file(username, token, wsgi_remote, temp)
        _reload(username, domain, token)
        trigger = _wait_http(f'https://{domain}/', expected=200, timeout=90)
        ok = 'extract=ok' in trigger.text
        _wait_http(f'https://{domain}{STATIC_PROBE}', expected=200, timeout=90)
        return ok
    except Exception as exc:
        print(f'[PA-DEPLOY-ROLLBACK-FAIL] {exc}')
        return False


def _init_evidence(project_root: Path, repo: Path, commit: str, domain: str) -> dict:
    return {
        'started_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'project_root': str(project_root),
        'repo': str(repo),
        'commit': commit,
        'domain': domain,
    }


def _verify_deploy(username: str, domain: str, token: str, wsgi_remote: str, prev: dict) -> bool:
    """部署后验收：首页 + 静态资源 + 源码探测；失败即回滚到上一成功包并升级人工告警。

    返回 True = 验收通过；False = 失败（已尽力回滚，调用方应判为部署失败）。
    """
    try:
        _wait_http(f'https://{domain}/', expected=200, timeout=90)
        _wait_http(f'https://{domain}{STATIC_PROBE}', expected=200, timeout=90)
        source_probe = requests.get(
            _api_file_url(username, f'/home/{username}/ican/src/decision_cockpit.py'),
            headers=_api_headers(token), timeout=30)
        if source_probe.status_code != 200:
            raise RuntimeError(f'线上 decision_cockpit.py 不可读: {source_probe.status_code}')
        return True
    except Exception as exc:
        print(f'[PA-DEPLOY-SMOKE-FAIL] {exc}')
        prev_pkg = Path(prev['package']) if prev.get('package') else None
        if prev_pkg is not None and prev_pkg.exists():
            rolled = _rollback(username, domain, token, prev_pkg, wsgi_remote)
            print(f'[PA-DEPLOY-ROLLBACK] {"OK" if rolled else "FAILED"} → 目标包 {prev_pkg.name}')
            if rolled:
                print('[PA-DEPLOY-ALERT] 已回滚到上一版本（止损），本次上线未成功，需人工复盘：'
                      '核对 output/ 下 evidence 与 PythonAnywhere error log')
            else:
                print('[PA-DEPLOY-ALERT] 自动回滚失败/复验未过，生产可能仍异常，请立即人工介入：'
                      'PythonAnywhere → Web → 检查源码目录与 error log')
        else:
            print('[PA-DEPLOY-ALERT] 无可用上一成功包（首次部署？），无法自动回滚，请人工介入')
        return False


def _write_evidence(output_dir: Path, payload: dict) -> Path:
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    path = output_dir / f'pa-deploy-{stamp}.json'
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description='iCAN PythonAnywhere one-click deploy')
    parser.add_argument('--allow-dirty', action='store_true',
                        help='允许权威仓库存在未提交改动')
    parser.add_argument('--token', default='', help='PA API Token；默认读环境变量或临时文件')
    parser.add_argument('--username', default=os.environ.get('PA_USERNAME', DEFAULT_USER))
    parser.add_argument('--domain', default=os.environ.get('PA_DOMAIN', ''),
                        help='默认 <username>.pythonanywhere.com')
    parser.add_argument('--snapshot-live', dest='snapshot_live', action='store_true',
                        default=True,
                        help='无本地成功包时，先快照线上代码作为回滚点（默认开）')
    parser.add_argument('--no-snapshot-live', dest='snapshot_live', action='store_false',
                        help='禁止线上快照（无本地回滚包时接受「本次部署不可自动回滚」）')
    parser.add_argument('--snapshot-only', action='store_true',
                        help='只快照线上代码并把结果登记为回滚点，不部署（人工刷新回滚点用）')
    args = parser.parse_args()

    project_root = _find_project_root()
    dev_root = project_root / 'iCAN大学生创新创业大赛'
    # 2026-09-23：权威源码目录即 `iCAN大学生创新创业大赛/`（旧布局的
    # `交付物/iCAN-参赛交付物/代码仓库` 已随交付包改版消失）。它本身不是 git 根，
    # 但 `git -C <子目录>` 会报告**整仓**状态，故「工作树是否干净」与 commit 取值不受影响。
    repo = dev_root
    output_dir = project_root / 'output'
    output_dir.mkdir(exist_ok=True)
    domain = args.domain or DEFAULT_DOMAIN.format(user=args.username)

    dirty = _run_git(repo, 'status', '--porcelain')
    if args.snapshot_only:
        # 只读动作：不碰线上，故不受「工作树须干净」约束（人工刷新回滚点的常用场景）
        token = _read_token(args)
        evidence = _init_evidence(project_root, repo, _run_git(repo, 'rev-parse', 'HEAD'), domain)
        rollback = _resolve_rollback_point(args, args.username, token, output_dir)
        evidence['rollback_point'] = rollback.get('package', '')
        evidence.update({'finished_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                         'status': 'SNAPSHOT-ONLY'})
        path = _write_evidence(output_dir, evidence)
        _save_last_success(output_dir, rollback)
        print(f'[PA-DEPLOY-SNAPSHOT-ONLY] 回滚点已就位：{rollback.get("package")} evidence={path}')
        return 0
    if dirty and not args.allow_dirty:
        # 把脏条目打出来：否则使用者只知道「不干净」，得自己去猜（2026-09-23 CI 首跑即栽在这）
        raise RuntimeError('权威仓库工作树不干净，拒绝上线；请先提交或使用 --allow-dirty\n'
                           '未提交条目：\n' + '\n'.join(dirty.splitlines()[:20]))
    commit = _run_git(repo, 'rev-parse', 'HEAD')
    token = _read_token(args)
    wsgi_remote = _remote_wsgi(domain)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    backup_path = output_dir / f'pa-wsgi-{stamp}.py'
    temp_wsgi_path = output_dir / f'pa-deploy-once-{stamp}.py'
    package = None
    original_wsgi = None
    evidence = _init_evidence(project_root, repo, commit, domain)
    # 回滚点必须在**任何写操作之前**确定：优先本地上一成功包，缺失则先快照线上代码。
    # 放到部署之后再取会拿到「刚被覆盖的线上代码」，等于回滚到自己。
    prev = _resolve_rollback_point(args, args.username, token, output_dir)
    evidence['rollback_point'] = prev.get('package', '')
    try:
        original_wsgi = _download_file(args.username, token, wsgi_remote)
        backup_path.write_bytes(original_wsgi)
        evidence['wsgi_backup'] = str(backup_path)

        package = _build_package(repo, dev_root, output_dir, commit)
        evidence['package'] = str(package)
        evidence['package_bytes'] = package.stat().st_size

        upload = _upload_file(
            args.username, token, REMOTE_ZIP.format(user=args.username), package)
        evidence['zip_upload_status'] = upload.status_code

        temp_wsgi_path.write_text(_temp_wsgi_text(args.username), encoding='utf-8')
        upload = _upload_file(args.username, token, wsgi_remote, temp_wsgi_path)
        evidence['temp_wsgi_upload_status'] = upload.status_code

        _reload(args.username, domain, token)
        trigger = _wait_http(f'https://{domain}/', expected=200, timeout=90)
        evidence['deploy_report'] = trigger.text[:4000]
        if 'extract=ok' not in trigger.text or 'init_db=ok' not in trigger.text:
            raise RuntimeError(f'远程解压/迁移失败: {trigger.text[:1000]}')
    finally:
        if original_wsgi is not None:
            restore = requests.post(
                _api_file_url(args.username, wsgi_remote),
                headers=_api_headers(token),
                files={'content': (Path(wsgi_remote).name, original_wsgi,
                                   'application/octet-stream')},
                timeout=60,
            )
            restore.raise_for_status()
            evidence['wsgi_restore_status'] = restore.status_code
            _reload(args.username, domain, token)

    # ── 部署后验收 + 失败自动回滚（2026-09-18 双向迭代 S1）──
    # 借鉴超市web ci.yml 的三要素：探活 → 失败回滚 → 复验 → 人工升级告警。
    # 回滚点 `prev` 已在部署前解析（见上），此处直接复用。
    if not _verify_deploy(args.username, domain, token, wsgi_remote, prev):
        evidence.update({'finished_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'status': 'FAIL'})
        _write_evidence(output_dir, evidence)
        return 1

    evidence.update({
        'finished_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'status': 'PASS',
    })
    evidence_path = _write_evidence(output_dir, evidence)
    _save_last_success(output_dir, {
        'package': str(package),
        'commit': commit,
        'finished_at': evidence['finished_at'],
    })
    print(f'[PA-DEPLOY-PASS] commit={commit[:8]} package={package} evidence={evidence_path}')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'[PA-DEPLOY-FAIL] {exc}')
        raise
