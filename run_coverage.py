"""覆盖率报告生成器 — 在三套测试（冒烟/漏洞/L4）下收集覆盖率并输出汇总。

用法：
    python run_coverage.py              # 跑全部三套件并输出终端报告
    python run_coverage.py --html       # 额外生成 HTML 报告到 htmlcov/
    python run_coverage.py --smoke      # 只跑冒烟测试
    python run_coverage.py --vuln       # 只跑漏洞回归
    python run_coverage.py --l4         # 只跑 L4 断言

依赖：pip install coverage（未安装时自动降级为提示）
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

# 实测基线与地板的出处锚点（门禁第 35 项按这条字面量对账地板是否低于实测）
COVERAGE_BASELINE = 82          # 2026-09-26 三套件合计 TOTAL 82%（9565 stmts / 1715 miss）
FLOOR_HEADROOM_MIN = 1          # 地板至少比基线低这么多，否则就是零余量棘轮

SUITES = {
    'smoke': {
        'script': os.path.join(ROOT, 'smoke_test.py'),
        'label': '冒烟测试',
        'data': os.path.join(ROOT, '.coverage.smoke'),
    },
    'vuln': {
        'script': os.path.join(ROOT, 'test_vuln_regression.py'),
        'label': '漏洞回归',
        'data': os.path.join(ROOT, '.coverage.vuln'),
    },
    'l4': {
        'script': os.path.join(ROOT, 'test_l4_assertions.py'),
        'label': 'L4 断言',
        'data': os.path.join(ROOT, '.coverage.l4'),
    },
}


def _check_coverage():
    try:
        import coverage  # noqa: F401
        return True
    except ImportError:
        return False


def _run_suite(name, info):
    data_file = info['data']
    if os.path.exists(data_file):
        os.remove(data_file)
    print(f'\n{"=" * 60}')
    print(f'  收集覆盖率：{info["label"]}')
    print(f'{"=" * 60}')
    env = os.environ.copy()
    env['COVERAGE_FILE'] = data_file
    cmd = [PY, '-m', 'coverage', 'run', '--rcfile', os.path.join(ROOT, '.coveragerc'),
           '--data-file', data_file, info['script']]
    rc = subprocess.run(cmd, cwd=ROOT, env=env).returncode
    status = '通过' if rc == 0 else f'退出码 {rc}'
    print(f'  → {info["label"]}：{status}')
    return rc == 0


def _combine(data_files):
    combined = os.path.join(ROOT, '.coverage')
    if os.path.exists(combined):
        os.remove(combined)
    cmd = [PY, '-m', 'coverage', 'combine', '--rcfile', os.path.join(ROOT, '.coveragerc')]
    cmd.extend(data_files)
    subprocess.run(cmd, cwd=ROOT, check=False)


def _report():
    """打印覆盖率汇总并**返回 coverage 的退出码**。

    coverage 在总覆盖率低于 `.coveragerc` 的 `fail_under` 时以 2 退出。这个返回值必须一路传到
    main 的 sys.exit —— 否则地板只是写在配置里的一句愿望：不达标也照样印「全部通过」。
    （本轮实测正是这个形态，门禁第 35 项现在看守这条传播链。）
    """
    print(f'\n{"=" * 60}')
    print(f'  覆盖率汇总')
    print(f'{"=" * 60}')
    return subprocess.run([PY, '-m', 'coverage', 'report', '--rcfile',
                           os.path.join(ROOT, '.coveragerc')], cwd=ROOT, check=False).returncode


def _emit_summary(results, floor_rc):
    """套件/地板两条结论只在"两条都过"时才说「全部通过」，返回进程退出码。

    单独成函数是为了把分支从 main 里挪出来（也方便第 35 项按 AST 盯 sys.exit 引用地板）。
    """
    print(f'\n{"=" * 60}')
    print('  套件结果汇总')
    print(f'{"=" * 60}')
    for name, ok in results.items():
        icon = '✅' if ok else '❌'
        print(f'  {icon} {SUITES[name]["label"]}')

    all_pass = all(results.values())
    if floor_rc != 0:
        print(f'\n  覆盖率地板：未通过（coverage report 退出码 {floor_rc}）。'
              f'地板取 .coveragerc 的 fail_under，本轮记录的实测基线是 {COVERAGE_BASELINE}%；'
              '退出码 2=低于地板，4=压根没跑出覆盖率数据。')
    if all_pass and floor_rc == 0:
        print('\n  总体：全部通过（套件与覆盖率地板均达标）')
        return 0
    print(f'\n  总体：存在未通过项（套件{"全过" if all_pass else "有失败"}，'
          f'地板{"达标" if floor_rc == 0 else f"未达标 rc={floor_rc}"}）')
    return 1 if not all_pass else 2


def _html_report():
    print(f'\n  生成 HTML 报告 → htmlcov/')
    htmlcov = os.path.join(ROOT, 'htmlcov')
    if os.path.exists(htmlcov):
        shutil.rmtree(htmlcov)
    subprocess.run([PY, '-m', 'coverage', 'html', '--rcfile',
                    os.path.join(ROOT, '.coveragerc')], cwd=ROOT, check=False)


def main():
    if not _check_coverage():
        print('错误：未安装 coverage 包。请执行：pip install coverage')
        sys.exit(1)

    args = set(sys.argv[1:])
    want_html = '--html' in args
    selected = [name for name in SUITES if f'--{name}' in args]
    if not selected:
        selected = list(SUITES.keys())

    data_files = []
    results = {}
    for name in selected:
        info = SUITES[name]
        ok = _run_suite(name, info)
        results[name] = ok
        if os.path.exists(info['data']):
            data_files.append(info['data'])

    if len(data_files) > 1:
        _combine(data_files)
    elif len(data_files) == 1:
        combined = os.path.join(ROOT, '.coverage')
        if os.path.exists(combined):
            os.remove(combined)
        shutil.copy2(data_files[0], combined)

    floor_rc = 0
    if os.path.exists(os.path.join(ROOT, '.coverage')):
        floor_rc = _report()
        if want_html:
            _html_report()
    else:
        print('⚠️ 没有 .coverage 数据文件（三套件都没产出覆盖率）——本次不判地板')
        floor_rc = 4

    sys.exit(_emit_summary(results, floor_rc))


if __name__ == '__main__':
    main()
