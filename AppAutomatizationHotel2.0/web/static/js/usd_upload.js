/**
 * USD Upload & Auto-Detection Logic (usd_upload.html)
 * Handles bulk drag-and-drop, structural fingerprint inspection,
 * manual or single-file slot replacement, and form submission.
 */

const ROLE_INPUT_MAP = {
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

function getDetectFilesUrl() {
  const form = document.getElementById('usdUploadForm');
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

    let badgeHtml = '';
    if (role.confidence === 'HIGH') {
      badgeHtml = '<span class="badge bg-success-subtle text-success border border-success-subtle rounded-pill px-2 py-1"><i class="bi bi-check-circle-fill me-1"></i>100% Seguro</span>';
    } else if (role.confidence === 'MEDIUM') {
      badgeHtml = '<span class="badge bg-warning-subtle text-dark border border-warning-subtle rounded-pill px-2 py-1"><i class="bi bi-info-circle-fill me-1"></i>Por Nombre</span>';
    } else {
      badgeHtml = '<span class="badge bg-danger-subtle text-danger border border-danger-subtle rounded-pill px-2 py-1"><i class="bi bi-exclamation-triangle-fill me-1"></i>Faltante</span>';
    }

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
      if (fileInfo.detected_role === roleKey && fileInfo.confidence === 'HIGH') {
        confidenceCell.innerHTML = '<span class="badge bg-success-subtle text-success border border-success-subtle rounded-pill px-2 py-1"><i class="bi bi-check-circle-fill me-1"></i>100% Seguro</span>';
      } else {
        confidenceCell.innerHTML = '<span class="badge bg-primary-subtle text-primary border border-primary-subtle rounded-pill px-2 py-1"><i class="bi bi-hand-index-thumb me-1"></i>Asignado Manual</span>';
      }
    } else {
      evidenceCell.innerHTML = '<span class="text-danger">Seleccione un archivo para este rol.</span>';
      confidenceCell.innerHTML = '<span class="badge bg-danger-subtle text-danger border border-danger-subtle rounded-pill px-2 py-1"><i class="bi bi-exclamation-triangle-fill me-1"></i>Faltante</span>';
    }
  });

  validateCurrentAlignmentSelection();
}

function validateCurrentAlignmentSelection() {
  const selects = Array.from(document.querySelectorAll('.role-file-select'));
  const chosenIndices = selects.map(s => s.value).filter(v => v !== '');
  const uniqueIndices = new Set(chosenIndices);
  const alertBox = document.getElementById('alignmentSummaryAlert');
  const confirmBtn = document.getElementById('btnConfirmAlignment');

  if (chosenIndices.length === 7 && uniqueIndices.size === 7) {
    alertBox.className = 'alert alert-success rounded-4 mb-3 py-2 px-3 small d-flex align-items-center justify-content-between';
    alertBox.innerHTML = `
      <div><i class="bi bi-check-circle-fill me-2"></i><strong>¡7 de 7 archivos alineados sin conflictos!</strong> Revise la tabla y confirme para iniciar el cruce financiero.</div>
      <span class="badge bg-success rounded-pill">${bulkUploadedFiles.length} archivo(s) analizado(s)</span>
    `;
    confirmBtn.disabled = false;
  } else if (chosenIndices.length === 7 && uniqueIndices.size < 7) {
    alertBox.className = 'alert alert-warning rounded-4 mb-3 py-2 px-3 small';
    alertBox.innerHTML = '<i class="bi bi-exclamation-triangle-fill me-2"></i><strong>Atención:</strong> Ha asignado el mismo archivo en más de un rol. Por favor asigne 7 archivos distintos.';
    confirmBtn.disabled = true;
  } else {
    const missingCount = 7 - chosenIndices.length;
    alertBox.className = 'alert alert-danger rounded-4 mb-3 py-2 px-3 small';
    alertBox.innerHTML = `<i class="bi bi-x-circle-fill me-2"></i><strong>Faltan ${missingCount} archivo(s) por alinear</strong> (se subieron ${bulkUploadedFiles.length} archivo(s)). Seleccione los faltantes o vuelva a cargar el lote completo de 7 archivos.`;
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
  for (const sel of selects) {
    const roleKey = sel.getAttribute('data-role-key');
    const fileIdx = sel.value !== '' ? parseInt(sel.value, 10) : null;
    if (fileIdx === null || !bulkUploadedFiles[fileIdx]) {
      alert('Por favor asegúrese de que los 7 roles tengan un archivo asignado.');
      return;
    }
    const inputId = ROLE_INPUT_MAP[roleKey];
    const fileInput = document.getElementById(inputId);
    const dt = new DataTransfer();
    dt.items.add(bulkUploadedFiles[fileIdx]);
    fileInput.files = dt.files;
  }

  showProcessingOverlay();
  document.getElementById('usdUploadForm').submit();
}

