/**
 * Read-only modal viewer for Borderó ERP row modifications ("como era antes vs. ahora").
 * Reused across consolidated tables, per-closing tables, Resultados del Cruce and preview source tables.
 */
(function () {
  if (window.__erpHistoryModalReady) return;
  window.__erpHistoryModalReady = true;

  let cachedHistoryMap = null;

  function getHistoryMap() {
    if (cachedHistoryMap !== null) return cachedHistoryMap;
    const el = document.getElementById('jsonErpHistoryData');
    if (!el) {
      cachedHistoryMap = {};
      return cachedHistoryMap;
    }
    try {
      cachedHistoryMap = JSON.parse(el.textContent || '{}') || {};
    } catch (_) {
      cachedHistoryMap = {};
    }
    return cachedHistoryMap;
  }

  function esc(val) {
    if (typeof escapeHtmlStr === 'function') return escapeHtmlStr(val);
    return String(val ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function renderSummaryField(label, beforeVal, afterVal) {
    const changed = String(beforeVal ?? '') !== String(afterVal ?? '');
    if (!changed) {
      return (
        `<div class="col-6 col-md-3">` +
          `<div class="p-2 rounded-3 border h-100 erp-history-field">` +
            `<div class="small text-muted text-uppercase fw-semibold" style="font-size: 0.68rem;">${esc(label)}</div>` +
            `<div class="font-mono small fw-semibold text-truncate" title="${esc(afterVal)}">${esc(afterVal || '—')}</div>` +
          `</div>` +
        `</div>`
      );
    }
    return (
      `<div class="col-6 col-md-3">` +
        `<div class="p-2 rounded-3 border border-warning h-100 erp-history-field erp-history-field-changed">` +
          `<div class="small text-warning-emphasis text-uppercase fw-bold" style="font-size: 0.68rem;">` +
            `<i class="bi bi-pencil-fill me-1"></i>${esc(label)}` +
          `</div>` +
          `<div class="font-mono small text-muted text-decoration-line-through text-truncate" title="Antes: ${esc(beforeVal)}">${esc(beforeVal || '—')}</div>` +
          `<div class="font-mono small fw-bold text-success text-truncate" title="Ahora: ${esc(afterVal)}">${esc(afterVal || '—')}</div>` +
        `</div>` +
      `</div>`
    );
  }

  function openErpHistoryModal(entry) {
    const modalEl = document.getElementById('erpHistoryModal');
    const titleEl = document.getElementById('erpHistoryModalTitle');
    const subEl = document.getElementById('erpHistoryModalSubtitle');
    const bodyEl = document.getElementById('erpHistoryModalBody');
    if (!modalEl || !bodyEl || !entry) return;

    if (titleEl) titleEl.textContent = entry.title || 'Modificación en Borderó ERP';
    if (subEl) subEl.textContent = entry.subtitle || '';

    const diffRowsHtml = (entry.diff_fields || []).map((df) => (
      `<tr>` +
        `<td class="fw-semibold">${esc(df.field_label)}</td>` +
        `<td><span class="sc-old">${esc(df.before)}</span></td>` +
        `<td class="text-center text-primary"><i class="bi bi-arrow-right"></i></td>` +
        `<td><span class="sc-new">${esc(df.after)}</span></td>` +
      `</tr>`
    )).join('');

    const beforeSum = entry.before_summary || {};
    const curSum = entry.current_summary || {};
    const summaryFields = [
      ['Cuenta', beforeSum.account, curSum.account],
      ['Documento', beforeSum.document, curSum.document],
      ['N° Reserva', beforeSum.reservation_number, curSum.reservation_number],
      ...(Object.prototype.hasOwnProperty.call(curSum, 'room_number')
        ? [['Habitación', beforeSum.room_number, curSum.room_number]]
        : []),
      ['Tipo', beforeSum.room_type, curSum.room_type],
      ...(Object.prototype.hasOwnProperty.call(curSum, 'guest_name')
        ? [['Huésped', beforeSum.guest_name, curSum.guest_name]]
        : []),
      ['Cód. Autorización', beforeSum.auth_code, curSum.auth_code],
      ['Factura', beforeSum.invoice_number, curSum.invoice_number],
      ['Cód. Pago', beforeSum.payment_code, curSum.payment_code],
      ['Fecha', beforeSum.tx_date, curSum.tx_date],
      ['Cajero', beforeSum.cashier, curSum.cashier],
      ['Monto (CLP)', beforeSum.amount, curSum.amount],
    ];
    const snapshotGrid = summaryFields.map(([lbl, b, a]) => renderSummaryField(lbl, b, a)).join('');

    const timelineRowsHtml = (entry.changes || []).map((ch) => {
      const badgeClass = ch.source === 'SUGGESTION' ? 'status-success' : 'status-warning';
      return (
        `<tr>` +
          `<td class="small text-nowrap">${esc(ch.created_at)}<div class="text-muted"><i class="bi bi-person me-1"></i>${esc(ch.username)}</div></td>` +
          `<td class="small fw-semibold">${esc(ch.field_label)}</td>` +
          `<td class="small font-mono">` +
            `<span class="text-muted text-decoration-line-through">${esc(ch.before)}</span>` +
            ` <i class="bi bi-arrow-right text-primary mx-1"></i> ` +
            `<strong>${esc(ch.after)}</strong>` +
          `</td>` +
          `<td class="small"><span class="status-chip ${badgeClass}">${esc(ch.source_label)}</span></td>` +
          `<td class="small text-muted">${esc(ch.reason)}</td>` +
        `</tr>`
      );
    }).join('');

    bodyEl.innerHTML = (
      `<div class="mb-4">` +
        `<div class="d-flex justify-content-between align-items-center mb-2">` +
          `<h6 class="fw-bold mb-0"><i class="bi bi-arrow-left-right me-1 text-warning"></i>Cómo era antes vs. ahora</h6>` +
          `<span class="badge bg-warning-subtle text-warning-emphasis">${(entry.changes || []).length} cambio(s)</span>` +
        `</div>` +
        (diffRowsHtml
          ? `<div class="table-responsive rounded-3 border mb-3">` +
              `<table class="table table-sm align-middle mb-0">` +
                `<thead class="table-light small text-uppercase text-muted">` +
                  `<tr><th>Campo modificado</th><th>Cómo era antes</th><th style="width:2rem;"></th><th>Valor actual</th></tr>` +
                `</thead>` +
                `<tbody>${diffRowsHtml}</tbody>` +
              `</table>` +
            `</div>`
          : '') +
        `<div class="small fw-semibold text-muted mb-2">Contexto completo de la fila (antes vs. actual)</div>` +
        `<div class="row g-2">${snapshotGrid}</div>` +
      `</div>` +
      `<div>` +
        `<h6 class="fw-bold mb-2"><i class="bi bi-journal-check me-1 text-primary"></i>Historial de modificaciones</h6>` +
        `<div class="table-responsive rounded-3 border">` +
          `<table class="table table-sm align-middle mb-0">` +
            `<thead class="table-light small text-uppercase text-muted">` +
              `<tr><th>Fecha / Auditor</th><th>Campo</th><th>Cambio</th><th>Origen</th><th>Motivo</th></tr>` +
            `</thead>` +
            `<tbody>${timelineRowsHtml || '<tr><td colspan="5" class="text-center text-muted py-3">Sin detalle registrado.</td></tr>'}</tbody>` +
          `</table>` +
        `</div>` +
      `</div>`
    );

    if (typeof bootstrap !== 'undefined' && bootstrap.Modal) {
      bootstrap.Modal.getOrCreateInstance(modalEl).show();
    }
  }

  window.openErpHistoryModal = openErpHistoryModal;

  document.addEventListener('click', (event) => {
    const trigger = event.target.closest('.js-erp-history-trigger');
    if (trigger) {
      event.preventDefault();
      event.stopPropagation();
      const entry = getHistoryMap()[trigger.dataset.historyKey];
      if (entry) openErpHistoryModal(entry);
      return;
    }

    const row = event.target.closest('tr.js-erp-history-row');
    if (row && !event.target.closest('a, button, input, select, label, summary')) {
      const entry = getHistoryMap()[row.dataset.historyKey];
      if (entry) openErpHistoryModal(entry);
    }
  });
})();
