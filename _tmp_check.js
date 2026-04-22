
    const form = document.getElementById('jobForm');
    const startBtn = document.getElementById('startBtn');
    const discoverBtn = document.getElementById('discoverBtn');
    const cancelBtn = document.getElementById('cancelBtn');
    const municipalityWrap = document.getElementById('municipalityWrap');
    const municipalityList = document.getElementById('municipalityList');
    const selectAllBtn = document.getElementById('selectAllBtn');
    const clearAllBtn = document.getElementById('clearAllBtn');
    const statusText = document.getElementById('statusText');
    const phaseText = document.getElementById('phaseText');
    const progressText = document.getElementById('progressText');
    const jobIdText = document.getElementById('jobIdText');
    const logBox = document.getElementById('logBox');
    const errorText = document.getElementById('errorText');
    const resultWrap = document.getElementById('resultWrap');

    let currentJobId = null;
    let pollTimer = null;
    let discoveredMunicipalities = [];

    function esc(s) {
      return String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
    }

    function setError(msg) {
      if (!msg) {
        errorText.style.display = 'none';
        errorText.textContent = '';
        return;
      }
      errorText.style.display = 'block';
      errorText.textContent = msg;
    }

    function renderResult(result) {
      if (!result) {
        resultWrap.style.display = 'none';
        resultWrap.innerHTML = '';
        return;
      }

      const rowsHtml = (result.rows || []).map(r => `
        <tr>
          <td>${esc(r.customer)}</td>
          <td>${esc(r.commodity)}</td>
          <td>${esc(r.contract_number)}</td>
          <td>${esc(r.supplier)}</td>
          <td>${esc(r.unit_price)}</td>
          <td>${esc(r.benchmark)}</td>
          <td>${esc(r.diff)}</td>
          <td>${esc(r.monthly_savings_eur)}</td>
          <td>${esc(r.confidence)} / ${esc(r.savings_method)}</td>
          <td>
            <details>
              <summary>Predmet + koncept</summary>
              <div><strong>${esc(r.subject)}</strong></div>
              <pre>${esc(r.concept)}</pre>
            </details>
          </td>
        </tr>
      `).join('');

      const source = String(result.export_url || '');
      const sourceHtml = source.startsWith('manual://')
        ? `<span>${esc(source)}</span>`
        : `<a href="${esc(source)}" target="_blank">${esc(source)}</a>`;

      resultWrap.style.display = 'block';
      resultWrap.innerHTML = `
        <div class="card">
          <h2>Vysledok</h2>
          <div class="meta">
            <div><strong>Okres:</strong> ${esc(result.district)}</div>
            <div><strong>Pocet obci so zmluvou:</strong> ${esc(result.count)}</div>
            <div><strong>Export CRZ datum:</strong> ${esc(result.export_date)}</div>
            <div><strong>Pouzite zdroje (dni):</strong> ${esc(result.source_count || 1)}</div>
            <div><strong>Zdroj:</strong> ${sourceHtml}</div>
            <div><strong>Sucet odhadovanej mesacnej uspory:</strong> ${esc(result.total_monthly_savings || 'N/A')}</div>
            <div><strong>Subjekty s vypoctom uspory:</strong> ${esc(result.total_count_with_savings || 0)}</div>
            <div><strong>Export:</strong> <a href="/export/${esc(result.job_id)}.csv" target="_blank">Stiahnut CSV konceptov</a></div>
          </div>
        </div>

        <div class="card">
          <h2>Najrelevantnejsi email koncept</h2>
          <p><strong>Predmet:</strong> ${esc(result.subject)}</p>
          <p><strong>Odhad uspory tento mesiac:</strong> ${esc(result.top_monthly_savings || 'N/A')} (${esc(result.top_confidence || 'n/a')} / ${esc(result.top_method || 'n/a')})</p>
          <pre>${esc(result.concept)}</pre>
        </div>

        <div class="card">
          <h2>Najdene zmluvy a kompletne koncepty</h2>
          <table>
            <thead>
              <tr>
                <th>Obec/Mesto</th>
                <th>Komodita</th>
                <th>Zmluva</th>
                <th>Dodavatel</th>
                <th>Cena v zmluve</th>
                <th>Benchmark</th>
                <th>Rozdiel</th>
                <th>Uspora / mesiac</th>
                <th>Istota / metoda</th>
                <th>Koncept</th>
              </tr>
            </thead>
            <tbody>${rowsHtml}</tbody>
          </table>
        </div>
      `;
    }

    function renderMunicipalities(options) {
      discoveredMunicipalities = options || [];
      if (!discoveredMunicipalities.length) {
        municipalityWrap.style.display = 'none';
        municipalityList.innerHTML = '';
        startBtn.disabled = true;
        return;
      }
      municipalityWrap.style.display = 'block';
      municipalityList.innerHTML = discoveredMunicipalities.map((o, idx) => `
        <label style="display:flex; gap:8px; align-items:flex-start; border:1px solid #e5e7eb; border-radius:8px; padding:6px;">
          <input type="checkbox" class="muni-checkbox" value="${esc(o.name)}" checked>
          <span><strong>${esc(o.name)}</strong><br><small>${o.contracts === null || o.contracts === undefined ? 'nezistene' : esc(o.contracts)} zmluv, posledna ${esc(o.latest || 'N/A')}</small></span>
        </label>
      `).join('');
      startBtn.disabled = false;
    }

    function getSelectedMunicipalities() {
      return Array.from(document.querySelectorAll('.muni-checkbox:checked')).map(x => x.value);
    }

    async function pollStatus() {
      if (!currentJobId) return;
      try {
        const res = await fetch(`/status/${currentJobId}`);
        const data = await res.json();
        if (!data.ok) {
          setError(data.error || 'Nepodarilo sa nacitat status jobu.');
          return;
        }

        const job = data.job;
        statusText.textContent = job.status;
        phaseText.textContent = job.phase || '-';
        progressText.textContent = `${job.progress_current || 0} / ${job.progress_total || 0}`;
        logBox.textContent = (job.logs || []).join('\n');
        logBox.scrollTop = logBox.scrollHeight;

        if (job.error) {
          setError(job.error);
        }

        if (job.status === 'completed') {
          renderResult(job.result);
          startBtn.disabled = false;
          cancelBtn.disabled = true;
          clearInterval(pollTimer);
          pollTimer = null;
        }

        if (job.status === 'failed' || job.status === 'cancelled') {
          renderResult(null);
          startBtn.disabled = false;
          cancelBtn.disabled = true;
          clearInterval(pollTimer);
          pollTimer = null;
        }
      } catch (e) {
        setError('Chyba spojenia so serverom pri nacitani statusu.');
      }
    }

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const district = document.getElementById('district').value.trim();
      if (!district) {
        setError('Zadaj okres.');
        return;
      }

      const selected = getSelectedMunicipalities();
      if (!selected.length) {
        setError('Oznac aspon jednu obec na spracovanie.');
        return;
      }

      setError('');
      resultWrap.style.display = 'none';
      resultWrap.innerHTML = '';
      logBox.textContent = 'Spustam job...';
      startBtn.disabled = true;
      cancelBtn.disabled = false;
      statusText.textContent = 'starting';
      phaseText.textContent = 'starting';
      progressText.textContent = '0 / 0';

      const formData = new FormData();
      formData.append('district', district);
      formData.append('selected_municipalities', JSON.stringify(selected));
      const zipInput = document.getElementById('crz_zip');
      if (zipInput && zipInput.files && zipInput.files.length > 0) {
        formData.append('crz_zip', zipInput.files[0]);
      }

      try {
        const res = await fetch('/start', { method: 'POST', body: formData });
        const data = await res.json();
        if (!data.ok) {
          setError(data.error || 'Nepodarilo sa spustit job.');
          startBtn.disabled = false;
          cancelBtn.disabled = true;
          return;
        }

        currentJobId = data.job_id;
        jobIdText.textContent = currentJobId;
        statusText.textContent = 'running';
        phaseText.textContent = 'download_parse';

        if (pollTimer) clearInterval(pollTimer);
        pollTimer = setInterval(pollStatus, 1500);
        pollStatus();
      } catch (err) {
        setError('Server neodpoveda pri starte jobu.');
        startBtn.disabled = false;
        cancelBtn.disabled = true;
      }
    });

    discoverBtn.addEventListener('click', async () => {
      const district = document.getElementById('district').value.trim();
      if (!district) {
        setError('Zadaj okres.');
        return;
      }
      setError('');
      discoverBtn.disabled = true;
      discoverBtn.textContent = 'Hladam obce...';
      startBtn.disabled = true;
      cancelBtn.disabled = true;
      statusText.textContent = 'discovering';
      logBox.textContent = 'Zacinam hladat obce v CRZ...';
      municipalityWrap.style.display = 'none';
      municipalityList.innerHTML = '';

      const formData = new FormData();
      formData.append('district', district);
      const zipInput = document.getElementById('crz_zip');
      if (zipInput && zipInput.files && zipInput.files.length > 0) {
        formData.append('crz_zip', zipInput.files[0]);
      }

      try {
        const res = await fetch('/discover', { method: 'POST', body: formData });
        const data = await res.json();
        if (!data.ok) {
          setError(data.error || 'Nepodarilo sa najst obce.');
          statusText.textContent = 'idle';
          return;
        }
        if (data.warning) {
          setError(`Poznamka: ${data.warning}`);
        }
        if (Array.isArray(data.logs) && data.logs.length) {
          logBox.textContent = data.logs.join('\n');
        } else {
          logBox.textContent = 'Obce nacitane.';
        }
        statusText.textContent = 'ready';
        renderMunicipalities(data.options || []);
      } catch (e) {
        setError('Chyba spojenia pri hladani obci.');
        statusText.textContent = 'idle';
      } finally {
        discoverBtn.disabled = false;
        discoverBtn.textContent = 'Najst obce v CRZ';
      }
    });

    selectAllBtn.addEventListener('click', () => {
      document.querySelectorAll('.muni-checkbox').forEach(x => x.checked = true);
    });

    clearAllBtn.addEventListener('click', () => {
      document.querySelectorAll('.muni-checkbox').forEach(x => x.checked = false);
    });

    cancelBtn.addEventListener('click', async () => {
      if (!currentJobId) {
        setError('Cancel funguje az po starte spracovania vybranych obci.');
        return;
      }
      cancelBtn.disabled = true;
      try {
        const res = await fetch(`/cancel/${currentJobId}`, { method: 'POST' });
        const data = await res.json();
        if (!data.ok) {
          setError(data.error || 'Cancel sa nepodaril.');
          cancelBtn.disabled = false;
          return;
        }
        statusText.textContent = 'cancelling';
      } catch (err) {
        setError('Cancel zlyhal (server neodpoveda).');
        cancelBtn.disabled = false;
      }
    });
  