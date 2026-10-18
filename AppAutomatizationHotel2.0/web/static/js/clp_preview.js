/**
 * CLP Preview Level 2 Suggestions Modal Logic (clp_preview.html)
 * Sorting, pagination, filters and the 2-step commit come from usd_preview.js (loaded first).
 * Supports opening the Level 2 Assistant modal for:
 *  - 'orphans' (En TBK pero NO en Borderó)
 *  - 'discrepancies' (Con Diferencia)
 *  - 'out_of_period' (Fuera de Período con sugerencia en Borderó histórico)
 */

const CLP_MODAL_SECTIONS = {
  orphans: {
    scriptId: 'jsonClpOrphansData',
    title: 'Sugerencias Asistidas — Vouchers en TBK pero NO en Borderó'
  },
  discrepancies: {
    scriptId: 'jsonClpDiscrepanciesData',
    title: 'Sugerencias Asistidas — Vouchers con Diferencia (TBK vs. Borderó)'
  },
  out_of_period: {
    scriptId: 'jsonClpOutOfPeriodData',
    title: 'Sugerencias Asistidas — Vouchers Fuera de Período (Borderó Histórico)'
  }
};

let clpModalWithItems = [];
let clpModalWithoutItems = [];
let clpModalWithPage = 1;
let clpModalWithoutPage = 1;
const CLP_MODAL_WITH_PAGE_SIZE = 5;
const CLP_MODAL_WITHOUT_PAGE_SIZE = 10;

function formatClpClient(val) {
  const num = Math.round(Number(val) || 0);
  return '$' + num.toLocaleString('es-CL');
}

function renderClpKv(label, value, tone) {
  const strVal = String(value ?? '').trim() || '—';
  const toneClass = tone === 'diff' ? 'suggestion-kv-row suggestion-kv-diff' : 'suggestion-kv-row';
  const icon = tone === 'match'
    ? '<i class="bi bi-check2 text-success fw-bold ms-1" title="Coincide entre archivos"></i>'
    : (tone === 'diff' ? '<i class="bi bi-exclamation-triangle-fill ms-1" title="Dato con diferencia"></i>' : '');
  return (
    `<div class="${toneClass}">` +
      `<span class="suggestion-kv-label">${escapeHtmlStr(label)}</span>` +
      `<span class="suggestion-kv-val" title="${escapeHtmlStr(strVal)}">${escapeHtmlStr(strVal)}${icon}</span>` +
    `</div>`
  );
}

function renderClpSuggestionCard(voucher, idx) {
  const sug = voucher.suggestion || {};
  const erp = sug.erp_row || {};
  const files = sug.files_involved || {};
  const sameAmount = Math.abs(Number(voucher.tbk_amount) - Number(erp.amount)) < 1;
  const sameDate = voucher.sale_date === erp.transaction_date;
  const sameCode = String(voucher.authorization_code || '').replace(/^0+/, '') === String(erp.authorization_code || '').replace(/^0+/, '');
  const reasons = (Array.isArray(sug.reasons) ? sug.reasons : [])
    .map(r => `<span class="reason-chip reason-${escapeHtmlStr(r.tone)}"><i class="bi ${escapeHtmlStr(r.icon)}"></i> ${escapeHtmlStr(r.label)}</span>`)
    .join(' ');

  const colTbk = (
    renderClpKv('Voucher', voucher.authorization_code, sameCode ? 'match' : 'diff') +
    renderClpKv('Monto TBK', formatClpClient(voucher.tbk_amount), sameAmount ? 'match' : 'diff') +
    renderClpKv('Fecha venta', voucher.sale_date, sameDate ? 'match' : '') +
    renderClpKv('Medio / Marca', `${voucher.modality_label} · ${voucher.card_name}`, 'match') +
    renderClpKv('Tarjeta / Cuota', `${voucher.masked_card || '—'} (${voucher.installment_number || 'S/C'})`, '') +
    renderClpKv('Local', voucher.merchant_location, '')
  );
  const colErp = (
    renderClpKv('Fila Borderó', `Fila ${erp.excel_row || '—'} · Cta. ${erp.account_code || '—'} · Fact. ${erp.invoice_number || '—'}`, '') +
    renderClpKv('Código ERP', erp.authorization_code, sameCode ? 'match' : 'diff') +
    renderClpKv('Monto ERP', formatClpClient(erp.amount), sameAmount ? 'match' : 'diff') +
    renderClpKv('Fecha / Hora', `${erp.transaction_date} ${erp.transaction_time || ''}`, sameDate ? 'match' : '') +
    renderClpKv('Medio ERP', erp.payment_code, 'match') +
    renderClpKv('Huésped / Hab.', `${erp.guest_name} · Hab. ${erp.room_number}`, '') +
    renderClpKv('Registró', erp.cashier_username, '')
  );

  return (
    `<div class="rounded-3 border overflow-hidden shadow-sm">` +
      `<div class="px-3 py-2 border-bottom d-flex flex-wrap justify-content-between align-items-center gap-2 suggestion-manual-box rounded-0">` +
        `<div class="d-flex flex-wrap align-items-center gap-2 small">` +
          `<span class="fw-bold font-monospace">#${idx + 1} · Voucher ${escapeHtmlStr(voucher.authorization_code)}</span>` +
          `<span class="text-muted">·</span><span class="fw-semibold">${escapeHtmlStr(voucher.modality_label)}</span>` +
          `<span class="text-muted">·</span><span class="fw-bold">${escapeHtmlStr(formatClpClient(voucher.tbk_amount))}</span>` +
        `</div>` +
        `<span class="suggestion-confidence ${sug.confidence === 'ALTA' ? 'suggestion-confidence-alta' : ''}">Confianza ${escapeHtmlStr(sug.confidence)}</span>` +
      `</div>` +
      `<div class="suggestion-2col-grid">` +
        `<div class="suggestion-col-box suggestion-col-tbk">` +
          `<div class="suggestion-col-title">1. Transbank (${escapeHtmlStr(files.tbk || 'TBK')})</div>${colTbk}` +
        `</div>` +
        `<div class="suggestion-col-box">` +
          `<div class="suggestion-col-title">2. Borderó ERP (${escapeHtmlStr(files.erp || 'BORDERO')})</div>${colErp}` +
        `</div>` +
      `</div>` +
      `<div class="px-3 py-2 border-top small">` +
        `<div class="d-flex flex-wrap gap-1 mb-2">${reasons}</div>` +
        `<div class="text-muted">${escapeHtmlStr(sug.manual_check_note || '')}</div>` +
        `<div class="mt-1 fw-semibold"><i class="bi bi-arrow-right-circle me-1"></i>Acción sugerida: ${escapeHtmlStr(sug.action || '')}</div>` +
      `</div>` +
    `</div>`
  );
}

function renderClpModalWithPage() {
  const listEl = document.getElementById('clpSugWithList');
  const pagEl = document.getElementById('clpSugWithPagination');
  if (!listEl) return;

  if (!clpModalWithItems.length) {
    listEl.innerHTML = '<div class="p-3 rounded-3 border text-muted small text-center"><i class="bi bi-info-circle me-1"></i>No se detectaron sugerencias asistidas para esta sección.</div>';
    if (pagEl) pagEl.innerHTML = '';
    return;
  }

  const totalPages = Math.max(1, Math.ceil(clpModalWithItems.length / CLP_MODAL_WITH_PAGE_SIZE));
  clpModalWithPage = Math.max(1, Math.min(clpModalWithPage, totalPages));
  const start = (clpModalWithPage - 1) * CLP_MODAL_WITH_PAGE_SIZE;
  const slice = clpModalWithItems.slice(start, start + CLP_MODAL_WITH_PAGE_SIZE);

  listEl.innerHTML = slice.map((v, i) => renderClpSuggestionCard(v, start + i)).join('');
  if (pagEl) {
    pagEl.innerHTML = totalPages > 1
      ? `<div class="d-flex justify-content-between align-items-center pt-2">` +
          `<span class="small text-muted">Mostrando ${start + 1}–${start + slice.length} de ${clpModalWithItems.length} sugerencias</span>` +
          `<ul class="pagination pagination-sm mb-0">${buildPaginationItemsHtml(clpModalWithPage, totalPages, (p) => `goToClpModalWithPage(${p})`)}</ul>` +
        `</div>`
      : '';
  }
}

function goToClpModalWithPage(page) {
  clpModalWithPage = page;
  renderClpModalWithPage();
}

function renderClpModalWithoutPage() {
  const listEl = document.getElementById('clpSugWithoutList');
  const pagEl = document.getElementById('clpSugWithoutPagination');
  if (!listEl) return;

  if (!clpModalWithoutItems.length) {
    listEl.innerHTML = '';
    if (pagEl) pagEl.innerHTML = '';
    return;
  }

  const totalPages = Math.max(1, Math.ceil(clpModalWithoutItems.length / CLP_MODAL_WITHOUT_PAGE_SIZE));
  clpModalWithoutPage = Math.max(1, Math.min(clpModalWithoutPage, totalPages));
  const start = (clpModalWithoutPage - 1) * CLP_MODAL_WITHOUT_PAGE_SIZE;
  const slice = clpModalWithoutItems.slice(start, start + CLP_MODAL_WITHOUT_PAGE_SIZE);

  const rows = slice.map((v, idx) => (
    `<tr>` +
      `<td class="ps-3 fw-bold font-monospace text-danger">#${start + idx + 1} · ${escapeHtmlStr(v.authorization_code)}</td>` +
      `<td><span class="badge bg-dark">${escapeHtmlStr(v.modality_label)}</span> <small class="text-muted">${escapeHtmlStr(v.card_name || '')}</small></td>` +
      `<td>${escapeHtmlStr(v.sale_date || '—')}</td>` +
      `<td class="text-end fw-bold">${escapeHtmlStr(formatClpClient(v.tbk_amount))}</td>` +
      `<td class="pe-3 small text-muted"><i class="bi bi-slash-circle text-warning me-1"></i>${escapeHtmlStr(v.action || '')}</td>` +
    `</tr>`
  )).join('');

  listEl.innerHTML = (
    `<h6 class="fw-bold text-uppercase small mb-3"><span class="reason-chip reason-gold"><i class="bi bi-slash-circle"></i> Sin Sugerencia en el Borderó (${clpModalWithoutItems.length})</span></h6>` +
    `<div class="table-responsive rounded-3 border"><table class="table table-hover align-middle mb-0">` +
      `<thead class="table-light small text-uppercase text-muted"><tr>` +
        `<th class="ps-3">Cód. Autorización</th><th>Medio</th><th>Fecha Venta</th><th class="text-end">Monto TBK</th><th class="pe-3">Acción Sugerida</th>` +
      `</tr></thead><tbody>${rows}</tbody>` +
    `</table></div>`
  );

  if (pagEl) {
    pagEl.innerHTML = totalPages > 1
      ? `<div class="d-flex justify-content-between align-items-center pt-2">` +
          `<span class="small text-muted">Mostrando ${start + 1}–${start + slice.length} de ${clpModalWithoutItems.length} casos sin sugerencia</span>` +
          `<ul class="pagination pagination-sm mb-0">${buildPaginationItemsHtml(clpModalWithoutPage, totalPages, (p) => `goToClpModalWithoutPage(${p})`)}</ul>` +
        `</div>`
      : '';
  }
}

function goToClpModalWithoutPage(page) {
  clpModalWithoutPage = page;
  renderClpModalWithoutPage();
}

function openClpSuggestionsModal(sectionKey = 'orphans') {
  const cfg = CLP_MODAL_SECTIONS[sectionKey] || CLP_MODAL_SECTIONS.orphans;
  const scriptEl = document.getElementById(cfg.scriptId);
  if (!scriptEl) return;

  let vouchers = [];
  try {
    vouchers = JSON.parse(scriptEl.textContent) || [];
  } catch (e) {
    console.error('Error parsing CLP suggestions JSON:', e);
    return;
  }

  clpModalWithItems = vouchers.filter(v => Boolean(v.suggestion));
  clpModalWithoutItems = vouchers.filter(v => !v.suggestion);
  clpModalWithPage = 1;
  clpModalWithoutPage = 1;

  const titleEl = document.getElementById('clpSugModalTitle');
  if (titleEl) titleEl.textContent = cfg.title;

  document.getElementById('clpSugStatsBadge').textContent =
    `${clpModalWithItems.length} con sugerencia · ${clpModalWithoutItems.length} sin sugerencia`;

  renderClpModalWithPage();
  renderClpModalWithoutPage();

  const modalEl = document.getElementById('clpSuggestionModal');
  if (modalEl && window.bootstrap) {
    window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
  }
}
