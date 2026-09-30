/* 离线待传队列的「条目归属」判定（零 DOM，可在 node 里逐字节真跑）
 *
 * 为什么单独成文件：归属判定必须能被**没有浏览器的执行面**驱动（对标第 49 轮）。
 * 它是纯函数，不读 `document`、不发请求，因此同一份字节既能在页面里用，也能在
 * 测试里用 —— 判据若只能读源码文本，就永远不知道它算得对不对。
 *
 * 口径（与 offline_submit.js 头部注释同源）：
 * - 归属 = 店员 × 门店 × 激活行业包 × 包协议版本 四元组，任一缺位即「不可归属」；
 * - 「不可归属」的一侧一律**不放行发送**（fail-closed）：宁可让条目停在本地，
 *   也不能把甲店员填的记录以乙店员身份入账。
 *
 * 为什么含"店员"而不是只含门店：第 49 轮探针实测——同店两店员、当日重放一条 A 未提交过的
 * 产品，服务端 `提交成功=True` 且 B 名下流水 0→1 ⇒ 「同一设备不同账号」是本缺陷的**主形态**，
 * 只按门店判归属对它零判定力。隔夜那一半则早已被服务端 `queued_for` 守卫拦住（L4 H4 段在测）。
 */
(function (root) {
    'use strict';

    function text(v) {
        return (v === null || v === undefined) ? '' : String(v);
    }

    /** 四元组拼归属键；任一段为空 ⇒ 返回空串（调用方据此判「不可归属」）。 */
    function scopeKey(staffId, storeId, packId, packVer) {
        var a = text(staffId).trim();
        var s = text(storeId).trim();
        var p = text(packId).trim();
        var v = text(packVer).trim();
        if (!a || !s || !p || !v) {
            return '';
        }
        return 'staff=' + a + '|store=' + s + '|pack=' + p + '|ver=' + v;
    }

    /** 从表单节点读 data-scope-*；节点缺失/属性不全 ⇒ 空串（老页面与快照页即此态）。 */
    function scopeOfElement(el) {
        if (!el || typeof el.getAttribute !== 'function') {
            return '';
        }
        return scopeKey(el.getAttribute('data-scope-staff'),
                        el.getAttribute('data-scope-store'),
                        el.getAttribute('data-scope-pack'),
                        el.getAttribute('data-scope-packver'));
    }

    /** 条目是否可在当前页面重放。任何不确定 ⇒ false（不放行）。 */
    function itemMatchesScope(item, currentScope) {
        if (!currentScope) {
            return false;
        }
        if (!item || typeof item !== 'object') {
            return false;
        }
        if (!item.scope) {
            return false;
        }
        return text(item.scope) === text(currentScope);
    }

    /**
     * 字段内容指纹（FNV/djb2 混合，纯 ES5 移位乘法，不用 Math.imul 以兼容旧内核）。
     * 只用于「同归属下内容是否被改过」的可疑提示，**不当身份用**：键序无关、
     * 值敏感、中文按码点累加（不截成单字节，否则同音产品名会撞成一个指纹）。
     */
    function fieldsDigest(fields) {
        var keys = [];
        var k;
        if (fields && typeof fields === 'object') {
            for (k in fields) {
                if (Object.prototype.hasOwnProperty.call(fields, k)) {
                    keys.push(k);
                }
            }
        }
        keys.sort();
        var parts = [];
        for (var i = 0; i < keys.length; i++) {
            parts.push(keys[i] + '=' + text(fields[keys[i]]));
        }
        var text2 = parts.join('&');
        var h = 5381;
        for (var j = 0; j < text2.length; j++) {
            h = (((h << 5) + h) ^ text2.charCodeAt(j)) >>> 0;
        }
        var out = h.toString(16);
        while (out.length < 8) {
            out = '0' + out;
        }
        return out;
    }

    var api = {
        scopeKey: scopeKey,
        scopeOfElement: scopeOfElement,
        itemMatchesScope: itemMatchesScope,
        fieldsDigest: fieldsDigest
    };
    root.ICANOfflineScope = api;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    }
})(typeof window !== 'undefined' ? window : globalThis);
