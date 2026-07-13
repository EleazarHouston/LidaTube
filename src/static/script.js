const get_wanted_lidarr = document.getElementById('get-lidarr-wanted-btn');
const stop_lidarr = document.getElementById('stop-lidarr-btn');
const reset_lidarr = document.getElementById('reset-lidarr-btn');
const lidarr_spinner = document.getElementById('lidarr-spinner');
const lidarr_progress_bar = document.getElementById('lidarr-progress-status-bar-inner');
const lidarr_scan_status_text = document.getElementById('lidarr-scan-status-text');
const lidarr_table = document.getElementById('lidarr-table').getElementsByTagName('tbody')[0];
const select_all_checkbox = document.getElementById('select-all-checkbox');
const lidarr_search = document.getElementById('lidarr-search');
const history_table = document.querySelector('#history-table tbody');
const override_table = document.querySelector('#override-table tbody');

const start_ytdlp = document.getElementById('start-ytdlp-btn');
const stop_ytdlp = document.getElementById('stop-ytdlp-btn');
const reset_ytdlp = document.getElementById('reset-ytdlp-btn');
const ytdlp_spinner = document.getElementById('ytdlp-spinner');
const ytdlp_progress_bar = document.getElementById('ytdlp-progress-status-bar-inner');
const ytdlp_status_text = document.getElementById('ytdlp-status-text');
const ytdlp_table = document.getElementById('ytdlp-table').getElementsByTagName('tbody')[0];

const config_modal = document.getElementById('config-modal');
const save_message = document.getElementById('save-message');
const save_changes_button = document.getElementById('save-changes-btn');
const lidarr_address = document.getElementById('lidarr-address');
const lidarr_api_key = document.getElementById('lidarr-api-key');
const sleep_interval = document.getElementById('sleep-interval');
const sync_schedule = document.getElementById('sync-schedule');
const minimum_match_ratio = document.getElementById('minimum-match-ratio');
const lidarr_count_text = document.getElementById('lidarr-count-text');
const cookies_status_badge = document.getElementById('cookies-status-badge');
const cookies_file_input = document.getElementById('cookies-file-input');
const upload_cookies_btn = document.getElementById('upload-cookies-btn');
const delete_cookies_btn = document.getElementById('delete-cookies-btn');
const socket = io();

let pending_download_request = false;
let pending_settings_save = false;
// Until the server sends its first queue snapshot, its state is unknown. In
// particular, do not present Idle just because the page loaded before SocketIO.
let has_received_ytdlp_update = false;

function set_button_loading(button, spinner, is_loading) {
    if (spinner) {
        spinner.classList.toggle('d-none', !is_loading);
    }
    if (is_loading) {
        button.setAttribute('aria-busy', 'true');
    } else {
        button.removeAttribute('aria-busy');
    }
}

function update_lidarr_progress_bar(status, scan_progress) {
    let percent = Number(scan_progress.percent ?? 0);
    if (!Number.isFinite(percent)) {
        percent = 0;
    }
    if (status === 'complete') {
        percent = 100;
    } else if (status === 'idle') {
        percent = 0;
    }
    percent = Math.max(0, Math.min(100, Math.round(percent)));

    lidarr_progress_bar.style.width = percent + '%';
    lidarr_progress_bar.setAttribute('aria-valuenow', percent);
    lidarr_progress_bar.textContent = percent + '%';
    lidarr_progress_bar.classList.remove('bg-primary', 'bg-success', 'bg-warning', 'bg-danger', 'bg-dark', 'progress-bar-animated');

    if (status === 'busy') {
        lidarr_progress_bar.classList.add('bg-success', 'progress-bar-animated');
    } else if (status === 'stopped') {
        lidarr_progress_bar.classList.add('bg-warning');
    } else if (status === 'complete') {
        lidarr_progress_bar.classList.add('bg-dark');
    } else if (status === 'error') {
        lidarr_progress_bar.classList.add('bg-danger');
    } else {
        lidarr_progress_bar.classList.add('bg-primary');
    }

    const phase = scan_progress.phase || 'Idle';
    let extra_detail = '';
    if (status === 'busy' && (phase === 'Fetching wanted albums' || phase === 'Fetching albums & tracks')) {
        extra_detail = `Pages: ${scan_progress.pages_scanned || 0}, Albums: ${scan_progress.albums_discovered || 0}, Tracks scanned: ${scan_progress.albums_processed || 0}`;
    } else if (status === 'busy' && phase === 'Fetching missing tracks') {
        extra_detail = `Albums: ${scan_progress.albums_processed || 0}/${scan_progress.albums_total || 0}`;
    }
    lidarr_scan_status_text.textContent = extra_detail ? `${phase} (${extra_detail})` : phase;
}

function update_progress_bar(percentage, status) {
    let percent = Number(percentage);
    if (!Number.isFinite(percent)) {
        percent = 0;
    }
    percent = Math.max(0, Math.min(100, Math.round(percent)));

    ytdlp_progress_bar.style.width = percent + '%';
    ytdlp_progress_bar.setAttribute('aria-valuenow', percent);
    ytdlp_progress_bar.textContent = percent + '%';
    ytdlp_progress_bar.classList.remove('bg-primary', 'bg-danger', 'bg-dark', 'bg-warning', 'bg-success', 'progress-bar-animated');
    ytdlp_progress_bar.classList.add('progress-bar-striped');

    if (status === 'running') {
        ytdlp_progress_bar.classList.add('bg-success', 'progress-bar-animated');
        ytdlp_status_text.textContent = 'Downloading';
    } else if (status === 'stopped') {
        ytdlp_progress_bar.classList.add('bg-warning');
        ytdlp_status_text.textContent = 'Stopped';
    } else if (status === 'idle') {
        ytdlp_progress_bar.classList.add('bg-primary');
        ytdlp_status_text.textContent = 'Idle';
    } else if (status === 'complete') {
        ytdlp_progress_bar.classList.add('bg-dark');
        ytdlp_status_text.textContent = 'Complete';
    } else if (status === 'failed') {
        ytdlp_progress_bar.classList.add('bg-danger');
        ytdlp_status_text.textContent = 'Failed';
    } else {
        ytdlp_progress_bar.classList.add('bg-primary');
        ytdlp_status_text.textContent = 'Loading';
    }
}

function set_lidarr_button_states(status, row_count) {
    const is_busy = status === 'busy';
    const has_rows = row_count > 0;

    get_wanted_lidarr.disabled = is_busy;
    stop_lidarr.disabled = !is_busy;
    reset_lidarr.disabled = is_busy ? false : !has_rows && status === 'idle';
    set_button_loading(get_wanted_lidarr, lidarr_spinner, is_busy);

    select_all_checkbox.style.visibility = has_rows ? 'visible' : 'hidden';
}

function set_ytdlp_button_states(status, row_count) {
    const is_running = status === 'running';
    const has_rows = row_count > 0;

    start_ytdlp.disabled = is_running || pending_download_request;
    stop_ytdlp.disabled = !is_running;
    reset_ytdlp.disabled = !is_running && !has_rows && status === 'idle';
    set_button_loading(start_ytdlp, ytdlp_spinner, is_running || pending_download_request);
}

function check_if_all_true() {
    let all_checked = true;
    const checkboxes = document.querySelectorAll('input[name="lidarr_item"]');
    checkboxes.forEach((checkbox) => {
        if (!checkbox.checked) {
            all_checked = false;
        }
    });
    select_all_checkbox.checked = all_checked;
}

async function fetch_json(url, options) {
    const response = await fetch(url, options);
    let data = {};
    try {
        data = await response.json();
    } catch (e) {
        // Leave as empty object for fallback error text.
    }

    if (!response.ok) {
        const message = data.error || data.message || `Request failed (${response.status})`;
        throw new Error(message);
    }
    return data;
}

select_all_checkbox.addEventListener('change', function () {
    const is_checked = this.checked;
    const checkboxes = document.querySelectorAll('input[name="lidarr_item"]');
    checkboxes.forEach((checkbox) => {
        checkbox.checked = is_checked;
    });
});

// Event delegation: a single listener handles every row checkbox, instead of
// attaching (and leaking) one listener per row on each table rebuild.
lidarr_table.addEventListener('change', function (event) {
    if (event.target && event.target.name === 'lidarr_item') {
        const index = Number(event.target.dataset.index);
        if (event.target.checked) selected_lidarr_indices.add(index); else selected_lidarr_indices.delete(index);
        check_if_all_true();
    }
});

get_wanted_lidarr.addEventListener('click', function () {
    if (get_wanted_lidarr.disabled) {
        return;
    }
    selected_lidarr_indices.clear();
    lidarr_table.replaceChildren();
    select_all_checkbox.checked = false;
    set_button_loading(get_wanted_lidarr, lidarr_spinner, true);
    show_toast('Lidarr', 'Refreshing wanted albums and tracks...');
    socket.emit('lidarr_get_wanted');
});

stop_lidarr.addEventListener('click', function () {
    if (stop_lidarr.disabled) {
        return;
    }
    show_toast('Lidarr', 'Stopping refresh...');
    socket.emit('stop_lidarr');
});

reset_lidarr.addEventListener('click', function () {
    if (!confirm('Reset Lidarr state and clear cached albums/tracks?')) {
        return;
    }
    socket.emit('reset_lidarr');
    selected_lidarr_indices.clear();
    lidarr_table.replaceChildren();
    select_all_checkbox.checked = false;
    lidarr_count_text.textContent = '';
    update_lidarr_progress_bar('idle', { phase: 'Idle', percent: 0 });
    set_lidarr_button_states('idle', 0);
    show_toast('Lidarr', 'Reset requested. Clearing cached albums/tracks...');
});

async function update_cookies_status() {
    try {
        const data = await fetch_json('/cookies_status');
        if (data.exists) {
            cookies_status_badge.textContent = 'File present';
            cookies_status_badge.className = 'badge bg-success';
        } else {
            cookies_status_badge.textContent = 'No file';
            cookies_status_badge.className = 'badge bg-secondary';
        }
    } catch (error) {
        cookies_status_badge.textContent = 'Status unavailable';
        cookies_status_badge.className = 'badge bg-warning text-dark';
    }
}

upload_cookies_btn.addEventListener('click', async function () {
    const file = cookies_file_input.files[0];
    if (!file) {
        show_toast('Upload Error', 'Please select a cookies.txt file first.');
        return;
    }

    upload_cookies_btn.disabled = true;
    delete_cookies_btn.disabled = true;
    const form_data = new FormData();
    form_data.append('cookies_file', file);

    try {
        const data = await fetch_json('/upload_cookies', { method: 'POST', body: form_data });
        show_toast('Cookies', data.message || 'Cookies file uploaded successfully');
        cookies_file_input.value = '';
        await update_cookies_status();
    } catch (error) {
        show_toast('Upload Error', error.message);
    } finally {
        upload_cookies_btn.disabled = false;
        delete_cookies_btn.disabled = false;
    }
});

delete_cookies_btn.addEventListener('click', async function () {
    if (!confirm('Delete cookies.txt from the server config folder?')) {
        return;
    }

    upload_cookies_btn.disabled = true;
    delete_cookies_btn.disabled = true;

    try {
        const data = await fetch_json('/delete_cookies', { method: 'DELETE' });
        show_toast('Cookies', data.message || 'Cookies file deleted');
        await update_cookies_status();
    } catch (error) {
        show_toast('Cookies Error', error.message);
    } finally {
        upload_cookies_btn.disabled = false;
        delete_cookies_btn.disabled = false;
    }
});

config_modal.addEventListener('show.bs.modal', function () {
    socket.emit('load_settings');
    update_cookies_status();

    function handle_settings_loaded(settings) {
        lidarr_address.value = settings.lidarr_address;
        lidarr_api_key.value = settings.lidarr_api_key;
        sleep_interval.value = settings.sleep_interval;
        sync_schedule.value = settings.sync_schedule.join(', ');
        minimum_match_ratio.value = settings.minimum_match_ratio;
        socket.off('settings_loaded', handle_settings_loaded);
    }

    socket.on('settings_loaded', handle_settings_loaded);
});

save_changes_button.addEventListener('click', () => {
    if (pending_settings_save) {
        return;
    }

    pending_settings_save = true;
    save_changes_button.disabled = true;
    save_message.className = 'alert alert-info mt-3';
    save_message.textContent = 'Saving settings...';
    save_message.style.display = 'block';

    socket.emit('update_settings', {
        lidarr_address: lidarr_address.value,
        lidarr_api_key: lidarr_api_key.value,
        sleep_interval: sleep_interval.value,
        sync_schedule: sync_schedule.value,
        minimum_match_ratio: minimum_match_ratio.value,
    });

    setTimeout(() => {
        pending_settings_save = false;
        save_changes_button.disabled = false;
        save_message.style.display = 'none';
    }, 1200);
});

start_ytdlp.addEventListener('click', function () {
    if (start_ytdlp.disabled) {
        return;
    }

    const checked_indices = Array.from(selected_lidarr_indices);

    if (checked_indices.length === 0) {
        show_toast('Downloads', 'Select at least one album before starting download.');
        return;
    }

    pending_download_request = true;
    set_ytdlp_button_states('idle', ytdlp_table.rows.length);
    show_toast('Downloads', `Adding ${checked_indices.length} album(s) to queue...`);
    socket.emit('add_to_download_list', checked_indices);
});

stop_ytdlp.addEventListener('click', function () {
    if (stop_ytdlp.disabled) {
        return;
    }
    show_toast('Downloads', 'Stopping downloads...');
    socket.emit('stop_ytdlp');
});

reset_ytdlp.addEventListener('click', function () {
    if (!confirm('Reset download queue and clear progress?')) {
        return;
    }
    socket.emit('reset_ytdlp');
    ytdlp_table.replaceChildren();
    update_progress_bar(0, 'loading');
    set_ytdlp_button_states('idle', 0);
    show_toast('Downloads', 'Reset requested. Clearing queue...');
});

const selected_lidarr_indices = new Set();
let lidarr_offset = 0;
let lidarr_total = 0;
let lidarr_status = 'idle';
let lidarr_loading = false;
let lidarr_search_timer;
async function load_lidarr_page(reset = false) {
    if (lidarr_loading || (!reset && lidarr_offset >= lidarr_total && lidarr_total !== 0)) return;
    lidarr_loading = true;
    if (reset) { lidarr_offset = 0; lidarr_total = 0; lidarr_table.replaceChildren(); }
    try {
        const query = encodeURIComponent(lidarr_search.value.trim());
        const page = await fetch_json(`/api/lidarr?limit=100&offset=${lidarr_offset}&q=${query}`);
        const fragment = document.createDocumentFragment();
        page.items.forEach((item) => { const row=document.createElement('tr'); row.innerHTML=`<td><input class="form-check-input" type="checkbox" name="lidarr_item" data-index="${item.index}" ${selected_lidarr_indices.has(item.index) || item.checked ? 'checked' : ''}></td><td></td><td class="text-center"></td>`; row.children[1].textContent=`${item.artist} - ${item.album_name}`; row.children[2].textContent=item.scan_ready === false ? 'Scanning...' : `${item.missing_count}/${item.track_count}`; fragment.appendChild(row); });
        lidarr_table.appendChild(fragment); while (lidarr_table.rows.length > 300) lidarr_table.deleteRow(0); lidarr_offset += page.items.length; lidarr_total = page.total;
        lidarr_count_text.textContent = lidarr_total ? `${lidarr_total.toLocaleString()} with missing tracks` : 'All albums downloaded';
        set_lidarr_button_states(lidarr_status, lidarr_total);
    } catch (error) { show_toast('Lidarr', error.message); } finally { lidarr_loading = false; }
}
const lidarr_observer = new IntersectionObserver((entries) => { if (entries[0].isIntersecting) load_lidarr_page(); });
const lidarr_sentinel = document.createElement('div'); lidarr_table.parentElement.appendChild(lidarr_sentinel); lidarr_observer.observe(lidarr_sentinel);
lidarr_search.addEventListener('input', () => { clearTimeout(lidarr_search_timer); lidarr_search_timer=setTimeout(() => load_lidarr_page(true), 250); });
socket.on('lidarr_update', (response) => { lidarr_status = response.status || 'idle'; update_lidarr_progress_bar(lidarr_status, response.scan_progress || {}); set_lidarr_button_states(lidarr_status, lidarr_total); if (response.data !== null) load_lidarr_page(true); });

socket.on('ytdlp_update', (response) => {
    const items = Array.isArray(response.data) ? response.data : [];
    const fragment = document.createDocumentFragment();
    items.forEach((entry) => {
        const row = document.createElement('tr');

        const cell_item = document.createElement('td');
        cell_item.textContent = `${entry.artist} - ${entry.album_name}`;

        const cell_item_status = document.createElement('td');
        cell_item_status.className = 'text-center';
        cell_item_status.textContent = entry.status;

        row.append(cell_item, cell_item_status);
        fragment.appendChild(row);
    });
    ytdlp_table.replaceChildren(fragment);

    pending_download_request = false;
    has_received_ytdlp_update = true;
    const percent_completion = response.percent_completion || 0;
    const actual_status = typeof response.status === 'string' ? response.status : 'loading';
    update_progress_bar(percent_completion, actual_status);
    set_ytdlp_button_states(actual_status, items.length);
});

async function show_session_tracks(row, sessionId) { const details = row.nextElementSibling; if (details && details.classList.contains('session-details')) { details.remove(); return; } const page = await fetch_json(`/api/sessions/${sessionId}/tracks?limit=200&offset=0`); const detail = document.createElement('tr'); detail.className = 'session-details'; const cell = document.createElement('td'); cell.colSpan = 5; const table = document.createElement('table'); table.className = 'table table-sm mb-0'; page.items.forEach((track) => { const tr=document.createElement('tr'); tr.innerHTML=`<td>${track.track_title}</td><td>${track.outcome}</td><td>${track.suspicion}</td>`; if (track.outcome === 'no_match') { tr.style.cursor='pointer'; tr.addEventListener('click', async () => { const evaluations=await fetch_json(`/api/track/${track.id}/evaluations`); const reasons=evaluations.items.map((e) => `${e.candidate_title || 'Unknown'} — ${e.rejected_by}`).join('; ') || 'No candidates returned'; show_toast('Match evaluation', reasons); }); } table.appendChild(tr); }); cell.appendChild(table); detail.appendChild(cell); row.after(detail); }
async function load_history() { const page = await fetch_json('/api/sessions?limit=100&offset=0'); history_table.replaceChildren(...page.items.map((s) => { const r=document.createElement('tr'); r.innerHTML=`<td>${new Date(s.started_at).toLocaleString()}</td><td>${s.status}</td><td>${s.requested_count}</td><td>${s.matched_count}</td><td>${s.failed_count}</td>`; r.style.cursor='pointer'; r.addEventListener('click', () => show_session_tracks(r, s.id).catch((e) => show_toast('History', e.message))); return r; })); }
async function load_overrides() { const page = await fetch_json('/api/attention?limit=100&offset=0'); override_table.replaceChildren(...page.items.map((track) => { const r=document.createElement('tr'); const form=document.createElement('form'); form.className='d-flex gap-1'; form.innerHTML='<input class="form-control form-control-sm" required placeholder="https://youtube.com/..."><button class="btn btn-sm btn-primary">Save</button>'; form.addEventListener('submit', async (e) => { e.preventDefault(); await fetch_json('/api/override', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({track_id:track.track_id, forced_url:form.querySelector('input').value})}); show_toast('Override','Saved'); }); r.innerHTML=`<td>${track.artist} - ${track.track_title} <small class="text-muted">(${track.outcome})</small></td><td>${track.suspicion}</td>`; const c=document.createElement('td'); c.appendChild(form); r.appendChild(c); return r; })); }
document.getElementById('history-panel').addEventListener('show.bs.collapse', () => load_history().catch((e) => show_toast('History', e.message)));
document.getElementById('override-panel').addEventListener('show.bs.collapse', () => load_overrides().catch((e) => show_toast('Override', e.message)));

socket.on('new_toast_msg', function (data) {
    show_toast(data.title, data.message);
});

function show_toast(header, message) {
    const toast_container = document.querySelector('.toast-container');
    const toast_template = document.getElementById('toast-template').cloneNode(true);
    toast_template.classList.remove('d-none');

    toast_template.querySelector('.toast-header strong').textContent = header;
    toast_template.querySelector('.toast-body').textContent = message;
    toast_template.querySelector('.text-muted').textContent = new Date().toLocaleString();

    toast_container.appendChild(toast_template);

    const toast = new bootstrap.Toast(toast_template);
    toast.show();

    toast_template.addEventListener('hidden.bs.toast', function () {
        toast_template.remove();
    });
}

// The theme itself is applied before first paint by the inline script in <head>.
// Here we just keep the toggle switch in sync with whatever ended up active.
const theme_switch = document.getElementById('theme-switch');
theme_switch.checked = document.documentElement.getAttribute('data-bs-theme') === 'dark';

theme_switch.addEventListener('change', () => {
    const next_theme = theme_switch.checked ? 'dark' : 'light';
    document.documentElement.setAttribute('data-bs-theme', next_theme);
    localStorage.setItem('theme', next_theme);
});

update_lidarr_progress_bar('idle', { phase: 'Idle', percent: 0 });
update_progress_bar(0, 'loading');
set_lidarr_button_states('idle', 0);
set_ytdlp_button_states('idle', 0);
