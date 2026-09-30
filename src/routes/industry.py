"""行业包配置后台（P4）—— L1 DIY：不写代码即可建包、改内容、切激活。

路由清单（全部 POST 写操作走 CSRFProtect 全局防护 + admin_required）：
  - GET  /industry/            包管理页（列表 + 新建/复制行内表单）
  - POST /industry/create      新建包（可复制现有包）
  - POST /industry/<pid>/activate   激活包（数据隔离随之切换）
  - POST /industry/<pid>/delete     删除 DB 包（文件包/激活包拒绝）
  - GET  /industry/<pid>       包内容编辑器（分区锚点 + 分区表单）
  - POST /industry/<pid>/terms|domains|knowledge|questions|scenarios|products|metrics|formula
  - POST /industry/<pid>/reset/<section>  恢复某段内置默认（删 DB 覆盖）

安全约定：
  - pack_id 一律白名单校验（PACK_ID_RE），非法即 404；
  - 内容段写入全部经 pack_store 校验（长度/数量/枚举白名单），非法 flash 拒绝；
  - 页面渲染走 Jinja 默认转义，不使用 | safe。
"""
import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import login_required

from src import industry_pack, pack_store
from src.decorators import admin_required
from src.rate_limit import rate_limit_post

industry_bp = Blueprint('industry', __name__, url_prefix='/admin/industry')

# pack_id 白名单（与 pack_store 同源，路由层先行拦截防 500）
PACK_ID_RE = re.compile(r'^[a-z][a-z0-9_]{0,49}$')


def _valid_pack_id(pack_id):
    """P7-E / S-7：格式 + **存在性** 双重校验，不存在一律 404。

    此前只校验格式，任意「格式合法但不存在的 id」都会拿默认包内容顶着该 id 渲染，
    一保存即在幽灵 id 下写入 DB 覆盖行（包列表看不到但读路径会命中）。
    """
    if not pack_id or not PACK_ID_RE.match(pack_id):
        abort(404)
    if not pack_store.pack_exists(pack_id):
        abort(404)
    return pack_id


@industry_bp.route('/')
@login_required
@admin_required
def pack_list():
    """包管理页：内置包 + DB 包列表，激活徽章与统计。"""
    packs = pack_store.all_packs()
    return render_template('admin/industry_pack.html', packs=packs,
                           active_id=pack_store.active_pack_id(),
                           override_error=industry_pack.override_error())


@industry_bp.route('/create', methods=['POST'])
@login_required
@admin_required
@rate_limit_post('admin_write')
def pack_create():
    pack_id = request.form.get('pack_id', '').strip()
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    copy_from = (request.form.get('copy_from') or '').strip() or None
    ok, msg = pack_store.create_pack(pack_id, name, description, copy_from)
    flash(msg, 'success' if ok else 'danger')
    if ok:
        return redirect(url_for('industry.pack_detail', pack_id=pack_id))
    return redirect(url_for('industry.pack_list'))


@industry_bp.route('/<pack_id>/activate', methods=['POST'])
@login_required
@admin_required
@rate_limit_post('admin_write')
def pack_activate(pack_id):
    _valid_pack_id(pack_id)
    ok, msg = pack_store.set_active(pack_id)
    flash(msg, 'success' if ok else 'danger')
    return redirect(url_for('industry.pack_list'))


@industry_bp.route('/activate', methods=['POST'])
@login_required
@admin_required
@rate_limit_post('admin_write')
def pack_activate_selected():
    """顶部常驻选择器：按表单 pack_id 激活后回到管理员看板。"""
    pack_id = request.form.get('pack_id', '').strip()
    _valid_pack_id(pack_id)
    ok, msg = pack_store.set_active(pack_id)
    flash(msg, 'success' if ok else 'danger')
    return redirect(url_for('admin.dashboard'))


@industry_bp.route('/<pack_id>/delete', methods=['POST'])
@login_required
@admin_required
@rate_limit_post('admin_write')
def pack_delete(pack_id):
    _valid_pack_id(pack_id)
    ok, msg = pack_store.delete_pack(pack_id)
    flash(msg, 'success' if ok else 'danger')
    return redirect(url_for('industry.pack_list'))


@industry_bp.route('/<pack_id>')
@login_required
@admin_required
def pack_detail(pack_id):
    _valid_pack_id(pack_id)
    detail = pack_store.pack_detail(pack_id)
    # scenarios 表单行展开（pool/roles/labels 对齐为行列表）
    scen = detail['scenarios'] or {}
    pool = scen.get('pool') or {}
    rows = []
    for t in pool:
        role = scen.get('roles', {}).get(t) or ['', '', '']
        rows.append({
            'type': t,
            'label': scen.get('labels', {}).get(t, t),
            'role': role[0], 'persona': role[1] if len(role) > 1 else '',
            'goal': role[2] if len(role) > 2 else '',
            'lines': '\n'.join(pool[t]),
        })
    detail['scenario_rows'] = rows
    detail['scenario_default'] = scen.get('default', '')
    return render_template('admin/industry_content.html', pack=detail,
                           domain_names=detail['domains'],
                           # M3：表单兜底值单一来源（不再在模板里硬编码 0.15）
                           default_waste_threshold=industry_pack.DEFAULT_WASTE_RATE_THRESHOLD,
                           override_error=industry_pack.override_error())


# ───────────────────────── 内容段保存（POST 分区表单）─────────────────────────
_SAVE_ACTIONS = {
    'terms': pack_store.save_terms,
    'domains': pack_store.save_domains,
    'knowledge': pack_store.save_knowledge,
    'questions': pack_store.save_questions,
    'scenarios': pack_store.save_scenarios,
    'products': pack_store.save_products,
    'metrics': pack_store.save_metrics,
    'formula': pack_store.save_formula_params,
}


@industry_bp.route('/<pack_id>/save/<action>', methods=['POST'])
@login_required
@admin_required
@rate_limit_post('admin_write')
def pack_save(pack_id, action):
    _valid_pack_id(pack_id)
    handler = _SAVE_ACTIONS.get(action)
    if handler is None:
        abort(404)
    ok, msg = handler(pack_id, request.form)
    flash(msg, 'success' if ok else 'danger')
    anchor = action if not ok else None
    return redirect(url_for('industry.pack_detail', pack_id=pack_id, _anchor=anchor))


@industry_bp.route('/<pack_id>/reset/<section>', methods=['POST'])
@login_required
@admin_required
@rate_limit_post('admin_write')
def pack_reset(pack_id, section):
    _valid_pack_id(pack_id)
    if section not in pack_store.SECTIONS and section != 'metrics':
        abort(404)
    ok, msg = pack_store.reset_section(pack_id, section)
    flash(msg, 'success' if ok else 'danger')
    return redirect(url_for('industry.pack_detail', pack_id=pack_id))
