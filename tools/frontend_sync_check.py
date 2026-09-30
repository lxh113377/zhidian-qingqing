#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""三副本同步守卫 —— 本文件是**唯一**的跨副本复制出口（R248 三类双向比对，第 37 轮扩面）。

背景（不变）：本仓库存在多份源码 —— 权威源 `iCAN大学生创新创业大赛/` 与两份交付副本
`deploy-free/`、`交付物/提交包/工程佐证材料/05-源代码全文/`。它们靠复制维持一致，
而"存在性 ≠ 一致性"、"数文件数 ≠ 逐字节一致"，所以本脚本做**内容哈希双向比对**，
三类全部归零才算同步完成：

    MISSING  权威源有、副本无
    DIFF     两边都有但 SHA256 不等
    EXTRA    副本有、权威源无（**不自动删除**，须人工复核）

第 37 轮为什么扩面（当场实测，不是推断）：本文件此前只覆盖 `src/ → deploy-free/src/` 一格，
根级交付件（`quality_gate.py` / `init_db.py` / `restore_db.py` … 共 31 项）与 **05 交付副本**
完全不在射程内 —— 于是第 31—36 轮每轮交付链都在跑一个**未版本控制的一次性脚本**
（`D:/tmp/bench32/sync31.py`，`git ls-files` 命中 0）补另一半。一个交付动作两个实现、
其中一个不在仓里，就是"换台机器就断链"的形态。现在：

    面 = src/**  ∪  (quality_gate.ROOT_DELIVERED ∪ set_version 的 md 载体)
    镜像 = deploy-free/  与  05-源代码全文/

**面从声明现取，不手抄名单**：名单一旦在两处各写一份，下一轮加交付件只会漏掉一处
（第 34 轮版本载体漏一格、第 36 轮备份人口清单都踩过同一形态）。声明读不出来 ⇒ 退 2 并点名，
禁止把"我没取到面"写成"面里没有差异 = 通过"。

用法：
    python tools/frontend_sync_check.py            # 只读检查（默认）
    python tools/frontend_sync_check.py --fix      # 权威源 → 副本 单向覆盖后**重新扫描复核**
                                                   # （复制完再扫一遍，不拿内存里那份字符串自证）

退出码：0 = 三类全零；1 = 存在任一差异；2 = 面无法解析（**跑在了镜像面上** /
        权威源缺失 / 声明读不出来 / --only 写错）。
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import ctypes
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# 本文件位于 <仓根>/iCAN大学生创新创业大赛/tools/ 下
HERE = Path(__file__).resolve().parent
DEV = HERE.parent                      # 权威源根（开发副本）
REPO = DEV.parent                      # 工作空间根
SRC_FACE = 'src'                       # 整棵树比对
# 权威源面的**目录名**（第 40 轮加的入场闸用）。本工具的全部根都由 `__file__` 上溯得出，
# 复制到镜像面后 DEV 会变成那个镜像、REPO 会变成镜像的爹 ⇒ 只读面报假红、--fix 会往交付树里造文件。
DEV_FACE_NAME = 'iCAN大学生创新创业大赛'
MIRROR_DIRS = {
    'deploy-free': REPO / 'deploy-free',
    '05-源代码全文': REPO / '交付物' / '提交包' / '工程佐证材料' / '05-源代码全文',
}
# set_version 的载体里只有这两项属于"跨副本必须一致"的仓内文件；
# REPO_ROOT_README 是仓根 README（交付副本里不存在），'src/version.py' 已含在 src/** 里。
MD_CARRIER_NAMES = ('README.md', 'UPGRADE.md')
DECL_FILE = 'quality_gate.py'          # 根级交付白名单的**唯一**真相源所在文件
CARRIER_FILE = 'set_version.py'        # md 载体的唯一声明所在文件
DRIFT_FILE = 'check_delivery_drift.py'  # 白名单外、但要求三副本一致的那批文件的声明处
EXCLUDE_DIRS = {'__pycache__', '.ipynb_checkpoints'}
# 工具自己必须进同步面：门禁第 52 项在**权威副本**里也要能看见这个写者，否则副本面里没有它 ⇒ 判据读到'零个写者'而红（本轮实测 `ls 05…/tools/` 无此文件）。
SELF_ON_FACE = 'tools/frontend_sync_check.py'
EXCLUDE_SUFFIXES = {'.pyc', '.pyo'}
LIST_LIMIT = 20


class FaceError(RuntimeError):
    """面取不出来就是**输入缺失**，不是"没有差异"。"""


def face_problems() -> list[str]:
    """入场闸：这份工具只许在**权威源面**上跑（第 40 轮）。

    一手代价：把同一份代码在 `05-源代码全文/tools/` 下跑一遍，DEV 解析成该镜像、
    REPO 解析成 `工程佐证材料`，两个镜像根都不在那里 ⇒ 扫出 `MISSING 472`（全是这个面
    自己的文件）。比假红更糟的是第 `--fix` 分支里有 `mkdir(parents=True)`：**照处方跑一次
    就把 472 个副本造进 `交付物/提交包/` 里**，污染的是要交出去的那棵树。
    所以"认不出权威源面"必须是 rc=2 的输入缺失，不得当成"有 472 项待同步"。
    """
    probs = []
    if DEV.name != DEV_FACE_NAME:
        probs.append(f'当前解析到的"权威源"不是源面：DEV={DEV}（源面应为 <仓根>/{DEV_FACE_NAME}/）')
    if not DEV.is_dir():
        probs.append(f'权威源根不存在：{DEV}')
    for k, v in MIRROR_DIRS.items():
        if not v.is_dir():
            probs.append(f'镜像「{k}」不存在：{v}')
        elif v.resolve() == DEV.resolve():
            probs.append(f'镜像「{k}」与权威源是同一个面：{v}（自己比自己必然全等）')
    return probs


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _module_literal(path: Path, name: str):
    """从模块源码里取一个模块级字面量（不 import，避免为读名单去执行门禁全量代码）。"""
    if not path.is_file():
        raise FaceError(f'声明文件不存在，无法取面: {path}')
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        raise FaceError(f'解析声明文件失败 {path.name}: {type(exc).__name__}: {exc}') from None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for tgt in targets:
            if isinstance(tgt, ast.Name) and tgt.id == name:
                try:
                    return ast.literal_eval(node.value)
                except ValueError as exc:
                    raise FaceError(f'{name} 不是字面量（{type(exc).__name__}）⇒ 面不可解析') from None
    raise FaceError(f'{path.name} 里找不到 {name} 的字面量赋值 ⇒ 面不可解析')


def declared_files() -> tuple[str, ...]:
    """根级同步面 = 三处声明的并集，**一律现取不手抄**：

      - `quality_gate.ROOT_DELIVERED`      交付白名单（31 项）
      - `check_delivery_drift.EXTRA_TRACKED` 白名单外但要求三副本一致的那批（漂移检查器已经在管它们，
        可同步侧此前完全不知道 ⇒ "查得到、修不了"就是本轮要收的口子）
      - `set_version.CARRIERS` 里的仓内 md 载体（README / UPGRADE）

    任何一处声明读不出来 ⇒ `FaceError`（面不可解析），**不得**退化成"面里没有差异 = 通过"。
    """
    names = {str(x) for x in _module_literal(DEV / DECL_FILE, 'ROOT_DELIVERED')}
    names |= {str(x) for x in _module_literal(DEV / DRIFT_FILE, 'EXTRA_TRACKED')}
    names.add(SELF_ON_FACE)                          # 见常量注释：工具自己也在面内
    names |= {str(rel) for rel, *_ in _module_literal(DEV / CARRIER_FILE, 'CARRIERS')
              if str(rel).endswith(MD_CARRIER_NAMES)}
    if len(names) < 30:                        # 31+6+2+1(自身) 的正常量级；骤降=某处声明被改坏
        raise FaceError(f'声明面只剩 {len(names)} 项（历史量级 39）⇒ 判声明被改坏，不判"无差异"')
    esc = sorted(n for n in names
                 if n.startswith('/') or '..' in Path(n).parts or Path(n).is_absolute())
    if esc:
        # 复制类工具一旦吃到 `../x`，等于"往镜像根之外写文件"。
        # 宁可停下并点名，也不静默丢掉一条声明（静默丢 = 面悄悄变小，下一轮没人知道）。
        raise FaceError(f'同步面里出现越界路径 {esc[:3]} ⇒ 拒绝执行')
    return tuple(sorted(names))


def scan_tree(root: Path) -> dict[str, str]:
    """返回 {相对路径: SHA256}；目录不存在 ⇒ 空 dict（由调用方判 MISSING 全量并退非 0）。"""
    if not root.is_dir():
        return {}
    out: dict[str, str] = {}
    for p in root.rglob('*'):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if p.suffix in EXCLUDE_SUFFIXES:
            continue
        out[str(rel).replace('\\', '/')] = sha256(p)
    return out


def scan_files(root: Path, names: tuple[str, ...]) -> dict[str, str]:
    """按声明名单逐项取哈希；**缺文件不进 map** —— 这样 set 差集自然把它归到 MISSING/EXTRA，
    而不是混进 DIFF（"两边都有但不同"与"这一侧根本没有"是两类处置）。"""
    out: dict[str, str] = {}
    for n in names:
        p = root / n
        if p.is_file():
            out[n] = sha256(p)
    return out


def classify(src_map: dict[str, str], dst_map: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    """三类差集的**唯一**算法：`(MISSING, DIFF, EXTRA)`。

    第 43 轮抽出来是给 EXTRA 出口复用 —— 同一事实只许一处判，
    两处各写一遍 set 差集，早晚会算出两个不一样的 EXTRA。
    """
    missing = sorted(set(src_map) - set(dst_map))
    diff = sorted(k for k in set(src_map) & set(dst_map) if src_map[k] != dst_map[k])
    extra = sorted(set(dst_map) - set(src_map))
    return missing, diff, extra


def report(label: str, mirror: str, src_map: dict[str, str], dst_map: dict[str, str]) -> tuple[int, int, int]:
    """打印一格的三类读数，并**把三个数回传给调用方**——门面行要带值，
    否则被 `verify_local` 折叠成一行时只剩 `[PASS]`，实测数就丢了（第 34 轮同一族）。"""
    missing, diff, extra = classify(src_map, dst_map)
    print(f'{label} -> {mirror}: 权威 {len(src_map)} / 副本 {len(dst_map)} / '
          f'MISSING {len(missing)} / DIFF {len(diff)} / EXTRA {len(extra)}')
    for k in missing[:LIST_LIMIT]:
        print(f'   - {k}')
    for k in diff[:LIST_LIMIT]:
        print(f'   ~ {k}')
    for k in extra[:LIST_LIMIT]:
        print(f'   + {k}')
    return len(missing), len(diff), len(extra)


def build_faces(base: Path, mirror_root: Path) -> list[tuple[str, Path, Path, tuple[str, ...] | None]]:
    """一个镜像 → 若干 (面名, 权威目录, 副本目录, 名单或 None=整树)。"""
    names = declared_files()
    return [(f'{SRC_FACE}/**', base / SRC_FACE, mirror_root / SRC_FACE, None),
            ('根级交付件+md载体', base, mirror_root, names)]


# ══════════════ EXTRA 出口（第 43 轮 M115）══════════════
# 第 37 轮把"EXTRA 只报不删"写成判据，第 39 轮登记了缺口：没有任何出口承接"人已看过、确认删这一个"。
# 本段把那三段范式平移到同步面：凭据绑**副本字节**、先留证再动作、动作后断"只有那一个消失、其余一字未变"。
EXTRA_SCHEMA = 'ican-sync-extra-v1'
EXTRA_LEDGER_NAME = 'SYNC-EXTRA-DISPOSITION.jsonl'
EXTRA_ARCHIVE_DIR_NAME = 'SYNC-EXTRA-ARCHIVE'
EXTRA_MAX_ENTRIES = 200
EXTRA_MIN_REASON_CHARS = 20
TOKEN_HEX_LEN = 16


class ExtraError(RuntimeError):
    """拒因：宁可停下点名，也不静默删掉一个可能只是"漏同步"的文件。"""


def _append_row(path: str, entry: dict) -> None:
    """本地 append-only 台账写入器。

    注：这与 `restore_db.append_jsonl` 是两份实现（形状相同）——本轮刻意不把同步工具耦合到 DB 备份模块，
    但重复本身就是漂移风险，已登记为在册待办（合并到一处再改）。
    """
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'a', encoding='utf-8', newline='\n') as f:
        f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + '\n')
        f.flush()
        os.fsync(f.fileno())


def _cap_outcome(ln: str) -> str:
    try:
        return str(json.loads(ln).get('outcome'))
    except ValueError:
        return 'unparsable'


def _cap_rows(path: str, limit: int = EXTRA_MAX_ENTRIES) -> dict:
    """封顶只削**可再生成的部分**（第 45 轮 M143，第 46 轮把名单式保护改成排除式）。

    上一版的方向正好相反：实测 210 行（intent/终态交替）削掉 57 行**全是终态证据**，
    而中间行一条没动 —— 终态行才是"人看过、确认删这一个"的唯一载体。
    第 45 轮的写法是 `k in ('dropped', 'failed', 'unparsable')`：**白名单式**保护，
    本轮新增 `lock-broken` / `archive-pruned` 时若忘了扩展它，封顶就会悄悄削掉新证据
    （同一缺陷的第二次发作，只是换了个 outcome）。现规则改为**排除式**：
    `intent` 是唯一可削的，其余（含读不懂的、None、将来任何新终态）一律是证据；
    终态本身超上限 ⇒ **不删证据**，如实回报 overflow（宁可文件变大，不可证据消失）。
    重写台账走"同目录临时件 + O_BINARY + fsync + os.replace"，不 open(path,'w') 直写。
    """
    empty = {'dropped': 0, 'terminal': 0, 'overflow': False}
    if not os.path.isfile(path):
        return empty
    with open(path, encoding='utf-8', newline='') as f:
        rows = [ln for ln in f.read().splitlines() if ln.strip()]
    kinds = [_cap_outcome(ln) for ln in rows]
    term_idx = [i for i, k in enumerate(kinds) if k != 'intent']
    intent_idx = [i for i, k in enumerate(kinds) if k == 'intent']
    keep_intent = max(0, min(len(intent_idx), limit - len(term_idx)))
    keep = set(term_idx) | set(intent_idx[len(intent_idx) - keep_intent:]) if keep_intent else set(term_idx)
    kept = [rows[i] for i in sorted(keep)]
    dropped = len(rows) - len(kept)
    if dropped:
        tmp = path + '.cap.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_BINARY', 0))
        try:
            os.write(fd, ('\n'.join(kept) + '\n').encode('utf-8'))
            os.fsync(fd)
        finally:
            os.close(fd)
        back = open(tmp, encoding='utf-8').read()
        if len([x for x in back.splitlines() if x.strip()]) != len(kept):
            raise ExtraError(f'封顶重写台账回读行数不符（应 {len(kept)}）⇒ 停在封顶前，不继续动盘')
        os.replace(tmp, path)
    return {'dropped': dropped, 'terminal': len(term_idx), 'overflow': len(term_idx) > limit}


def extra_ledger_path() -> str:
    """留证必须落在**两份镜像之外**（对齐"证据不得记在被处置对象名下"）。"""
    p = Path(os.path.abspath(str(DEV / EXTRA_LEDGER_NAME)))
    for mname, mroot in MIRROR_DIRS.items():
        if p.is_relative_to(Path(os.path.abspath(str(mroot)))):
            raise ExtraError(f'台账落点 {p} 在镜像「{mname}」内 ⇒ 会被自己的处置面吞掉，拒')
    return str(p)


SYNC_LOCK_NAME = 'SYNC-WRITE.lock'
SYNC_HOLDER_NAME = 'SYNC-WRITE.holder'
SYNC_LOCK_WAIT_S = 10.0
# 心跳间隔必须由**被它保护的那把尺的节奏**推导，不能拍脑袋（对标 restic
# `internal/repository/lock.go:34 defaultRefreshInterval = 5 * time.Minute` 与 `:42`
# `refreshabilityTimeout = staleLockTimeout - defaultRefreshInterval*3/2`：它把"刷新失败"
# 判在锁真正变陈旧**之前**，留出的差值用来补偿客户端间的时钟漂移）。
# 本仓的 staleLockTimeout 同位量是 `LOCK_BREAK_MIN_AGE_S`（120s），故心跳取 120/4=30s，
# 留三次漏拍余量；`lock_timing_consistent()` 把这条推导变成机器判据。
SYNC_LOCK_REFRESH_S = 30.0


class SyncLockError(ExtraError):
    """写态取不到锁 ⇒ 一个字节都不动（仍是 ExtraError 家族，CLI 侧退 2，不静默降级）。"""


def lock_timing_consistent(refresh_s: float = SYNC_LOCK_REFRESH_S,
                           min_age_s: float | None = None) -> dict:
    """心跳与残件阈值必须同源：刷新间隔若不小于残件阈值，"锁龄"这把尺就自相矛盾。

    写这个函数而不是把关系写成注释，是因为注释不会红：把 `SYNC_LOCK_REFRESH_S` 调到 999
    之后，长跑写态照样会在还活着的时候被判成残件，而全仓没有任何判据会拦。
    """
    floor = LOCK_BREAK_MIN_AGE_S if min_age_s is None else float(min_age_s)
    ok = refresh_s > 0 and refresh_s * 2 < floor
    return {'refresh_s': refresh_s, 'min_age_s': floor, 'ok': ok,
            'margin': round(floor - refresh_s, 3),
            'detail': (f'心跳 {refresh_s}s 的两倍 < 残件阈值 {floor}s ⇒ 允许连续漏拍两次仍不被误判'
                       if ok else
                       f'心跳 {refresh_s}s 与残件阈值 {floor}s 不同源（须满足 0<refresh×2<min_age）')}


def sync_lock_path(root: Path | None = None) -> str:
    """锁文件必须落在**两份镜像之外**：锁在被同步的面上，等于锁会被自己的复制/删除搬走。"""
    base = Path(os.path.abspath(str(root if root is not None else DEV)))
    p = base / SYNC_LOCK_NAME
    for mname, mroot in MIRROR_DIRS.items():
        if p.is_relative_to(Path(os.path.abspath(str(mroot)))):
            raise SyncLockError(f'锁文件 {p} 落在镜像「{mname}」内 ⇒ 同步动作会把它一起搬走，拒')
    return str(p)


def read_lock_holder(lock_path: str) -> str:
    """取锁失败时点名持锁者；读不到就如实说读不到（不得冒充"没人拿"）。"""
    try:
        doc = json.loads(open(Path(lock_path).with_name(SYNC_HOLDER_NAME),
                              encoding='utf-8').read())
        return f"pid={doc.get('pid')} action={doc.get('action')} ts={doc.get('ts')}"
    except (OSError, ValueError):
        return 'pid=? action=? ts=?（归因侧件读不到 ⇒ 只知有人在写）'


def _utc_now_iso() -> str:
    # 不用 datetime.UTC：PA 免费层解释器不保证 3.11+，UP017 的建议写法在本仓不可移植
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')  # noqa: UP017


class SyncLease:
    """写态租约：把「锁龄」从"我什么时候拿的锁"改成"我上一次证明自己还活着是什么时候"。

    对标 restic `internal/repository/lock.go`：`:107` 起一个 `refreshLocks` goroutine 每
    `defaultRefreshInterval` 重写一次锁（`:173`），刷新失败只记一行日志（`:175`），而
    `:258-263 tryRefreshStaleLock` 在**补救也失败**时直接 `cancel()` 自己的上下文——
    原文注释 `:261` "cancel context ... to prevent accidental modifications"。
    本仓借的是那半边语义：**证明不了自己还持有，就停手**，而不是"照样写完再说"。
    差别也如实记下：restic 判不出持锁进程死活（仓库可在任何机器）所以只能靠刷新；
    本仓锁文件就在本机、pid 判得出 ⇒ 心跳是**第二**把尺，不是唯一一把（见 `lock_state()`）。
    """

    def __init__(self, lock_path: Path, holder_path: Path, action: str,
                 refresh_s: float = SYNC_LOCK_REFRESH_S):
        self.lock_path, self.holder_path = lock_path, holder_path
        self.action, self.refresh_s = action, refresh_s
        self.beats = 0
        self.last_beat = 0.0
        self._tick = 0
        self.created_ts = _utc_now_iso()
        self.dead: str | None = None

    def _doc(self, ts: str) -> dict:
        # `ts` 保持"取锁时刻"的原语义（read_lock_holder/破锁台账都按它归因），
        # 心跳另记 beat_ts：把两者混成一个键会让"锁拿了多久"这个读数悄悄变成"上次跳是几时"。
        return {'pid': os.getpid(), 'action': self.action, 'ts': self.created_ts,
                'beat_ts': ts, 'beats': self.beats}

    def renew(self, force: bool = False) -> dict:
        """刷新心跳：重写归因侧件（含 beats 计数）+ 顶新锁文件 mtime。

        任一步失败 ⇒ 记下死因并抛 `SyncLockError`（调用方是写态循环，抛出去就是整轮停手）。
        锁文件被人删了也算失败：那说明"我持有这把锁"已经证明不了了。
        """
        if self.dead is not None:
            raise SyncLockError(f'租约已失效（{self.dead}）⇒ 写态自停，一个字节都不再动')
        now = time.time()
        if not force and (now - self.last_beat) < self.refresh_s:
            return {'skipped': True, 'beats': self.beats, 'age_since_beat_s': round(now - self.last_beat, 3)}
        if not self.lock_path.exists():
            self.dead = f'锁文件 {self.lock_path.name} 已不在 ⇒ 持锁这件事证明不了'
            raise SyncLockError(f'心跳失败：{self.dead} ⇒ 写态自停（不降级成"没心跳也照写"）')
        ts = _utc_now_iso()
        self.beats += 1
        tmp = self.holder_path.with_name(self.holder_path.name + '.beat')
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_BINARY', 0))
            try:
                os.write(fd, json.dumps(self._doc(ts), ensure_ascii=False).encode('utf-8'))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self.holder_path)
            os.utime(self.lock_path, None)
        except OSError as exc:
            self.dead = f'{type(exc).__name__}: {exc}'
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise SyncLockError(f'心跳刷新失败（{self.dead}）⇒ 写态自停，本轮不再动盘') from exc
        self.last_beat = now
        return {'skipped': False, 'beats': self.beats, 'beat_ts': ts}

    def beat(self, n: int = 1) -> dict:
        """写态循环里逐件调用：累计满 n 次才真刷新一次（心跳是"我还活着"的证据，不必每件都落盘）。"""
        self._tick = getattr(self, '_tick', 0) + 1
        if self._tick % max(1, int(n)):
            return {'skipped': True, 'beats': self.beats}
        return self.renew()

    def close(self) -> None:
        try:
            self.holder_path.unlink()
        except OSError:
            pass


@contextlib.contextmanager
def sync_lock(action: str, root: Path | None = None, wait_s: float = SYNC_LOCK_WAIT_S,
              refresh_s: float = SYNC_LOCK_REFRESH_S):
    """唯一写态锁：`--fix` 与 `--drop-extra` 都必须在它里面动盘；只读档不得取它（不得排队）。

    形状借 restic `internal/repository/lock.go` 的 `Lock()`（锁的是"我正在这个面写"这件事本身，
    锁内容是 pid+时刻的归因侧件）；第 47 轮 M149 起另外 yield 一个 `SyncLease` 心跳租约，
    让"锁龄"变成"距上次心跳"（restic 的 `refreshLocks` 那半边；restic 另有 stale 超时破锁通道，
    本通道的破锁仍归处置侧 `break_lock`，这里不越权）。
    """
    try:
        from filelock import FileLock, Timeout
    except ImportError as exc:
        raise SyncLockError(f'filelock 不可用（{type(exc).__name__}: {exc}）⇒ 写态一律拒，'
                            '不许降级成"没锁也照写"') from exc
    path = Path(sync_lock_path(root))
    holder = path.with_name(SYNC_HOLDER_NAME)
    lk = FileLock(str(path), timeout=wait_s)
    try:
        lk.acquire()
    except Timeout:
        raise SyncLockError(f'写态锁被占用：{read_lock_holder(str(path))} ⇒ 本轮一个字节都不动'
                            '（只读档——不加 --fix 的比对与 --list-extra——不取锁，不必排队）') from None
    lease = SyncLease(path, holder, action, refresh_s)
    try:
        lease.renew(force=True)
        yield lease
    finally:
        lease.close()
        lk.release()



# ══════════════ 锁的残件处置出口（第 46 轮 M145）══════════════
# 对标 `borgbackup/borg` `src/borg/archiver/lock_cmds.py:34 do_break_lock`（docstring 原文
# "Breaks the repository lock (for example, if it was left by a dead Borg process)"）与
# `restic/restic` `cmd/restic/cmd_unlock.go:21`（"removes **stale** locks"）/ `:47`
# （`--remove-all` = "remove all locks, even non-stale ones"）/ `:57-60`（默认走
# `RemoveStaleLocks`，加了 flag 才走 `RemoveAllLocks`）/ `:68`（打印 removed 计数）。
# **不照抄 restic 的 escalation**：restic 的仓库可在远端、判不出持锁进程死活，所以它把
# "连活锁一起清"留给人显式勾选；本仓的锁文件就在本机、pid 判得出 ⇒ 活锁永远拒，
# 少一个能毁掉别人在途写的开关。破锁**天然不能取那把锁**（它正被一个死进程占着），
# 并发保护由「pid 已死 ∧ 锁龄到点 ∧ 凭据绑 holder 当前字节」三条承担，
# 动作后再断"SYNC-WRITE.* 一个不剩"。
LOCK_SCHEMA = 'ican-sync-lock-v1'
LOCK_STATES = ('absent', 'held-living', 'stale-residue', 'unattributable', 'too-young')
LOCK_BREAK_MIN_AGE_S = 120.0
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


def _pid_alive(pid: int) -> bool | None:
    """持锁进程是否还活着：True 活 / False 死或不存在的 pid / None 判不了（调用方一律 fail-closed）。

    Windows **不能**用 `os.kill(pid, 0)` 探活：CPython 在 Windows 上对非 CTRL_* 信号走
    OpenProcess + TerminateProcess，`0` 会被当成退出码 ⇒ "探活"变成"把持锁的人杀掉"。
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if sys.platform.startswith('win'):
        try:
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
        except (AttributeError, ValueError, OSError):
            return None
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def validate_holder(doc: object) -> list[str]:
    """归因侧件的字段级契约（与 `validate_extra_item` 同一取向：坏字段领不到凭据）。"""
    bad: list[str] = []
    if not isinstance(doc, dict):
        return [f'holder 不是对象：{type(doc).__name__}']
    pid = doc.get('pid')
    if not (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0):
        bad.append(f'pid 不是正整数：{pid!r}')
    if not (isinstance(doc.get('action'), str) and doc.get('action')):
        bad.append(f'action 不是非空字符串：{doc.get("action")!r}')
    if not (isinstance(doc.get('ts'), str) and len(doc.get('ts')) >= 8):
        bad.append(f'ts 缺失或过短：{doc.get("ts")!r}')
    # 心跳两键**有就必须合法**（M149）：不校验会让"beats 是个字符串"这种坏侧件
    # 一路走到 lock_state 的读数里；但两键**可以没有**——老侧件与手工造的残件本就没有。
    if 'beat_ts' in doc and not (isinstance(doc['beat_ts'], str) and len(doc['beat_ts']) >= 8):
        bad.append(f'beat_ts 存在但不合法：{doc["beat_ts"]!r}')
    if 'beats' in doc and (isinstance(doc['beats'], bool)
                           or not isinstance(doc['beats'], int) or doc['beats'] < 0):
        bad.append(f'beats 存在但不是非负整数：{doc["beats"]!r}')
    return bad


def lock_state(root: Path | None = None,
               min_age_s: float = LOCK_BREAK_MIN_AGE_S) -> dict:
    """只读的锁状态判定（五态不折叠）；只有 `stale-residue` 才发得出破锁凭据。"""
    path = Path(sync_lock_path(root))
    holder = path.with_name(SYNC_HOLDER_NAME)
    out = {'schema': LOCK_SCHEMA, 'state': 'absent', 'lock': path.as_posix(),
           'holder': holder.as_posix(), 'age_s': None, 'pid': None, 'action': None,
           'ts': None, 'holder_sha': None, 'breakable': False, 'min_age_s': min_age_s,
           'beat_ts': None, 'beats': None, 'acquire_age_s': None,
           'age_source': 'lock_mtime(heartbeat)'
                         '（M149 起：持锁者每 SYNC_LOCK_REFRESH_S 顶一次 mtime ⇒ age 是"距上次心跳"，'
                         '不再只是"距取锁"；无心跳的老残件仍按取锁时刻计）',
           'timing': lock_timing_consistent(),
           'detail': '锁文件不在 ⇒ 没有锁可破（破锁出口对这一态必须拒，不得判"清理完成"）'}
    if not path.exists():
        return out
    out['age_s'] = round(max(0.0, time.time() - path.stat().st_mtime), 3)
    if not holder.exists():
        out.update(state='unattributable',
                   detail='锁在、归因侧件不在 ⇒ 不知是谁持有，破它等于蒙眼动别人的写态')
        return out
    try:
        raw = holder.read_bytes()
        doc = json.loads(raw.decode('utf-8'))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        out.update(state='unattributable',
                   detail=f'归因侧件读不进（{type(exc).__name__}）⇒ 无法归因，不破')
        return out
    bad = validate_holder(doc)
    if bad:
        out.update(state='unattributable', detail='归因侧件字段不合规：' + '；'.join(bad))
        return out
    out['holder_sha'] = hashlib.sha256(raw).hexdigest()
    out['pid'], out['action'], out['ts'] = doc['pid'], doc['action'], doc['ts']
    out['beat_ts'], out['beats'] = doc.get('beat_ts'), doc.get('beats')
    # 「取了锁多久」与「上次心跳是几时」是两个读数，必须同时给：只给后者会让一次
    # 跑了 3 小时的 --fix 在报告里看起来像刚开始的（M149 的读数面）。解析失败退 None，
    # 不让一个可选读数有能力把整条判据带崩。
    try:
        born = datetime.datetime.fromisoformat(str(doc['ts']))
        out['acquire_age_s'] = round(
            max(0.0, (datetime.datetime.now(born.tzinfo) - born).total_seconds()), 3)
    except (TypeError, ValueError):
        out['acquire_age_s'] = None
    alive = _pid_alive(int(doc['pid']))
    if alive is None:
        out.update(state='unattributable',
                   detail=f'pid={doc["pid"]} 的活性判不了 ⇒ 宁可判"不知道能不能破"，不猜')
        return out
    if alive:
        out.update(state='held-living',
                   detail=f'pid={doc["pid"]} 仍活着（action={doc["action"]}，'
                          f'取锁已 {out["acquire_age_s"]}s、上次心跳 {out["age_s"]}s 前、'
                          f'beats={out["beats"]}）⇒ 这是活锁不是残件')
        return out
    if out['age_s'] < min_age_s:
        out.update(state='too-young',
                   detail=f'持锁进程已死但**距上次心跳** {out["age_s"]}s < {min_age_s}s ⇒ '
                          '可能刚被正常回收，先等一会儿再判残件'
                          f'（取锁已 {out["acquire_age_s"]}s：长跑写态靠心跳续命，'
                          '不靠"取锁时刻"撑龄）')
        return out
    out.update(state='stale-residue', breakable=True,
               detail=f'pid={doc["pid"]} 已不存在且距上次心跳 {out["age_s"]}s ⇒ 残件，可破')
    return out


def lock_break_token(st: dict) -> str:
    """破锁凭据绑 **holder 文件当前字节**，不绑字段值：侧件被改一个字节 ⇒ token 自然失效。"""
    if not st.get('breakable') or not st.get('holder_sha'):
        raise SyncLockError(f'状态 {st.get("state")} 不可破 ⇒ 不发凭据（凭据只能发给残件）')
    body = json.dumps({'schema': LOCK_SCHEMA, 'lock': st['lock'], 'holder': st['holder'],
                       'holder_sha': st['holder_sha']}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode('utf-8')).hexdigest()[:TOKEN_HEX_LEN]


def archive_lock_file(target: Path, kind: str) -> tuple[str, str]:
    """破锁前先把锁与侧件的字节归档到镜像外（沿用第 45 轮"删除前先归档"范式）。"""
    data = target.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    name = f'{kind}-{sha[:12]}-{target.name}'
    # 归档面前置对账（M151）：破锁要写两份字节，人口尺先量；越界 ⇒ **锁原地不动**。
    problem = archive_cap_problem(len(data))
    if problem:
        raise SyncLockError(f'{problem} ⇒ 本次不破锁，锁与侧件都留在原地')
    adir = extra_archive_dir(create=True)
    dst, tmp = adir / name, adir / (name + '.part')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_BINARY', 0))
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, dst)
    back = hashlib.sha256(dst.read_bytes()).hexdigest()
    if back != sha:
        raise ExtraError(f'归档件回读不一致（{back[:12]}… ≠ {sha[:12]}…）⇒ 原件不动')
    return dst.as_posix(), sha


def break_lock(confirm: str | None = None, reason: str | None = None,
               root: Path | None = None,
               min_age_s: float = LOCK_BREAK_MIN_AGE_S) -> dict:
    """唯一破锁出口：只破**判得清归属的死锁残件**，破之前先归档两份字节。

    五道拒因：① 没有锁（不得把"无可破"写成通过）；② 归属判不了（侧件缺/坏/活性未知）；
    ③ 活锁；④ 锁龄未到；⑤ 凭据与 holder 当前字节不符或理由不足 20 字。
    """
    st = lock_state(root, min_age_s)
    if st['state'] == 'absent':
        raise SyncLockError(f'没有锁可破（{st["lock"]} 不存在）⇒ {st["detail"]}')
    if st['state'] == 'held-living':
        raise SyncLockError('活锁拒破：' + st['detail'] +
                            ' ⇒ 正解是等它写完，不是替它结束（restic 的 --remove-all 在这里不开：'
                            '本仓判得出死活，就不该给一个能毁在途写的开关）')
    if st['state'] in ('unattributable', 'too-young'):
        raise SyncLockError(f'拒破（{st["state"]}）：{st["detail"]}')
    if confirm is None:
        raise SyncLockError('缺 --confirm：破锁没有默认动作，非交互环境也不隐式降级')
    if not reason or len(str(reason).strip()) < EXTRA_MIN_REASON_CHARS:
        raise SyncLockError(f'理由不足 {EXTRA_MIN_REASON_CHARS} 字 ⇒ 一句话不算复核')
    want = lock_break_token(st)
    if str(confirm) != want:
        raise SyncLockError(f'confirm 与现算 token 不符（期望 {want}）⇒ '
                            '未复核，或侧件自出凭据后又被改过')
    lock_p, holder_p = Path(st['lock']), Path(st['holder'])
    face = sorted(p.name for p in lock_p.parent.glob('SYNC-WRITE.*'))
    ledger = extra_ledger_path()
    base = {'schema': LOCK_SCHEMA, 'kind': 'sync-lock', 'state': st['state'],
            'lock': st['lock'], 'holder': st['holder'], 'pid': st['pid'],
            'action': st['action'], 'ts': st['ts'], 'age_s': st['age_s'],
            'holder_sha': st['holder_sha'], 'face_before': face,
            'token': str(confirm), 'reason': str(reason).strip()}
    _append_row(ledger, dict(base, outcome='intent'))
    try:
        a_lock, s_lock = archive_lock_file(lock_p, 'lock')
        a_holder, s_holder = archive_lock_file(holder_p, 'holder')
    except (OSError, ExtraError) as exc:
        _append_row(ledger, dict(base, outcome='lock-break-failed', error=f'归档失败:{type(exc).__name__}',
                                 archive_path=None, sidecar_archive_path=None))
        _cap_rows(ledger)
        raise SyncLockError(f'归档失败 ⇒ 锁与侧件一个字节没动：{exc}') from exc
    errs = []
    for stale_path in (holder_p, lock_p):
        try:
            stale_path.unlink()
        except OSError as exc:
            errs.append(f'{stale_path.name}:{type(exc).__name__}')
    after = sorted(p.name for p in lock_p.parent.glob('SYNC-WRITE.*'))
    ok = not errs and not after
    _append_row(ledger, dict(base, outcome='lock-broken' if ok else 'lock-break-failed',
                             error='；'.join(errs) or None, archive_path=a_lock,
                             archive_sha=s_lock, sidecar_archive_path=a_holder,
                             sidecar_archive_sha=s_holder, face_after=after))
    _cap_rows(ledger)
    if not ok:
        raise SyncLockError(f'破锁未完成（{errs}，剩余={after}）⇒ 已如实落账，不静默吞：{ledger}')
    return {'broken': 2, 'pid': st['pid'], 'action': st['action'], 'age_s': st['age_s'],
            'archive_path': a_lock, 'sidecar_archive_path': a_holder,
            'face_before': face, 'face_after': after, 'ledger': ledger}


def git_state(abs_path: str) -> str:
    """该件在版本控制里的身份：tracked / untracked / not-a-repo / unknown。

    这是本通道的**第一道**拒因依据：`tracked` 意味着"权威源里本该有它而没了"=漏同步，
    删它就等于毁掉别人未提交的在途件；只有 `untracked` 才是真孤儿。取不到身份一律 fail-closed。
    """
    try:
        # 件不在仓根之下（面配错、镜像被指到别处）⇒ 这是**第四种**身份，不能靠异常冒出来：
        # 原型实测 ValueError 会一路冲到用户面前，而正确答案是"我不知道它是不是孤儿 ⇒ 拒删"。
        rel = Path(os.path.abspath(abs_path)).relative_to(
            Path(os.path.abspath(str(REPO)))).as_posix()
    except ValueError:
        return 'outside-repo'
    try:
        r = subprocess.run(['git', '-C', str(REPO), 'ls-files', '--error-unmatch', '--', rel],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return 'unknown'
    if r.returncode == 0:
        return 'tracked'
    if 'not a git repository' in (r.stderr or '').lower():
        return 'not-a-repo'
    return 'untracked'


def scan_extra(mirrors: dict | None = None) -> list[dict]:
    """现扫的 EXTRA 清单（不缓存：凭据必须绑"当下这份字节"）。"""
    rows: list[dict] = []
    for mname, mroot in sorted((mirrors if mirrors is not None else MIRROR_DIRS).items()):
        for label, src_dir, dst_dir, names in build_faces(DEV, mroot):
            src_map = scan_tree(src_dir) if names is None else scan_files(src_dir, names)
            dst_map = scan_tree(dst_dir) if names is None else scan_files(dst_dir, names)
            for rel in classify(src_map, dst_map)[2]:
                p = dst_dir / rel
                q = src_dir / rel          # 同一面内的"应然源件"位置（src/** 面与根面口径不同）
                rows.append({'schema': EXTRA_SCHEMA, 'mirror': mname, 'face': label, 'rel': rel,
                             'bytes': p.stat().st_size, 'sha256': sha256(p),
                             'git': git_state(str(p)),
                             'path': Path(os.path.abspath(str(p))).as_posix(),
                             'src': Path(os.path.abspath(str(q))).as_posix(),
                             'src_absent': not q.exists()})
    return rows


EXTRA_GIT_STATES = frozenset({'tracked', 'untracked', 'not-a-repo', 'outside-repo', 'unknown'})


def validate_extra_item(item: dict) -> dict:
    """EXTRA 项的字段级契约（形状借 mutagen `pkg/synchronization/core/conflict.go:11 EnsureValid`）。

    凭据只能发给"形状合法且可复算"的件：字段坏了就不出 token —— 否则 `drop_extra` 会拿着
    一份没有 sha 的清单去 unlink，事后连"删的是什么字节"都复不出来。
    """
    bad = []
    if item.get('schema') != EXTRA_SCHEMA:
        bad.append(f'schema={item.get("schema")!r}≠{EXTRA_SCHEMA!r}')
    sha = item.get('sha256')
    if not (isinstance(sha, str) and len(sha) == 64 and all(c in '0123456789abcdef' for c in sha)):
        bad.append('sha256 不是 64 位小写十六进制')
    n = item.get('bytes')
    if not (isinstance(n, int) and not isinstance(n, bool) and n >= 0):
        bad.append('bytes 不是非负整数')
    if item.get('git') not in EXTRA_GIT_STATES:
        bad.append(f'git 身份不在名册：{item.get("git")!r}')
    rel = item.get('rel')
    if not (isinstance(rel, str) and rel) or '..' in Path(str(rel)).parts or Path(str(rel)).is_absolute():
        bad.append(f'rel 不是干净的相对名：{rel!r}')
    if not isinstance(item.get('src_absent'), bool):
        bad.append('src_absent 必须是布尔（缺了就没法判断它到底是不是孤儿）')
    if bad:
        raise ExtraError(f'EXTRA 项字段不合规（{len(bad)} 项）：{"；".join(bad)} ⇒ 不发凭据、不处置')
    return item


def extra_archive_dir(create: bool = False) -> Path:
    """归档落点同样必须在两份镜像之外；默认**不建目录**（只读档不该动盘）。"""
    p = Path(os.path.abspath(str(DEV / EXTRA_ARCHIVE_DIR_NAME)))
    for mname, mroot in MIRROR_DIRS.items():
        if p.is_relative_to(Path(os.path.abspath(str(mroot)))):
            raise ExtraError(f'归档落点 {p} 在镜像「{mname}」内 ⇒ 会被同步动作再把它复制回来，拒')
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def archive_extra(target: Path, item: dict, mirror: str) -> tuple[str, str]:
    """删除前把字节搬到镜像外并回读复算 sha（对标 rclone `fs/sync/sync.go:608 DeleteFilesWithBackupDir`）。

    任何一步失败都**不 unlink** ⇒ 复核错了还查得到原件；这一步是给"误删"留的最后一道退路。
    """
    data = target.read_bytes()
    live = hashlib.sha256(data).hexdigest()
    if live != item['sha256']:
        raise ExtraError(f'现场字节与出凭据时不一致（{live[:12]}… ≠ {item["sha256"][:12]}…）⇒ 不删')
    # 两把尺（M151）量在**连目录都不建**之前：越界即整笔拒 ⇒ 原件与文件系统状态都不动，
    # 而不是"删了原件、归档又写不下"。
    problem = archive_cap_problem(len(data))
    if problem:
        raise ExtraError(f'{problem} ⇒ 原件不动、不删')
    adir = extra_archive_dir(create=True)
    name = f'{mirror}-{item["sha256"][:12]}-{Path(item["rel"]).name}'
    dst, tmp = adir / name, adir / (name + '.part')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_BINARY', 0))
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, dst)
    back = hashlib.sha256(dst.read_bytes()).hexdigest()
    if back != item['sha256']:
        raise ExtraError(f'归档件回读不一致（{back[:12]}…）⇒ 原件不动')
    return dst.as_posix(), back


ARCHIVE_TTL_DEFAULT_S = 30 * 24 * 3600.0
ARCHIVE_REF_OUTCOMES = ('dropped', 'lock-broken')
ARCHIVE_SCHEMA = 'ican-sync-archive-v1'
# 台账里"归档件路径 ⇄ 该件字节"的成对字段（第 46 轮 M144）。判据第 52 项按**双向差集**守它：
# 终态行新增一个归档字段而这个名册没扩 ⇒ 判红（新字段没人核字节，等于台账写了个查不到的承诺）。
ARCHIVE_SHA_PAIRS = (('archive_path', 'archive_sha'),
                     ('sidecar_archive_path', 'sidecar_archive_sha'))
ARCHIVE_SHA_VERIFY_MAX = 200
# ── 归档面的两把尺（第 47 轮 M151）───────────────────────────────
# 第 45 轮给 EXTRA 面立了人口尺 `EXTRA_MAX_ENTRIES`（台账行封顶）、第 46 轮立了 30 天 TTL，
# 但**归档目录自身从来没有上界**：它只按件龄清，人口与字节总量无人量过 ⇒ 处置频率一高
# 就是无界增长，而磁盘是那个唯一会被牺牲掉的读数。两把尺都必须由**结构量**推导：
#   · 人口尺 = `EXTRA_MAX_ENTRIES`：一行终态台账最多指向一对归档引用（`ARCHIVE_SHA_PAIRS`
#     成对登记），台账既封顶 200 行，归档件一旦超过 200 件就必然有件查不到来历（孤儿）
#     ⇒ 超界不是"再多留点没事"，而是"账与件其中一侧坏了"。
ARCHIVE_MAX_FILES = EXTRA_MAX_ENTRIES
#   · 字节尺 = 人口尺 × 实测单件上界。归档件里唯一的大件来源是被判孤儿的前端镜像文件：
#     2026-09-29 当场实测 `deploy-free/src` 与 `05-源代码全文/src` 各 196 件 / 3,132,296B，
#     最大单件 618,956B（`src/static/js/echarts.custom.js`）、p95 35,303B、中位 4,723B
#     ⇒ 200 × 618,956 = 123,791,200B，向上取整到 128 MiB（余量 8.4%）。
ARCHIVE_SINGLE_MAX_MEASURED_BYTES = 618956
ARCHIVE_MAX_BYTES = 128 * 1024 * 1024
# TTL 与"发现节奏"的关系：保留窗口必须盖住**若干个最坏发现周期**，否则"删了还能捞回来"
# 只在运气好时成立。4 个周期是下限（一次发现 + 一次复核 + 一次重跑 + 一次留痕）。
ARCHIVE_TTL_MIN_CYCLES = 4.0
# 本项目镜像面**最坏**提交间隔的实测值（2026-09-29 取 `git log --since=90.days --
# deploy-free/src/static 交付物/.../05-源代码全文/src`：54 条提交跨 8.9 天，
# 中位 0.07 天、均值 0.17 天、**最大 1.87 天**）。它是历史观测，不是结构常数 ⇒
# 由 `archive_ttl_basis()` 以参数形式暴露，判据注入异值须能翻转结论。
ARCHIVE_DISCOVERY_MAX_GAP_DAYS = 1.87


def archive_ttl_basis(ttl_s: float = ARCHIVE_TTL_DEFAULT_S,
                      max_gap_days: float = ARCHIVE_DISCOVERY_MAX_GAP_DAYS) -> dict:
    """把"为什么是 30 天"从一句推演变成一条可复算的关系（第 47 轮 M151）。

    第 46 轮的原话是"30 天没有和快照重建周期/提交窗口对过账"。对账之后如实记下：
    本项目**没有**固定的快照重建周期（`01-演示快照集` 按交付节点手工重建，
    2026-09-29 实测该目录的相关提交为 0 条 ⇒ 拿它当分母会得出"从不"），
    所以窗口只钉得住"发现节奏"这一头：TTL ÷ 实测最坏提交间隔 ≥ 下限周期数才算够。
    """
    days = ttl_s / 86400.0
    cycles = round(days / max_gap_days, 2) if max_gap_days > 0 else None
    ok = cycles is not None and cycles >= ARCHIVE_TTL_MIN_CYCLES
    return {'schema': 'ican-archive-ttl-basis-v1', 'ttl_days': round(days, 2),
            'max_gap_days': max_gap_days, 'cycles': cycles,
            'min_cycles': ARCHIVE_TTL_MIN_CYCLES, 'ok': bool(ok),
            'detail': (f'TTL {days:g} 天 ÷ 实测最坏发现间隔 {max_gap_days} 天 = {cycles} 个周期'
                       f' ≥ 下限 {ARCHIVE_TTL_MIN_CYCLES:g} ⇒ 窗口与节奏对得上账'
                       if ok else
                       f'TTL {days:g} 天 ÷ 最坏发现间隔 {max_gap_days} 天 = {cycles}'
                       f' < 下限 {ARCHIVE_TTL_MIN_CYCLES:g} ⇒ 窗口短于发现节奏，'
                       '误删会在被看见之前就过期（拒：要么延长 TTL，要么承认捞不回来）')}


def archive_face_reading() -> dict:
    """只量不写：归档面当前人口/字节 + 两把尺的余量。落盘前调用，超限即由调用方拒。"""
    adir = extra_archive_dir()
    files = [p for p in adir.iterdir() if p.is_file()] if adir.is_dir() else []
    used = sum(p.stat().st_size for p in files)
    return {'files': len(files), 'bytes': used, 'dir_exists': adir.is_dir(),
            'max_files': ARCHIVE_MAX_FILES, 'max_bytes': ARCHIVE_MAX_BYTES,
            'headroom_files': ARCHIVE_MAX_FILES - len(files),
            'headroom_bytes': ARCHIVE_MAX_BYTES - used}


def archive_cap_problem(incoming_bytes: int) -> str | None:
    """新归档件落盘前的一道闸：**人口或字节**任一将越界即给出拒因（None = 放行）。

    拒的方向是刻意的：这一步失败 ⇒ 上游的删除动作也做不了（先归档才准删）。
    宁可"这一次删不成"，也不能"删了但没留字节"。
    """
    r = archive_face_reading()
    if r['files'] + 1 > ARCHIVE_MAX_FILES:
        return (f'归档面人口 {r["files"]} 件 + 本件 > 上限 {ARCHIVE_MAX_FILES} 件'
                f'（与台账行尺 EXTRA_MAX_ENTRIES 同源）⇒ 先 `--archive-status` 看哪件已过窗，'
                '按件清理后再来')
    if r['bytes'] + incoming_bytes > ARCHIVE_MAX_BYTES:
        return (f'归档面字节 {r["bytes"]}B + 本件 {incoming_bytes}B > 上限 {ARCHIVE_MAX_BYTES}B'
                f'（推导：{ARCHIVE_MAX_FILES} 件 × 实测单件上界 {ARCHIVE_SINGLE_MAX_MEASURED_BYTES}B）'
                ' ⇒ 同上，先清已过窗的件')
    return None


def _archive_path_of(row: dict, key: str) -> str | None:
    val = row.get(key)
    if isinstance(val, str) and EXTRA_ARCHIVE_DIR_NAME in val.replace('\\', '/'):
        return val.replace('\\', '/')
    return None


def _iter_strings(obj: object):
    """递归吐出容器里所有字符串值（含 dict 的键与值、list 的元素）。"""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                yield k
            yield from _iter_strings(v)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            yield from _iter_strings(v)


def _ledger_rows(ledger: str) -> list[dict]:
    """台账按行取数；读不懂的行返回占位（outcome=unparsable），**不静默跳过**。"""
    if not os.path.isfile(ledger):
        return []
    out = []
    with open(ledger, encoding='utf-8', newline='') as f:
        for ln in f.read().splitlines():
            if not ln.strip():
                continue
            try:
                doc = json.loads(ln)
            except ValueError:
                out.append({'outcome': 'unparsable'})
                continue
            out.append(doc if isinstance(doc, dict) else {'outcome': 'unparsable'})
    return out


def archive_inventory(ledger: str | None = None) -> dict:
    """归档面回读（第 46 轮 M148/M144）：**目录里的件 ⇄ 台账里的引用与字节** 三向对账。

    第 45 轮把"删除前先归档"接上之后，归档目录第一次有了"只写不读"的形态：
    悬空引用（台账指向的件没了）和孤儿件（件在目录里但没人记账）都看不见 ⇒ 本函数把它们量出来。
    M144 的另一半：台账写的 `archive_sha` **要拿去和归档件实算比对**，否则"删了还能捞回来"
    只是字符串上的承诺 —— 手改一个 hex 位，此前没有任何判据会红。
    取引用按**数据流**划（扫每行的全部字符串值），不写死字段名清单：
    字段名一旦列成名单，下一轮加第三个归档字段就会漏（⑬ 那条 `archive_path` 判据踩过同形）。
    但"路径 ⇄ 字节"这一对**必须**成对登记（`ARCHIVE_SHA_PAIRS`），门禁按双向差集守它。
    恒等式：`referenced + unfinalized + orphan_files + partial_writes == files`
    与 `terminal_refs == referenced + dangling_refs + pruned_explained`，
    对不上就是取数面坏了 ⇒ 判"没测到"，绝不把漏计当通过。
    """
    led = ledger or extra_ledger_path()
    adir = extra_archive_dir()
    files = sorted([p for p in adir.iterdir() if p.is_file()]) if adir.is_dir() else []
    present = {p.name: p for p in files}
    term_ref: set[str] = set()
    intent_ref: set[str] = set()
    pruned_ref: set[str] = set()
    want_sha: dict[str, str] = {}
    rows = _ledger_rows(led)
    for doc in rows:
        outcome = str(doc.get('outcome', 'unparsable'))
        hit = set()
        for val in _iter_strings(doc):
            if EXTRA_ARCHIVE_DIR_NAME in val.replace('\\', '/'):
                hit.add(Path(val.replace('\\', '/')).name)
        if not hit:
            continue
        if outcome in ARCHIVE_REF_OUTCOMES:
            term_ref |= hit
            for path_key, sha_key in ARCHIVE_SHA_PAIRS:
                pv, sv = _archive_path_of(doc, path_key), doc.get(sha_key)
                if pv and isinstance(sv, str) and len(sv) == 64:
                    want_sha[Path(pv).name] = sv
        elif outcome == 'intent':
            intent_ref |= hit
        elif outcome in ('archive-pruned',):
            pruned_ref |= hit
    referenced = {n for n in present if n in term_ref}
    unfinalized = {n for n in present if n not in term_ref and n in intent_ref
                   and not n.endswith('.part')}
    partial = {n for n in present if n.endswith('.part')}
    orphans = {n for n in present if n not in term_ref and n not in intent_ref
               and not n.endswith('.part')}
    dangling = {n for n in term_ref if n not in present and n not in pruned_ref}
    explained = {n for n in term_ref if n not in present and n in pruned_ref}
    checked, mismatch = 0, []
    for name in sorted(referenced)[:ARCHIVE_SHA_VERIFY_MAX]:
        if name not in want_sha:
            continue
        try:
            live = hashlib.sha256(present[name].read_bytes()).hexdigest()
        except OSError:
            mismatch.append(f'{name}:归档件读不出')
            continue
        checked += 1
        if live != want_sha[name]:
            mismatch.append(f'{name}:实算{live[:12]}≠账载{want_sha[name][:12]}')
    ages = [(time.time() - p.stat().st_mtime) for p in present.values()]
    # 两把尺的读数（M151）：人口与字节各自"用到几成"，以及**哪一把先 binding**。
    # 只报"件数/字节"不报余量 = 下一轮还是没人知道上限在哪；越界值单列，不并进 anomalies
    # 那批"逐件可指认"的异常（那是面级状态，不是某一件的问题）。
    n_files, n_bytes = len(present), sum(p.stat().st_size for p in present.values())
    file_ratio = n_files / ARCHIVE_MAX_FILES
    byte_ratio = n_bytes / ARCHIVE_MAX_BYTES
    expired = sorted((name for name, p in present.items()
                      if (time.time() - p.stat().st_mtime) > ARCHIVE_TTL_DEFAULT_S))
    # 只读**批量计划**（M150 的前置：任何批量清理档都得先有一份不动盘的清单）。
    # 这里给到逐件 token ⇒ 执行仍是一行一条 `--prune-archive NAME --confirm TOKEN`，
    # 本工具至今没有"一次清一批"的出口，将来加也必须建在这份计划的同一把尺上。
    plan = []
    for name in expired[:10]:
        p = present[name]
        try:
            size, digest = p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            continue
        plan.append({'name': name, 'bytes': size,
                     'age_s': round(time.time() - p.stat().st_mtime, 1),
                     'token': archive_token(name, digest)})
    doc = {'schema': ARCHIVE_SCHEMA, 'dir': adir.as_posix(), 'ledger': str(led),
           'ledger_rows': len(rows), 'files': n_files,
           'max_files': ARCHIVE_MAX_FILES, 'max_bytes': ARCHIVE_MAX_BYTES,
           'file_ratio': round(file_ratio, 4), 'byte_ratio': round(byte_ratio, 4),
           'over_file_cap': n_files > ARCHIVE_MAX_FILES,
           'over_byte_cap': n_bytes > ARCHIVE_MAX_BYTES,
           'binding_cap': ('files' if file_ratio >= byte_ratio else 'bytes'),
           'expired_over_ttl': len(expired), 'expired_names': expired[:6],
           'expired_plan': plan, 'expired_plan_shown': len(plan),
           'ttl_basis': archive_ttl_basis(),
           'referenced': len(referenced), 'unfinalized': len(unfinalized),
           'orphan_files': len(orphans), 'partial_writes': len(partial),
           'dangling_refs': len(dangling), 'pruned_explained': len(explained),
           'terminal_refs': len(term_ref),
           'sha_checked': checked, 'sha_mismatch': len(mismatch),
           'sha_unrecorded': len(referenced) - checked,
           'bytes_total': sum(p.stat().st_size for p in present.values()),
           'oldest_age_s': round(max(ages), 1) if ages else None,
           'ttl_s': ARCHIVE_TTL_DEFAULT_S,
           'anomalies': sorted(orphans) + sorted(dangling) + sorted(unfinalized)
                        + sorted(partial) + sorted(mismatch),
           'name_ledger': sorted(orphans)[:6]}
    doc['files_balanced'] = (doc['referenced'] + doc['unfinalized'] + doc['orphan_files']
                             + doc['partial_writes'] == doc['files'])
    doc['refs_balanced'] = (doc['referenced'] + doc['dangling_refs']
                            + doc['pruned_explained'] == doc['terminal_refs'])
    if not (doc['files_balanced'] and doc['refs_balanced']):
        raise ExtraError(f'归档面对账不平：files={doc["files"]} 分类合计='
                         f'{doc["referenced"] + doc["unfinalized"] + doc["orphan_files"] + doc["partial_writes"]}｜'
                         f'terminal_refs={doc["terminal_refs"]} 解释合计='
                         f'{doc["referenced"] + doc["dangling_refs"] + doc["pruned_explained"]}'
                         ' ⇒ 判"取数面坏"，不判"没有问题"')
    return doc


def archive_token(name: str, sha256_hex: str) -> str:
    """清理凭据绑**该归档件当前字节**：件被改过一个字节，旧 token 就失效。"""
    if not (isinstance(sha256_hex, str) and len(sha256_hex) == 64):
        raise ExtraError('sha 不是 64 位十六进制 ⇒ 不发凭据')
    body = json.dumps({'schema': ARCHIVE_SCHEMA, 'name': name, 'sha256': sha256_hex},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode('utf-8')).hexdigest()[:TOKEN_HEX_LEN]


def prune_archive(name: str, confirm: str | None = None, reason: str | None = None,
                  ttl_s: float = ARCHIVE_TTL_DEFAULT_S, ledger: str | None = None) -> dict:
    """归档件的唯一清理出口：**过了保留窗口、且台账里查得回来历**才允许清。

    六道拒因：① 名字带路径分隔（不许越出归档目录）；② 件不在（"没得清"不得写成通过）；
    ③ 龄期未到 TTL（点名还剩多少小时）；④ 台账里没有指向它的终态行 ⇒ 孤儿件，
    正解是先补账不是直接删（删了就查不到它从哪来）；⑤ 凭据与当前字节不符；⑥ 理由不足 20 字。
    清理**不**再套一层归档（那是无限递归）：证据由台账承担 —— 终态行留下 sha256 与
    `referenced_by`，字节没了但"曾经是什么"仍可对账，这正是 `dangling ⇄ pruned_explained` 的分界。
    """
    if '..' in Path(str(name)).parts or '/' in str(name) or '\\' in str(name):
        raise ExtraError(f'归档件名不接受路径：{name!r} ⇒ 清理面只在归档目录内，越界即拒')
    adir = extra_archive_dir()
    target = adir / name
    if not target.is_file():
        raise ExtraError(f'归档件不存在：{target} ⇒ 没有可清理的对象（这不判"已清理"）')
    led = ledger or extra_ledger_path()
    age = time.time() - target.stat().st_mtime
    if age < ttl_s:
        raise ExtraError(f'龄期 {round(age / 3600, 1)}h < 保留窗口 {round(ttl_s / 3600, 1)}h ⇒ '
                         '仍在可回退窗口内，拒清')
    inv = archive_inventory(led)
    refs = [d for d in _ledger_rows(led)
            if str(d.get('outcome')) in ARCHIVE_REF_OUTCOMES
            and name in {Path(v.replace('\\', '/')).name for v in _iter_strings(d)
                         if EXTRA_ARCHIVE_DIR_NAME in v.replace('\\', '/')}]
    if not refs:
        raise ExtraError(f'台账里没有指向 {name} 的终态行（孤儿件；当前 orphan='
                         f'{inv["orphan_files"]} dangling={inv["dangling_refs"]}）⇒ '
                         '来历查不到就先补账，本通道不替无声删除背书')
    data = target.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if confirm is None:
        raise ExtraError(f'缺 --confirm：该件当前凭据={archive_token(name, sha)}')
    if str(confirm) != archive_token(name, sha):
        raise ExtraError(f'confirm 与现算 token 不符（期望 {archive_token(name, sha)}）⇒ '
                         '未复核，或该件自出凭据后又被改过')
    if not reason or len(str(reason).strip()) < EXTRA_MIN_REASON_CHARS:
        raise ExtraError(f'理由不足 {EXTRA_MIN_REASON_CHARS} 字 ⇒ 一句话不算复核')
    base = {'schema': ARCHIVE_SCHEMA, 'kind': 'sync-archive', 'name': name,
            'path': target.as_posix(), 'sha256': sha, 'bytes': len(data),
            'age_s': round(age, 1), 'ttl_s': ttl_s,
            'referenced_by': [str(r.get('token')) for r in refs],
            'token': str(confirm), 'reason': str(reason).strip()}
    _append_row(led, dict(base, outcome='intent'))
    err = None
    try:
        target.unlink()
    except OSError as exc:
        err = f'{type(exc).__name__}: {exc}'
    _append_row(led, dict(base, outcome='archive-pruned' if err is None else 'archive-prune-failed',
                          error=err, still_present=target.exists()))
    _cap_rows(led)
    if err is not None:
        raise ExtraError(f'清理失败：{err}（已如实落账，不静默吞）')
    return {'pruned': 1, 'name': name, 'freed_bytes': len(data), 'sha256': sha,
            'age_s': round(age, 1), 'referenced_by': base['referenced_by'], 'ledger': led}


def extra_token(item: dict) -> str:
    validate_extra_item(item)
    body = json.dumps({k: item.get(k) for k in ('schema', 'mirror', 'face', 'rel', 'sha256',
                                                'bytes', 'git')},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode('utf-8')).hexdigest()[:TOKEN_HEX_LEN]


def list_extra(mirrors: dict | None = None) -> dict:
    items = scan_extra(mirrors)
    by = {}
    for it in items:
        by[it['git']] = by.get(it['git'], 0) + 1
    return {'schema': EXTRA_SCHEMA, 'count': len(items), 'by_git_state': by, 'items': items}


def drop_extra(mirror: str, rel: str, confirm: str | None = None,
               reason: str | None = None) -> dict:
    """唯一真删 EXTRA 的出口：一次一个，删的是**凭据绑定的那一份副本**。

    四道拒因：① 现扫必须是**恰好一条** EXTRA（0 条 = 不是孤儿或镜像名写错）；
    ② `git` 身份必须 `untracked` —— `tracked` 是漏同步（删它等于毁他人在途件），
    `unknown/not-a-repo` 取不到身份一律 fail-closed；③ `confirm` 逐字等于由**当前字节**复算的 token
    （副本被改过 ⇒ token 自然失效）；④ 理由 ≥ 20 字。动作后断"镜像里只少了这一个"。
    删除**之前**先把字节归档到镜像外（M140，对标 rclone `--backup-dir`）：归档失败即不删。
    """
    if mirror not in MIRROR_DIRS:
        raise ExtraError(f'镜像 {mirror!r} 不在册（在册：{sorted(MIRROR_DIRS)}）⇒ 不猜')
    if confirm is None:
        raise ExtraError('缺 --confirm：本通道没有"默认删"，非交互环境也不隐式降级')
    if not reason or len(str(reason).strip()) < EXTRA_MIN_REASON_CHARS:
        raise ExtraError(f'理由不足 {EXTRA_MIN_REASON_CHARS} 字 ⇒ 一句话不算复核')
    items = [x for x in scan_extra({mirror: MIRROR_DIRS[mirror]}) if x['rel'] == rel]
    if len(items) != 1:
        raise ExtraError(f'现扫 {mirror}/{rel} 命中 {len(items)} 条 EXTRA ⇒ '
                         '要么它不是孤儿，要么路径写错；本轮一个字节都不动')
    item = items[0]
    if item['git'] != 'untracked':
        why = {'tracked': '它在版本控制里是**已跟踪**件 ⇒ 这是漏同步不是孤儿，正解是补回权威源或走 git rm',
               'not-a-repo': '所在仓不是 git 仓库 ⇒ 无从判断它是否别人的在途件',
               'outside-repo': '该件不在仓根之下 ⇒ 面配置错了，先修面再来删',
               'unknown': 'git 调用取不到结果 ⇒ 判没测到'}
        raise ExtraError(f"{item['git']} ⇒ {why.get(item['git'], '身份不明即拒')}")
    if extra_token(item) != str(confirm):
        raise ExtraError(f'confirm 与现算 token 不符（期望 {extra_token(item)}）⇒ '
                         '未复核，或该副本自出凭据后又被改过')
    mroot = Path(os.path.abspath(str(MIRROR_DIRS[mirror])))
    target = Path(os.path.abspath(item['path']))
    if not target.is_relative_to(mroot):
        raise ExtraError(f'目标 {target} 不在镜像根 {mroot} 内 ⇒ 越界即拒')
    in_mirror = target.relative_to(mroot).as_posix()
    # 写态必须在锁内，**人口快照也在锁内取**：锁外数完再删，别人的动作会被记成本轮删的（第 44 轮 M141）。
    # 只读的现扫/token/身份判定留在锁外——排队等锁的是"要动手的人"，不是"来看一眼的人"。
    with sync_lock('drop-extra') as lease:
        # 心跳：动作真开始的那一刻再顶一次龄（M149）。取锁与删件之间隔着一次只读现扫，
        # 若这段被人拖长，"锁龄"没有心跳就会先越过残件阈值 —— 那时别的会话的
        # --lock-status 会把一个正在删件的锁判成可破。
        lease.beat()
        ledger = extra_ledger_path()
        before = sorted(p.relative_to(mroot).as_posix() for p in mroot.rglob('*') if p.is_file())
        if not item.get('src_absent'):
            raise ExtraError(f'现扫说源件仍在（{item.get("src")}）⇒ 它不是孤儿，本通道拒删')
        # 两套坐标分清：`rel`=面内相对名，人口清单=镜像根相对路径；混用会"删对了却判 failed"
        entry = {k: item[k] for k in ('schema', 'mirror', 'face', 'rel', 'sha256', 'bytes', 'git', 'src')
                 if k in item}
        entry.update(outcome='intent', in_mirror=in_mirror, token=str(confirm),
                     reason=str(reason).strip())
        _append_row(ledger, entry)
        expect = [x for x in before if x != in_mirror]
        # 先归档再删（M140）：归档失败 ⇒ 记 failed 终态行并冲出，unlink 根本没被执行到。
        try:
            arch_path, arch_sha = archive_extra(target, item, mirror)
        except OSError as exc:
            _append_row(ledger, dict(entry, outcome='failed', only_that_one=False,
                                     error=f'归档失败:{type(exc).__name__}', archive_path=None))
            _cap_rows(ledger)
            raise ExtraError(f'归档失败 ⇒ 原件一个字节没动：{type(exc).__name__}: {exc}') from exc
        err = None
        try:
            target.unlink()
        except OSError as exc:
            err = f'{type(exc).__name__}: {exc}'
        after = sorted(p.relative_to(mroot).as_posix() for p in mroot.rglob('*') if p.is_file())
        only_one = (after == expect)
        _append_row(ledger, dict(entry, outcome='dropped' if (err is None and only_one) else 'failed',
                                 error=err, only_that_one=only_one, archive_path=arch_path,
                                 archive_sha=arch_sha, population_before=len(before),
                                 population_after=len(after)))
        _cap_rows(ledger)
    if err is not None:
        raise ExtraError(f'删除失败：{err}（已如实落账，不静默吞）')
    if not only_one:
        raise ExtraError(f'删后镜像人口 != "只少那一个"（期望 {len(expect)} 实得 {len(after)}）'
                         f'⇒ 已记 failed，请人工核对：{ledger}')
    return {'mirror': mirror, 'rel': rel, 'removed': 1, 'freed_bytes': item['bytes'],
            'archive_path': arch_path,
            'population_before': len(before), 'population_after': len(after), 'ledger': ledger}


def print_extra(doc: dict) -> int:
    print(f'EXTRA 体检（只读，不发凭据也不删）：合计 {doc["count"]} 件｜按 git 身份 '
          f'{doc["by_git_state"] or "无"}')
    for it in doc['items'][:LIST_LIMIT]:
        print(f'   + [{it["mirror"]}] {it["rel"]}  {it["bytes"]}B sha={it["sha256"][:12]}… '
              f'git={it["git"]}（tracked ⇒ 是漏同步，本通道拒删）')
    if doc['count'] > LIST_LIMIT:
        print(f'   …另有 {doc["count"] - LIST_LIMIT} 件未列出')
    if not doc['count']:
        print('   当前无 EXTRA 孤儿 ⇒ 通道未启用；这不代表需求不存在（第 39 轮原话）')
    return 0
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='三副本同步守卫（唯一同步出口，三类双向比对）')
    ap.add_argument('--fix', action='store_true', help='用权威源覆盖副本后再**重新扫描**复核')
    ap.add_argument('--only', default='', help='只查某个镜像（deploy-free / 05-源代码全文），排障用')
    ap.add_argument('--list-extra', action='store_true',
                    help='只读：列出 EXTRA 孤儿副本及其在版本控制里的身份（不发凭据、不删）')
    ap.add_argument('--drop-extra', nargs=2, metavar=('MIRROR', 'REL'),
                    help='唯一真删 EXTRA 出口：一次一个，须配 --confirm <token> 与 --reason（≥20 字）')
    ap.add_argument('--confirm', metavar='TOKEN', help='逐字回交该 EXTRA 件由当前字节复算的 token')
    ap.add_argument('--reason', metavar='TEXT',
                    help=f'复核理由（≥{EXTRA_MIN_REASON_CHARS} 字，一句话不算复核）')
    ap.add_argument('--lock-status', action='store_true',
                    help='只读：五态锁报告（absent/held-living/stale-residue/unattributable/too-young）；'
                         '仅残件给出破锁凭据')
    ap.add_argument('--break-lock', action='store_true',
                    help='唯一破锁出口：只破"归属查得清 + 持锁进程已死 + 锁龄到点"的残件'
                         '（须配 --confirm 与 --reason）')
    ap.add_argument('--archive-status', action='store_true',
                    help='只读：归档面人口 ⇄ 台账引用双向对账（孤儿件/悬空引用看得见）')
    ap.add_argument('--prune-archive', metavar='NAME',
                    help='归档件唯一清理出口：过了保留窗口且来历可查才允许清（须配 --confirm/--reason）')
    ap.add_argument('--ttl-days', type=float, default=None,
                    help=f'配合 --prune-archive 覆盖保留窗口（默认 {ARCHIVE_TTL_DEFAULT_S / 86400:g} 天）')
    ap.add_argument('--json', action='store_true',
                    help='配合只读档（--list-extra / --lock-status / --archive-status）输出单个 JSON 文档')
    args = ap.parse_args(argv)

    probs = face_problems()
    if probs:
        for p in probs:
            print(f'   ! {p}')
        # 判词与处方放在**末行**：`verify_local` 折叠时只留末行，分类词若排在前面
        # 就会在本地闸的账本里退化成一句没有诊断的路径提示（第 39 轮"门面行必须带数"同族）。
        print(f'[FAIL] 面无法确认 ⇒ 不比对、不写盘（判"取数面坏"，不判"有 N 项未同步"）'
              f'｜请用源面那一份跑：python {DEV_FACE_NAME}/tools/{Path(__file__).name}')
        return 2
    mirrors = {k: v for k, v in MIRROR_DIRS.items() if not args.only or k == args.only}
    if not mirrors:
        print(f'[FAIL] --only 指向不存在的镜像：{args.only!r}（在册：{sorted(MIRROR_DIRS)}）')
        return 2
    # ── EXTRA 通道（面闸之后、比对/复制之前）──
    if args.list_extra:
        doc = list_extra(mirrors)
        if args.json:
            print(json.dumps(doc, ensure_ascii=False))   # 单文档到 stdout，别再混人类表格
            return 0
        if args.reason or args.confirm:
            print('[FAIL] --list-extra 是只读档，--confirm/--reason 在这里没有作用 ⇒ 拒，不静默忽略')
            return 2
        return print_extra(doc)
    if args.lock_status or args.archive_status:
        if args.fix or args.drop_extra is not None or args.break_lock or args.prune_archive:
            print('[FAIL] 只读档与写态档同时给出 ⇒ 一次调用只允许一种意图，删没动盘看不出来')
            return 2
        if args.reason or args.confirm:
            print('[FAIL] 只读档不接受 --confirm/--reason（列出凭据 ≠ 已复核）⇒ 拒，不静默忽略')
            return 2
    if args.lock_status:
        st = lock_state()
        if args.json:
            print(json.dumps(st, ensure_ascii=False))
            return 0 if st['state'] != 'unattributable' else 1
        print(f'写态锁状态（只读，不取锁也不排队）：{st["state"]}｜锁龄={st["age_s"]}s｜'
              f'pid={st["pid"]}｜action={st["action"]}')
        print(f'   {st["detail"]}')
        if st['breakable']:
            tok = lock_break_token(st)
            print(f'   残件可破 ⇒ 破锁凭据={tok}（绑 holder **当前字节**，改一个字节即失效）')
            print(f'   执行：python {DEV_FACE_NAME}/tools/{Path(__file__).name} --break-lock '
                  f'--confirm {tok} --reason "<≥{EXTRA_MIN_REASON_CHARS} 字>"')
            return 0
        return 1 if st['state'] == 'unattributable' else 0
    if args.archive_status:
        inv = archive_inventory()
        # 结论面：异常件（逐件可指认）+ 两把尺越界 + TTL 与节奏对不上账，三类任一即 rc=1。
        # 只看 anomalies 会把"面快满了"读成"一切正常"——那正是 M151 要堵的口径。
        cap_bad = bool(inv['over_file_cap'] or inv['over_byte_cap'] or not inv['ttl_basis']['ok'])
        if args.json:
            print(json.dumps(inv, ensure_ascii=False))
            return 0 if (not inv['anomalies'] and not cap_bad) else 1
        print(f'归档面回读：件 {inv["files"]}（{inv["bytes_total"]}B，最老 '
              f'{inv["oldest_age_s"]}h）｜台账 {inv["ledger_rows"]} 行、终态引用 {inv["terminal_refs"]} 个')
        print(f'   referenced={inv["referenced"]} unfinalized={inv["unfinalized"]} '
              f'orphan={inv["orphan_files"]} partial={inv["partial_writes"]} '
              f'dangling={inv["dangling_refs"]} pruned_explained={inv["pruned_explained"]}｜'
              f'字节核账 已核 {inv["sha_checked"]} / 不符 {inv["sha_mismatch"]} / 账载缺 sha {inv["sha_unrecorded"]}')
        print(f'   恒等式：files 分类合计={inv["files"]} 平={inv["files_balanced"]}｜'
              f'terminal 解释合计={inv["terminal_refs"]} 平={inv["refs_balanced"]}')
        print(f'   两把尺：人口 {inv["files"]}/{inv["max_files"]}（{inv["file_ratio"]:.1%}）'
              f'｜字节 {inv["bytes_total"]}/{inv["max_bytes"]}B（{inv["byte_ratio"]:.1%}）'
              f'｜先 binding 的是「{inv["binding_cap"]}」'
              f'｜越界 人口={inv["over_file_cap"]} 字节={inv["over_byte_cap"]}')
        tb = inv['ttl_basis']
        print(f'   保留窗口对账：TTL {tb["ttl_days"]} 天 ÷ 实测最坏发现间隔 {tb["max_gap_days"]} 天'
              f' = {tb["cycles"]} 个周期（下限 {tb["min_cycles"]:g}）⇒ '
              f'{"对得上账" if tb["ok"] else "对不上账：" + tb["detail"]}')
        print(f'   已过窗 {inv["expired_over_ttl"]} 件，只读批量计划列前 {inv["expired_plan_shown"]} 件'
              f'（逐件带绑字节 token；本工具**没有**一次清一批的出口）')
        if inv['anomalies']:
            print(f'[FAIL] 归档面有 {len(inv["anomalies"])} 项需人工看：'
                  f'{inv["anomalies"][:6]}（正解：先补账或查因，不直接删）')
            return 1
        print(f'[PASS] 归档面人口与台账引用双向对平：件 {inv["files"]}/{inv["max_files"]}、'
              f'字节 {inv["bytes_total"]}/{inv["max_bytes"]}B、已过窗 {inv["expired_over_ttl"]} 件、'
              f'异常 0 项')
        return 0
    if args.fix and args.drop_extra is not None:
        # 互斥必须写成**一个**条件：门禁第 52 项按"同一个 test 里同时出现 fix 与 drop_extra"判，
        # 拆成两层 if 会让那层保护对判据隐形（本轮实测：拆层时第 52 项直接判缺互斥）。
        # 第 46 轮加了通用"写态动作计数"闸之后，这一条必须**排在它前面**：
        # 否则这条最具体的处方（点名 --fix 与 --drop-extra）永远不会被命中，日志只剩一句泛化提示。
        print('[FAIL] --fix 与 --drop-extra 互斥：一次调用只允许一种动作'
              '（复制补齐 vs 删掉一个孤儿），同时给等于让人猜你删没删')
        return 2
    writes = [n for n, on in (('fix', args.fix), ('drop-extra', args.drop_extra is not None),
                              ('break-lock', args.break_lock),
                              ('prune-archive', args.prune_archive is not None)) if on]
    if len(writes) > 1:
        print(f'[FAIL] 一次调用给了 {len(writes)} 种写态动作 {writes} ⇒ 只许一个，'
              '否则"做没做、做了哪个"在日志里分不开')
        return 2
    if args.drop_extra is not None:
        mirror, rel = args.drop_extra
        try:
            res = drop_extra(mirror, rel, confirm=args.confirm, reason=args.reason)
        except ExtraError as exc:
            print(f'[FAIL] EXTRA 处置被拒：{exc}')
            return 2
        print(f'[OK] 已删除 {mirror}/{rel}（释放 {res["freed_bytes"]}B，镜像人口 '
              f'{res["population_before"]}→{res["population_after"]}，只少那一个）；'
              f'留证 {res["ledger"]}（在两份镜像之外）')
        return 0
    if args.break_lock:
        try:
            res = break_lock(confirm=args.confirm, reason=args.reason)
        except SyncLockError as exc:
            print(f'[FAIL] 破锁被拒：{exc}')
            return 2
        print(f'[OK] 已破残件锁（removed={res["broken"]} 件，pid={res["pid"]} '
              f'action={res["action"]} 锁龄={res["age_s"]}s，锁面 '
              f'{res["face_before"]}→{res["face_after"]}）；两份字节先归档再删，'
              f'留证 {res["ledger"]}')
        return 0
    if args.prune_archive is not None:
        ttl = ARCHIVE_TTL_DEFAULT_S if args.ttl_days is None else args.ttl_days * 86400.0
        if args.ttl_days is not None and args.ttl_days <= 0:
            print(f'[FAIL] --ttl-days={args.ttl_days} 非正 ⇒ 0 等于"任何件都可清"，'
                  '那是把保留窗口拆掉，拒')
            return 2
        try:
            res = prune_archive(args.prune_archive, confirm=args.confirm,
                                reason=args.reason, ttl_s=ttl)
        except ExtraError as exc:
            print(f'[FAIL] 归档件清理被拒：{exc}')
            return 2
        print(f'[OK] 已清理归档件 {res["name"]}（释放 {res["freed_bytes"]}B，'
              f'龄期 {res["age_s"]}s，来历 referenced_by={res["referenced_by"]}）；'
              f'sha={res["sha256"][:12]}… 仍留在台账里，留证 {res["ledger"]}')
        return 0
    try:
        declared_files()                      # 先验面可解析，别复制一半才发现名单读不出来
    except FaceError as exc:
        print(f'[FAIL] 面无法解析：{exc}')
        return 2

    # 只有 --fix 这一档取锁；不加 --fix 的三路比对保持只读（不排队、不落锁文件）。
    with (sync_lock('fix') if args.fix else contextlib.nullcontext()) as lease:
        tally = [0, 0, 0]
        for mname, mroot in sorted(mirrors.items()):
            for label, src_dir, dst_dir, names in build_faces(DEV, mroot):
                if names is None and not src_dir.is_dir():
                    print(f'[FAIL] 权威面目录不存在：{src_dir}')
                    return 2
                src_map = scan_tree(src_dir) if names is None else scan_files(src_dir, names)
                if not src_map:
                    print(f'[FAIL] {label} -> {mname}：权威面为空，判取数面坏而不是判通过')
                    return 2
                if args.fix:
                    mroot.mkdir(parents=True, exist_ok=True)
                    copied = 0
                    for rel in src_map:
                        # 逐件心跳（M149）：一次 --fix 要复制上百个文件，整轮可跑到分钟级。
                        # 没有心跳时"锁龄"会先越过残件阈值，只剩 pid 判活在兜；有了心跳，
                        # 锁龄本身就成了活性证据 —— 与 restic `refreshLocks` 每 5 分钟重写
                        # 锁（lock.go:173）同一取向，区别是本仓 pid 判得出，所以是两把尺而不是一把。
                        lease.beat()
                        s = (src_dir / rel)
                        d = (dst_dir / rel)
                        d.parent.mkdir(parents=True, exist_ok=True)
                        if (not d.exists()) or sha256(s) != sha256(d):
                            shutil.copy2(s, d)
                            copied += 1
                    print(f'[FIX] {label} -> {mname}：覆盖复制 {copied} 个文件（权威源 → 副本）')
                # 复制过也要重新扫一遍再下结论：拿内存里的 src_map 自证＝没验副本
                dst_map = scan_tree(dst_dir) if names is None else scan_files(dst_dir, names)
                for k, val in enumerate(report(label, mname, src_map, dst_map)):
                    tally[k] += val
    counts = f'MISSING {tally[0]} / DIFF {tally[1]} / EXTRA {tally[2]}'
    total = sum(tally)
    if total == 0:
        print(f'[PASS] 三类全零（{counts}）：两份副本与权威源逐字节一致')
        return 0
    print(f'[FAIL] {counts}，合计 {total} 项未同步'
          f'（要修：--fix；要删 EXTRA 必须人工复核，本工具不代删）')
    return 1


if __name__ == '__main__':
    sys.exit(main())
