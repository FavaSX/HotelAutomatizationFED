/**
 * CLP Preview Logic (clp_preview.html)
 * Sorting, filters and the 2-step commit come from usd_preview.js (loaded first).
 * This file renders the Level 2 suggestions modal: Transbank voucher vs. Bordero row, side by side.
 */

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
  const reasons = (Array.isArray(sug.reasons) ? sug.reasons : [])
    .map(r => `<span class="reason-chip reason-${escapeHtmlStr(r.tone)}"><i class="bi ${escapeHtmlStr(r.icon)}"></i> ${escapeHtmlStr(r.label)}</span>`)
    .join(' ');

  const colTbk = (
    renderClpKv('Voucher', voucher.authorization_code, 'diff') +
    renderClpKv('Monto', formatClpClient(voucher.tbk_amount), sameAmount ? 'match' : 'diff') +
    renderClpKv('Fecha venta', voucher.sale_date, sameDate ? 'match' : '') +
    renderClpKv('Medio', `${voucher.modality_label} · ${voucher.card_name}`, 'match') +
    renderClpKv('Tarjeta', voucher.masked_card, '') +
    renderClpKv('Local', voucher.merchant_location, '')
  );
  const colErp = (
    renderClpKv('Fila Borderó', erp.excel_row, '') +
    renderClpKv('Código', erp.authorization_code, 'diff') +
    renderClpKv('Monto', formatClpClient(erp.amount), sameAmount ? 'match' : 'diff') +
    renderClpKv('Fecha', `${erp.transaction_date} ${erp.transaction_time || ''}`, sameDate ? 'match' : '') +
    renderClpKv('Medio', erp.payment_code, 'match') +
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

function openClpSuggestionsModal() {
  const scriptEl = document.getElementById('jsonClpOrphansData');
  if (!scriptEl) return;

  let vouchers = [];
  try {
    vouchers = JSON.parse(scriptEl.textContent) || [];
  } catch (e) {
    console.error('Error parsing CLP suggestions JSON:', e);
    return;
  }
  const withSuggestion = vouchers.filter(v => Boolean(v.suggestion));
  const withoutSuggestion = vouchers.filter(v => !v.suggestion);

  document.getElementById('clpSugStatsBadge').textContent =
    `${withSuggestion.length} con sugerencia · ${withoutSuggestion.length} sin sugerencia`;

  document.getElementById('clpSugWithList').innerHTML = withSuggestion.length
    ? withSuggestion.map(renderClpSuggestionCard).join('')
    : '<div class="p-3 rounded-3 border text-muted small text-center"><i class="bi bi-info-circle me-1"></i>No se detectaron sugerencias en el Borderó.</div>';

  const rows = withoutSuggestion.map((v, idx) => (
    `<tr>` +
      `<td class="ps-3 fw-bold font-monospace text-danger">#${idx + 1} · ${escapeHtmlStr(v.authorization_code)}</td>` +
      `<td><span class="badge bg-dark">${escapeHtmlStr(v.modality_label)}</span></td>` +
      `<td>${escapeHtmlStr(v.sale_date || '—')}</td>` +
      `<td class="text-end fw-bold">${escapeHtmlStr(formatClpClient(v.tbk_amount))}</td>` +
      `<td class="pe-3 small text-muted"><i class="bi bi-slash-circle text-warning me-1"></i>${escapeHtmlStr(v.action || '')}</td>` +
    `</tr>`
  )).join('');
  document.getElementById('clpSugWithoutList').innerHTML = withoutSuggestion.length ? (
    `<h6 class="fw-bold text-uppercase small mb-3"><span class="reason-chip reason-gold"><i class="bi bi-slash-circle"></i> Sin Sugerencia en el Borderó</span></h6>` +
    `<div class="table-responsive rounded-3 border"><table class="table table-hover align-middle mb-0">` +
      `<thead class="table-light small text-uppercase text-muted"><tr>` +
        `<th class="ps-3">Cód. Autorización</th><th>Medio</th><th>Fecha Venta</th><th class="text-end">Monto TBK</th><th class="pe-3">Acción Sugerida</th>` +
      `</tr></thead><tbody>${rows}</tbody>` +
    `</table></div>`
  ) : '';

  const modalEl = document.getElementById('clpSuggestionModal');
  if (modalEl && window.bootstrap) {
    window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
  }
}
