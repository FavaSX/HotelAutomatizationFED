// Validación y Loading Overlay
  function validateAndShowLoading() {
    const archivos = ['archivo1','archivo2','archivo3','archivo4','archivo5','archivo6','archivo7'];
    const faltantes = archivos.filter(id => !document.getElementById(id).files.length);
    
    if (faltantes.length > 0) {
      alert('Faltan ' + faltantes.length + ' archivo(s) por seleccionar. Sube los 7 archivos antes de procesar.');
      return false;
    }
    
    document.getElementById('loadingOverlay').classList.add('active');
    return true;
  }

  function validarPesos() {
    const archivos = ['clp_credito','clp_debito','clp_prepago','clp_bordero','clp_cartola'];
    const faltantes = archivos.filter(id => !document.getElementById(id).files.length);

    if (faltantes.length > 0) {
      alert('Faltan ' + faltantes.length + ' archivo(s) por seleccionar. Sube los 5 archivos antes de procesar.');
      return false;
    }

    const overlay = document.getElementById('loadingOverlay');
    overlay.querySelector('h4').innerText = 'Procesando 5 archivos...';
    overlay.querySelector('p').innerText = 'Cruzando Transbank, Borderó y Cartola. Por favor espere.';
    overlay.classList.add('active');
    return true;
  }

  // Función para actualizar UI cuando se selecciona un archivo
  function updateUI(inputId) {
    const fileInput = document.getElementById(inputId);
    let fileName = "Ningún archivo";
    let hasFile = false;
    
    if(fileInput.files.length > 0) {
        fileName = fileInput.files[0].name;
        hasFile = true;
    }

    // Update Classic UI
    const lblClassic = document.getElementById('lbl_' + inputId);
    const cardClassic = document.getElementById('card_' + inputId);
    if(lblClassic) lblClassic.innerText = fileName;
    if(cardClassic) {
        if(hasFile) cardClassic.classList.add('loaded');
        else cardClassic.classList.remove('loaded');
    }

    // Update Wizard UI
    const lblWizard = document.getElementById('wiz_lbl_' + inputId);
    const cardWizard = document.getElementById('wiz_card_' + inputId);
    if(lblWizard) lblWizard.innerText = fileName;
    if(cardWizard) {
        if(hasFile) cardWizard.classList.add('loaded');
        else cardWizard.classList.remove('loaded');
    }
  }

  // Función para cambiar entre modos (Toggle)
  function toggleMode() {
    const isWizard = document.getElementById('modeSwitch').checked;
    
    const classicDiv = document.getElementById('classicMode');
    const wizardDiv = document.getElementById('wizardMode');
    const label = document.getElementById('modeLabel');

    if(isWizard) {
        classicDiv.classList.add('d-none');
        wizardDiv.classList.remove('d-none');
        label.innerText = "Modo Asistente (Paso a Paso)";
        showStep(1);
    } else {
        classicDiv.classList.remove('d-none');
        wizardDiv.classList.add('d-none');
        label.innerText = "Modo Clásico (Todo en uno)";
    }
  }

  // Lógica del Wizard
  let currentStep = 1;
  function showStep(step) {
    document.querySelectorAll('.wizard-step').forEach(el => el.classList.add('d-none'));
    document.querySelectorAll('.step-indicator').forEach(el => el.classList.remove('active'));
    document.getElementById('step' + step).classList.remove('d-none');
    document.getElementById('indStep' + step).classList.add('active');
    currentStep = step;
  }

  // Formateo de moneda y Coloreado de Tablas
  function formatCurrency(value) {
    const num = parseFloat(value);
    if (isNaN(num)) return value;
    return num.toLocaleString('es-CL', { minimumFractionDigits: 0, maximumFractionDigits: 2 });
  }

  document.addEventListener("DOMContentLoaded", function() {
    const tables = document.querySelectorAll("table:not(.tabla-clp)");
    tables.forEach(table => {
        const rows = table.querySelectorAll("tbody tr");
        const lastIdx = rows.length - 1;
        rows.forEach((row, idx) => {
            const cells = row.querySelectorAll("td");
            
            // Formatear celdas numéricas (columnas 2, 3, 4)
            cells.forEach((cell, cellIdx) => {
                if (cellIdx >= 1 && cellIdx <= 3) {
                    const val = cell.innerText.trim();
                    const num = parseFloat(val);
                    if (!isNaN(num)) {
                        cell.innerText = formatCurrency(val);
                    }
                }
            });
            
            // Resaltar fila de Totales completa
            if (idx === lastIdx) {
                row.classList.add('fw-bold');
                row.style.backgroundColor = '#fff3cd';
                cells.forEach(cell => cell.style.fontWeight = 'bold');
            }
            
            // Colorear la columna de Diferencia
            if (cells.length === 4) {
                const diffCell = cells[3];
                const valStr = diffCell.innerText.trim().replace(/[^0-9.,-]+/g,"").replace(",",".");
                const val = parseFloat(valStr);
                if (!isNaN(val)) {
                    if (val === 0) {
                        diffCell.classList.add("text-success", "fw-bold");
                        diffCell.innerHTML = "<i class='bi bi-check-circle-fill'></i> 0";
                    } else {
                        diffCell.classList.add("text-danger", "fw-bold");
                    }
                }
            }
        });
    });
  });