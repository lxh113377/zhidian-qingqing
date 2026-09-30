/* 离线待传队列的幂等键与入队去重（零 DOM，可在 node 里逐字节真跑）
 *
 * 为什么单独成文件：与 offline_scope.js 同一理由 —— 判据若只能读源码文本，
 * 就永远不知道它算得对不对（对标第 51 轮 M163）。
 *
 * 治的问题：offline_submit.js 的去重键是 (saved_at, queued_for)，而 saved_at
 * 是建条目那一刻的时间戳 —— 同一内容在双击提交、多标签页、reconcileDraft 与
 * submit 双通道同时触发时，saved_at 必然不同 ⇒ 去重恒放行，队列里躺着内容
 * 完全相同的复件；多标签同时重放还会重复发送（服务端唯一约束能兜住不重复入账，
 * 但用户会收到两条互相矛盾的 flash）。
 *
 * 口径：
 * - 幂等键 = scope + fields_digest + queued_for（v2 条目三者齐全，跨标签稳定）；
 * - v1 条目（无 scope/digest）回落 saved_at|queued_for（迁移期过渡，不保证跨标签）；
 * - 缺库时调用方回落旧行为（fail-open）：去重是体验优化，最终一致性由服务端
 *   唯一约束保证；缺归属库则相反（fail-closed），那是安全判定 —— 两者不可混用。
 */
(function (root) {
    'use strict';

    function text(v) {
        return (v === null || v === undefined) ? '' : String(v);
    }

    /** 稳定幂等键；缺关键段 ⇒ 空串（调用方不得据此放行"已存在"的结论）。 */
    function itemKey(item) {
        if (!item || typeof item !== 'object') {
            return '';
        }
        var scope = text(item.scope);
        var digest = text(item.fields_digest);
        var day = text(item.queued_for);
        if (scope && digest && day) {
            return 'v2|' + scope + '|' + digest + '|' + day;
        }
        var ts = text(item.saved_at);
        if (ts && day) {
            return 'v1|' + ts + '|' + day;
        }
        return '';
    }

    /** 按幂等键找下标（重放迟回包在列表变动后仍能对上正确条目）；找不到 ⇒ -1。 */
    function findByKey(list, key) {
        if (!key || Object.prototype.toString.call(list) !== '[object Array]') {
            return -1;
        }
        for (var i = 0; i < list.length; i++) {
            if (itemKey(list[i]) === key) {
                return i;
            }
        }
        return -1;
    }

    /** 入队去重：同键已在 ⇒ {added:false, reason:'duplicate'}；无键 ⇒ 不可判。 */
    function dedupeEnqueue(list, item) {
        var key = itemKey(item);
        if (!key) {
            return {added: false, reason: 'unkeyable'};
        }
        if (findByKey(list, key) >= 0) {
            return {added: false, reason: 'duplicate', key: key};
        }
        return {added: true, key: key};
    }

    root.ICANOfflineIdem = {
        itemKey: itemKey,
        findByKey: findByKey,
        dedupeEnqueue: dedupeEnqueue
    };
})(typeof window !== 'undefined' ? window : this);
