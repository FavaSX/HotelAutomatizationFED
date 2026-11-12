/**
 * Step 2 (preview): apply the suggested ERP correction of a voucher before saving the closing.
 * The page sets data-preview-correction-url and data-preview-currency on <body>.
 *
 * Applying does NOT reload the page: the card turns into "Aplicada" inside the suggestions modal so the
 * accountant can keep going with the next one. The page reloads once, when the modal is closed, so the
 * counters, tabs and totals show the corrected vouchers.
 */

const PREVIEW_NOTICE_KEY = 'previewCorrectionNotice';
const PREVIEW_SUGGESTION_MODALS = ['clpSuggestionModal', 'suggestionDetailModal'];
let previewCorrectionsApplied = 0;

function previewVoucherLists() {
  const lists = [];
  if (typeof currentClpSugAllVouchers !== 'undefined') lists.push(currentClpSugAllVouchers);
  if (typeof currentSugAllVouchers !== 'undefined') lists.push(currentSugAllVouchers);
  return lists;
}

function findPreviewVoucher(tbkIndex) {
  for (const list of previewVoucherLists()) {
    const found = (list || []).find(v => Number(v.tbk_index) === Number(tbkIndex));
    if (found) return found;
  }
  return null;
}

function applyResultNote(data) {
  if (data.applied_on_save) return 'Aplicada. El voucher se vuelve a cruzar cuando confirmes el cierre.';
  if (data.matched) return 'Aplicada. El voucher ahora cuadra y pasa a Ubicadas.';
  return 'Aplicada, pero el voucher todavía no cuadra.';
}

async function applyPreviewCorrection(tbkIndex, inputId, btn) {
  const url = document.body.dataset.previewCorrectionUrl;
  const currency = document.body.dataset.previewCurrency || 'CLP';
  const tokenEl = document.querySelector('[name=csrfmiddlewaretoken]');
  const errEl = document.getElementById(`${inputId}-error`);
  const inputEl = document.getElementById(inputId);
  const card = btn.closest('.sc-card');
  const originalLabel = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status"></span>Aplicando…';
  if (errEl) errEl.hidden = true;

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'X-CSRFToken': tokenEl ? tokenEl.value : '',
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest'
      },
      body: JSON.stringify({ tbk_index: tbkIndex, value: inputEl ? inputEl.value : '' })
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.success) throw new Error(data.error || 'No se pudo aplicar la corrección.');

    previewCorrectionsApplied += 1;
    const note = applyResultNote(data);
    const voucher = findPreviewVoucher(tbkIndex);
    if (voucher && voucher.suggestion) {
      voucher.suggestion.proposed_change = data.proposal || { ...voucher.suggestion.proposed_change, status: 'applied' };
      voucher._applyNote = note;
    }
    if (card && voucher && typeof renderSuggestionCard === 'function') {
      const indexEl = card.querySelector('.sc-index');
      const index = indexEl ? Number(indexEl.textContent.replace('#', '')) : undefined;
      const wrapper = document.createElement('div');
      wrapper.innerHTML = renderSuggestionCard(voucher, currency, { mode: 'preview', index });
      const fresh = wrapper.firstElementChild;
      card.replaceWith(fresh);
      fresh.classList.add('sc-just-applied');
    } else if (card) {
      btn.innerHTML = '<i class="bi bi-check2-circle me-1"></i>Aplicada';
    }
  } catch (error) {
    if (errEl) {
      errEl.textContent = error.message;
      errEl.hidden = false;
    }
    btn.disabled = false;
    btn.innerHTML = originalLabel;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  // Closing the suggestions modal after applying refreshes the page once, with a summary notice
  PREVIEW_SUGGESTION_MODALS.forEach(id => {
    const modal = document.getElementById(id);
    if (!modal) return;
    modal.addEventListener('hidden.bs.modal', () => {
      if (!previewCorrectionsApplied) return;
      const n = previewCorrectionsApplied;
      try {
        sessionStorage.setItem(PREVIEW_NOTICE_KEY, `${n} corrección${n === 1 ? '' : 'es'} aplicada${n === 1 ? '' : 's'}. El cruce y los totales ya las incluyen; se guardan en el ERP al confirmar el cierre.`);
      } catch (e) { /* storage blocked: no notice */ }
      window.location.reload();
    });
  });

  let notice = null;
  try {
    notice = sessionStorage.getItem(PREVIEW_NOTICE_KEY);
    sessionStorage.removeItem(PREVIEW_NOTICE_KEY);
  } catch (e) { /* storage blocked */ }
  const box = document.getElementById('previewCorrectionNotice');
  if (notice && box) {
    box.querySelector('[data-text]').textContent = notice;
    box.hidden = false;
  }
});
