"""SQLite → MySQL 数据层迁移脚本（OPT-B，数据库可插拔升级路径）。

对标差距清单 G2：SQLAlchemy 已把数据访问抽象掉，缺的不是重写数据层，而是一条
**可验证、幂等、可演练** 的迁移路径 + 双引擎兼容性证明。ProductionConfig 早已支持
`DATABASE_URL` 注入与 utf8mb4 补齐（见 src/config.py），本脚本补上「搬数据 + 对行数」
这一环，SQLite 保留为演示态。

设计
────
- 表结构来自 `src.models.db.metadata`（单一真相源），迁移时按外键顺序在 MySQL 建表，
  不复制 SQLite 的自增序列以外的方言私有物。
- **幂等**：全表重装 —— 目标表按子→父顺序清空、再按父→子顺序整表写入，可反复执行且
  结果一致；不做「半迁移」增量。
- **--dry-run**：全程离线演练，只读源库行数 + 用 MySQL 方言编译每张表 DDL，
  **既不连目标库也不写任何数据**。这同时是「双引擎兼容」的可复现证据：DDL 能编译成
  合法 MySQL 语句即说明 ORM 模型不含 MySQL 不支持的构造。
- **真实迁移需要可达的 MySQL**，属用户决策项（未经批准不执行生产库迁移），故其执行
  路径以 --dry-run 与兼容性编译为可测边界。
- 输出：逐表 源行数 / 目标行数 / 是否一致 的比对报告 + 完整性汇总。

依赖：pymysql（已在 requirements.txt，纯 Python，PythonAnywhere 免费层可用）。
"""
import argparse
import os
import sys
from urllib.parse import urlsplit, urlunsplit

# 允许以脚本方式直接运行时导入 src 包（脚本与本文件同目录 = 仓库根，src 已可导入）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine, func, insert, select, text
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from src.models import db  # 导入即填充 db.metadata


def default_sqlite_path():
    """开发库默认路径（与 DevelopmentConfig 指向同一文件）。"""
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, 'data', 'ican.db')


def mask_url(url):
    """隐去连接串中的口令，供日志/报告安全展示。"""
    if not url:
        return url
    try:
        parts = urlsplit(url)
        if parts.password:
            netloc = parts.netloc.replace(':' + parts.password, ':***')
            return urlunsplit(parts._replace(netloc=netloc))
    except ValueError:
        pass
    return url


def compile_mysql_ddl():
    """把全部 ORM 表按 MySQL 方言编译成建表 DDL 列表（双引擎兼容的离线证明）。

    Returns:
        list[tuple[str, str]]: [(表名, MySQL CREATE TABLE 语句)]。编译任何一张表抛错，
            即说明模型里存在 MySQL 不支持的构造（应视为不兼容）。
    """
    dialect = mysql.dialect()
    out = []
    for table in db.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=dialect)).strip()
        out.append((table.name, ddl))
    return out


def _source_table_counts(source_engine):
    """读取源库每张已知表的行数；表不存在记为 None（区分 0 与缺失）。"""
    counts = {}
    with source_engine.connect() as conn:
        existing = set(conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'").scalars().all())
        for table in db.metadata.sorted_tables:
            if table.name not in existing:
                counts[table.name] = None
                continue
            try:
                counts[table.name] = conn.execute(
                    select(func.count()).select_from(table)).scalar() or 0
            except Exception:  # 方言差异等回退裸 SQL COUNT
                counts[table.name] = conn.execute(
                    text(f'SELECT COUNT(1) FROM "{table.name}"')).scalar() or 0
    return counts


def migrate(sqlite_path, mysql_url, dry_run=True, verbose=True):
    """执行（或演练）一次 SQLite → MySQL 迁移，返回比对报告 dict。

    Args:
        sqlite_path: 源 SQLite 文件路径。
        mysql_url: 目标 MySQL 连接串（dry-run 时不会被连接）。
        dry_run: True 仅离线演练（读源行数 + 编译 MySQL DDL），不碰目标库。
        verbose: 打印人类可读进度。

    Returns:
        dict: {dry_run, source, target, ddl_ok, tables:[{name, source_rows,
            target_rows, action, ok}], total_source_rows, mismatch, integrity_ok}
    """
    if not os.path.exists(sqlite_path):
        raise FileNotFoundError(f'源 SQLite 文件不存在：{sqlite_path}')

    source_uri = 'sqlite:///' + sqlite_path.replace('\\', '/')
    source_engine = create_engine(source_uri, future=True)

    # 兼容性证据：无论是否 dry-run，都先把 DDL 编译到 MySQL 方言
    try:
        compile_mysql_ddl()
        ddl_ok = True
    except Exception as exc:
        ddl_ok = False
        if verbose:
            print(f'❌ MySQL DDL 编译失败（数据层不兼容）：{exc}')

    source_counts = _source_table_counts(source_engine)
    total_source = sum(v or 0 for v in source_counts.values())

    report = {'dry_run': dry_run, 'source': sqlite_path, 'target': mask_url(mysql_url),
              'ddl_ok': ddl_ok, 'tables': [], 'total_source_rows': total_source,
              'mismatch': [], 'integrity_ok': None}

    if dry_run:
        for table in db.metadata.sorted_tables:
            rows = source_counts.get(table.name)
            # 源库缺失的表（如旧库尚无新加的表）不算失败：目标侧 create_all 会新建空表
            report['tables'].append({'name': table.name, 'source_rows': rows,
                                     'target_rows': None,
                                     'action': 'dry-run' if rows is not None else 'create-empty',
                                     'ok': True})
        report['integrity_ok'] = ddl_ok
        if verbose:
            _print_report(report)
        return report

    if not ddl_ok:
        raise RuntimeError('DDL 兼容性校验未通过，终止真实迁移')
    if not mysql_url:
        raise ValueError('非 dry-run 迁移必须提供 --mysql 连接串')

    _run_real_copy(source_engine, source_counts, mysql_url, report)
    if verbose:
        _print_report(report)
    return report


def _run_real_copy(source_engine, source_counts, mysql_url, report):
    """真实迁移：目标建表（幂等）→ 子→父清空 → 父→子整表写入 → 逐表行数比对。

    直接改写传入的 report（就地填充 tables / mismatch / integrity_ok）。目标引擎用
    通用 SQLAlchemy Core 写入，与方言无关，故同一套逻辑对 MySQL 与（测试用的）第二
    SQLite 目标均成立。
    """
    target_engine = create_engine(mysql_url, future=True)
    db.metadata.create_all(target_engine, checkfirst=True)  # 只建缺失，幂等
    with target_engine.begin() as tconn:                    # 子→父清空
        for table in reversed(db.metadata.sorted_tables):
            tconn.execute(table.delete())
    with source_engine.connect() as sconn, target_engine.begin() as tconn:
        for table in db.metadata.sorted_tables:             # 父→子整表写入
            _copy_one_table(sconn, tconn, table, source_counts.get(table.name), report)
    report['integrity_ok'] = not report['mismatch']


def _copy_one_table(sconn, tconn, table, src_rows, report):
    """搬运单表并记录行数比对结果；源表缺失记为 skip，不视为失败。"""
    if src_rows is None:
        report['tables'].append({'name': table.name, 'source_rows': None,
                                 'target_rows': None, 'action': 'skip-no-source',
                                 'ok': True})
        return
    data = sconn.execute(select(table)).mappings().all()
    if data:
        tconn.execute(insert(table), [dict(d) for d in data])
    tgt = tconn.execute(select(func.count()).select_from(table)).scalar() or 0
    ok = (tgt == src_rows)
    report['tables'].append({'name': table.name, 'source_rows': src_rows,
                             'target_rows': tgt, 'action': 'copy', 'ok': ok})
    if not ok:
        report['mismatch'].append(table.name)


def _print_report(report):
    mode = 'DRY-RUN（离线演练，未写目标库）' if report['dry_run'] else 'REAL（已写入目标库）'
    print('=' * 64)
    print(f'SQLite → MySQL 迁移报告   模式：{mode}')
    print(f'源库：{report["source"]}')
    print(f'目标：{report["target"]}')
    print(f'MySQL DDL 兼容编译：{"✅ 通过" if report["ddl_ok"] else "❌ 失败"}')
    print('-' * 64)
    print(f'{"表名":<28}{"源行数":>10}{"目标行数":>12}{"动作":>16}{"结果":>8}')
    for row in report['tables']:
        res = '✅' if row['ok'] else '❌'
        tgt = '-' if row['target_rows'] is None else row['target_rows']
        src = '缺失' if row['source_rows'] is None else row['source_rows']
        print(f'{row["name"]:<28}{src:>10}{tgt:>12}{row["action"]:>16}{res:>8}')
    print('-' * 64)
    print(f'源总行数：{report["total_source_rows"]}   '
          f'不一致表：{report["mismatch"] or "无"}   '
          f'完整性：{"✅ OK" if report["integrity_ok"] else "❌ 有差异"}')
    print('=' * 64)


def build_parser():
    parser = argparse.ArgumentParser(
        description='SQLite → MySQL 迁移（OPT-B，默认 --dry-run 离线演练）')
    parser.add_argument('--sqlite', default=default_sqlite_path(),
                        help='源 SQLite 文件路径（默认 data/ican.db）')
    parser.add_argument('--mysql', default=os.environ.get('DATABASE_URL'),
                        help='目标 MySQL 连接串（默认取环境变量 DATABASE_URL）')
    parser.add_argument('--dry-run', action='store_true',
                        help='离线演练：只读源行数 + 编译 MySQL DDL，绝不连接或写入目标库')
    parser.add_argument('--execute', action='store_true',
                        help='真实迁移（需要可达 MySQL；生产执行须用户批准）')
    return parser


def main(argv=None):
    """CLI 入口：无 --execute 时一律按 --dry-run 处理，杜绝误写目标库。"""
    args = build_parser().parse_args(argv)
    dry_run = (not args.execute)  # 未显式 --execute 即演练（--dry-run 或缺省）
    report = migrate(args.sqlite, args.mysql, dry_run=dry_run or args.dry_run)
    return 0 if report['integrity_ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
