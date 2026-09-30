"""全行业 DIY 工作台路由（PH2-4E）：向导 → 草稿预览 → 人工确认落库 + 导入导出。

权限：仅老板（ROLE_OWNER）可操作；平台管理员/店员 403。
写操作一律 POST + CSRF + `admin_write` 限流。
草稿只进进程内缓存（`pack_wizard`），确认前不写库。
"""
from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from src import ai_quota, industry_pack, pack_io, pack_wizard
from src.decorators import role_required
from src.models import ROLE_OWNER
from src.rate_limit import rate_limit_post

diy_bp = Blueprint('diy', __name__, url_prefix='/admin/diy')

# LLM 注入口（默认未接入 → 直接模板草稿，不消耗配额）；P5 可接 DeepSeek
_LLM = {'fn': None}


def set_llm(fn):
    """注入 LLM 调用器（测试桩 / 真实模型）。"""
    _LLM['fn'] = fn


def _llm_configured():
    return _LLM['fn'] is not None


@diy_bp.route('/')
@login_required
@role_required(ROLE_OWNER)
def diy_home():
    """建包向导首页 + 导入导出入口。"""
    used = ai_quota.usage_today(current_user.merchant_id, ai_quota.KIND_PACK_GEN)
    return render_template('admin/diy_home.html', steps=pack_wizard.WIZARD_STEPS,
                           packs=industry_pack.list_packs(),
                           quota={'used': used['calls'],
                                  'limit': ai_quota.PACK_GEN_DAILY_LIMIT,
                                  'tokens': used['tokens'],
                                  'token_limit': ai_quota.TOKEN_DAILY_LIMIT},
                           llm_ready=_llm_configured())


@diy_bp.route('/draft', methods=['POST'])
@login_required
@role_required(ROLE_OWNER)
@rate_limit_post('admin_write')
def diy_draft():
    """根据向导生成草稿（AI 优先，未接入/超限回落模板），跳预览页。"""
    ok, errors, brief = pack_wizard.validate_brief(request.form)
    if not ok:
        flash('；'.join(errors), 'danger')
        return redirect(url_for('diy.diy_home'))

    quota = ai_quota.check_quota(current_user.merchant_id, ai_quota.KIND_PACK_GEN)
    if not _llm_configured():
        result = pack_wizard.generate_draft(brief, llm=None)
    elif not quota['ok']:
        result = pack_wizard.generate_draft(
            brief, llm=None,
            timeout_note=('AI 配额已用尽，已回落模板草稿'
                          if quota['reason'] == 'quota_exceeded'
                          else 'AI Token 额度已用尽，已回落模板草稿'))
    else:
        cache_key = f"packgen:{brief.get('industry_type')}:{brief.get('main_items')}"

        def _producer(_timeout):
            return pack_wizard.generate_draft(brief, llm=_LLM['fn'])['draft']

        run = ai_quota.run_with_quota(
            current_user.merchant_id, ai_quota.KIND_PACK_GEN, _producer,
            lambda: pack_wizard.build_template_draft(brief),
            cache_key=cache_key, store_id=getattr(current_user, 'store_id', None))
        if run['source'] == 'fallback':
            note = {'quota_exceeded': 'AI 配额已用尽，已回落模板草稿',
                    'token_exceeded': 'AI Token 额度已用尽，已回落模板草稿',
                    'timeout': 'AI 调用超时（30 秒），已回落模板草稿',
                    'error': 'AI 调用失败，已回落模板草稿'}.get(run['reason'], '已回落模板草稿')
            result = pack_wizard.generate_draft(brief, llm=None, timeout_note=note)
        else:
            result = {'draft': run['value'], 'source': run['source'],
                      'warnings': [], 'validation': pack_wizard.validate_draft(run['value'])}

    token = pack_wizard.put_draft(result['draft'], brief=brief, source=result['source'])
    return redirect(url_for('diy.diy_preview', token=token))


@diy_bp.route('/draft/<token>')
@login_required
@role_required(ROLE_OWNER)
def diy_preview(token):
    """草稿预览：展示来源、评分、错误与各段摘要（不落库）。"""
    entry = pack_wizard.peek_draft(token)
    if entry is None:
        abort(404, description='草稿不存在或已过期，请重新生成。')
    draft = entry['draft']
    return render_template('admin/diy_preview.html', token=token,
                           draft=draft, entry=entry,
                           validation=pack_wizard.validate_draft(draft),
                           domains=[d.get('name') for d in draft.get('domains') or []],
                           knowledge=draft.get('knowledge') or {})


@diy_bp.route('/draft/<token>/confirm', methods=['POST'])
@login_required
@role_required(ROLE_OWNER)
@rate_limit_post('admin_write')
def diy_confirm(token):
    """人工确认后落库（草稿 token 一次性）。"""
    entry = pack_wizard.take_draft(token)
    if entry is None:
        abort(404, description='草稿不存在或已确认/丢弃，请重新生成。')
    pack_id = (request.form.get('pack_id') or '').strip()
    payload = pack_io.export_shape_of(entry['draft'], pack_id=pack_id)
    ok, message, report = pack_io.import_pack(payload, overwrite=False)
    if not ok:
        flash(f'{message}：' + '；'.join(report.get('errors', [])[:3]), 'danger')
        return redirect(url_for('diy.diy_home'))
    flash(message, 'success')
    return redirect(url_for('industry.pack_list'))


@diy_bp.route('/draft/<token>/discard', methods=['POST'])
@login_required
@role_required(ROLE_OWNER)
@rate_limit_post('admin_write')
def diy_discard(token):
    """丢弃草稿（同样一次性）。"""
    pack_wizard.take_draft(token)
    flash('草稿已丢弃', 'success')
    return redirect(url_for('diy.diy_home'))


@diy_bp.route('/export/<pack_id>')
@login_required
@role_required(ROLE_OWNER)
def diy_export(pack_id):
    """导出行业包 JSON（附件下载）。"""
    import re as _re
    if not _re.fullmatch(r'[A-Za-z0-9_-]{1,64}', pack_id or ''):
        abort(400, description='包ID不合法。')
    text = pack_io.dumps_pack(pack_id)
    if text is None:
        abort(404, description='包不存在。')
    return Response(
        text, mimetype='application/json',
        headers={'Content-Disposition': f'attachment; filename="{pack_id}.json"'})


@diy_bp.route('/import', methods=['POST'])
@login_required
@role_required(ROLE_OWNER)
@rate_limit_post('admin_write')
def diy_import():
    """导入行业包 JSON：校验失败停在预览页，不落库。"""
    raw = request.form.get('payload') or ''
    overwrite = (request.form.get('overwrite') or '') in ('1', 'true', 'on')
    report = pack_io.validate_import(raw)
    if not report['ok']:
        flash('导入校验未通过：' + '；'.join(report['errors'][:3]), 'danger')
        return render_template('admin/diy_import.html', payload=raw,
                               report=report, packs=industry_pack.list_packs()), 400
    ok, message, final = pack_io.import_pack(raw, overwrite=overwrite)
    if not ok:
        flash(message + '：' + str(final.get('conflicts') or final.get('errors') or ''), 'warning')
        return render_template('admin/diy_import.html', payload=raw,
                               report=final, packs=industry_pack.list_packs()), 400
    flash(message, 'success')
    return redirect(url_for('industry.pack_list'))


@diy_bp.route('/import', methods=['GET'])
@login_required
@role_required(ROLE_OWNER)
def diy_import_page():
    return render_template('admin/diy_import.html', payload='', report=None,
                           packs=industry_pack.list_packs())


@diy_bp.route('/copy', methods=['POST'])
@login_required
@role_required(ROLE_OWNER)
@rate_limit_post('admin_write')
def diy_copy():
    """复制现有包为新包。"""
    src = (request.form.get('src_id') or '').strip()
    new_id = (request.form.get('new_id') or '').strip()
    name = (request.form.get('name') or '').strip() or None
    ok, message = pack_io.copy_pack(src, new_id, name)
    flash(message, 'success' if ok else 'danger')
    return redirect(url_for('diy.diy_home'))


@diy_bp.route('/restore/<pack_id>', methods=['POST'])
@login_required
@role_required(ROLE_OWNER)
@rate_limit_post('admin_write')
def diy_restore(pack_id):
    """恢复内置示例（清 DB 覆盖层）。"""
    ok, message = pack_io.restore_builtin(pack_id)
    flash(message, 'success' if ok else 'danger')
    return redirect(url_for('industry.pack_list'))
