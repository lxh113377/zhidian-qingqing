// AI trainer: AJAX ask, knowledge references and feedback.
(function () {
    var form = document.getElementById('trainerForm');
    var log = document.getElementById('trainerLog');
    var input = form ? form.querySelector('input[name="ask"]') : null;
    var btn = form ? form.querySelector('button[type="submit"]') : null;
    if (!form || !input || !log) return;

    var feedbackUrl = form.getAttribute('data-feedback-url') || '';
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

    var refBox = document.getElementById('trainerRefs');
    var refList = document.getElementById('trainerRefList');
    function renderRefs(refs) {
        if (!refBox || !refList) return;
        refList.innerHTML = '';
        if (!refs || !refs.length) {
            refBox.hidden = true;
            return;
        }
        refs.forEach(function (r) {
            var chip = document.createElement('span');
            chip.className = 'kw-chip ref-chip';
            chip.setAttribute('data-domain', r.domain || '');
            chip.setAttribute('data-title', r.title || '');
            chip.textContent = (r.domain || '') + ' · ' + (r.title || '');
            [['有用', '1'], ['没用', '0']].forEach(function (pair) {
                var b = document.createElement('button');
                b.type = 'button';
                b.className = 'ref-btn';
                b.setAttribute('data-useful', pair[1]);
                b.textContent = pair[0];
                b.addEventListener('click', function () {
                    sendFeedback(chip.getAttribute('data-domain'),
                                 chip.getAttribute('data-title'), pair[1], chip);
                });
                chip.appendChild(b);
            });
            refList.appendChild(chip);
        });
        refBox.hidden = false;
    }

    function sendFeedback(domain, title, useful, chip) {
        var fd = new FormData();
        fd.append('csrf_token',
            (form.querySelector('input[name="csrf_token"]') || {}).value || '');
        fd.append('domain', domain);
        fd.append('title', title);
        fd.append('useful', useful);
        fetch(feedbackUrl, {
            method: 'POST',
            body: fd,
            headers: { 'X-Requested-With': 'fetch' }
        }).then(function (r) { return r.json(); })
          .then(function () {
              chip.classList.add('ref-chip-done');
              Array.prototype.forEach.call(chip.querySelectorAll('.ref-btn'), function (b) {
                  b.disabled = true;
              });
          })
          .catch(function () { /* Feedback is best-effort. */ });
    }

    if (refList) {
        refList.querySelectorAll('.ref-chip').forEach(function (chip) {
            chip.querySelectorAll('.ref-btn').forEach(function (b) {
                b.addEventListener('click', function () {
                    sendFeedback(chip.getAttribute('data-domain'),
                                 chip.getAttribute('data-title'),
                                 b.getAttribute('data-useful'), chip);
                });
            });
        });
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
                  renderRefs(j.refs);
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
