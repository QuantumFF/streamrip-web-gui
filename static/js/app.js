// static/js/app.js

let currentTab = 'active';
let currentSearchType = 'album';
let currentPage = 1;
let itemsPerPage = 10;
let totalResults = 0;
let allSearchResults = [];


let eventSource = null;
let activeDownloads = new Map();
let downloadHistory = [];


// Non-blocking, stacking, auto-dismissing toast. Replaces every alert() so user
// feedback (Download accepted/failed, config saved, search/connection errors)
// never blocks the page or yanks focus. `type` is 'success' | 'error' | 'info'.
function showToast(message, type = 'info', duration = 4000) {
    const container = document.getElementById('toastContainer');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    toast.setAttribute('role', type === 'error' ? 'alert' : 'status');

    const dismiss = () => {
        if (toast.dataset.dismissed) return;
        toast.dataset.dismissed = '1';
        toast.classList.remove('visible');
        // Wait for the fade-out transition before removing from the DOM.
        setTimeout(() => toast.remove(), 200);
    };

    toast.addEventListener('click', dismiss);
    container.appendChild(toast);
    // Force a reflow so the entrance transition runs from the hidden state.
    requestAnimationFrame(() => toast.classList.add('visible'));

    if (duration > 0) {
        setTimeout(dismiss, duration);
    }

    return toast;
}


function initializeSSE() {
    if (eventSource) {
        eventSource.close();
    }
    
    eventSource = new EventSource('/api/events');
    
    
    eventSource.onerror = function(error) {
        console.error('SSE error:', error);
        setTimeout(initializeSSE, 5000);
    };
    
    eventSource.onmessage = function(event) {
        const data = JSON.parse(event.data);
        handleSSEMessage(data);
    };
}

function handleSSEMessage(data) {

    switch(data.type) {
        case 'download_queued':
            handleDownloadQueued(data);
            break;
        case 'download_started':
            handleDownloadStarted(data);
            break;
        case 'download_progress':
            handleDownloadProgress(data);
            break;
        case 'download_completed':
            handleDownloadCompleted(data);
            break;
        case 'download_error':
            handleDownloadError(data);
            break;
        case 'connected':
        case 'heartbeat':
            break;
        default:
            console.log('Unknown SSE message type:', data.type);
    }
}

// Pull the authoritative Active list + History from the server (ADR-0002) so a
// page refresh loses nothing. Runs before live SSE deltas are processed.
async function rehydrateState() {
    try {
        const response = await fetch('/api/status');
        if (!response.ok) return;
        const data = await response.json();

        activeDownloads.clear();
        (data.active || []).forEach(item => {
            activeDownloads.set(item.id, {
                id: item.id,
                url: item.url,
                quality: item.quality,
                metadata: item.metadata || {},
                status: item.status || 'queued',
                output: ''
            });
        });

        // History retains the original URL and quality (plus metadata) so the
        // Redownload slice can re-run a History entry verbatim.
        downloadHistory = (data.history || []).map(item => ({
            id: item.id,
            url: item.url,
            quality: item.quality,
            metadata: item.metadata || {},
            status: item.status,
            output: item.output || '',
            completedAt: item.completed_at ? item.completed_at * 1000 : Date.now()
        }));
    } catch (error) {
        console.error('Failed to rehydrate state:', error);
    } finally {
        if (currentTab === 'active') {
            renderActiveDownloads();
        } else if (currentTab === 'history') {
            renderDownloadHistory();
        }
    }
}

function handleDownloadQueued(data) {
    // A Queued card appears the instant the server accepts a Download, even
    // when every worker is busy.
    activeDownloads.set(data.id, {
        id: data.id,
        url: data.url,
        quality: data.quality,
        metadata: data.metadata || {},
        status: 'queued',
        output: '',
        queuedAt: Date.now()
    });

    if (currentTab === 'active') {
        renderActiveDownloads();
    }
}

function handleDownloadStarted(data) {
    // Transition the existing Queued card in place to Downloading.
    const download = activeDownloads.get(data.id) || {
        id: data.id,
        output: ''
    };
    download.url = data.url || download.url;
    download.quality = data.quality != null ? data.quality : download.quality;
    download.metadata = data.metadata || download.metadata || {};
    download.status = 'downloading';
    download.startTime = Date.now();
    activeDownloads.set(data.id, download);

    if (currentTab === 'active') {
        // Re-render so the card gains its spinner; updateDownloadElement alone
        // cannot add the spinner that a Queued card lacks.
        renderActiveDownloads();
    }
}

function handleDownloadError(data) {
    const download = activeDownloads.get(data.id);
    if (download) {
        download.status = 'error';
        download.error = data.error;
        updateDownloadElement(data.id, download);
        
        setTimeout(() => {
            downloadHistory.unshift({
                ...download,
                completedAt: Date.now()
            });
            activeDownloads.delete(data.id);
            
            if (currentTab === 'active') {
                renderActiveDownloads();
            } else if (currentTab === 'history') {
                renderDownloadHistory();
            }
        }, 2000);
    }
}

function statusLabel(status) {
    return status === 'skipped' ? 'already downloaded' : status;
}

function renderDownloadHistory() {
    const container = document.getElementById('downloadHistory');
    if (!container) return;

    if (downloadHistory.length === 0) {
        container.innerHTML = '<div class="empty-state">NO DOWNLOAD HISTORY</div>';
        return;
    }

    container.innerHTML = downloadHistory.map(item => {
        const isOk = item.status === 'completed' || item.status === 'skipped';
        const statusIcon = isOk ? '✓' : '✗';
        const statusClass = isOk ? 'success' : 'error';

        // Every History entry whose source URL is known offers a Redownload
        // (re-run with --no-db). On `skipped` ("already downloaded") items it is
        // emphasized as the natural next action; elsewhere it stays quiet.
        const isSkipped = item.status === 'skipped';
        let redownloadAction = '';
        if (item.url) {
            redownloadAction = isSkipped
                ? `<button class="redownload-btn prominent" onclick="redownload('${item.id}')">Already downloaded — Redownload anyway</button>`
                : `<button class="redownload-btn" onclick="redownload('${item.id}')">Redownload</button>`;
        }

        return `
        <div class="download-item ${item.status}" data-history-id="${item.id}">
            <div class="download-content">
                ${item.metadata?.album_art ?
                    `<img src="${item.metadata.album_art}" class="download-album-art" onerror="this.style.display='none'">` :
                    `<div class="download-album-art placeholder ${statusClass}">${statusIcon}</div>`
                }
                <div class="download-info">
                    <div class="download-title">${item.metadata?.title || 'Unknown'}</div>
                    <div class="download-artist">${item.metadata?.artist || 'Unknown Artist'}</div>
                    <div class="download-meta">
                        <span class="status-badge ${item.status}">${statusLabel(item.status)}</span>
                        ${item.metadata?.service ?
                            `<span class="service-badge">${item.metadata.service.toUpperCase()}</span>` : ''}
                    </div>
                </div>
                ${redownloadAction}
            </div>
        </div>
    `}).join('');
}

// Redownload a History entry: re-enqueue it bypassing streamrip's database
// (--no-db) so an already-downloaded item downloads again. The new Download
// enters the normal lifecycle and appears in the Active tab as queued.
async function redownload(historyId) {
    const btn = document.querySelector(`[data-history-id="${historyId}"] .redownload-btn`);
    if (btn) {
        btn.disabled = true;
    }

    try {
        const response = await fetch('/api/redownload', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: historyId })
        });

        const data = await response.json();

        if (response.ok) {
            showToast('Redownload queued', 'success');
        } else {
            showToast(data.error || 'Failed to redownload', 'error');
            if (btn) btn.disabled = false;
        }
    } catch (error) {
        showToast('Error: ' + error.message, 'error');
        if (btn) btn.disabled = false;
    }
}


function handleDownloadProgress(data) {
    const download = activeDownloads.get(data.id);
    if (download) {
        download.latestOutput = data.output;
        download.allOutput = download.allOutput || [];
        download.allOutput.push(data.output);
    }
}

function handleDownloadCompleted(data) {
    const download = activeDownloads.get(data.id);
    if (download) {
        download.status = data.status;
        download.url = data.url || download.url;
        download.quality = data.quality != null ? data.quality : download.quality;
        download.endTime = Date.now();
        download.output = data.output || (download.allOutput && download.allOutput.join('\n')) || 'No output captured';
        updateDownloadElement(data.id, download);
        
        setTimeout(() => {
            downloadHistory.unshift({
                ...download,
                completedAt: Date.now()
            });
            
            if (downloadHistory.length > 50) {
                downloadHistory.pop();
            }
            
            activeDownloads.delete(data.id);
            
            if (currentTab === 'active') {
                renderActiveDownloads();
            } else if (currentTab === 'history') {
                renderDownloadHistory();
            }
        }, 3000); //3 seconds in tab
    }
}


function updateDownloadElement(id, download) {
    const element = document.querySelector(`[data-download-id="${id}"]`);
    if (!element) {
        renderActiveDownloads();
        return;
    }
    
    const statusBadge = element.querySelector('.status-badge');
    if (statusBadge) {
        statusBadge.textContent = statusLabel(download.status);
        statusBadge.className = `status-badge ${download.status}`;
    }
    
    if (element.classList.contains('expanded')) {
        const outputEl = element.querySelector('.download-output');
        if (outputEl) {
            outputEl.textContent = download.output;
            outputEl.scrollTop = outputEl.scrollHeight;
        }
    }
}
function renderActiveDownloads() {
    const container = document.getElementById('activeDownloads');
    
    if (activeDownloads.size === 0) {
        container.innerHTML = '<div class="empty-state">NO ACTIVE DOWNLOADS</div>';
        return;
    }
    
    container.innerHTML = Array.from(activeDownloads.values()).map(item => `
        <div class="download-item ${item.status}" data-download-id="${item.id}">
            <div class="download-content">
                ${item.metadata.album_art ? 
                    `<img src="${item.metadata.album_art}" class="download-album-art">` : 
                    `<div class="download-album-art placeholder">▶</div>`
                }
                <div class="download-info">
                    <div class="download-title">${item.metadata.title || 'Unknown'}</div>
                    <div class="download-artist">${item.metadata.artist || 'Unknown Artist'}</div>
                    <div class="download-meta">
                        <span class="status-badge ${item.status}">${statusLabel(item.status)}</span>
                        ${item.metadata.service ? 
                            `<span class="service-badge">${item.metadata.service.toUpperCase()}</span>` : ''}
                    </div>
                    ${item.output ? `<a class="toggle-output" onclick="toggleOutput('${item.id}')">SHOW OUTPUT</a>` : ''}
                </div>
                ${item.status === 'downloading' ? '<div class="download-spinner"></div>' : ''}
            </div>
            ${item.output ? `
                <div class="download-output">
                    ${item.output}
                </div>
            ` : ''}
        </div>
    `).join('');
}


function toggleOutput(id) {
    const item = document.querySelector(`.download-item[data-download-id="${id}"]`);
    if (item) {
        item.classList.toggle('expanded');
        const toggleBtn = item.querySelector('.toggle-output');
        if (toggleBtn) {
            toggleBtn.textContent = item.classList.contains('expanded') ? 'HIDE OUTPUT' : 'SHOW OUTPUT';
        }
    }
}


function switchTab(tab, element) {
    currentTab = tab;
    
    if (element && !element.classList.contains('search-header')) {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        element.classList.add('active');
    }
    
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.getElementById(tab + 'Tab').classList.add('active');
    
    if (tab === 'active') {
        renderActiveDownloads(); 
    } else if (tab === 'history') {
        renderDownloadHistory(); 
    } else if (tab === 'config') {
        loadConfig();
    } else if (tab === 'files') {
        loadLibrary();
    }
}

async function startDownload() {
    const url = document.getElementById('urlInput').value.trim();
    const quality = document.getElementById('qualitySelect').value;
    
    if (!url) {
        showToast('Please enter a URL', 'error');
        return;
    }

    const btn = document.getElementById('downloadBtn');
    btn.disabled = true;

    try {
        const response = await fetch('/api/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url, quality: parseInt(quality) })
        });

        const data = await response.json();

        if (response.ok) {
            document.getElementById('urlInput').value = '';
            // The toast confirms acceptance; the user chooses when to look at the
            // Active tab, so we no longer yank them there on submit.
            showToast('Queued', 'success');
        } else {
            showToast(data.error || 'Failed to start download', 'error');
        }
    } catch (error) {
        showToast('Error: ' + error.message, 'error');
    } finally {
        btn.disabled = false;
    }
}


async function loadConfig() {
    try {
        const response = await fetch('/api/config');
        const data = await response.json();
        document.getElementById('configEditor').value = data.config || '';
    } catch (error) {
        showToast('Failed to load config: ' + error.message, 'error');
    }
}

async function saveConfig() {
    const config = document.getElementById('configEditor').value;
    
    try {
        const response = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config })
        });
        
        if (response.ok) {
            showToast('Config saved successfully', 'success');
        } else {
            const data = await response.json();
            showToast('Failed to save config: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        showToast('Error: ' + error.message, 'error');
    }
}

// The Library view: an Artist -> Album -> Track tree over the albums on disk.
// The album list comes back instantly (no tags read server-side); a given
// album's tracks are fetched lazily the first time it is expanded. Read-only:
// a folder on disk carries no source URL, so there is no Redownload here.
async function loadLibrary() {
    const container = document.getElementById('libraryTree');
    container.innerHTML = '<div class="empty-state">LOADING LIBRARY...</div>';
    try {
        const response = await fetch('/api/library');
        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.error || 'Failed to load library');
        }

        const albums = data.albums || [];
        if (albums.length === 0) {
            container.innerHTML = '<div class="empty-state">NO ALBUMS FOUND</div>';
            return;
        }

        // Group albums under their artist to build the Artist -> Album tree.
        const byArtist = new Map();
        albums.forEach(album => {
            if (!byArtist.has(album.artist)) {
                byArtist.set(album.artist, []);
            }
            byArtist.get(album.artist).push(album);
        });

        container.innerHTML = Array.from(byArtist.entries()).map(([artist, artistAlbums]) => `
            <div class="library-artist">
                <div class="library-artist-name">${escapeHtml(artist)}</div>
                ${artistAlbums.map(album => `
                    <div class="library-album" data-path="${escapeHtml(album.path)}">
                        <button class="library-album-header" onclick="toggleAlbum(this)">
                            <span class="library-album-caret">▸</span>
                            <span class="library-album-name">${escapeHtml(album.album)}</span>
                            <span class="library-album-badge badge-pending" data-role="badge">…</span>
                        </button>
                        <div class="library-tracks"></div>
                    </div>
                `).join('')}
            </div>
        `).join('');

        // Every album carries a completeness badge (ADR-0003). The badge is
        // derived from embedded tags, so it is filled lazily per album from the
        // per-album endpoint (cached server-side on folder mtime) without
        // blocking the instant, tag-free album listing above.
        document.querySelectorAll('#libraryTree .library-album').forEach(loadAlbumBadge);
    } catch (error) {
        container.innerHTML = '<div class="empty-state">FAILED TO LOAD LIBRARY</div>';
        showToast('Failed to load library: ' + error.message, 'error');
    }
}

// Render a completeness badge into an album's header: COMPLETE / INCOMPLETE
// (n missing) / UNKNOWN. Caches the fetched album payload on the element so
// expanding it later reuses it without a second request.
function applyCompletenessBadge(albumEl, completeness) {
    const badge = albumEl.querySelector('[data-role="badge"]');
    if (!badge) return;
    const status = completeness ? completeness.status : 'unknown';
    badge.classList.remove('badge-pending', 'badge-complete', 'badge-incomplete', 'badge-unknown');
    if (status === 'complete') {
        badge.classList.add('badge-complete');
        badge.textContent = 'COMPLETE';
    } else if (status === 'incomplete') {
        const n = completeness.missing_count || 0;
        badge.classList.add('badge-incomplete');
        badge.textContent = `INCOMPLETE (${n} missing)`;
    } else {
        badge.classList.add('badge-unknown');
        badge.textContent = 'UNKNOWN';
    }
}

async function loadAlbumBadge(albumEl) {
    const path = albumEl.dataset.path;
    try {
        const response = await fetch('/api/library/album?path=' + encodeURIComponent(path));
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Failed');
        // Stash the payload so toggleAlbum reuses it (server already cached it).
        albumEl._albumData = data;
        applyCompletenessBadge(albumEl, data.completeness);
    } catch (error) {
        applyCompletenessBadge(albumEl, { status: 'unknown' });
    }
}

async function toggleAlbum(button) {
    const albumEl = button.closest('.library-album');
    const tracksEl = albumEl.querySelector('.library-tracks');
    const caret = button.querySelector('.library-album-caret');

    // Collapse if already expanded.
    if (albumEl.classList.contains('expanded')) {
        albumEl.classList.remove('expanded');
        caret.textContent = '▸';
        return;
    }

    albumEl.classList.add('expanded');
    caret.textContent = '▾';

    // Lazily fetch this album's tracks only the first time it is expanded.
    if (albumEl.dataset.loaded === 'true') {
        return;
    }

    const path = albumEl.dataset.path;
    tracksEl.innerHTML = '<div class="library-tracks-loading">LOADING TRACKS...</div>';

    try {
        // Reuse the payload the badge already fetched (the server caches it on
        // folder mtime, so this would hit that cache anyway).
        let data = albumEl._albumData;
        if (!data) {
            const response = await fetch('/api/library/album?path=' + encodeURIComponent(path));
            data = await response.json();
            if (!response.ok) {
                throw new Error(data.error || 'Failed to load tracks');
            }
            albumEl._albumData = data;
        }
        applyCompletenessBadge(albumEl, data.completeness);

        const tracks = data.tracks || [];
        if (tracks.length === 0) {
            tracksEl.innerHTML = '<div class="library-tracks-empty">NO TRACKS</div>';
        } else {
            // The expected sequence 1…tracktotal per disc, with absent tracks as
            // greyed, number-only "Track N — missing" rows (ADR-0003).
            tracksEl.innerHTML = tracks.map(track => {
                const num = track.tracknumber != null ? String(track.tracknumber).padStart(2, '0') : '--';
                if (track.missing) {
                    return `
                        <div class="library-track library-track-missing">
                            <span class="library-track-number">${escapeHtml(num)}</span>
                            <span class="library-track-title">Track ${escapeHtml(String(track.tracknumber))} — missing</span>
                        </div>
                    `;
                }
                return `
                    <div class="library-track">
                        <span class="library-track-number">${escapeHtml(num)}</span>
                        <span class="library-track-title">${escapeHtml(track.title || '')}</span>
                    </div>
                `;
            }).join('');
        }
        albumEl.dataset.loaded = 'true';
    } catch (error) {
        tracksEl.innerHTML = '<div class="library-tracks-empty">FAILED TO LOAD TRACKS</div>';
        showToast('Failed to load tracks: ' + error.message, 'error');
    }
}

function setSearchType(type, element) {
    currentSearchType = type;
    document.querySelectorAll('.search-type-btn').forEach(btn => btn.classList.remove('active'));
    element.classList.add('active');
}

async function searchMusic() {
    const query = document.getElementById('searchInput').value.trim();
    const source = document.getElementById('searchSource').value;
    
    if (!query) {
        showToast('Please enter a search query', 'error');
        return;
    }
    
    const resultsDiv = document.getElementById('searchResults');
    resultsDiv.innerHTML = '<div class="empty-state">SEARCHING ' + source.toUpperCase() + '...</div>';
    
    currentPage = 1;
    allSearchResults = [];
    
    try {
        const response = await fetch('/api/search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ 
                query: query,
                type: currentSearchType,
                source: source
            })
        });
        
        const data = await response.json();
        
        // error handling new
        if (!response.ok) {
            const errorMsg = data.error || 'Search failed';
            let errorHtml = `<div class="error-state">
                <div class="error-title">⚠ SEARCH ERROR</div>
                <div class="error-message">${escapeHtml(errorMsg)}</div>`;
            
            if (data.debug_info) {
                errorHtml += `<div class="error-details">`;
                
                if (data.debug_info.return_code !== undefined) {
                    errorHtml += `<div class="error-detail-line">Return Code: ${data.debug_info.return_code}</div>`;
                }
                
                if (data.debug_info.stdout_preview) {
                    errorHtml += `<details class="error-traceback">
                        <summary>▼ Show Technical Details</summary>
                        <pre>${escapeHtml(data.debug_info.stdout_preview)}</pre>
                    </details>`;
                }
                
                errorHtml += `</div>`;
            }
            
            errorHtml += `</div>`;
            resultsDiv.innerHTML = errorHtml;
            showToast(errorMsg, 'error');
            updatePaginationControls();
            return;
        }
        
        if (data.message) {
            resultsDiv.innerHTML = `<div class="empty-state">${data.message.toUpperCase()}</div>`;
            updatePaginationControls();
        } else if (data.results && data.results.length > 0) {
            allSearchResults = data.results;
            totalResults = data.results.length;
            displayCurrentPage();
        } else {
            resultsDiv.innerHTML = '<div class="empty-state">NO RESULTS FOUND ON ' + source.toUpperCase() + '</div>';
            updatePaginationControls();
        }
    } catch (error) {
        console.error('Search error:', error);
        resultsDiv.innerHTML = `<div class="error-state">
            <div class="error-title">⚠ CONNECTION ERROR</div>
            <div class="error-message">${escapeHtml(error.message)}</div>
        </div>`;
        showToast('Connection error: ' + error.message, 'error');
        updatePaginationControls();
    }
}

// Helper function to escape HTML
function escapeHtml(unsafe) {
    return unsafe
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function displayCurrentPage() {
    const startIndex = (currentPage - 1) * itemsPerPage;
    const endIndex = Math.min(startIndex + itemsPerPage, totalResults);
    const pageResults = allSearchResults.slice(startIndex, endIndex);
    
    const resultsDiv = document.getElementById('searchResults');
    
    if (pageResults.length === 0) {
        resultsDiv.innerHTML = '<div class="empty-state">NO RESULTS FOUND</div>';
        return;
    }
    
    resultsDiv.innerHTML = pageResults.map(result => `
        <div class="search-result-item" data-id="${result.id}" data-source="${result.service}" data-type="${result.type}">
            <div class="result-album-art placeholder" id="art-${result.id}">▶</div>
            <div class="result-info">
                <span class="result-service">${result.service}</span>
                ${result.title ? `<div class="result-title">${result.title}</div>` : ''}
                <div class="result-artist">${result.artist || result.desc}</div>
                ${result.id ? `<div class="result-id">ID: ${result.id} (${result.type})</div>` : ''}
            </div>
            ${result.url ? `
                <button class="result-download-btn" onclick="downloadFromUrl('${result.url}')">
                    DOWNLOAD
                </button>
            ` : `
                <button class="result-download-btn" disabled style="opacity: 0.3;">
                    NO URL
                </button>
            `}
        </div>
    `).join('');
    
    updatePaginationControls();
    loadAlbumArtForVisibleItems();
}
function updatePaginationControls() {
    const totalPages = Math.ceil(totalResults / itemsPerPage);
    document.getElementById('pageInfo').textContent = `Page ${currentPage} of ${totalPages}`;
    document.getElementById('resultsCount').textContent = `${totalResults} results`;
    
    document.getElementById('prevPage').disabled = currentPage <= 1;
    document.getElementById('nextPage').disabled = currentPage >= totalPages;
}

function changePage(direction) {
    const totalPages = Math.ceil(totalResults / itemsPerPage);
    const newPage = currentPage + direction;
    
    if (newPage >= 1 && newPage <= totalPages) {
        currentPage = newPage;
        displayCurrentPage();
    }
}

document.getElementById('searchSource').addEventListener('change', function(e) {
    const source = e.target.value;
    const albumBtn = document.querySelector('.search-type-btn[onclick*="album"]');
    const artistBtn = document.querySelector('.search-type-btn[onclick*="artist"]');
    const trackBtn = document.querySelector('.search-type-btn[onclick*="track"]');
    
    if (source === 'soundcloud') {
        if (currentSearchType === 'album' || currentSearchType === 'artist') {
            trackBtn.click();
        }
        
        albumBtn.style.opacity = '0.3';
        albumBtn.style.pointerEvents = 'none';
        albumBtn.title = 'Not available on SoundCloud';
        
        artistBtn.style.opacity = '0.3';
        artistBtn.style.pointerEvents = 'none';
        artistBtn.title = 'Not available on SoundCloud';
    } else {
        albumBtn.style.opacity = '1';
        albumBtn.style.pointerEvents = 'auto';
        albumBtn.title = '';
        
        artistBtn.style.opacity = '1';
        artistBtn.style.pointerEvents = 'auto';
        artistBtn.title = '';
    }
});

async function loadAlbumArtForVisibleItems() {
    const visibleItems = document.querySelectorAll('.search-result-item');
    
    for (const item of visibleItems) {
        const itemId = item.dataset.id;
        const source = item.dataset.source;
        const type = item.dataset.type;
        
        if (!itemId) continue;
        
        try {
            const response = await fetch(`/api/album-art?source=${source}&type=${type}&id=${encodeURIComponent(itemId)}`);
            const data = await response.json();
            if (data.album_art) {
                const artElement = document.getElementById(`art-${itemId}`);
                if (artElement) {
                    artElement.classList.remove('placeholder');
                    artElement.innerHTML = `<img src="${data.album_art}" alt="Album art" class="result-album-art" onerror="this.parentElement.classList.add('placeholder'); this.parentElement.innerHTML='▶'">`;
                }
            } else {
                const artElement = document.getElementById(`art-${itemId}`);
                if (artElement && artElement.classList.contains('placeholder')) {
                    artElement.classList.add('loaded');
                    if (type === 'artist') {
                        artElement.innerHTML = '👤';
                    } else if (type === 'track') {
                        artElement.innerHTML = '🎵';
                    } else {
                        artElement.innerHTML = '▶';
                    }
                }
            }

            if (data.release_type || data.tracks_count || data.year) {
                const infoEl = item.querySelector('.result-info');
                if (infoEl) {
                    const existing = infoEl.querySelector('.result-extra-meta');
                    if (existing) existing.remove();

                    const parts = [];
                    if (data.release_type) parts.push(data.release_type.toUpperCase());
                    if (data.year) parts.push(data.year);
                    if (data.tracks_count) parts.push(`${data.tracks_count} tracks`);


                    const meta = document.createElement('div');
                    meta.className = 'result-extra-meta';
                    meta.textContent = parts.join(' · ');
                    const resultId = infoEl.querySelector('.result-id');
                    if (resultId) {
                        infoEl.insertBefore(meta, resultId);
                    } else {
                        infoEl.appendChild(meta); // fallback if no result-id exists
                    }
                }
            }

        } catch (error) {
            console.error('Error loading album art:', error);
            const artElement = document.getElementById(`art-${itemId}`);
            if (artElement && artElement.classList.contains('placeholder')) {
                if (type === 'artist') {
                    artElement.innerHTML = '👤';
                } else if (type === 'track') {
                    artElement.innerHTML = '🎵';
                } else {
                    artElement.innerHTML = '▶';
                }
            }
        }
    }
}
async function downloadFromUrl(url) {
    const quality = document.getElementById('qualitySelect').value;
    
    const searchResults = document.querySelectorAll('.search-result-item');
    let metadata = {};
    
    searchResults.forEach(item => {
        const btn = item.querySelector('.result-download-btn');
        if (btn && btn.onclick && btn.onclick.toString().includes(url)) {
            const serviceEl = item.querySelector('.result-service');
            const titleEl = item.querySelector('.result-title');
            const artistEl = item.querySelector('.result-artist');
            const artImg = item.querySelector('.result-album-art img');
            
            metadata = {
                title: titleEl?.textContent || '',
                artist: artistEl?.textContent || '',
                service: serviceEl?.textContent?.toLowerCase() || '',
                album_art: artImg?.src || ''
            };
        }
    });
    
    try {
        const response = await fetch('/api/download-from-url', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                url: url,
                quality: parseInt(quality),
                ...metadata
            })
        });

        const data = await response.json();

        if (response.ok) {
            // Confirm acceptance via toast and leave the user on Search; they
            // choose when to switch to the Active tab.
            showToast('Queued', 'success');
        } else {
            showToast('Failed to start download: ' + (data.error || 'Unknown error'), 'error');
        }

    } catch (error) {
        showToast('Error: ' + error.message, 'error');
    }
}


window.addEventListener('load', () => {
    // Rehydrate authoritative server state first (ADR-0002), then attach the
    // live SSE delta channel on top of it.
    rehydrateState();
    initializeSSE();
});

window.addEventListener('beforeunload', () => {
    if (eventSource) {
        eventSource.close();
    }
});

if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch((err) => {
        console.log('Service worker registration failed:', err);
    });
}

document.addEventListener('DOMContentLoaded', () => {
    const urlInput = document.getElementById('urlInput');
    if (urlInput) {
        urlInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                startDownload();
            }
        });
    }
    
    const searchInput = document.getElementById('searchInput');
    if (searchInput) {
        searchInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                searchMusic();
            }
        });
    }
});
