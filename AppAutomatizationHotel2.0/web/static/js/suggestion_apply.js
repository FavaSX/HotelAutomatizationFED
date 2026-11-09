/**
 * Step 2 (preview): apply the suggested ERP correction of a voucher before saving the closing.
 * Used by the CLP and USD suggestion cards. The page sets data-preview-correction-url on <body>.
 * The accountant may edit the suggested value. After applying, the page reloads with the voucher
 * crossed again (CLP) or marked to be applied when the closing is saved (USD / sale in database).
 */

const PROPOSAL_FIELD_LABELS = {
  amount: 'Monto',
  authorization_code: 'Código de autorización',
  transaction_date: 'Fecha',
  payment_code: 'Medio de pago',
  document_number: 'Documento'
};
const PREVIEW_NOTICE_KEY = 'previewCorrectionNotice';

function escapeProposal(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function proposalInputValue(pc) {
  if (pc.field === 'amount') {
    const num = Number(pc.new);
    return Number.isInteger(num) ? String(num) : String(pc.new);
  }
  return String(pc.new ?? '');
}

function renderProposalBlock(voucher) {
  const pc = (voucher.suggestion || {}).proposed_change;
  if (!pc) return '';
  if (pc.status) {
    return (
      `<div class="px-3 py-2 border-top small proposal-block proposal-applied">` +
        `<i class="bi bi-check2-circle me-1"></i><strong>Corrección aplicada:</strong> ` +
        `<span class="font-monospace">${escapeProposal(pc.label)}</span>. ` +
        `Se guarda en el ERP, con historial, al confirmar el cierre.` +
      `</div>`
    );
  }
  const id = `proposal-${voucher.tbk_index}`;
  const label = PROPOSAL_FIELD_LABELS[pc.field] || pc.field;
  const input = pc.field === 'transaction_date'
    ? `<input type="date" id="${id}" class="form-control form-control-sm" value="${escapeProposal(pc.new)}">`
    : `<input id="${id}" class="form-control form-control-sm font-monospace" value="${escapeProposal(proposalInputValue(pc))}"${pc.field === 'amount' ? ' inputmode="decimal"' : ''}>`;
  return (
    `<div class="px-3 py-2 border-top proposal-block">` +
      `<div class="small fw-bold mb-1"><i class="bi bi-magic me-1"></i>Corrección sugerida en el ERP</div>` +
      `<div class="small font-monospace mb-2">${escapeProposal(pc.label)}</div>` +
      `<div class="d-flex flex-wrap align-items-end gap-2">` +
        `<div><label class="form-label small mb-0" for="${id}">${escapeProposal(label)} nuevo (puedes editarlo)</label>${input}</div>` +
        `<button type="button" class="btn btn-success btn-sm rounded-pill px-3 fw-semibold" ` +
          `onclick="applyPreviewCorrection(${Number(voucher.tbk_index)}, '${id}', this)">` +
          `<i class="bi bi-check2 me-1"></i>Aplicar corrección</button>` +
      `</div>` +
      `<div class="small text-danger mt-1" id="${id}-error" role="alert" hidden></div>` +
    `</div>`
  );
}

async function applyPreviewCorrection(tbkIndex, inputId, btn) {
  const url = document.body.dataset.previewCorrectionUrl;
  const tokenEl = document.querySelector('[name=csrfmiddlewaretoken]');
  const errEl = document.getElementById(`${inputId}-error`);
  const inputEl = document.getElementById(inputId);
  btn.disabled = true;
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
    let notice;
    if (data.applied_on_save) {
      notice = 'Corrección aplicada. El voucher se vuelve a cruzar cuando confirmes el cierre.';
    } else if (data.matched) {
      notice = 'Corrección aplicada. El voucher ahora cuadra y pasó a Cuadrados.';
    } else {
      notice = 'Corrección aplicada, pero el voucher todavía no cuadra.';
    }
    try { sessionStorage.setItem(PREVIEW_NOTICE_KEY, notice); } catch (e) { /* storage blocked: no notice */ }
    window.location.reload();
  } catch (error) {
    if (errEl) {
      errEl.textContent = error.message;
      errEl.hidden = false;
    }
    btn.disabled = false;
  }
}

document.addEventListener('DOMContentLoaded', () => {
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
