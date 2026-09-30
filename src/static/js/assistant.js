// Admin assistant: AJAX ask and decision trace insertion.
(function () {
    var form = document.getElementById('assistantForm');
    var log = document.getElementById('assistantLog');
    var input = form ? form.querySelector('input[name="ask"]') : null;
    var btn = form ? form.querySelector('button[type="submit"]') : null;
    if (!form || !input || !log) return;

    var degradedText = form.getAttribute('data-degraded-text') || '';

    function appendBubble(cls, text) {
        var b = document.createElement('div');
        b.className = 'bubble ' + cls;
        b.textContent = text;
        log.appendChild(b);
        log.scrollIntoView({ behavior: 'smooth', block: 'end' });
        return b;
    }
    function showInlineError(bubble, text) {
        bubble.innerHTML = '';
        var err = document.createElement('div');
        err.className = 'record-note';
        err.textContent = text;
        bubble.appendChild(err);
    }

    function send(text) {
        input.disabled = true;
        btn.disabled = true;
        btn.textContent = '思考中…';
        appendBubble('staff', text);
        var typing = appendBubble('customer', '▪▪▪ 正在生成回答…');

        var fd = new FormData(form);
        fd.set('ask', text);
        fetch(form.action, {
            method: 'POST',
            body: fd,
            headers: { 'X-Requested-With': 'fetch' }
        }).then(function (r) { return r.json(); })
          .then(function (j) {
              if (j && j.ok) {
                  typing.textContent = j.reply;
                  if (j.trace_html) {
                      typing.insertAdjacentHTML('afterend', j.trace_html);
                  }
              } else {
                  showInlineError(typing, (j && j.reply) || degradedText);
              }
          })
          .catch(function () {
              showInlineError(typing, '网络开了个小差，请稍后重试。');
          })
          .finally(function () {
              input.value = '';
              input.disabled = false;
              btn.disabled = false;
              btn.textContent = '提问';
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

    document.querySelectorAll('.quick-questions button[data-quick]').forEach(function (b) {
        b.addEventListener('click', function () {
            send(b.getAttribute('data-quick'));
        });
    });
})();
