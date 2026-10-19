/**
 * Upload & Auto-Detection Logic shared by usd_upload.html and clp_upload.html
 * Handles bulk drag-and-drop, structural fingerprint inspection,
 * manual or single-file slot replacement, and form submission.
 * The upload form declares its detection URL (data-detect-url) and, for CLP,
 * its role -> hidden input map (data-role-inputs).
 */

const USD_ROLE_INPUT_MAP = {
  erp: 'input_usd_erp',
  transbank: 'input_usd_tbk',
  bank: 'input_usd_bank',
  amex: 'input_usd_ax',
  diners: 'input_usd_di',
  visa: 'input_usd_vi',
  mastercard: 'input_usd_mc'
};

let bulkUploadedFiles = [];
let latestDetectionReport = null;

function getUploadForm() {
  return document.querySelector('form[data-detect-url]');
}

function getRoleInputMap() {
  const form = getUploadForm();
  const declared = form ? form.getAttribute('data-role-inputs') : null;
  return declared ? JSON.parse(declared) : USD_ROLE_INPUT_MAP;
}

function getRequiredFileCount() {
  return Object.keys(getRoleInputMap()).length;
}

function getDetectFilesUrl() {
  const form = getUploadForm();
  return form ? form.getAttribute('data-detect-url') : '/reconciliation/usd/detect-files/';
}

function showProcessingOverlay() {
  const overlay = document.getElementById('processingOverlay');
  if (overlay) overlay.style.display = 'flex';
}

document.addEventListener('DOMContentLoaded', () => {
  const dropzoneBox = document.getElementById('bulkDropzoneBox');
  if (!dropzoneBox) return;

  ['dragenter', 'dragover'].forEach(evtName => {
    dropzoneBox.addEventListener(evtName, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzoneBox.classList.add('drag-active');
    });
  });

  ['dragleave', 'drop'].forEach(evtName => {
    dropzoneBox.addEventListener(evtName, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzoneBox.classList.remove('drag-active');
    });
  });

  dropzoneBox.addEventListener('drop', (e) => {
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      handleBulkFilesSelected(e.dataTransfer.files);
    }
  });
});

async function handleBulkFilesSelected(fileList) {
  if (!fileList || fileList.length === 0) return;
  bulkUploadedFiles = Array.from(fileList);

  document.getElementById('expPhase1Dropzone').classList.add('d-none');
  document.getElementById('expPhase2Alignment').classList.add('d-none');
  document.getElementById('expScanningSpinner').classList.remove('d-none');

  const formData = new FormData();
  bulkUploadedFiles.forEach(file => formData.append('bulk_files', file));

  const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]').value;

  try {
    const response = await fetch(getDetectFilesUrl(), {
      method: 'POST',
      headers: { 'X-CSRFToken': csrfToken },
      body: formData
    });
    const data = await response.json();
    if (!response.ok) {
      alert(data.error || 'Error al analizar los archivos.');
      resetExperimentalFiles();
      return;
    }
    latestDetectionReport = data;
    renderAlignmentTable(data);
  } catch (err) {
    alert('Error de conexión al inspeccionar los archivos: ' + err);
    resetExperimentalFiles();
  } finally {
    document.getElementById('expScanningSpinner').classList.add('d-none');
  }
}

function getConfidenceBadgeHtml(type) {
  switch (type) {
    case 'HIGH':
      return '<span class="badge bg-success-subtle text-success border border-success-subtle rounded-pill px-2 py-1"><i class="bi bi-check-circle-fill me-1"></i>100% Seguro</span>';
    case 'MEDIUM':
      return '<span class="badge bg-warning-subtle text-dark border border-warning-subtle rounded-pill px-2 py-1"><i class="bi bi-info-circle-fill me-1"></i>Por Nombre</span>';
    case 'MANUAL':
      return '<span class="badge bg-primary-subtle text-primary border border-primary-subtle rounded-pill px-2 py-1"><i class="bi bi-hand-index-thumb me-1"></i>Asignado Manual</span>';
    default:
      return '<span class="badge bg-danger-subtle text-danger border border-danger-subtle rounded-pill px-2 py-1"><i class="bi bi-exclamation-triangle-fill me-1"></i>Faltante</span>';
  }
}

function renderAlignmentTable(data) {
  const tbody = document.getElementById('alignmentTableBody');
  tbody.innerHTML = '';

  data.aligned_roles.forEach(role => {
    const tr = document.createElement('tr');

    let optionsHtml = '<option value="">— Sin archivo asignado —</option>';
    data.inspected_files.forEach(f => {
      const selected = (role.file_index === f.file_index) ? 'selected' : '';
      optionsHtml += `<option value="${f.file_index}" ${selected}>${f.file_name} (${f.row_count} filas)</option>`;
    });

    const badgeHtml = getConfidenceBadgeHtml(role.confidence);

    tr.innerHTML = `
      <td class="ps-3 py-3">
        <div class="d-flex align-items-center gap-2">
          <span class="badge ${role.badge_class}">${role.badge}</span>
          <span class="fw-bold text-dark">${role.role_label}</span>
        </div>
      </td>
      <td class="py-3">
        <div class="d-flex align-items-center gap-2">
          <select class="form-select form-select-sm role-file-select" id="select_${role.role_key}" data-role-key="${role.role_key}" onchange="onManualAlignmentChange()">
            ${optionsHtml}
          </select>
          <label class="btn btn-outline-primary btn-sm rounded-pill px-3 text-nowrap mb-0" title="Subir un archivo nuevo directamente para este rol">
            <i class="bi bi-upload me-1"></i>Subir
            <input type="file" class="d-none" accept=".xls,.xlsx" onchange="uploadSingleFileForRole('${role.role_key}', this)">
          </label>
        </div>
      </td>
      <td class="py-3 text-muted" id="evidence_${role.role_key}">
        <i class="bi bi-cpu me-1 text-primary"></i> ${role.evidence}
      </td>
      <td class="text-center pe-3 py-3" id="confidence_${role.role_key}">
        ${badgeHtml}
      </td>
    `;
    tbody.appendChild(tr);
  });

  document.getElementById('expPhase2Alignment').classList.remove('d-none');
  validateCurrentAlignmentSelection();
}

async function uploadSingleFileForRole(roleKey, inputElem) {
  if (!inputElem.files || inputElem.files.length === 0) return;
  const singleFile = inputElem.files[0];
  inputElem.value = '';

  const evidenceCell = document.getElementById('evidence_' + roleKey);
  if (evidenceCell) {
    evidenceCell.innerHTML = '<span class="text-primary"><span class="spinner-border spinner-border-sm me-1"></span>Analizando archivo subido...</span>';
  }

  const formData = new FormData();
  formData.append('bulk_files', singleFile);
  const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]').value;

  try {
    const response = await fetch(getDetectFilesUrl(), {
      method: 'POST',
      headers: { 'X-CSRFToken': csrfToken },
      body: formData
    });
    const data = await response.json();
    if (!response.ok || !data.inspected_files || data.inspected_files.length === 0) {
      alert(data.error || 'No se pudo analizar el archivo.');
      onManualAlignmentChange();
      return;
    }

    const newFileIndex = bulkUploadedFiles.length;
    bulkUploadedFiles.push(singleFile);

    const inspected = data.inspected_files[0];
    inspected.file_index = newFileIndex;
    latestDetectionReport.inspected_files.push(inspected);

    document.querySelectorAll('.role-file-select').forEach(sel => {
      const opt = document.createElement('option');
      opt.value = newFileIndex;
      opt.textContent = `${inspected.file_name} (${inspected.row_count} filas)`;
      sel.appendChild(opt);
    });

    const targetSelect = document.getElementById('select_' + roleKey);
    if (targetSelect) {
      targetSelect.value = String(newFileIndex);
    }

    onManualAlignmentChange();
  } catch (err) {
    alert('Error al subir el archivo individual: ' + err);
    onManualAlignmentChange();
  }
}

function onManualAlignmentChange() {
  if (!latestDetectionReport) return;
  const selects = document.querySelectorAll('.role-file-select');
  selects.forEach(sel => {
    const roleKey = sel.getAttribute('data-role-key');
    const fileIdx = sel.value !== '' ? parseInt(sel.value, 10) : null;
    const evidenceCell = document.getElementById('evidence_' + roleKey);
    const confidenceCell = document.getElementById('confidence_' + roleKey);

    if (fileIdx !== null && latestDetectionReport.inspected_files[fileIdx]) {
      const fileInfo = latestDetectionReport.inspected_files[fileIdx];
      evidenceCell.innerHTML = `<i class="bi bi-cpu me-1 text-primary"></i> ${fileInfo.evidence}`;
      confidenceCell.innerHTML = getConfidenceBadgeHtml(
        (fileInfo.detected_role === roleKey && fileInfo.confidence === 'HIGH') ? 'HIGH' : 'MANUAL'
      );
    } else {
      evidenceCell.innerHTML = '<span class="text-danger">Seleccione un archivo para este rol.</span>';
      confidenceCell.innerHTML = getConfidenceBadgeHtml('MISSING');
    }
  });

  validateCurrentAlignmentSelection();
}

function getExtraErpFiles(chosenSet) {
  if (!latestDetectionReport || !document.getElementById('input_clp_extra_erp')) return [];
  return (latestDetectionReport.inspected_files || []).filter(
    f => !chosenSet.has(String(f.file_index)) && f.detected_role === 'erp'
  );
}

function validateCurrentAlignmentSelection() {
  const selects = Array.from(document.querySelectorAll('.role-file-select'));
  const chosenIndices = selects.map(s => s.value).filter(v => v !== '');
  const uniqueIndices = new Set(chosenIndices);
  const alertBox = document.getElementById('alignmentSummaryAlert');
  const confirmBtn = document.getElementById('btnConfirmAlignment');
  const required = getRequiredFileCount();

  if (chosenIndices.length === required && uniqueIndices.size === required) {
    const extraErp = getExtraErpFiles(uniqueIndices);
    const extraNote = extraErp.length > 0
      ? `<div class="mt-1 text-dark"><i class="bi bi-journal-plus text-primary me-1"></i><strong>Borderó histórico adicional incluido automáticamente para cruce fuera de período:</strong> ${extraErp.map(f => f.file_name).join(', ')}</div>`
      : '';
    alertBox.className = 'alert alert-success rounded-4 mb-3 py-2 px-3 small d-flex align-items-center justify-content-between';
    alertBox.innerHTML = `
      <div>
        <div><i class="bi bi-check-circle-fill me-2"></i><strong>¡${required} de ${required} archivos alineados sin conflictos!</strong> Revise la tabla y confirme para iniciar el cruce financiero.</div>
        ${extraNote}
      </div>
      <span class="badge bg-success rounded-pill">${bulkUploadedFiles.length} archivo(s) analizado(s)</span>
    `;
    confirmBtn.disabled = false;
  } else if (chosenIndices.length === required && uniqueIndices.size < required) {
    alertBox.className = 'alert alert-warning rounded-4 mb-3 py-2 px-3 small';
    alertBox.innerHTML = `<i class="bi bi-exclamation-triangle-fill me-2"></i><strong>Atención:</strong> Ha asignado el mismo archivo en más de un rol. Por favor asigne ${required} archivos distintos.`;
    confirmBtn.disabled = true;
  } else {
    const missingCount = required - chosenIndices.length;
    alertBox.className = 'alert alert-danger rounded-4 mb-3 py-2 px-3 small';
    alertBox.innerHTML = `<i class="bi bi-x-circle-fill me-2"></i><strong>Faltan ${missingCount} archivo(s) por alinear</strong> (se subieron ${bulkUploadedFiles.length} archivo(s)). Seleccione los faltantes o vuelva a cargar el lote completo de ${required} archivos.`;
    confirmBtn.disabled = true;
  }
}

function resetExperimentalFiles() {
  bulkUploadedFiles = [];
  latestDetectionReport = null;
  document.getElementById('bulkFilesInput').value = '';
  document.getElementById('expPhase2Alignment').classList.add('d-none');
  document.getElementById('expScanningSpinner').classList.add('d-none');
  document.getElementById('expPhase1Dropzone').classList.remove('d-none');
}

function confirmAlignmentAndSubmit() {
  const selects = document.querySelectorAll('.role-file-select');
  const chosenSet = new Set();
  for (const sel of selects) {
    const roleKey = sel.getAttribute('data-role-key');
    const fileIdx = sel.value !== '' ? parseInt(sel.value, 10) : null;
    if (fileIdx === null || !bulkUploadedFiles[fileIdx]) {
      alert(`Por favor asegúrese de que los ${getRequiredFileCount()} roles tengan un archivo asignado.`);
      return;
    }
    chosenSet.add(String(fileIdx));
    const inputId = getRoleInputMap()[roleKey];
    const fileInput = document.getElementById(inputId);
    const dt = new DataTransfer();
    dt.items.add(bulkUploadedFiles[fileIdx]);
    fileInput.files = dt.files;
  }

  const extraErpInput = document.getElementById('input_clp_extra_erp');
  if (extraErpInput) {
    const extraDt = new DataTransfer();
    getExtraErpFiles(chosenSet).forEach(f => {
      if (bulkUploadedFiles[f.file_index]) {
        extraDt.items.add(bulkUploadedFiles[f.file_index]);
      }
    });
    extraErpInput.files = extraDt.files;
  }

  showProcessingOverlay();
  getUploadForm().submit();
}


