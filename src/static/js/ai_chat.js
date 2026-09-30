// Scenario chat: AJAX send, typing feedback, mood and negotiation panels.
(function () {
    var form = document.getElementById('chatForm');
    if (!form) return;
    var input = form.querySelector('input[name="msg"]');
    var btn = form.querySelector('button[type="submit"]');
    var log = document.querySelector('.chat-log');
    if (!input || !log) return;

    function addTypingBubble() {
        var typing = document.createElement('div');
        typing.className = 'bubble customer';
        typing.innerHTML = '<span class="typing-dots"><i></i><i></i><i></i></span>'
            + '<div class="skeleton skeleton-line"></div>'
            + '<div class="skeleton skeleton-line skeleton-narrow"></div>';
        log.appendChild(typing);
        log.scrollIntoView({ behavior: 'smooth', block: 'end' });
        return typing;
    }

    function showInlineError(bubble, text, retryFn) {
        bubble.innerHTML = '';
        var err = document.createElement('div');
        err.className = 'record-note';
        err.textContent = text;
        bubble.appendChild(err);
        var retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'btn-inline';
        retry.textContent = '↻ 重试';
        retry.addEventListener('click', retryFn);
        bubble.appendChild(retry);
    }

    function updateMood(m) {
        var bar = document.getElementById('moodBar');
        if (!bar || !m || !m.mood) return;
        var p = Number(m.patience || 0);
        bar.hidden = false;
        var badge = document.getElementById('moodBadge');
        badge.textContent = m.label || '';
        badge.setAttribute('data-mood', m.mood);
        var fill = document.getElementById('moodFill');
        if (fill) {
            fill.style.setProperty('--pct', p + '%');
            fill.setAttribute('data-level', p < 35 ? 'low' : (p < 60 ? 'mid' : 'high'));
        }
        var num = document.getElementById('moodPatience');
        if (num) num.textContent = String(Math.round(p));
    }

    var NEGO_LABEL = { deal: '已成交', lost: '顾客流失', pending: '谈判进行中' };
    function updateNego(n) {
        var panel = document.getElementById('negoPanel');
        if (!panel || !n) return;
        panel.hidden = false;
        var range = n.price_range || [0, 0];
        var low = document.getElementById('negoRangeLow');
        var high = document.getElementById('negoRangeHigh');
        if (low) low.textContent = String(range[0]);
        if (high) high.textContent = String(range[1]);
        var price = document.getElementById('negoPrice');
        if (price) price.textContent = String(n.current_price);
        var fill = document.getElementById('negoFill');
        var pct = Number(n.progress || 0);
        if (fill) {
            fill.style.setProperty('--pct', pct + '%');
            fill.setAttribute('data-level', pct < 34 ? 'low' : (pct < 67 ? 'mid' : 'high'));
        }
        var pctEl = document.getElementById('negoPct');
        if (pctEl) pctEl.textContent = String(Math.round(pct));
        var v = n.verdict || {};
        var st = document.getElementById('negoStatus');
        if (st) {
            var label = NEGO_LABEL[v.result] || '';
            st.textContent = (label ? label + '：' : '') +
                (v.reason || '谈判进行中，顾客仍在观望');
            st.setAttribute('data-result', v.result || '');
        }
    }

    function send(text) {
        input.disabled = true;
        btn.disabled = true;
        btn.textContent = '思考中…';
        var staff = document.createElement('div');
        staff.className = 'bubble staff';
        staff.textContent = text;
        log.appendChild(staff);
        var typing = addTypingBubble();
        log.scrollIntoView({ behavior: 'smooth', block: 'end' });

        var fd = new FormData(form);
        fd.set('msg', text);
        fetch(form.action, {
            method: 'POST',
            body: fd,
            headers: { 'X-Requested-With': 'fetch' }
        }).then(function (r) { return r.json(); })
          .then(function (j) {
              if (j && j.ok) {
                  typing.innerHTML = '';
                  typing.textContent = j.reply;
                  if (j.mood) updateMood(j.mood);
                  if (j.negotiation) updateNego(j.negotiation);
                  if (j.tip) {
                      var tip = document.createElement('div');
                      tip.className = 'tip-inline';
                      tip.textContent = j.tip;
                      log.insertBefore(tip, typing.nextSibling);
                  }
              } else {
                  showInlineError(typing, '网络开了个小差，发送失败。', function () {
                      send(text);
                  });
              }
          })
          .catch(function () {
              showInlineError(typing, '网络开了个小差，发送失败。', function () {
                  send(text);
              });
          })
          .finally(function () {
              if (typing.isConnected && !typing.querySelector('.btn-inline')) {
                  input.value = '';
                  input.disabled = false;
                  btn.disabled = false;
                  btn.textContent = '发送';
                  log.scrollIntoView({ behavior: 'smooth', block: 'end' });
              }
          });
    }

    form.addEventListener('submit', function (e) {
        var text = input.value.trim();
        if (!text) {
            e.preventDefault();
            return;
        }
        e.preventDefault();
        send(text);
    });
})();

(function () {
    var log = document.querySelector('.chat-log');
    var btn = document.querySelector('.back-to-bottom');
    if (!log || !btn) return;
    var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    function nearBottom() {
        return window.innerHeight + window.scrollY
            >= document.documentElement.scrollHeight - 160;
    }
    function refresh() {
        var show = window.scrollY > 600 && !nearBottom();
        btn.classList.toggle('show', show);
    }
    btn.addEventListener('click', function () {
        window.scrollTo({
            top: document.documentElement.scrollHeight,
            behavior: reduced ? 'auto' : 'smooth'
        });
    });
    window.addEventListener('scroll', refresh, { passive: true });
    refresh();
})();
