/**
 * Read-only source tables viewer for CLP & USD reconciliation previews.
 * Renders Borderó ERP, Cartola Bancaria, Posición por Clientes (USD) and Transbank Detallado
 * from #jsonSourceTablesData with client-side search, filtering and pagination.
 * Strictly read-only: no create, edit or delete actions exist here.
 */
(function () {
  const dataEl = document.getElementById('jsonSourceTablesData');
  if (!dataEl) return;

  let payload = {};
  try {
    payload = JSON.parse(dataEl.textContent || '{}');
  } catch (_) {
    return;
  }

  const PAGE_SIZE = 15;
  let activeTab = 'erp';
  let currentPage = 1;

  const theadEl = document.getElementById('srcThead');
  const tbodyEl = document.getElementById('srcTbody');
  const searchEl = document.getElementById('srcSearchInput');
  const clearBtn = document.getElementById('btnClearSrcSearch');
  const statusEl = document.getElementById('srcStatusFilter');
  const cardEl = document.getElementById('srcCardFilter');
  const visibleEl = document.getElementById('srcVisibleCount');
  const rangeEl = document.getElementById('srcPageRange');
  const totalEl = document.getElementById('srcPageTotal');
  const navListEl = document.getElementById('srcPaginationList');
  const collapseEl = document.getElementById('sourceTablesCollapse');
  const toggleBtn = document.getElementById('btnToggleSourceTables');

  const esc = typeof escapeHtmlStr === 'function'
    ? escapeHtmlStr
    : (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  const fmtClp = (v) => {
    const n = Math.round(Number(v) || 0);
    return (n < 0 ? '-$' : '$') + Math.abs(n).toLocaleString('es-CL');
  };

  const fmtUsd = (v) => {
    const n = Number(v) || 0;
    return (n < 0 ? '-US$ ' : 'US$ ') + Math.abs(n).toLocaleString('es-CL', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  };

  const fmtMoney = (v) => (payload.currency === 'USD' ? fmtUsd(v) : fmtClp(v));

  const TAB_STATUS_OPTIONS = {
    erp: [
      ['ALL', 'Todos los estados'],
      ['unlinked', 'Solo en ERP (sin TBK)'],
      ['matched', 'Conciliado'],
      ['diff', 'Con diferencia / otro'],
      ['modified', 'Modificadas (ver antes)'],
    ],
    bank: [
      ['ALL', 'Todos los movimientos'],
      ['matched', 'Abono cuadrado'],
      ['tbk', 'Movimiento TBK'],
      ['other', 'Otro movimiento'],
    ],
    operator: [
      ['ALL', 'Todos'],
      ['used', 'Usado en cruce'],
      ['unused', 'Sin usar en cruce'],
    ],
    tbk: [
      ['ALL', 'Todos los estados'],
      ['matched', 'Conciliado'],
      ['pending', 'Pendiente'],
    ],
  };

  const TAB_HEADERS = {
    erp: '<tr><th class="ps-4 text-nowrap">#</th><th class="text-nowrap">Cuenta</th><th class="text-nowrap">Documento</th><th class="text-nowrap">N° Reserva</th><th class="text-nowrap">Tipo</th><th class="text-nowrap">Cód. Autorización</th><th class="text-nowrap">Factura</th><th class="text-nowrap">Cód. Pago</th><th class="text-nowrap">Tarjeta</th><th class="text-nowrap">Fecha</th><th class="text-nowrap">Hora</th><th class="text-nowrap">Cajero</th><th class="text-end text-nowrap">Monto (CLP)</th><th class="text-nowrap">Moneda</th><th class="text-nowrap">Origen</th><th class="pe-4 text-nowrap">Cruce TBK</th></tr>',
    bank: '<tr><th class="ps-4">#</th><th>Fecha</th><th>Descripción</th><th>Sucursal / Canal</th><th>N° Doc.</th><th class="text-end">Cargo</th><th class="text-end">Abono</th><th class="text-end">Saldo</th><th class="pe-4">Clasificación</th></tr>',
    operator: '<tr><th class="ps-4">#</th><th>Franquicia</th><th>Documento (PC)</th><th>Parcela</th><th>Cód. Auth vinculado</th><th class="text-end">Otra Moneda (US$)</th><th class="text-end">Saldo (CLP)</th><th class="text-end">Saldo Corregido</th><th class="pe-4">Estado</th></tr>',
    tbk: '<tr><th class="ps-4 text-nowrap">#</th><th class="text-nowrap">Cód. Autorización</th><th class="text-nowrap">Modalidad</th><th class="text-nowrap">Tarjeta</th><th class="text-nowrap">Fecha Venta</th><th class="text-nowrap">Tarjeta Enmascarada</th><th class="text-nowrap">Cód. Comercio</th><th class="text-nowrap">Comercio / Local</th><th class="text-nowrap">Boleta</th><th class="text-nowrap">TID</th><th class="text-nowrap">Tipo Cuota</th><th class="text-nowrap">N° Cuota</th><th class="text-nowrap">Moneda</th><th class="text-end text-nowrap">Venta</th><th class="text-end text-nowrap">Comisión TBK</th><th class="text-end text-nowrap">IVA Com.</th><th class="text-end text-nowrap">Abono Neto</th><th class="pe-4 text-nowrap">Estado</th></tr>',
  };

  function statusToneClass(code) {
    if (code === 'matched' || code === 'used') return 'status-success';
    if (code === 'diff' || code === 'tbk' || code === 'pending') return 'status-warning';
    if (code === 'unlinked') return 'status-danger';
    return 'status-neutral';
  }

  function populateFiltersForTab(tab) {
    if (statusEl) {
      statusEl.innerHTML = (TAB_STATUS_OPTIONS[tab] || []).map(
        ([val, lbl]) => `<option value="${esc(val)}">${esc(lbl)}</option>`
      ).join('');
    }
    if (cardEl) {
      cardEl.classList.toggle('d-none', tab === 'bank');
      cardEl.value = 'ALL';
    }
  }

  function filterRows() {
    const rows = Array.isArray(payload[activeTab]) ? payload[activeTab] : [];
    const query = (searchEl ? searchEl.value : '').trim().toLowerCase();
    const status = statusEl ? statusEl.value : 'ALL';
    const card = cardEl ? cardEl.value : 'ALL';

    return rows.filter((r) => {
      if (status !== 'ALL') {
        if (activeTab === 'erp') {
          if (status === 'modified') {
            if (!r.is_modified) return false;
          } else if (r.match_status !== status) {
            return false;
          }
        }
        if ((activeTab === 'bank' || activeTab === 'operator') && r.status_code !== status) return false;
        if (activeTab === 'tbk') {
          if (status === 'matched' && !r.reconciled) return false;
          if (status === 'pending' && r.reconciled) return false;
        }
      }
      if (card !== 'ALL' && activeTab !== 'bank') {
        const c = String(r.card_code || '').toUpperCase();
        if (card === 'REDCOMPRA') {
          if (c && c !== 'RC' && c !== 'DEBIT' && c !== 'PREPAID') return false;
        } else if (c !== card) {
          return false;
        }
      }
      if (query) {
        const hay = Object.values(r).filter((v) => v === null || typeof v !== 'object').join(' ').toLowerCase();
        if (!hay.includes(query)) return false;
      }
      return true;
    });
  }

  function renderRowHtml(r) {
    if (activeTab === 'erp') {
      const tone = statusToneClass(r.match_status);
      const isMod = Boolean(r.is_modified && r.history);
      const trAttrs = isMod
        ? ` class="erp-row-modified js-preview-erp-row" data-erp-index="${r.index}" title="Fila modificada: clic para ver cómo era antes"`
        : '';
      const modChip = isMod
        ? `<button type="button" class="status-chip status-warning erp-modified-chip js-preview-erp-history" data-erp-index="${r.index}" title="Ver qué cambió y cómo era antes"><i class="bi bi-clock-history"></i>Modificada${r.modifications_count > 1 ? ' (' + r.modifications_count + ')' : ''}</button>`
        : '';
      const payBadge = r.payment_code && r.payment_code !== '—'
        ? `<span class="badge bg-secondary-subtle text-secondary-emphasis">${esc(r.payment_code)}</span>`
        : '—';
      const curUpper = String(r.source_currency || payload.currency || '—').toUpperCase();
      const curTone = curUpper === 'USD' ? 'status-neutral' : 'status-success';
      const timeTxt = r.transaction_time ? esc(String(r.transaction_time).slice(0, 5)) : '—';
      return `<tr${trAttrs}>
        <td class="ps-4 text-muted small">${r.index}</td>
        <td class="font-mono fw-semibold text-nowrap">${esc(r.account_code)}</td>
        <td class="font-mono text-nowrap">${esc(r.document_number)}</td>
        <td class="font-mono small text-nowrap">${esc(r.reservation_number || '—')}</td>
        <td class="small text-nowrap">${esc(r.room_type || '—')}</td>
        <td class="font-mono text-nowrap">${esc(r.authorization_code)}</td>
        <td class="font-mono small text-nowrap">${esc(r.invoice_number)}</td>
        <td class="small text-nowrap">${payBadge}</td>
        <td class="small text-nowrap">${esc(r.card_code)}</td>
        <td class="small text-nowrap">${esc(r.transaction_date)}</td>
        <td class="small text-muted text-nowrap">${timeTxt}</td>
        <td class="small font-mono text-nowrap">${esc(r.cashier_username)}</td>
        <td class="text-end fw-semibold text-nowrap">${fmtClp(r.amount)}</td>
        <td class="small text-nowrap"><span class="status-chip ${curTone}">${esc(curUpper)}</span></td>
        <td class="small"><div class="d-flex flex-wrap align-items-center gap-1"><span class="status-chip status-neutral">${esc(r.origin_label)}</span>${modChip}</div></td>
        <td class="pe-4"><span class="status-chip ${tone}">${esc(r.match_label)}</span></td>
      </tr>`;
    }
    if (activeTab === 'bank') {
      const tone = statusToneClass(r.status_code);
      return `<tr>
        <td class="ps-4 text-muted small">${r.index}</td>
        <td class="small text-nowrap">${esc(r.movement_date)}</td>
        <td class="small">${esc(r.description)}</td>
        <td class="small text-muted">${esc(r.branch_or_channel)}</td>
        <td class="small font-mono">${esc(r.document_number)}</td>
        <td class="text-end small text-nowrap">${r.charge_amount ? fmtMoney(r.charge_amount) : '—'}</td>
        <td class="text-end fw-semibold text-nowrap">${r.deposit_amount ? fmtMoney(r.deposit_amount) : '—'}</td>
        <td class="text-end small text-nowrap">${fmtMoney(r.balance_amount)}</td>
        <td class="pe-4"><span class="status-chip ${tone}">${esc(r.status_label)}</span></td>
      </tr>`;
    }
    if (activeTab === 'operator') {
      const tone = statusToneClass(r.status_code);
      return `<tr>
        <td class="ps-4 text-muted small">${r.index}</td>
        <td><span class="badge bg-dark">${esc(r.card_code)}</span></td>
        <td class="font-mono fw-semibold">${esc(r.document_number)}</td>
        <td class="font-mono small">${esc(r.sequence_number)}</td>
        <td class="font-mono">${esc(r.authorization_code)}</td>
        <td class="text-end fw-semibold text-nowrap">${fmtUsd(r.foreign_currency_amount)}</td>
        <td class="text-end text-nowrap">${fmtClp(r.balance_amount)}</td>
        <td class="text-end text-nowrap">${fmtClp(r.corrected_balance_amount)}</td>
        <td class="pe-4"><span class="status-chip ${tone}">${esc(r.status_label)}</span></td>
      </tr>`;
    }
    const tbkTone = r.reconciled ? 'status-success' : 'status-warning';
    const tbkLabel = r.reconciled ? 'Conciliado' : 'Pendiente';
    const tbkCur = String(r.currency || payload.currency || '—').toUpperCase();
    const tbkCurTone = tbkCur === 'USD' ? 'status-neutral' : 'status-success';
    return `<tr>
      <td class="ps-4 text-muted small">${r.index}</td>
      <td class="font-mono fw-semibold text-nowrap">${esc(r.authorization_code)}</td>
      <td class="small text-nowrap">${esc(r.modality_label)}</td>
      <td class="small text-nowrap">${esc(r.card_code)}</td>
      <td class="small text-nowrap">${esc(r.sale_date)}</td>
      <td class="small font-mono text-nowrap">${esc(r.masked_card_number)}</td>
      <td class="small font-mono text-nowrap">${esc(r.merchant_code)}</td>
      <td class="small">${esc(r.merchant_location_name)}</td>
      <td class="small font-mono text-nowrap">${esc(r.receipt_number)}</td>
      <td class="small font-mono text-nowrap">${esc(r.transaction_identifier)}</td>
      <td class="small text-nowrap">${esc(r.installment_type)}</td>
      <td class="small font-mono text-nowrap">${esc(r.installment_number)}</td>
      <td class="small text-nowrap"><span class="status-chip ${tbkCurTone}">${esc(tbkCur)}</span></td>
      <td class="text-end fw-semibold text-nowrap">${fmtMoney(r.original_sale_amount)}</td>
      <td class="text-end small text-nowrap">${fmtMoney(r.transbank_commission)}</td>
      <td class="text-end small text-nowrap">${fmtMoney(r.commission_vat_tax)}</td>
      <td class="text-end fw-semibold text-nowrap">${fmtMoney(r.net_deposit_amount)}</td>
      <td class="pe-4"><span class="status-chip ${tbkTone}">${tbkLabel}</span></td>
    </tr>`;
  }

  function render() {
    if (!theadEl || !tbodyEl) return;
    theadEl.innerHTML = TAB_HEADERS[activeTab] || '';
    const filtered = filterRows();
    const total = filtered.length;
    const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    currentPage = Math.max(1, Math.min(currentPage, totalPages));

    const start = (currentPage - 1) * PAGE_SIZE;
    const slice = filtered.slice(start, start + PAGE_SIZE);

    if (visibleEl) visibleEl.textContent = String(total);
    if (totalEl) totalEl.textContent = String(total);
    if (rangeEl) rangeEl.textContent = total === 0 ? '0' : `${start + 1}–${Math.min(start + slice.length, total)}`;

    if (slice.length === 0) {
      const colSpan = theadEl.querySelectorAll('th').length || 8;
      tbodyEl.innerHTML = `<tr><td colspan="${colSpan}" class="text-center py-4 text-muted"><i class="bi bi-search me-1"></i>No hay registros para ese filtro.</td></tr>`;
    } else {
      tbodyEl.innerHTML = slice.map(renderRowHtml).join('');
    }

    if (navListEl) {
      if (totalPages <= 1) {
        navListEl.innerHTML = '';
      } else if (typeof buildPaginationItemsHtml === 'function') {
        navListEl.innerHTML = buildPaginationItemsHtml(currentPage, totalPages, (p) => `window.__goSourceTablesPage(${p})`);
      } else {
        let h = '';
        for (let p = 1; p <= totalPages; p++) {
          h += `<li class="page-item${p === currentPage ? ' active' : ''}"><a class="page-link" href="javascript:void(0)" onclick="window.__goSourceTablesPage(${p})">${p}</a></li>`;
        }
        navListEl.innerHTML = h;
      }
    }
  }

  window.__goSourceTablesPage = function (p) {
    currentPage = p;
    render();
  };

  document.querySelectorAll('#sourceTablesTabs [data-src-tab]').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#sourceTablesTabs [data-src-tab]').forEach((b) => {
        b.classList.remove('active');
        b.setAttribute('aria-selected', 'false');
      });
      btn.classList.add('active');
      btn.setAttribute('aria-selected', 'true');
      activeTab = btn.dataset.srcTab;
      currentPage = 1;
      populateFiltersForTab(activeTab);
      render();
    });
  });

  if (searchEl) {
    searchEl.addEventListener('input', () => {
      currentPage = 1;
      if (clearBtn) clearBtn.classList.toggle('d-none', !searchEl.value);
      render();
    });
  }
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      if (searchEl) searchEl.value = '';
      clearBtn.classList.add('d-none');
      currentPage = 1;
      render();
    });
  }
  if (statusEl) {
    statusEl.addEventListener('change', () => {
      currentPage = 1;
      render();
    });
  }
  if (cardEl) {
    cardEl.addEventListener('change', () => {
      currentPage = 1;
      render();
    });
  }
  if (collapseEl && toggleBtn) {
    collapseEl.addEventListener('shown.bs.collapse', () => {
      toggleBtn.innerHTML = '<i class="bi bi-chevron-up me-1"></i>Ocultar tablas de respaldo';
    });
    collapseEl.addEventListener('hidden.bs.collapse', () => {
      toggleBtn.innerHTML = '<i class="bi bi-chevron-down me-1"></i>Explorar tablas de respaldo';
    });
  }

  if (tbodyEl) {
    tbodyEl.addEventListener('click', (event) => {
      if (activeTab !== 'erp') return;
      const trigger = event.target.closest('.js-preview-erp-history');
      const rowEl = trigger || event.target.closest('tr.js-preview-erp-row');
      if (!rowEl) return;
      if (!trigger && event.target.closest('a, button, input, select, label')) return;
      const rowIndex = Number(rowEl.dataset.erpIndex);
      const erpList = Array.isArray(payload.erp) ? payload.erp : [];
      const row = erpList.find((item) => item.index === rowIndex);
      if (row && row.history && typeof window.openErpHistoryModal === 'function') {
        event.preventDefault();
        window.openErpHistoryModal(row.history);
      }
    });
  }

  populateFiltersForTab(activeTab);
  render();
})();
