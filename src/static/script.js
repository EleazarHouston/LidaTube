const get_wanted_lidarr = document.getElementById('get-lidarr-wanted-btn');
const stop_lidarr = document.getElementById('stop-lidarr-btn');
const reset_lidarr = document.getElementById('reset-lidarr-btn');
const lidarr_spinner = document.getElementById('lidarr-spinner');
const lidarr_progress_bar = document.getElementById('lidarr-progress-status-bar-inner');
const lidarr_scan_status_text = document.getElementById('lidarr-scan-status-text');
const lidarr_table = document.getElementById('lidarr-table').getElementsByTagName('tbody')[0];
const select_all_checkbox = document.getElementById('select-all-checkbox');

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
        ytdlp_status_text.textContent = 'Idle';
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

get_wanted_lidarr.addEventListener('click', function () {
    if (get_wanted_lidarr.disabled) {
        return;
    }
    lidarr_table.innerHTML = '';
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
    lidarr_table.innerHTML = '';
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

    const checked_indices = [];
    const checkboxes = document.getElementsByName('lidarr_item');

    checkboxes.forEach((checkbox) => {
        if (checkbox.checked) {
            checked_indices.push(parseInt(checkbox.dataset.index, 10));
        }
    });

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
    ytdlp_table.innerHTML = '';
    update_progress_bar(0, 'idle');
    set_ytdlp_button_states('idle', 0);
    show_toast('Downloads', 'Reset requested. Clearing queue...');
});

socket.on('lidarr_update', (response) => {
    const status = response.status || 'idle';
    const scan_progress = response.scan_progress || {};
    update_lidarr_progress_bar(status, scan_progress);

    if (response.data === null || response.data === undefined) {
        set_lidarr_button_states(status, lidarr_table.rows.length);
        return;
    }

    const items = Array.isArray(response.data) ? response.data : [];
    const total_count = response.total_count ?? 0;

    if (total_count > 0 && items.length === 0) {
        lidarr_count_text.textContent = `All ${total_count.toLocaleString()} albums downloaded`;
    } else if (total_count > 0) {
        lidarr_count_text.textContent = `${items.length.toLocaleString()} with missing tracks (${total_count.toLocaleString()} total)`;
    } else {
        lidarr_count_text.textContent = '';
    }

    lidarr_table.innerHTML = '';

    let all_checked = true;
    items.forEach((item, i) => {
        if (!item.checked) {
            all_checked = false;
        }
        const row = lidarr_table.insertRow();
        const cell1 = row.insertCell(0);
        const cell2 = row.insertCell(1);
        const cell3 = row.insertCell(2);

        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'form-check-input';
        checkbox.id = 'lidarr_' + i;
        checkbox.name = 'lidarr_item';
        checkbox.checked = item.checked;
        checkbox.dataset.index = item.index ?? i;
        checkbox.addEventListener('change', check_if_all_true);

        const label = document.createElement('label');
        label.className = 'form-check-label';
        label.htmlFor = 'lidarr_' + i;
        label.textContent = item.artist + ' - ' + item.album_name;

        cell1.appendChild(checkbox);
        cell2.appendChild(label);
        cell3.textContent = item.scan_ready === false ? 'Scanning...' : `${item.missing_count}/${item.track_count}`;
        cell3.classList.add('text-center');
    });

    select_all_checkbox.checked = items.length > 0 ? all_checked : false;
    set_lidarr_button_states(status, items.length);
});

socket.on('ytdlp_update', (response) => {
    const items = Array.isArray(response.data) ? response.data : [];
    ytdlp_table.innerHTML = '';
    items.forEach((entry) => {
        const row = ytdlp_table.insertRow();
        const cell_item = row.insertCell(0);
        const cell_item_status = row.insertCell(1);

        cell_item.textContent = `${entry.artist} - ${entry.album_name}`;
        cell_item_status.textContent = entry.status;
        cell_item_status.classList.add('text-center');
    });

    pending_download_request = false;
    const percent_completion = response.percent_completion || 0;
    const actual_status = response.status || 'idle';
    update_progress_bar(percent_completion, actual_status);
    set_ytdlp_button_states(actual_status, items.length);
});

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

const theme_switch = document.getElementById('theme-switch');
const saved_theme = localStorage.getItem('theme');
const saved_switch_position = localStorage.getItem('switchPosition');

if (saved_switch_position) {
    theme_switch.checked = saved_switch_position === 'true';
}

if (saved_theme) {
    document.documentElement.setAttribute('data-bs-theme', saved_theme);
}

theme_switch.addEventListener('click', () => {
    if (document.documentElement.getAttribute('data-bs-theme') === 'dark') {
        document.documentElement.setAttribute('data-bs-theme', 'light');
    } else {
        document.documentElement.setAttribute('data-bs-theme', 'dark');
    }
    localStorage.setItem('theme', document.documentElement.getAttribute('data-bs-theme'));
    localStorage.setItem('switchPosition', theme_switch.checked);
});

update_lidarr_progress_bar('idle', { phase: 'Idle', percent: 0 });
update_progress_bar(0, 'idle');
set_lidarr_button_states('idle', 0);
set_ytdlp_button_states('idle', 0);
