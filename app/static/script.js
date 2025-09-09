const API_BASE_URL = '/api';

// --- Global State (variables that don't hold DOM elements) ---
let currentSelectedSeries = null;
let watchedUrlsData = [];
let statusInterval = null;

// --- Modal Control Functions ---
function openAddUrlModal() {
    resetModal();
    document.getElementById('initial-choice-view').classList.remove('hidden');
    document.getElementById('modal-overlay').classList.remove('hidden');
}

function showInitialChoice() {
    resetModal();
    document.getElementById('initial-choice-view').classList.remove('hidden');
}

function closeModal() {
    document.getElementById('modal-overlay').classList.add('hidden');
    currentSelectedSeries = null;
}

function resetModal() {
    document.getElementById('initial-choice-view').classList.add('hidden');
    document.getElementById('add-url-form').classList.add('hidden');
    document.getElementById('search-view').classList.add('hidden');
    document.getElementById('series-details').classList.add('hidden');
    document.getElementById('bulk-import-view').classList.add('hidden');
}

function openUrlSelectionModal(urls, onSelectCallback) {
    const urlSelectionList = document.getElementById('url-selection-list');
    urlSelectionList.innerHTML = '';
    urls.forEach(url => {
        const urlButton = document.createElement('button');
        urlButton.className = 'w-full text-left p-3 rounded-md bg-gray-700 hover:bg-gray-600 text-blue-400 truncate';
        urlButton.textContent = url;
        urlButton.onclick = () => {
            onSelectCallback(url);
            closeUrlSelectionModal();
        };
        urlSelectionList.appendChild(urlButton);
    });
    document.getElementById('url-selection-overlay').classList.remove('hidden');
}

function closeUrlSelectionModal() {
    document.getElementById('url-selection-overlay').classList.add('hidden');
}


// --- Core Application & API Functions ---
async function performSearch(term, container, site = 'All Sites', limit = 10) {
    container.innerHTML = '<p class="text-center col-span-full">Searching...</p>';
    try {
        const url = `${API_BASE_URL}/search?term=${encodeURIComponent(term)}&site=${encodeURIComponent(site)}&limit=${limit}`;
        const response = await fetch(url);
        const data = await response.json();
        container.innerHTML = '';
        if (data.results.length === 0) {
            container.innerHTML = '<p class="text-center col-span-full">No results found.</p>';
            return;
        }
        for (const result of data.results) {
            const card = await createCard(result, true);
            container.appendChild(card);
        }
    } catch (error) {
        container.innerHTML = `<p class="text-center col-span-full text-red-400">Search failed: ${error.message}</p>`;
    }
}

async function createCard(entry, isSearchResult = false) {
    const card = document.createElement('div');
    card.className = 'card rounded-lg shadow-md cursor-pointer relative overflow-hidden fixed-card-size';
    const placeholderCover = 'https://placehold.co/218x330/2d3748/ffffff?text=No+Cover';
    let coverUrl, title, site, clickHandler;
    let metadata = null;

    if (isSearchResult) {
        coverUrl = entry.cover_url || placeholderCover;
        title = entry.title;
        site = entry.site;
        clickHandler = () => showPreview(entry);
    } else {
        coverUrl = `/downloads/${entry.library}/${entry.series_folder_name}/cover.jpg`;
        metadata = await getSeriesMetadata(entry.series_folder_name, entry.library);
        title = metadata?.name || entry.series_folder_name;
        site = entry.display_site_name || "Unknown";
        clickHandler = () => openSeriesDetailsModal(entry, metadata);
    }

    card.metadata = metadata;
    card.innerHTML = `
        <img id="cover-image-${entry.series_folder_name}" src="${coverUrl}" alt="Cover for ${title}" class="object-cover w-full h-full" onerror="this.onerror=null;this.src='${placeholderCover}';">
        <div class="card-overlay">
            <h3 class="text-white truncate w-full">${title}</h3>
            <p class="text-gray-400 truncate w-full">${site}</p>
        </div>
    `;
    card.onclick = clickHandler;

    if (!isSearchResult && entry.missing_chapters_count > 0) {
        const badge = document.createElement('div');
        badge.className = 'missing-badge';
        badge.textContent = entry.missing_chapters_count;
        badge.title = `${entry.missing_chapters_count} missing chapters`;
        card.appendChild(badge);
    }

    return card;
}

async function refreshImage(series_folder_name, source_url, library, use_flaresolverr) {
    try {
        const response = await fetch(`${API_BASE_URL}/refresh_image`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ series_folder_name, source_url, library, use_flaresolverr })
        });
        if (!response.ok) {
            throw new Error('Failed to refresh image.');
        }
        alert('Image refresh task sent!');
    } catch (error) {
        alert(error.message);
    }
}

async function refreshMetadata(series_folder_name, series_urls, library, use_flaresolverr) {
    try {
        const response = await fetch(`${API_BASE_URL}/refresh_metadata`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ series_folder_name, series_urls, library, use_flaresolverr })
        });
        if (!response.ok) {
            throw new Error('Failed to refresh metadata.');
        }
        alert('Metadata refresh task sent!');
        fetchWatchedUrls();
    } catch (error) {
        alert(error.message);
    }
}

async function getSeriesMetadata(folder, library) {
    try {
        const response = await fetch(`${API_BASE_URL}/series_metadata/${folder}?library=${library}`);
        if (response.ok) return (await response.json()).metadata;
    } catch (error) {
        console.error("Could not fetch series metadata:", error);
    }
    return null;
}

async function populateSiteFilter() {
    try {
        const siteFilterSelect = document.getElementById('site-filter-select');
        const response = await fetch(`${API_BASE_URL}/sites`);
        const data = await response.json();

        siteFilterSelect.innerHTML = '';
        
        const allSitesOption = document.createElement('option');
        allSitesOption.value = 'All Sites';
        allSitesOption.textContent = 'All Sites';
        siteFilterSelect.appendChild(allSitesOption);

        data.sites.forEach(siteName => {
            const option = document.createElement('option');
            option.value = siteName;
            option.textContent = siteName;
            siteFilterSelect.appendChild(option);
        });
    } catch (error) {
        console.error("Failed to populate site filter:", error);
    }
}


// --- Helper Functions ---
function normalizeStringForComparison(str) {
    if (!str) return '';
    return str
        .toLowerCase()
        .replace(/[\-:_']/g, '')
        .replace(/\s\s+/g, ' ')
        .trim();
}

function calculateLevenshteinDistance(s1, s2) {
    s1 = s1.toLowerCase();
    s2 = s2.toLowerCase();
    const costs = [];
    for (let i = 0; i <= s1.length; i++) {
        let lastValue = i;
        for (let j = 0; j <= s2.length; j++) {
            if (i === 0) {
                costs[j] = j;
            } else if (j > 0) {
                let newValue = costs[j - 1];
                if (s1.charAt(i - 1) !== s2.charAt(j - 1)) {
                    newValue = Math.min(newValue, lastValue, costs[j]) + 1;
                }
                costs[j - 1] = lastValue;
                lastValue = newValue;
            }
        }
        if (i > 0) costs[s2.length] = lastValue;
    }
    return costs[s2.length];
}

async function addSourceToExistingSeries(series, newUrl) {
    try {
        const response = await fetch(`${API_BASE_URL}/add_source_to_series`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                series_folder_name: series.series_folder_name,
                new_source_url: newUrl
            })
        });
        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || 'Failed to add new source.');
        }
        alert(`New source added to "${series.series_folder_name}" successfully!`);
        closeModal();
        fetchWatchedUrls();
    } catch (error) {
        alert(error.message);
    }
}

function formatRelativeTime(isoString) {
    if (!isoString) return "Pending first check...";
    
    const futureDate = new Date(isoString);
    const now = new Date();
    const diffSeconds = Math.round((futureDate - now) / 1000);

    if (diffSeconds <= 0) return "Checking soon...";

    const hours = Math.floor(diffSeconds / 3600);
    const minutes = Math.floor((diffSeconds % 3600) / 60);

    let result = "in ";
    if (hours > 0) result += `${hours} hour${hours > 1 ? 's' : ''}`;
    if (hours > 0 && minutes > 0) result += ' and ';
    if (minutes > 0) result += `${minutes} minute${minutes > 1 ? 's' : ''}`;
    
    return result.trim() === 'in' ? 'less than a minute' : result;
}


// --- UI Interaction & Event Handler Functions ---
function showPreview(result) {
    resetModal();
    document.getElementById('series-details').classList.remove('hidden');
    document.getElementById('details-back-button').onclick = () => {
        resetModal();
        document.getElementById('search-view').classList.remove('hidden');
    };
    document.getElementById('detail-title').textContent = result.title;
    document.getElementById('source-urls-list').innerHTML = `<li class="text-blue-400"><a href="${result.source_url}" target="_blank" class="hover:underline break-all">${result.source_url}</a></li>`;
    document.getElementById('meta-publisher').textContent = result.author || 'N/A';
    document.getElementById('meta-status').textContent = result.status || 'N/A';
    document.getElementById('meta-year').textContent = result.year || 'N/A';
    document.getElementById('meta-tags').textContent = result.tags ? result.tags.join(', ') : 'N/A';
    document.getElementById('meta-description').textContent = result.description || 'N/A';

    const monitorFrequencySelect = document.getElementById('monitor-frequency');
    const actionButtonsContainer = document.getElementById('action-buttons');
    actionButtonsContainer.innerHTML = `<button class="w-full py-2 px-4 rounded-md shadow-sm text-white bg-green-600 hover:bg-green-700">Monitor</button>`;
    actionButtonsContainer.querySelector('button').onclick = () => {
        handleMonitor(result.source_url, result.title, monitorFrequencySelect.value);
    };
}

async function openSeriesDetailsModal(entry, metadata) {
    resetModal();
    document.getElementById('series-details').classList.remove('hidden');
    currentSelectedSeries = entry;
    document.getElementById('details-back-button').onclick = () => closeModal();

    const scheduleStatus = await (await fetch(`${API_BASE_URL}/schedule_status`)).json();
    const seriesFrequency = entry.frequency || 'daily';
    const nextUpdateTimeISO = scheduleStatus[seriesFrequency];

    document.getElementById('monitor-frequency').value = seriesFrequency;
    document.getElementById('detail-title').textContent = metadata?.name ?? entry.series_folder_name;
    
    const sourceList = document.getElementById('source-urls-list');
    sourceList.innerHTML = ''; 
    entry.series_urls.forEach(url => {
        const li = document.createElement('li');
        li.className = 'flex items-center space-x-2 text-gray-300 mb-1';
        const removeBtn = document.createElement('button');
        removeBtn.className = 'text-red-500 hover:text-red-400 font-bold text-lg';
        removeBtn.innerHTML = '&times;';
        removeBtn.title = 'Remove this source';
        removeBtn.onclick = () => handleRemoveSource(entry.series_folder_name, url);
        const link = document.createElement('a');
        link.href = url;
        link.target = '_blank';
        link.className = 'text-blue-400 hover:underline break-all';
        link.textContent = url;
        li.appendChild(removeBtn);
        li.appendChild(link);
        sourceList.appendChild(li);
    });
    
    document.getElementById('meta-publisher').textContent = metadata?.publisher ?? 'N/A';
    document.getElementById('meta-status').textContent = metadata?.status ?? 'N/A';
    document.getElementById('meta-year').textContent = metadata?.year ?? 'N/A';
    document.getElementById('meta-next-update').textContent = formatRelativeTime(nextUpdateTimeISO);
    document.getElementById('meta-tags').textContent = metadata?.tags?.join(', ') ?? 'N/A';
    document.getElementById('meta-description').textContent = metadata?.description_text ?? 'No description.';

    const missingChaptersContainer = document.getElementById('missing-chapters-container');
    const missingChaptersList = document.getElementById('missing-chapters-list');
    missingChaptersList.innerHTML = '';
    if (entry.missing_chapters_list && entry.missing_chapters_list.length > 0) {
        entry.missing_chapters_list.forEach(chapterName => {
            const li = document.createElement('li');
            li.textContent = chapterName;
            missingChaptersList.appendChild(li);
        });
        missingChaptersContainer.classList.remove('hidden');
    } else {
        missingChaptersContainer.classList.add('hidden');
    }

    const actionButtonsContainer = document.getElementById('action-buttons');
    actionButtonsContainer.innerHTML = '';
    const updateBtn = document.createElement('button');
    updateBtn.className = 'flex-1 py-2 px-4 rounded-md shadow-sm text-white bg-green-600 hover:bg-green-700';
    updateBtn.textContent = 'Update Now';
    updateBtn.onclick = handleUpdateNow;
    const refreshMetaBtn = document.createElement('button');
    refreshMetaBtn.className = 'flex-1 py-2 px-4 rounded-md shadow-sm text-white bg-blue-600 hover:bg-blue-700';
    refreshMetaBtn.textContent = 'Refresh Metadata';
    refreshMetaBtn.onclick = () => {
        const action = (selectedUrl) => {
            const orderedUrls = [selectedUrl, ...entry.series_urls.filter(u => u !== selectedUrl)];
            refreshMetadata(entry.series_folder_name, orderedUrls, entry.library, true);
        };
        if (entry.series_urls.length > 1) {
            openUrlSelectionModal(entry.series_urls, action);
        } else {
            action(entry.series_urls[0]);
        }
    };
    const refreshImgBtn = document.createElement('button');
    refreshImgBtn.className = 'flex-1 py-2 px-4 rounded-md shadow-sm text-white bg-indigo-600 hover:bg-indigo-700';
    refreshImgBtn.textContent = 'Refresh Image';
    refreshImgBtn.onclick = () => {
        const action = (selectedUrl) => {
            refreshImage(entry.series_folder_name, selectedUrl, entry.library, true);
        };
        if (entry.series_urls.length > 1) {
            openUrlSelectionModal(entry.series_urls, action);
        } else {
            action(entry.series_urls[0]);
        }
    };
    const removeBtn = document.createElement('button');
    removeBtn.className = 'flex-1 py-2 px-4 rounded-md shadow-sm text-white bg-red-600 hover:bg-red-700';
    removeBtn.textContent = 'Remove';
    removeBtn.onclick = handleRemoveUrl;
    actionButtonsContainer.appendChild(updateBtn);
    actionButtonsContainer.appendChild(refreshMetaBtn);
    actionButtonsContainer.appendChild(refreshImgBtn);
    actionButtonsContainer.appendChild(removeBtn);
    
    document.getElementById('modal-overlay').classList.remove('hidden');
}

async function handleRemoveSource(seriesFolderName, urlToRemove) {
    if (!confirm(`Are you sure you want to remove this source URL?\n\n${urlToRemove}`)) {
        return;
    }
    try {
        const response = await fetch(`${API_BASE_URL}/remove_source_from_series`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                series_folder_name: seriesFolderName,
                source_url_to_remove: urlToRemove
            })
        });
        const result = await response.json();
        if (!response.ok) {
            throw new Error(result.detail || 'Failed to remove source.');
        }
        alert(result.message || 'Source removed successfully!');
        closeModal();
        fetchWatchedUrls();
    } catch (error) {
        alert(`Error: ${error.message}`);
    }
}

async function handleAddUrl(event) {
    event.preventDefault();
    const urlInput = document.getElementById('new-url');
    const librarySelect = document.getElementById('new-library');
    const frequencySelect = document.getElementById('new-frequency');
    const submitButton = event.target.querySelector('button[type="submit"]');

    const url = urlInput.value;
    const library = librarySelect.value;
    const frequency = frequencySelect.value;
    const originalButtonText = submitButton.textContent;

    try {
        submitButton.textContent = 'Finding Title...';
        submitButton.disabled = true;

        const titleResponse = await fetch(`${API_BASE_URL}/get_title_from_url`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: url })
        });

        if (!titleResponse.ok) {
            const error = await titleResponse.json();
            throw new Error(error.detail || 'Failed to get title from URL.');
        }

        const data = await titleResponse.json();
        const scrapedTitle = data.title;
        
        await triggerDownload([url], library, null, scrapedTitle, frequency);
        urlInput.value = '';
    } catch (error) {
        alert(`Error: ${error.message}`);
    } finally {
        submitButton.textContent = originalButtonText;
        submitButton.disabled = false;
    }
}

async function handleMonitor(url, title, frequency) {
    await triggerDownload([url], 'manga', null, title, frequency);
}

async function handleUpdateNow() {
    if (currentSelectedSeries) {
        const newFrequency = document.getElementById('monitor-frequency').value;
        await triggerDownload(
            currentSelectedSeries.series_urls, 
            currentSelectedSeries.library, 
            currentSelectedSeries.series_folder_name,
            null,
            newFrequency
        );
    }
}

async function triggerDownload(urls, library, series_folder_name = null, title = null, frequency = 'daily') {
    if (!series_folder_name && title && urls.length > 0) {
        const newUrl = urls[0];
        const normalizedNewTitle = normalizeStringForComparison(title);
        const similarityThreshold = 3;
        let similarSeries = null;
        for (const existingSeries of watchedUrlsData) {
            const normalizedExistingTitle = normalizeStringForComparison(existingSeries.series_folder_name);
            const distance = calculateLevenshteinDistance(normalizedNewTitle, normalizedExistingTitle);
            if (distance <= similarityThreshold) {
                similarSeries = existingSeries;
                break;
            }
        }
        if (similarSeries) {
            const userChoice = confirm(
                `A similar series named "${similarSeries.series_folder_name}" already exists.\n\n` +
                `Do you want to add this URL as a new source to the existing series?\n\n` +
                `(Click 'Cancel' to create a new series instead)`
            );
            if (userChoice) {
                await addSourceToExistingSeries(similarSeries, newUrl);
                return;
            }
        }
    }

    const body = {
        source_urls: urls,
        library: library,
        use_flaresolverr: true,
        frequency: frequency
    };
    if (series_folder_name) {
        body.series_folder_name = series_folder_name;
    }
    if (title) {
        body.title = title;
    }
    try {
        const response = await fetch(`${API_BASE_URL}/download`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        if (!response.ok) throw new Error('Request failed.');
        alert('Request sent successfully!');
        closeModal();
        fetchWatchedUrls();
    } catch (error) {
        alert(error.message);
    }
}

async function handleRemoveUrl() {
    if (!currentSelectedSeries || !confirm("Are you sure?")) return;
    try {
        const response = await fetch(`${API_BASE_URL}/watched_urls`, {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ series_folder_name: currentSelectedSeries.series_folder_name })
        });
        if (!response.ok) throw new Error('Failed to remove series.');
        alert('Series removed from watched list.');
        closeModal();
        fetchWatchedUrls();
    } catch (error) {
        alert(error.message);
    }
}

async function fetchWatchedUrls() {
    try {
        const response = await fetch(`${API_BASE_URL}/watched_urls`);
        const data = await response.json();
        watchedUrlsData = data.watched_urls;
        renderCards();
    } catch (error) {
        console.error("Error fetching watched URLs:", error);
    }
}

async function renderCards() {
    const container = document.getElementById('cards-container');
    const addCard = document.getElementById('add-url-card');
    const sortValue = document.getElementById('sort-select').value;

    const sortedData = [...watchedUrlsData].sort((a, b) => {
        const nameA = a.series_folder_name.toLowerCase();
        const nameB = b.series_folder_name.toLowerCase();
        const siteA = (a.display_site_name || '').toLowerCase();
        const siteB = (b.display_site_name || '').toLowerCase();
        switch (sortValue) {
            case 'name-desc': return nameB.localeCompare(nameA);
            case 'site-asc': return siteA.localeCompare(siteB);
            case 'site-desc': return siteB.localeCompare(siteA);
            case 'name-asc': default: return nameA.localeCompare(nameB);
        }
    });
    container.innerHTML = '';
    container.appendChild(addCard);
    for (const entry of sortedData) {
        const card = await createCard(entry, false);
        container.appendChild(card);
    }
}

async function handleBulkImport(event) {
    event.preventDefault();
    const fileInput = document.getElementById('csv-file');
    const library = document.getElementById('bulk-library').value;
    const frequency = document.getElementById('bulk-frequency').value;
    const submitButton = event.target.querySelector('button[type="submit"]');

    if (!fileInput.files || fileInput.files.length === 0) {
        alert("Please select a CSV file.");
        return;
    }
    const file = fileInput.files[0];
    const reader = new FileReader();
    reader.onload = async (e) => {
        try {
            const text = e.target.result;
            const urls = text.split('\n').map(url => url.trim()).filter(url => url);
            if (urls.length === 0) {
                alert("CSV file is empty or contains no valid URLs.");
                return;
            }
            submitButton.textContent = 'Importing...';
            submitButton.disabled = true;
            const response = await fetch(`${API_BASE_URL}/bulk_add`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ urls, library, frequency })
            });
            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.detail || "Bulk import failed.");
            }
            alert(`Bulk import of ${urls.length} URLs has been started! Check the Jobs Status panel for progress.`);
            closeModal();
            fetchWatchedUrls();
        } catch (error) {
            alert(`Error: ${error.message}`);
        } finally {
            submitButton.textContent = 'Start Import';
            submitButton.disabled = false;
        }
    };
    reader.readAsText(file);
}

// --- Job Status Viewer Logic ---
async function fetchJobStatus() {
    try {
        const response = await fetch(`${API_BASE_URL}/job_status`);
        if (!response.ok) return;
        const data = await response.json();
        const activeJobsList = document.getElementById('active-jobs-list');
        const scheduledJobsList = document.getElementById('scheduled-jobs-list');
        activeJobsList.innerHTML = '';
        if (data.active_jobs.length > 0) {
            data.active_jobs.forEach(job => {
                const li = document.createElement('li');
                li.textContent = job;
                li.className = 'truncate';
                activeJobsList.appendChild(li);
            });
        } else {
            activeJobsList.innerHTML = '<li class="text-gray-500">No active jobs.</li>';
        }
        scheduledJobsList.innerHTML = '';
        if (data.scheduled_jobs.length > 0) {
            data.scheduled_jobs.forEach(job => {
                const li = document.createElement('li');
                li.textContent = job;
                li.className = 'truncate';
                scheduledJobsList.appendChild(li);
            });
        } else {
            scheduledJobsList.innerHTML = '<li class="text-gray-500">Queue is empty.</li>';
        }
    } catch (error) {
        console.error("Failed to fetch job status:", error);
    }
}

function toggleStatusPanel() {
    const statusPanel = document.getElementById('status-panel');
    if (statusPanel.classList.contains('hidden')) {
        statusPanel.classList.remove('hidden');
        fetchJobStatus();
        statusInterval = setInterval(fetchJobStatus, 5000);
    } else {
        statusPanel.classList.add('hidden');
        clearInterval(statusInterval);
        statusInterval = null;
    }
}


// --- Initial Page Load ---
document.addEventListener('DOMContentLoaded', () => {
    // Define DOM element constants after the document is loaded
    const modalSearchForm = document.getElementById('search-form');
    const addSourceForm = document.getElementById('add-source-form');
    const statusButton = document.getElementById('status-button');
    const statusCloseButton = document.getElementById('status-close-button');
    const sortSelect = document.getElementById('sort-select');
    const bulkImportForm = document.getElementById('bulk-import-form');

    // Attach event listeners
    document.getElementById('show-url-form-btn').onclick = () => {
        resetModal();
        document.getElementById('add-url-form').classList.remove('hidden');
    };
    document.getElementById('show-search-view-btn').onclick = () => {
        resetModal();
        document.getElementById('search-view').classList.remove('hidden');
    };
    document.getElementById('show-bulk-import-btn').onclick = () => {
        resetModal();
        document.getElementById('bulk-import-view').classList.remove('hidden');
    };

    modalSearchForm.addEventListener('submit', (event) => {
        event.preventDefault();
        const searchTermInput = document.getElementById('search-term');
        const siteFilterSelect = document.getElementById('site-filter-select');
        const limitSelect = document.getElementById('limit-select');
        const term = searchTermInput.value;
        const site = siteFilterSelect.value;
        const limit = limitSelect.value;
        if (term) {
            performSearch(term, document.getElementById('search-results-container'), site, limit);
            searchTermInput.value = '';
        }
    });

    addSourceForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (!currentSelectedSeries) return;
        const newUrlInput = document.getElementById('new-source-url-input');
        const newUrl = newUrlInput.value;
        try {
            const response = await fetch(`${API_BASE_URL}/add_source_to_series`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    series_folder_name: currentSelectedSeries.series_folder_name,
                    new_source_url: newUrl
                })
            });
            if (!response.ok) throw new Error('Request failed.');
            alert('New source added successfully!');
            newUrlInput.value = '';
            closeModal();
            fetchWatchedUrls();
        } catch (error) {
            alert(error.message);
        }
    });
    
    bulkImportForm.addEventListener('submit', handleBulkImport);
    statusButton.addEventListener('click', toggleStatusPanel);
    statusCloseButton.addEventListener('click', toggleStatusPanel);

    if (sortSelect) {
        sortSelect.addEventListener('change', renderCards);
    }

    // Initial function calls
    populateSiteFilter();
    fetchWatchedUrls();
});