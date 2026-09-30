/* H5 弱网离线填报队列（H4，2026-09-24 第 6 轮对标）
 *
 * 场景：店员在微信内置浏览器里填报，门店后厨/仓库常年弱网或无信号。
 * 现状是「提交 → 转圈 → 失败 → 整张表单没了」，用户必须重填。
 *
 * 借鉴对象与取舍（详见 output/PH6-materials/benchmarking-report-round6 的 GAP1 节）
 * - Enketo 不用 navigator.onLine 判在线，而是探服务端；本模块同样不把
 *   navigator.onLine 当唯一依据 —— 它只反映「有无网络接口」，一格信号/门户劫持
 *   下它为 true 而请求根本到不了服务端。
 * - 因此主干是**提交前先把草稿落到本机**：在线路径完全不拦截原生提交（零延迟、
 *   零行为变化），失败后用户重开页面时由落地页回执判定上一条的结局，未获确认的
 *   草稿自动升级为待传队列。这样「显示在线但连不通」这个主导场景才真的被兜住。
 * - 幂等沿用 ODK/CommCare 的思路但用本域自然键：DailySales 已有唯一约束
 *   (store, staff, record_date, pack, product)，隔夜重放最坏是「今天已经提交过」，
 *   不会产生重复流水，因此无需引入 client UUID 与新列。
 * - 零依赖零构建：ES5 + XMLHttpRequest（不用 fetch，兼容旧版 X5/WKWebView），
 *   不引 IndexedDB、不加 service worker —— 微信 webview 对 SW 支持不稳；
 *   若将来补 SW，必须照 Enketo 的做法对非 GET 请求直接放行，绝不拦截提交。
 * - 不静默丢数据：只有拿到服务端明确回执（success / 已存在）才清草稿或出队。
 */
(function () {
    'use strict';

    var QUEUE_KEY = 'ican_h5_offline_v2';
    var LEGACY_KEY = 'ican_h5_offline_v1';   // v1 条目不带归属 ⇒ 只能迁进 v2 并挂起，不许静默发
    var DRAFT_KEY = 'ican_h5_draft_v1';
    var MAX_RECORDS = 30;          // 队列条数上限（超则拒收并提示先处理）
    var MAX_TRIES = 12;            // 单条最大自动重试次数，超过即挂起待人工
    var ITEM_BYTE_CAP = 8192;      // 单条序列化上限
    var RETRY_WINDOW_MS = 30 * 60 * 1000;
    var PROBE_URL = '/healthz';
    var PROBE_TIMEOUT_MS = 4000;
    // 归属判定来自 offline_scope.js（零 DOM 纯函数，与测试驱动的是同一份字节）。
    // 取不到该库 ⇒ Scope 为 null ⇒ CURRENT_SCOPE 为空 ⇒ 所有条目一律不放行（fail-closed），
    // 而不是退回"按当前会话盲发"。
    var Scope = (typeof window !== 'undefined' && window.ICANOfflineScope) || null;
    // 幂等键库（offline_idem.js，零 DOM 纯函数）。缺库 ⇒ 回落旧行为（只按条数上限收，
    // 不去重）：去重是体验优化，最终一致性由服务端唯一约束保证 —— 与归属判定缺库即
    // 全拦（fail-closed）不可混用，那是安全判定。
    var Idem = (typeof window !== 'undefined' && window.ICANOfflineIdem) || null;
    // 链路可达性由后台探测得出。navigator.onLine 只说明「有网络接口」，一格信号或
    // 门户劫持下它为 true 而请求根本到不了服务端 —— 那正是本模块要救的主场景，
    // 而原生表单提交一旦发出去就再也拦不住（数据随错误页丢失）。
    // 刻意用后台探测而非提交前阻塞探测：主干零延迟，探测未回时行为同旧版。
    var linkDown = false;

    function probeLink() {
        try {
            var x = new XMLHttpRequest();
            x.open('HEAD', PROBE_URL, true);
            x.timeout = PROBE_TIMEOUT_MS;
            x.onload = function () { linkDown = !(x.status >= 200 && x.status < 400); render(); };
            x.onerror = x.ontimeout = function () { linkDown = true; render(); };
            x.send(null);
        } catch (e) {
            linkDown = true;
        }
    }

    // 离线判定 = 接口层离线 或 探测到服务端连不通
    function isOffline() {
        return navigator.onLine === false || linkDown === true;
    }

    var form = document.querySelector('form.sales-form[data-record-date]');
    if (!form) return;
    var SUBMIT_URL = form.getAttribute('action');
    var TODAY = form.getAttribute('data-record-date');
    // 当前页面归属：门店 × 激活包 × 包协议版本。任一缺位或取不到判定库 ⇒ 空串。
    var CURRENT_SCOPE = Scope ? Scope.scopeOfElement(form) : '';
    var NO_SCOPE_NOTE = '本机这条记录缺少归属信息（门店/行业包），已暂停自动续传，请核对后重传或清空';
    var OTHER_SCOPE_NOTE = '这条记录属于别的门店或行业包，不能按当前身份补交，请切回原门店或手动处理';

    // ── 存储（Safari 无痕/省流模式会直接抛，全部包住）──────────────
    function readJson(key) {
        try {
            var raw = localStorage.getItem(key);
            return raw ? JSON.parse(raw) : null;
        } catch (e) {
            return null;
        }
    }

    function writeJson(key, value) {
        try {
            localStorage.setItem(key, JSON.stringify(value));
            return true;
        } catch (e) {
            return false;
        }
    }

    function drop(key) {
        try { localStorage.removeItem(key); } catch (e) {}
    }

    function load() {
        var list = readJson(QUEUE_KEY);
        return Object.prototype.toString.call(list) === '[object Array]' ? list : [];
    }

    /**
     * v1 → v2 迁移：v1 条目不带归属，**无法追溯是谁在哪个门店填的**，
     * 因此一律挂起（parked）交给人判断，绝不"按当前登录身份补发"。
     * 幂等：按 (saved_at, queued_for) 去重，重复调用不会搬第二遍；
     * 顺序：先写 v2 成功、再删 v1 —— 写失败时 v1 原样留着，下页重试（不静默丢数据）。
     */
    function migrateLegacy() {
        var legacy = readJson(LEGACY_KEY);
        if (Object.prototype.toString.call(legacy) !== '[object Array]' || !legacy.length) {
            return 0;
        }
        var list = load();
        var seen = {};
        var i;
        for (i = 0; i < list.length; i++) {
            seen[queueSeenKey(list[i])] = 1;
        }
        var moved = 0;
        for (i = 0; i < legacy.length; i++) {
            var it = legacy[i];
            if (!it || typeof it !== 'object') {
                continue;
            }
            var dedup = queueSeenKey(it);
            if (seen[dedup]) {
                continue;
            }
            it.scope = '';
            it.parked = true;
            it.note = NO_SCOPE_NOTE;
            list.push(it);
            seen[dedup] = 1;
            moved++;
        }
        if (!save(list)) {
            return 0;
        }
        drop(LEGACY_KEY);
        return moved;
    }

    function save(list) {
        return writeJson(QUEUE_KEY, list);
    }

    // 迁移与去重的统一键：有幂等库用稳定键，否则回落时间戳对（迁移期过渡）。
    function queueSeenKey(it) {
        if (Idem) {
            var k = Idem.itemKey(it || {});
            if (k) return k;
        }
        return String(it.saved_at) + '|' + String(it.queued_for);
    }

    // ── 表单序列化 ──────────────────────────────────────────
    function serialize() {
        var fields = {};
        var nodes = form.querySelectorAll('input, select, textarea');
        for (var i = 0; i < nodes.length; i++) {
            var el = nodes[i];
            var name = el.getAttribute('name');
            if (!name || el.disabled || el.type === 'submit') continue;
            if ((el.type === 'checkbox' || el.type === 'radio') && !el.checked) continue;
            fields[name] = el.value === null ? '' : String(el.value);
        }
        return fields;
    }

    function dimValue(fields) {
        var dim = form.querySelector('[data-skeleton="dimension"]');
        if (!dim) return true;                       // 无维度字段的包不因此挡住提交
        return !!String(fields[dim.getAttribute('name')] || '').trim();
    }

    function makeItem(fields) {
        var scoped = !!CURRENT_SCOPE;
        return {
            saved_at: Date.now(),
            queued_for: TODAY,
            tries: 0,
            next_at: 0,
            // 建条目时就取不到归属 ⇒ 直接挂起：宁可让用户手动处理，也不按"当时登录的人"补交
            parked: !scoped,
            note: scoped ? '' : NO_SCOPE_NOTE,
            scope: CURRENT_SCOPE,
            fields_digest: Scope ? Scope.fieldsDigest(fields) : '',
            fields: fields
        };
    }

    function pushQueue(item) {
        var list = load();
        if (Idem) {
            var verdict = Idem.dedupeEnqueue(list, item);
            if (!verdict.added) {
                return verdict.reason === 'duplicate' ? 'duplicate' : false;
            }
        }
        if (list.length >= MAX_RECORDS) return false;
        list.push(item);
        if (!save(list)) { list.pop(); return false; }
        return true;
    }

    // ── 落地页回执判定：服务端渲染的第一条 flash 即上一条提交的权威结局 ──
    function bootOutcome() {
        var el = document.querySelector('.flash-messages [data-flash]');
        return el ? (el.getAttribute('data-flash') || '') : '';
    }

    var BOOT_OUTCOME = bootOutcome();

    function reconcileDraft() {
        var draft = readJson(DRAFT_KEY);
        if (!draft) return;
        drop(DRAFT_KEY);
        var fields = draft.fields || {};
        if (BOOT_OUTCOME === 'success' || BOOT_OUTCOME === 'warning') return;  // 已入账或已存在
        var item = makeItem(fields);
        item.saved_at = draft.saved_at || item.saved_at;
        item.queued_for = draft.queued_for || TODAY;
        if (BOOT_OUTCOME === 'danger') {
            item.parked = true;                   // 服务端明确拒绝：不自动重试，留内容给用户
            item.note = '服务端未接受这条，请核对内容后重传或清空';
        }
        var queued = pushQueue(item);
        if (queued === 'duplicate') {
            flashBox('相同记录已在待传队列中，未重复暂存', 'warning');
            return;
        }
        if (!queued) {
            flashBox('本机已有一条未确认记录，但待传队列已满，请先处理', 'danger');
            return;
        }
        flashBox(item.parked
            ? '上次提交被服务端拒绝，内容已保留在「离线待传」里'
            : '上次提交未确认成功（可能网络不通），已自动转入待传队列，联网后自动续传',
            item.parked ? 'danger' : 'warning');
    }

    // ── 重放 ───────────────────────────────────────────────
    function classify(status, finalUrl, body) {
        if (status === 0 || status >= 500 || status === 429) return 'transient';
        // 会话过期：login_required 把 POST 302 到 /login，XHR 跟随后拿到登录页
        if (/\/login/.test(finalUrl || '')) return 'session';
        if (status === 400 || status === 403 || status === 404) return 'parked';
        var kind = '';
        try {
            var doc = new DOMParser().parseFromString(body || '', 'text/html');
            var el = doc.querySelector('[data-flash]');
            kind = el ? (el.getAttribute('data-flash') || '') : '';
        } catch (e) {
            return 'unknown';
        }
        if (kind === 'success') return 'ok';
        if (kind === 'warning') return 'exists';          // 「今天已经提交过」= 幂等命中
        if (kind === 'danger') return 'parked';
        return 'unknown';                                 // 无回执 = 不认成功
    }

    function messageOf(body) {
        try {
            var doc = new DOMParser().parseFromString(body || '', 'text/html');
            var el = doc.querySelector('[data-flash] span');
            return el ? String(el.textContent || '').trim() : '';
        } catch (e) {
            return '';
        }
    }

    var busy = false;

    function backoffMs(tries) {
        var minutes = Math.min(Math.pow(2, Math.min(tries, 5)), 30);
        return minutes * 60 * 1000;
    }

    function nextPlayable(list) {
        var now = Date.now();
        for (var i = 0; i < list.length; i++) {
            var it = list[i];
            if (it.parked || (it.tries || 0) >= MAX_TRIES || (it.next_at || 0) > now) {
                continue;
            }
            // 归属闸：判定库缺失也走这里（fail-closed）。用户点「立即重传」会把 parked 清掉，
            // 但这一道与 playOne 内那道仍在拦 —— 换店/换包后的隔夜条目绝不按当前身份补发。
            if (!Scope || !Scope.itemMatchesScope(it, CURRENT_SCOPE)) {
                continue;
            }
            return i;
        }
        return -1;
    }

    function hasPlayable() { return nextPlayable(load()) >= 0; }

    function indexOf(list, item) {
        for (var i = 0; i < list.length; i++) {
            if (list[i].saved_at === item.saved_at && list[i].queued_for === item.queued_for) return i;
        }
        return -1;
    }

    function replayAll() {
        if (busy) return;
        var idx = nextPlayable(load());
        if (idx < 0) { render(); return; }
        busy = true;
        playOne(load()[idx]);
    }

    function playOne(item) {
        // 唯一发送出口上的第二道闸（nextPlayable 之外再拦一次）：任何绕过队列挑选、
        // 直接调 playOne 的后续改动都必须在归属不符时挂起，而不是把数据发给当前会话。
        if (!Scope || !Scope.itemMatchesScope(item, CURRENT_SCOPE)) {
            var orphan = load();
            var oi = indexOf(orphan, item);
            if (oi >= 0) {
                orphan[oi].parked = true;
                orphan[oi].note = (item && item.scope) ? OTHER_SCOPE_NOTE : NO_SCOPE_NOTE;
                save(orphan);
            }
            busy = false;
            render();
            return;
        }
        var pairs = [];
        for (var k in item.fields) {
            if (Object.prototype.hasOwnProperty.call(item.fields, k)) {
                pairs.push(encodeURIComponent(k) + '=' + encodeURIComponent(item.fields[k]));
            }
        }
        // 仅重放时回传日期守卫字段；正常在线提交不带该字段，服务端行为保持不变
        pairs.push('queued_for=' + encodeURIComponent(item.queued_for || ''));

        var xhr = new XMLHttpRequest();
        xhr.open('POST', SUBMIT_URL, true);
        xhr.withCredentials = true;
        xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded; charset=UTF-8');
        xhr.timeout = RETRY_WINDOW_MS;

        function settle(mutate) {
            var cur = load();
            var at = indexOf(cur, item);
            if (at >= 0) mutate(cur, at);
            save(cur);
            busy = false;
            render();
            if (hasPlayable()) replayAll();
        }

        xhr.onload = function () {
            var outcome = classify(xhr.status, xhr.responseURL, xhr.responseText);
            settle(function (cur, at) {
                if (outcome === 'ok' || outcome === 'exists') {
                    cur.splice(at, 1);
                    flashBox(outcome === 'ok' ? '离线记录已补交成功' : '该记录系统中已存在，已移出待传列表',
                             outcome === 'ok' ? 'success' : 'warning');
                } else if (outcome === 'session') {
                    cur[at].note = '登录已过期，重新登录后自动续传';       // 不计次
                } else if (outcome === 'parked') {
                    cur[at].parked = true;
                    cur[at].note = messageOf(xhr.responseText) || '服务端拒绝写入，请核对后手动重传';
                } else {
                    cur[at].tries = (cur[at].tries || 0) + 1;
                    cur[at].next_at = Date.now() + backoffMs(cur[at].tries);
                    if (cur[at].tries >= MAX_TRIES) {
                        cur[at].parked = true;
                        cur[at].note = '多次重传未成功，请联网后手动提交';
                    }
                }
            });
        };
        xhr.onerror = xhr.ontimeout = function () {
            settle(function (cur, at) {
                cur[at].tries = (cur[at].tries || 0) + 1;
                cur[at].next_at = Date.now() + backoffMs(cur[at].tries);
                cur[at].note = '网络不可达，稍后自动重试';
            });
        };
        xhr.send(pairs.join('&'));
    }

    // ── UI：横幅与提示全部用 DOM 构造，不拼 innerHTML ─────────────
    var box = null;

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function button(label, cls, handler) {
        var b = el('button', cls, label);
        b.setAttribute('type', 'button');
        b.addEventListener('click', handler);
        return b;
    }

    var countNode = null;
    var noteNode = null;

    function ensureBox() {
        if (box && box.parentNode) return box;
        box = el('div', 'offline-box');
        box.setAttribute('hidden', 'hidden');
        var head = el('div', 'offline-box-head');
        head.appendChild(el('span', 'offline-box-title', '离线待传'));
        countNode = el('span', 'offline-box-count', '0');
        head.appendChild(countNode);
        noteNode = el('p', 'offline-box-note', '');
        var actions = el('div', 'offline-box-actions');
        actions.appendChild(button('立即重传', 'btn btn-primary btn-sm', function () {
            var list = load();
            for (var i = 0; i < list.length; i++) {
                list[i].parked = false; list[i].tries = 0; list[i].next_at = 0;
            }
            save(list);
            replayAll();
        }));
        actions.appendChild(button('清空待传', 'btn btn-outline btn-sm', function () {
            if (window.confirm('确认丢弃本机 ' + load().length + ' 条未上传记录？此操作不可恢复。')) {
                drop(QUEUE_KEY);
                render();
                flashBox('已清空本机离线待传记录', 'warning');
            }
        }));
        box.appendChild(head);
        box.appendChild(noteNode);
        box.appendChild(actions);
        form.parentNode.insertBefore(box, form.nextSibling);
        return box;
    }

    function render() {
        var list = load();
        var b = ensureBox();
        if (!list.length) { b.setAttribute('hidden', 'hidden'); return; }
        b.removeAttribute('hidden');
        var parked = 0;
        var foreign = 0;
        for (var i = 0; i < list.length; i++) {
            if (list[i].parked) parked++;
            if (!Scope || !Scope.itemMatchesScope(list[i], CURRENT_SCOPE)) foreign++;
        }
        countNode.textContent = String(list.length);
        var text;
        if (isOffline()) {
            text = (navigator.onLine === false ? '当前离线：' : '服务端连不通：') + list.length + ' 条已暂存本机，联网后自动续传（不会重复入账）';
        } else if (foreign) {
            text = foreign + ' 条与当前门店/行业包不符或无归属信息，已暂停自动续传（不会按当前身份补交）';
        } else if (parked) {
            text = parked + ' 条需要你处理：' + (list[0].note || '请核对内容后重传');
        } else {
            text = '正在自动续传…';
        }
        noteNode.textContent = text;
    }

    function flashBox(text, kind) {
        var holder = document.querySelector('.flash-messages');
        if (!holder) return;
        var node = el('div', 'flash flash-' + kind);
        node.setAttribute('role', 'status');
        node.setAttribute('data-flash', kind);
        node.appendChild(el('span', null, text));
        holder.appendChild(node);
        if (kind === 'success') {
            window.setTimeout(function () {
                if (node.parentNode) node.parentNode.removeChild(node);
            }, 5000);
        }
    }

    // ── 接线 ───────────────────────────────────────────────
    form.addEventListener('submit', function (ev) {
        var fields = serialize();
        if (!dimValue(fields)) return;            // 维度未选：交给既有校验，不存垃圾
        if (JSON.stringify(fields).length > ITEM_BYTE_CAP) {
            if (ev) { ev.preventDefault(); }
            flashBox('这条记录内容过长，无法离线暂存，请精简备注后重试', 'danger');
            return;
        }
        var draft = makeItem(fields);
        writeJson(DRAFT_KEY, draft);              // 主干：不论在线与否都先落草稿
        if (isOffline()) {
            ev.preventDefault();                   // 离线/连不通：入队，不发必败请求
            drop(DRAFT_KEY);
            draft.saved_at = Date.now();
            var queued = pushQueue(draft);
            if (queued === true) {
                form.reset();
                render();
                flashBox('已离线暂存，联网后会自动提交', 'success');
            } else if (queued === 'duplicate') {
                flashBox('相同记录已在待传队列中，未重复暂存', 'warning');
            } else {
                flashBox('待传队列已满（上限 ' + MAX_RECORDS + ' 条），请先处理', 'danger');
            }
        }
        // 在线：不拦截，原生提交照常；结局由下次落地页的 reconcileDraft() 判定
    });

    window.addEventListener('online', function () { probeLink(); render(); replayAll(); });
    document.addEventListener('visibilitychange', function () {
        if (!document.hidden) { render(); replayAll(); }
    });

    var migratedCount = migrateLegacy();
    reconcileDraft();
    render();
    probeLink();
    if (hasPlayable()) replayAll();
    if (migratedCount) {
        // 迁移是"动了用户数据"的动作，必须当场说清去了哪里、为什么不再自动发
        flashBox('发现 ' + migratedCount + ' 条旧版离线记录（不带门店/行业包归属），'
                 + '已移入「离线待传」并暂停自动续传', 'warning');
    }
})();
