/**
 * Suggestion card: ONE component for the assisted suggestion of a voucher, used in
 *   - step 2 (CLP and USD preview modals), and
 *   - the Pendientes inbox (panel above the table and the detail of each case).
 *
 * Layout: header (voucher, amount, what happened, confidence) → one card per file with the same
 * rows side by side, mismatches marked, and a "Por qué" card on the right → the ERP correction (old value struck through → editable new value) with its actions.
 *
 * renderSuggestionCard(voucher, currency, { mode, index })
 *   mode 'preview' → "Aplicar corrección" (suggestion_apply.js → applyPreviewCorrection)
 *   mode 'inbox'   → Aceptar / Rechazar forms (voucher.accept_url, voucher.reject_url)
 */

const SC_FIELD_LABELS = {
  amount: 'Monto',
  authorization_code: 'Código de autorización',
  transaction_date: 'Fecha',
  payment_code: 'Medio de pago',
  document_number: 'Documento'
};
const SC_RESULT_LABELS = {
  NOT_FOUND_IN_ERP: 'No está en el ERP',
  AMOUNT_MISMATCH: 'Diferencia de monto',
  CARD_TYPE_MISMATCH: 'Medio de pago distinto',
  OUT_OF_PERIOD: 'Fuera de período',
  MATCHED: 'Cuadrado'
};

function scEsc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function scPresent(value) {
  const s = String(value ?? '').trim();
  return Boolean(s && !s.startsWith('—'));
}

function scClp(value) {
  return '$' + Math.round(Number(value) || 0).toLocaleString('es-CL');
}

function scUsd(value) {
  return 'US$ ' + (Number(value) || 0).toLocaleString('es-CL', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function scNormCode(value) {
  return String(value ?? '').trim().toUpperCase().replace(/^0+/, '');
}

function scProposalInputValue(pc) {
  if (pc.field === 'amount') {
    const num = Number(pc.new);
    return Number.isInteger(num) ? String(num) : String(pc.new);
  }
  return String(pc.new ?? '');
}

function scProposalOldDisplay(pc) {
  const part = String(pc.label || '').split(': ').slice(1).join(': ');
  const [oldText] = part.split(' → ');
  return oldText || pc.old || '—';
}

function scProposalDoc(pc) {
  return String(pc.label || '').split(' · ')[0] || '';
}

/* ------------------------------------------------------------------ */
/* "Por qué" of each row                                                */
/* ------------------------------------------------------------------ */

function scParseDate(text) {
  const m = String(text || '').match(/(\d{2})\/(\d{2})\/(\d{4})/);
  return m ? new Date(Number(m[3]), Number(m[2]) - 1, Number(m[1])) : null;
}

function scDayGap(a, b) {
  const da = scParseDate(a);
  const db = scParseDate(b);
  if (!da || !db) return null;
  return Math.round(Math.abs(db - da) / 86400000);
}

/* The engine's reason chip about the code (typo, similar code, same code in the database) */
function scCodeReason(sug) {
  const reasons = Array.isArray(sug.reasons) ? sug.reasons : [];
  const chip = reasons.find(r => /tipeo|similar|c[oó]d/i.test(r.label || ''));
  if (!chip) return '';
  const typo = String(chip.label).match(/\(([^)]*tipeo[^)]*)\)/i);
  return typo ? typo[1].charAt(0).toUpperCase() + typo[1].slice(1) : chip.label;
}

function scDbReason(sug) {
  const db = sug.db_match || {};
  return db.process_id ? `Venta guardada en el cierre #${db.process_id}` : '';
}

/* ------------------------------------------------------------------ */
/* Model: the same shape for CLP and USD                                */
/* ------------------------------------------------------------------ */

function scModelClp(v) {
  const sug = v.suggestion || {};
  const erp = sug.erp_row || {};
  const files = sug.files_involved || {};
  const sameCode = scNormCode(v.authorization_code) === scNormCode(erp.authorization_code);
  const sameAmount = Math.abs(Number(v.tbk_amount) - Number(erp.amount)) < 1;
  const sameDate = Boolean(v.sale_date) && v.sale_date === erp.transaction_date;
  const isCredit = v.modality_label === 'Crédito' || v.payment_modality === 'CREDIT';
  const sameMedium = isCredit ? erp.payment_group === 'CREDIT' : erp.payment_group === 'REDCOMPRA';
  const erpFile = erp.source_file || files.erp || 'Borderó';
  const gap = Math.abs(Number(v.tbk_amount) - Number(erp.amount));
  const days = scDayGap(v.sale_date, erp.transaction_date);
  const why = {
    code: sameCode
      ? 'Mismo código'
      : (!scPresent(erp.authorization_code) || String(erp.authorization_code).startsWith('—')
        ? 'El Borderó no tiene código'
        : (scCodeReason(sug) || 'Código distinto')),
    amount: sameAmount ? 'Monto exacto' : `Diferencia de ${scClp(gap)}`,
    date: sameDate ? 'Misma fecha' : (days === null ? '' : `${days} día${days === 1 ? '' : 's'} de diferencia`),
    medium: sameMedium
      ? `Mismo medio (${erp.payment_code || (isCredit ? 'crédito' : 'Red Compra')})`
      : `Transbank: ${isCredit ? 'crédito' : 'débito/prepago'} · Borderó: ${erp.payment_code || '—'}`,
    detail: scDbReason(sug) || 'Para identificar la fila'
  };
  return {
    amountLabel: scClp(v.tbk_amount),
    medium: [v.modality_label, v.card_name].filter(Boolean).join(' · '),
    cashier: erp.cashier_username || '',
    columns: [
      { title: 'Transbank', file: files.tbk || 'Abono TBK' },
      { title: 'Borderó ERP', file: erp.excel_row ? `${erpFile} · fila ${erp.excel_row}` : erpFile }
    ],
    rows: [
      { label: 'Código', values: [v.authorization_code, erp.authorization_code || '— sin código'], state: sameCode ? 'ok' : 'diff', why: why.code },
      { label: 'Monto', values: [scClp(v.tbk_amount), erp.amount !== undefined ? scClp(erp.amount) : '—'], state: sameAmount ? 'ok' : 'diff', why: why.amount },
      { label: 'Fecha', values: [v.sale_date || '—', `${erp.transaction_date || '—'}${erp.transaction_time ? ' ' + erp.transaction_time : ''}`], state: sameDate ? 'ok' : (v.sale_date && erp.transaction_date ? 'diff' : ''), why: why.date },
      { label: 'Medio', values: [[v.modality_label, v.card_name].filter(Boolean).join(' · ') || '—', [erp.payment_code, erp.card_name].filter(Boolean).join(' · ') || '—'], state: sameMedium ? 'ok' : 'diff', why: why.medium },
      { label: 'Detalle', values: [v.merchant_location || '—', `Cta ${erp.account_code || '—'}${scPresent(erp.invoice_number) ? ' · Fact ' + erp.invoice_number : ''}`], state: '', why: why.detail }
    ]
  };
}

function scModelUsd(v) {
  const sug = v.suggestion || {};
  const files = sug.files_involved || {};
  const rows = Array.isArray(sug.comparison_rows) ? sug.comparison_rows : [];
  const [rAuth = {}, rDoc = {}, rUsd = {}, rClp = {}, rCtx = {}] = rows;
  const tone = (r) => (r.status_tone === 'gold' ? 'diff' : (r.status_tone === 'emerald' ? 'ok' : ''));
  const shortDoc = (s) => String(s || '').split('(')[0].trim() || '—';
  const why = (r, ok, diff, neutral = '') => (tone(r) === 'ok' ? ok : (tone(r) === 'diff' ? diff : neutral));
  const usdGap = Math.abs((Number(String(rUsd.tbk_value || '').replace(/[^\d,]/g, '').replace(',', '.')) || Number(v.gross_amount_usd) || 0)
    - (Number(String(rUsd.op_value || '').replace(/[^\d,]/g, '').replace(',', '.')) || 0));
  return {
    amountLabel: scUsd(v.gross_amount_usd),
    medium: v.card_name || '',
    cashier: sug.erp_cashier && sug.erp_cashier !== '—' ? sug.erp_cashier : (v.cashier_username && v.cashier_username !== '—' ? v.cashier_username : ''),
    columns: [
      { title: 'Transbank', file: files.tbk || 'Abono TBK' },
      { title: 'ERP Hotel', file: files.erp || 'ERP' },
      { title: 'Operadora', file: files.op || 'Operadora' }
    ],
    rows: [
      { label: 'Código', values: [rAuth.tbk_value || v.authorization_code, rAuth.erp_value || '—', rAuth.op_value || '—'], state: tone(rAuth), why: why(rAuth, 'Mismo código', scCodeReason(sug) || 'Código distinto') },
      { label: 'Cuenta / Doc.', values: ['—', shortDoc(rDoc.erp_value), shortDoc(rDoc.op_value)], state: tone(rDoc), why: why(rDoc, 'Misma cuenta ERP y Doc. Pos. Clientes', 'Cuenta ERP y Doc. Pos. Clientes distintos', scDbReason(sug)) },
      { label: 'Monto US$', values: [rUsd.tbk_value || scUsd(v.gross_amount_usd), '—', rUsd.op_value || '—'], state: tone(rUsd), why: why(rUsd, 'Monto exacto en dólares', `Diferencia de ${scUsd(usdGap)}`) },
      { label: 'Saldo CLP', values: ['—', rClp.erp_value || '—', rClp.op_value || '—'], state: tone(rClp), why: why(rClp, 'Mismo saldo en pesos', 'Saldo en pesos distinto') },
      { label: 'Detalle', values: [rCtx.tbk_value || v.sale_date || '—', rCtx.erp_value || '—', rCtx.op_value || '—'], state: tone(rCtx), why: why(rCtx, 'Misma fecha', 'Fecha distinta', 'Para identificar la venta') }
    ]
  };
}

/* ------------------------------------------------------------------ */
/* Pieces                                                              */
/* ------------------------------------------------------------------ */

/* One card per file (Transbank | Borderó, or Transbank | ERP | Operadora) plus a "Por qué" card
   on the right. Rows line up across the cards (CSS subgrid). */
function scComparison(model) {
  const icon = (r) => (r.state === 'diff'
    ? '<i class="bi bi-exclamation-triangle-fill" aria-label="Distinto"></i>'
    : (r.state === 'ok' ? '<i class="bi bi-check2" aria-label="Coincide"></i>' : ''));
  const span = `style="grid-row: span ${model.rows.length + 1}"`;
  const cards = model.columns.map((c, ci) => {
    const rows = model.rows.map(r => {
      const val = r.values[ci];
      return (
        `<div class="sc-frow sc-row-${r.state || 'neutral'}">` +
          `<span class="sc-flabel"><span class="sc-state">${icon(r)}</span>${scEsc(r.label)}</span>` +
          `<span class="sc-val" title="${scEsc(val)}">${scEsc(val || '—')}</span>` +
        `</div>`
      );
    }).join('');
    return (
      `<section class="sc-file${ci === 0 ? ' sc-file-tbk' : ''}" ${span} aria-label="${scEsc(c.title)}">` +
        `<header class="sc-file-head"><span class="sc-col-title">${scEsc(c.title)}</span><span class="sc-col-file">${scEsc(c.file)}</span></header>` +
        rows +
      `</section>`
    );
  }).join('');
  const whyRows = model.rows.map(r =>
    `<div class="sc-frow sc-row-${r.state || 'neutral'}"><span class="sc-flabel">${scEsc(r.label)}</span><span class="sc-why-text">${scEsc(r.why || '—')}</span></div>`
  ).join('');
  const whyCard = (
    `<section class="sc-file sc-file-why" ${span} aria-label="Por qué">` +
      `<header class="sc-file-head"><span class="sc-col-title">Por qué</span><span class="sc-col-file">Lo que se comparó</span></header>` +
      whyRows +
    `</section>`
  );
  return `<div class="sc-files sc-files-${model.columns.length}">${cards}${whyCard}</div>`;
}

function scInput(pc, id, name, choices) {
  if (pc.field === 'transaction_date') {
    return `<input type="date" id="${id}" ${name ? `name="${name}"` : ''} class="form-control form-control-sm sc-input" value="${scEsc(pc.new)}">`;
  }
  if (pc.field === 'payment_code' && Array.isArray(choices) && choices.length) {
    const opts = choices.map(([code, label]) => `<option value="${scEsc(code)}"${code === pc.new ? ' selected' : ''}>${scEsc(label)}</option>`).join('');
    return `<select id="${id}" ${name ? `name="${name}"` : ''} class="form-select form-select-sm sc-input">${opts}</select>`;
  }
  return `<input id="${id}" ${name ? `name="${name}"` : ''} class="form-control form-control-sm sc-input font-monospace" value="${scEsc(scProposalInputValue(pc))}"${pc.field === 'amount' ? ' inputmode="decimal"' : ''}>`;
}

function scFix(v, opts) {
  const sug = v.suggestion || {};
  const pc = sug.proposed_change;
  const actionText = sug.action || v.action || '';

  if (!pc) {
    return actionText
      ? `<div class="sc-fix sc-fix-manual"><div class="sc-section-label">Acción sugerida</div><p>${scEsc(actionText)}</p><p class="sc-hint">No es un cambio de un solo dato: corrígelo a mano desde el caso en Pendientes.</p></div>`
      : '';
  }

  const fieldLabel = SC_FIELD_LABELS[pc.field] || pc.field;
  const doc = scProposalDoc(pc);
  const head = `<div class="sc-section-label">Corrección en el ERP</div><div class="sc-fix-target">${scEsc(doc)} · ${scEsc(fieldLabel)}</div>`;

  if (pc.status) {
    const done = v._applyNote || (opts.mode === 'preview'
      ? 'Aplicada. Se guarda en el ERP, con historial, al confirmar el cierre.'
      : (pc.status === 'rejected' ? 'Rechazada. El ERP no se modificó.' : 'Aceptada y guardada en el ERP.'));
    return (
      `<div class="sc-fix sc-fix-done">${head}` +
        `<div class="sc-diff"><span class="sc-old">${scEsc(scProposalOldDisplay(pc))}</span><i class="bi bi-arrow-right" aria-hidden="true"></i><span class="sc-new">${scEsc(scProposalInputValue(pc))}</span></div>` +
        `<p class="sc-done"><i class="bi bi-check2-circle me-1"></i>${scEsc(done)}</p>` +
      `</div>`
    );
  }

  const key = opts.mode === 'preview' ? `p${v.tbk_index}` : `c${v.case_id}${opts.suffix || ''}`;
  const id = `sc-input-${key}`;
  const diffLine = `<div class="sc-diff"><span class="sc-old" title="Valor actual en el ERP">${scEsc(scProposalOldDisplay(pc))}</span><i class="bi bi-arrow-right" aria-hidden="true"></i>`;

  if (opts.mode === 'preview') {
    return (
      `<div class="sc-fix">${head}` +
        `<div class="sc-fix-row">` +
          `${diffLine}<label class="visually-hidden" for="${id}">${scEsc(fieldLabel)} nuevo</label>${scInput(pc, id, '', opts.choices)}</div>` +
          `<div class="sc-actions">` +
            `<button type="button" class="btn btn-success btn-sm rounded-pill px-3 fw-semibold" onclick="applyPreviewCorrection(${Number(v.tbk_index)}, '${id}', this)"><i class="bi bi-check2 me-1"></i>Aplicar corrección</button>` +
          `</div>` +
        `</div>` +
        `<p class="sc-hint">Puedes editar el valor nuevo antes de aplicarlo.</p>` +
        `<div class="sc-error" id="${id}-error" role="alert" hidden></div>` +
      `</div>`
    );
  }

  const csrf = (document.querySelector('[name=csrfmiddlewaretoken]') || {}).value || '';
  const back = opts.returnQuery || '';
  return (
    `<form method="post" action="${scEsc(v.accept_url)}" class="sc-fix" data-inbox-fix="${scEsc(v.case_id)}">${head}` +
      `<input type="hidden" name="csrfmiddlewaretoken" value="${scEsc(csrf)}">` +
      `<input type="hidden" name="return_query" value="${scEsc(back)}">` +
      `<div class="sc-fix-row">` +
        `${diffLine}<label class="visually-hidden" for="${id}">${scEsc(fieldLabel)} nuevo</label>${scInput(pc, id, 'value', opts.choices)}</div>` +
        `<div class="sc-actions">` +
          `<button type="submit" class="btn btn-success btn-sm rounded-pill px-3 fw-semibold"><i class="bi bi-check2 me-1"></i>Aceptar</button>` +
          `<button type="submit" formaction="${scEsc(v.reject_url)}" formnovalidate class="btn btn-outline-secondary btn-sm rounded-pill px-3 fw-semibold">Rechazar</button>` +
        `</div>` +
      `</div>` +
      `<p class="sc-hint">Aceptar corrige el ERP con historial y vuelve a cruzar el voucher. Rechazar no toca el ERP. Puedes editar el valor nuevo.</p>` +
      `<div class="sc-error" role="alert" hidden></div>` +
    `</form>`
  );
}

/* ------------------------------------------------------------------ */
/* Card                                                                */
/* ------------------------------------------------------------------ */

function renderSuggestionCard(v, currency, opts = {}) {
  const sug = v.suggestion || {};
  const model = currency === 'USD' ? scModelUsd(v) : scModelClp(v);
  const resultLabel = v.type_label || SC_RESULT_LABELS[v.system_result] || '';
  const resultTone = v.system_result === 'NOT_FOUND_IN_ERP' ? 'bad' : 'warn';
  const confidence = String(sug.confidence || 'MEDIA').toUpperCase();
  const index = opts.index ? `<span class="sc-index">#${opts.index}</span>` : '';
  const cashier = model.cashier ? `<span class="sc-meta-item"><i class="bi bi-person"></i>${scEsc(model.cashier)}</span>` : '';
  const applied = (sug.proposed_change || {}).status ? ' sc-card-done' : '';
  return (
    `<article class="sc-card${applied}" aria-label="Sugerencia para el voucher ${scEsc(v.authorization_code)}">` +
      `<header class="sc-head">` +
        `<div class="sc-head-main">` +
          `<div class="sc-title">${index}<span class="sc-voucher">${scEsc(v.authorization_code)}</span><span class="sc-amount">${scEsc(model.amountLabel)}</span></div>` +
          `<div class="sc-meta">` +
            (resultLabel ? `<span class="sc-pill sc-pill-${resultTone}">${scEsc(resultLabel)}</span>` : '') +
            (model.medium ? `<span class="sc-meta-item"><i class="bi bi-credit-card-2-front"></i>${scEsc(model.medium)}</span>` : '') +
            (v.sale_date ? `<span class="sc-meta-item"><i class="bi bi-calendar3"></i>${scEsc(v.sale_date)}</span>` : '') +
            cashier +
          `</div>` +
        `</div>` +
        `<span class="sc-confidence sc-confidence-${confidence === 'ALTA' ? 'high' : 'mid'}" title="Confianza de la sugerencia"><span class="sc-dot"></span>Confianza ${scEsc(confidence.toLowerCase())}</span>` +
      `</header>` +
      (sug.headline ? `<p class="sc-headline">${scEsc(sug.headline)}</p>` : '') +
      scComparison(model) +
      scFix(v, opts) +
    `</article>`
  );
}

/* ------------------------------------------------------------------ */
/* Inbox (Pendientes)                                                  */
/* ------------------------------------------------------------------ */

let scInboxPayload = { cases: {} };
let scInboxChanges = 0;
const SC_INBOX_NOTICE_KEY = 'inboxSuggestionNotice';

function scRenderInboxCard(el) {
  const item = scInboxPayload.cases[el.dataset.suggestionCase];
  if (!item) return;
  el.innerHTML = renderSuggestionCard(item, item.currency, {
    mode: 'inbox',
    index: el.dataset.index ? Number(el.dataset.index) : undefined,
    suffix: el.dataset.suffix || '',
    returnQuery: scInboxPayload.return_query || '',
    choices: (scInboxPayload.payment_choices || {})[item.currency] || []
  });
}

/* Aceptar / Rechazar inside a modal: no page reload, the card shows the result in place */
async function scSubmitInboxFix(form, submitter) {
  const item = scInboxPayload.cases[form.dataset.inboxFix];
  const url = submitter && submitter.getAttribute('formaction') ? submitter.getAttribute('formaction') : form.action;
  const errEl = form.querySelector('.sc-error');
  const buttons = form.querySelectorAll('button');
  const original = submitter ? submitter.innerHTML : '';
  buttons.forEach(b => { b.disabled = true; });
  if (submitter) submitter.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status"></span>Guardando…';
  if (errEl) errEl.hidden = true;
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      body: new FormData(form)
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.success) throw new Error(data.error || 'No se pudo guardar.');
    scInboxChanges += 1;
    if (item && item.suggestion && item.suggestion.proposed_change) {
      const input = form.querySelector('[name=value]');
      if (data.status === 'accepted' && input && input.value) item.suggestion.proposed_change.new = input.value;
      item.suggestion.proposed_change.status = data.status;
      item._applyNote = data.message;
    }
    document.querySelectorAll(`[data-suggestion-case="${form.dataset.inboxFix}"]`).forEach(el => {
      scRenderInboxCard(el);
      const card = el.querySelector('.sc-card');
      if (card) card.classList.add('sc-just-applied');
    });
  } catch (error) {
    if (errEl) {
      errEl.textContent = error.message;
      errEl.hidden = false;
    }
    buttons.forEach(b => { b.disabled = false; });
    if (submitter) submitter.innerHTML = original;
  }
}

document.addEventListener('submit', (event) => {
  const form = event.target.closest('form[data-inbox-fix]');
  if (!form) return;
  event.preventDefault();
  scSubmitInboxFix(form, event.submitter);
});

/* Closing a modal after accepting or rejecting refreshes the inbox once, with a summary notice */
document.addEventListener('hidden.bs.modal', () => {
  if (!scInboxChanges) return;
  const n = scInboxChanges;
  try {
    sessionStorage.setItem(SC_INBOX_NOTICE_KEY, `${n} sugerencia${n === 1 ? '' : 's'} procesada${n === 1 ? '' : 's'}. La bandeja ya muestra los cambios.`);
  } catch (e) { /* storage blocked */ }
  window.location.reload();
});

document.addEventListener('DOMContentLoaded', () => {
  const dataEl = document.getElementById('inboxSuggestionData');
  if (!dataEl) return;
  try { scInboxPayload = JSON.parse(dataEl.textContent) || { cases: {} }; } catch (e) { return; }
  scInboxPayload.cases = scInboxPayload.cases || {};
  document.querySelectorAll('[data-suggestion-case]').forEach(scRenderInboxCard);

  let notice = null;
  try {
    notice = sessionStorage.getItem(SC_INBOX_NOTICE_KEY);
    sessionStorage.removeItem(SC_INBOX_NOTICE_KEY);
  } catch (e) { /* storage blocked */ }
  const box = document.getElementById('inboxSuggestionNotice');
  if (notice && box) {
    box.querySelector('[data-text]').textContent = notice;
    box.hidden = false;
  }
});
