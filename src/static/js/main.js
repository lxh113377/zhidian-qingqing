// 门店管理助手 — 通用JS（V2.1：表单防重复提交 + Flash 自动消失）
// 注：主题切换逻辑已内联进 base.html（快照剥离本文件后切换仍可用）
document.addEventListener('DOMContentLoaded', function() {

    // ── Flash 消息：成功类 4s 自动消失；错误/警告保留至手动关闭 ──
    document.querySelectorAll('.flash-success').forEach(function(el) {
        setTimeout(function() {
            el.style.transition = 'opacity 0.5s';
            el.style.opacity = '0';
            setTimeout(function() { el.remove(); }, 500);
        }, 4000);
    });
    document.querySelectorAll('.flash-close').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var el = btn.closest('.flash');
            if (el) el.remove();
        });
    });

    // ── 表单提交防重复点击（按钮进入 loading 态）──
    // data-ajax 表单由页内脚本自管 loading（如情景对话 AJAX），跳过通用逻辑
    document.querySelectorAll('form[method="POST"]:not([data-ajax]), form[method="post"]:not([data-ajax])').forEach(function(form) {
        form.addEventListener('submit', function() {
            var btn = form.querySelector('button[type="submit"]:not([name="action"])');
            if (btn && !btn.classList.contains('loading')) {
                btn.classList.add('loading');
                btn.dataset.originText = btn.textContent;
                btn.textContent = '提交中…';
                // 兜底：5s 后恢复（后端报错回显时按钮可用）
                setTimeout(function() {
                    btn.classList.remove('loading');
                    if (btn.dataset.originText) btn.textContent = btn.dataset.originText;
                }, 5000);
            }
        });
    });

    // ── 页面开场 stagger reveal（CSS 见 style.css 区块19；尊重减少动态偏好）──
    // 仅对 .main-content 的直接子元素编排，delay 递增封顶 300ms，避免长页拖沓
    if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        var kids = document.querySelectorAll('.main-content > *');
        kids.forEach(function(el, i) {
            el.classList.add('reveal');
            el.style.animationDelay = Math.min(i * 45, 300) + 'ms';
        });
    }
});
