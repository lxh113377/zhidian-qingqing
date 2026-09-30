(function () {
    const root = document.getElementById('decisionCockpit');
    if (!root) return;

    root.addEventListener('click', async function (event) {
        const button = event.target.closest('#decisionRefresh');
        if (!button) return;
        const inner = root.querySelector('#decisionCockpitInner');
        button.disabled = true;
        button.textContent = '正在刷新';
        try {
            const form = new FormData();
            form.append('csrf_token', root.dataset.csrfToken || '');
            form.append('days', root.dataset.days || '30');
            form.append('refresh', '1');
            const response = await fetch(root.dataset.runUrl, {
                method: 'POST',
                body: form,
                headers: {'X-Requested-With': 'XMLHttpRequest'},
                credentials: 'same-origin'
            });
            const data = await response.json();
            if (!response.ok || !data.ok) {
                throw new Error('决策刷新失败');
            }
            inner.innerHTML = data.fragment;
        } catch (error) {
            const note = document.createElement('p');
            note.className = 'decision-error';
            note.textContent = '决策刷新失败，请稍后重试。';
            inner.prepend(note);
        } finally {
            button.disabled = false;
            button.textContent = '刷新决策';
        }
    });
}());
