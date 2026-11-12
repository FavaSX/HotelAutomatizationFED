(function () {
  const cfg = document.getElementById('usersManageConfig');

  function showModal(id) {
    const el = document.getElementById(id);
    if (!el || typeof bootstrap === 'undefined') return;
    bootstrap.Modal.getOrCreateInstance(el).show();
  }

  document.addEventListener('click', (ev) => {
    const editBtn = ev.target.closest('.js-edit-user-btn');
    if (editBtn) {
      const form = document.getElementById('editUserForm');
      const nameLbl = document.getElementById('editUserNameLabel');
      const hintEl = document.getElementById('editSelfRoleHint');
      if (form) {
        form.action = editBtn.dataset.editUrl || '';
        const fn = form.querySelector('[name="first_name"]');
        const ln = form.querySelector('[name="last_name"]');
        const em = form.querySelector('[name="email"]');
        const rl = form.querySelector('[name="role"]');
        const pw1 = form.querySelector('[name="new_password"]');
        const pw2 = form.querySelector('[name="confirm_new_password"]');
        if (fn) fn.value = editBtn.dataset.firstName || '';
        if (ln) ln.value = editBtn.dataset.lastName || '';
        if (em) em.value = editBtn.dataset.email || '';
        if (rl) rl.value = editBtn.dataset.role || 'STANDARD';
        if (pw1) pw1.value = '';
        if (pw2) pw2.value = '';
        if (hintEl) hintEl.classList.toggle('d-none', editBtn.dataset.isSelf !== '1');
      }
      if (nameLbl) nameLbl.textContent = editBtn.dataset.username || '';
      showModal('editUserModal');
      return;
    }

    const delBtn = ev.target.closest('.js-soft-delete-user-btn');
    if (delBtn) {
      const form = document.getElementById('softDeleteUserForm');
      const nameLbl = document.getElementById('softDeleteUserNameLabel');
      const reasonInput = document.getElementById('softDeleteReasonInput');
      if (form) form.action = delBtn.dataset.deleteUrl || '';
      if (nameLbl) nameLbl.textContent = delBtn.dataset.username || '';
      if (reasonInput) reasonInput.value = '';
      showModal('softDeleteUserModal');
      return;
    }

    const restoreBtn = ev.target.closest('.js-restore-user-btn');
    if (restoreBtn) {
      const form = document.getElementById('restoreUserForm');
      const nameLbl = document.getElementById('restoreUserNameLabel');
      const reasonInput = document.getElementById('restoreReasonInput');
      if (form) form.action = restoreBtn.dataset.restoreUrl || '';
      if (nameLbl) nameLbl.textContent = restoreBtn.dataset.username || '';
      if (reasonInput) reasonInput.value = '';
      showModal('restoreUserModal');
    }
  });

  if (cfg) {
    if (cfg.dataset.openCreate === '1') {
      showModal('createUserModal');
    } else if (cfg.dataset.openEdit === '1') {
      showModal('editUserModal');
    }
  }
})();
