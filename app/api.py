import json
import os
import traceback
import uuid
from fastapi import FastAPI, Query, HTTPException, Body
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from datetime import datetime, timedelta

from urllib.parse import urlparse

from app.worker import process_series, refresh_cover_image, refresh_series_metadata, record_check_timestamp,celery_app, bulk_add_task
from app.services import redis_client, WATCHED_URLS_REDIS_KEY, LIBRARIES
from app.scraping import search_all_sites, get_display_site_name, _scrape_series_metadata_from_html
from app.models import RefreshImageRequest, RefreshMetadataRequest, RemoveSeriesRequest, RemoveSourceRequest, BulkAddRequest, UrlRequest
from app.utils import sanitize_filename

app = FastAPI(
    title="Manga Downloader API",
    description="An API to search for and download manga."
)

# --- Static File Mounts ---
app.mount("/downloads", StaticFiles(directory="/downloads"), name="downloads")
# Corrected path for static files within the new structure
app.mount("/static", StaticFiles(directory="/app/app/static"), name="static")


# --- API Routes ---
@app.get("/")
async def serve_frontend():
    # Corrected path to the index.html file
    return FileResponse("/app/app/static/index.html")

@app.get("/api/search")
async def search_sites(
    term: str = Query(..., min_length=3), 
    site: str | None = Query(None),
    limit: int = Query(10, ge=1, le=50) # Add limit with a default and validation
):
    results = await search_all_sites(term, site, limit)
    return {"results": results}

@app.post("/api/get_title_from_url")
async def get_title_from_url(request: UrlRequest):
    """
    Receives a URL, scrapes the page for its title using existing logic,
    and returns the title as a JSON response.
    """
    try:
        with open('/app/app/sites_config.json', 'r') as f:
            sites_config_list = json.load(f)

        # Find the site configuration that matches the provided URL's domain
        site_config = next((s for s in sites_config_list if urlparse(s['search_url_template']).hostname in request.url), None)

        if not site_config:
            raise HTTPException(status_code=400, detail="Could not find a site configuration for this URL.")

        # Reuse the existing scraping function to get all metadata
        scraped_metadata = _scrape_series_metadata_from_html(request.url, site_config)
        scraped_title = scraped_metadata.get("title")

        if not scraped_title:
            raise HTTPException(status_code=404, detail="Could not find a title on the provided page.")

        return {"title": scraped_title}

    except Exception as e:
        # Catch any exception during scraping and return a generic error
        raise HTTPException(status_code=500, detail=f"An error occurred while scraping: {e}")

@app.post("/api/download")
async def create_download_job(
    source_urls: list[str] = Body(...), 
    library: str = Body(...), 
    series_folder_name: str = Body(None), 
    title: str = Body(None), 
    use_flaresolverr: bool = Body(True),
    frequency: str = Body("daily")
):
    if library not in LIBRARIES:
        raise HTTPException(status_code=400, detail="Invalid library selected.")

    watched_urls_raw = redis_client.smembers(WATCHED_URLS_REDIS_KEY)
    found_entry = None
    
    # 1. Try to find a match by the explicit series_folder_name
    if series_folder_name:
        for entry_json in watched_urls_raw:
            entry = json.loads(entry_json)
            if entry.get("series_folder_name") == series_folder_name:
                found_entry = entry
                break

    # 2. If not found, and a title is provided, try to match by sanitized title
    if not found_entry and title:
        potential_folder_name = sanitize_filename(title)
        for entry_json in watched_urls_raw:
            entry = json.loads(entry_json)
            if entry.get("series_folder_name") == potential_folder_name:
                found_entry = entry
                series_folder_name = potential_folder_name # Lock in the found folder name
                break

    final_urls_for_processing = source_urls

    if found_entry:
        # An existing series was found, so we merge the URLs and update frequency
        redis_client.srem(WATCHED_URLS_REDIS_KEY, json.dumps(found_entry))
        
        # If a new frequency was sent with the request, update the entry.
        if frequency and found_entry.get("frequency") != frequency:
            found_entry["frequency"] = frequency

        updated_urls = sorted(list(set(found_entry['series_urls'] + source_urls)))
        found_entry['series_urls'] = updated_urls
        final_urls_for_processing = updated_urls
        
        redis_client.sadd(WATCHED_URLS_REDIS_KEY, json.dumps(found_entry))

        # When updating an existing series, record a check for its frequency pool.
        record_check_timestamp.delay(found_entry.get("frequency", "daily"))

    else:
        # No existing series found, create a new one
        if not title and source_urls:
            try:
                with open('/app/app/sites_config.json', 'r') as f:
                    sites_config_list = json.load(f)
                first_url = source_urls[0]
                site_config = next((s for s in sites_config_list if urlparse(s['search_url_template']).hostname in first_url), None)
                if site_config:
                    scraped_metadata = _scrape_series_metadata_from_html(first_url, site_config)
                    scraped_title = scraped_metadata.get("title")
                    if scraped_title:
                        title = scraped_title
            except Exception as e:
                print(f"Could not scrape title from URL, will proceed with fallback name. Error: {e}")

        if title:
            series_folder_name = sanitize_filename(title)
        else:
            series_folder_name = f"{sanitize_filename(source_urls[0].strip('/').split('/')[-1])}_{uuid.uuid4().hex[:8]}"
        
        watched_entry = {
            "series_folder_name": series_folder_name,
            "series_urls": sorted(source_urls),
            "library": library,
            "use_flaresolverr": use_flaresolverr,
            "frequency": frequency
        }
        redis_client.sadd(WATCHED_URLS_REDIS_KEY, json.dumps(watched_entry))

    destination_path = LIBRARIES[library]
    task = process_series.delay(series_folder_name, final_urls_for_processing, destination_path, use_flaresolverr)

    return {"job_id": task.id, "status": "Discovery process initiated.", "series_folder_name": series_folder_name}

@app.post("/api/add_source_to_series")
async def add_source_to_series(series_folder_name: str = Body(...), new_source_url: str = Body(...)):
    watched_urls_raw = redis_client.smembers(WATCHED_URLS_REDIS_KEY)
    found_entry = None
    for entry_json in watched_urls_raw:
        entry = json.loads(entry_json)
        if entry.get("series_folder_name") == series_folder_name:
            found_entry = entry
            break

    if not found_entry:
        raise HTTPException(status_code=404, detail="Series not found.")

    redis_client.srem(WATCHED_URLS_REDIS_KEY, json.dumps(found_entry))
    if new_source_url not in found_entry['series_urls']:
        found_entry['series_urls'].append(new_source_url)
        found_entry['series_urls'].sort()
    redis_client.sadd(WATCHED_URLS_REDIS_KEY, json.dumps(found_entry))

    return {"status": "success", "message": "New source added to series."}

@app.post("/api/refresh_image")
async def refresh_image(request: RefreshImageRequest):
    if request.library not in LIBRARIES:
        raise HTTPException(status_code=400, detail="Invalid library selected.")
    
    destination_path = LIBRARIES[request.library]
    task = refresh_cover_image.delay(request.series_folder_name, request.source_url, destination_path, request.use_flaresolverr)
    
    return {"job_id": task.id, "status": "Cover image refresh initiated."}

@app.post("/api/refresh_metadata")
async def refresh_metadata(request: RefreshMetadataRequest):
    if request.library not in LIBRARIES:
        raise HTTPException(status_code=400, detail="Invalid library selected.")
    
    destination_path = LIBRARIES[request.library]
    task = refresh_series_metadata.delay(request.series_folder_name, request.series_urls, destination_path, request.use_flaresolverr)
    
    return {"job_id": task.id, "status": "Series metadata refresh initiated."}

@app.get("/api/watched_urls")
async def get_watched_urls():
    watched_urls_raw = redis_client.smembers(WATCHED_URLS_REDIS_KEY)
    watched_urls = []
    for url_json in watched_urls_raw:
        try:
            parsed_entry = json.loads(url_json)
            parsed_entry["display_site_name"] = get_display_site_name(parsed_entry["series_urls"][0])

            parsed_entry["missing_chapters_count"] = 0
            parsed_entry["missing_chapters_list"] = []
            
            chapters_redis_key = f"chapters:{parsed_entry['series_folder_name']}"
            all_chapters_json = redis_client.get(chapters_redis_key)
            
            if all_chapters_json:
                all_chapter_names = json.loads(all_chapters_json)
                series_path = os.path.join(LIBRARIES.get(parsed_entry["library"]), parsed_entry["series_folder_name"])
                
                if os.path.exists(series_path) and all_chapter_names:
                    downloaded_cbz_names = {os.path.splitext(name)[0] for name in os.listdir(series_path) if name.endswith('.cbz')}
                    
                    missing_chapters = [
                        name for name in all_chapter_names 
                        if sanitize_filename(name) not in downloaded_cbz_names
                    ]
                    
                    parsed_entry["missing_chapters_list"] = sorted(missing_chapters)
                    parsed_entry["missing_chapters_count"] = len(missing_chapters)

            watched_urls.append(parsed_entry)
        except (json.JSONDecodeError, IndexError, TypeError):
            continue
            
    return {"watched_urls": sorted(watched_urls, key=lambda x: x['series_folder_name'])}

@app.delete("/api/watched_urls")
async def remove_watched_url(request: RemoveSeriesRequest):
    series_folder_name = request.series_folder_name
    watched_urls_raw = redis_client.smembers(WATCHED_URLS_REDIS_KEY)
    entry_to_remove = None
    for entry_json in watched_urls_raw:
        entry = json.loads(entry_json)
        if entry.get("series_folder_name") == series_folder_name:
            entry_to_remove = entry_json
            break
    
    if not entry_to_remove:
        raise HTTPException(status_code=404, detail="Series not found in watched list.")

    if redis_client.srem(WATCHED_URLS_REDIS_KEY, entry_to_remove) > 0:
        return {"status": "success", "message": "Series removed."}
    else:
        raise HTTPException(status_code=500, detail="Failed to remove series from watched list.")

@app.get("/api/series_metadata/{series_folder_name}")
async def get_series_metadata(series_folder_name: str, library: str = Query(...)):
    if library not in LIBRARIES:
        raise HTTPException(status_code=400, detail="Invalid library.")
    
    json_path = os.path.join(LIBRARIES[library], series_folder_name, 'series.json')

    if not os.path.exists(json_path):
        raise HTTPException(status_code=404, detail="Series metadata not found.")
    
    try:
        with open(json_path, 'r') as f:
            data = json.load(f)
        return JSONResponse(content=data)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error reading metadata: {str(e)}")
        
# In api.py

@app.post("/api/remove_source_from_series")
async def remove_source_from_series(request: RemoveSourceRequest):
    """
    Finds a series by its folder name and removes a single source URL from its list.
    If the last source URL is removed, the entire series is removed from the watched list.
    """
    watched_urls_raw = redis_client.smembers(WATCHED_URLS_REDIS_KEY)
    entry_to_modify_json = None
    found_entry = None

    for entry_json in watched_urls_raw:
        entry = json.loads(entry_json)
        if entry.get("series_folder_name") == request.series_folder_name:
            found_entry = entry
            entry_to_modify_json = entry_json
            break
    
    if not found_entry:
        raise HTTPException(status_code=404, detail="Series not found in watched list.")

    if request.source_url_to_remove not in found_entry.get("series_urls", []):
        raise HTTPException(status_code=404, detail="Source URL not found in this series.")

    # Remove the old entry from the Redis set
    redis_client.srem(WATCHED_URLS_REDIS_KEY, entry_to_modify_json)

    # Remove the specific URL from the list
    found_entry["series_urls"].remove(request.source_url_to_remove)

    # If sources remain, add the updated entry back. Otherwise, the series is no longer watched.
    if found_entry["series_urls"]:
        redis_client.sadd(WATCHED_URLS_REDIS_KEY, json.dumps(found_entry))
        return {"status": "success", "message": "Source removed from series."}
    else:
        return {"status": "success", "message": "Last source removed. Series is no longer being watched."}
        
@app.get("/api/schedule_status")
async def get_schedule_status():
    """
    Calculates and returns the next scheduled run time for each frequency pool.
    """
    SCHEDULE_INTERVALS = {
        'hourly': timedelta(hours=1),
        'half_daily': timedelta(hours=12),
        'daily': timedelta(days=1),
        'weekly': timedelta(weeks=1)
    }
    
    next_run_times = {}

    for frequency, interval in SCHEDULE_INTERVALS.items():
        last_run_iso = redis_client.get(f"last_run:{frequency}")
        if last_run_iso:
            try:
                last_run_time = datetime.fromisoformat(last_run_iso)
                next_run_time = last_run_time + interval
                next_run_times[frequency] = next_run_time.isoformat()
            except ValueError:
                next_run_times[frequency] = None # Handle malformed timestamp
        else:
            # If a pool has never run, we can't calculate its next run time yet.
            next_run_times[frequency] = None

    return next_run_times
    
@app.get("/api/job_status")
async def get_job_status():
    """
    Inspects the Celery workers to find active and scheduled (reserved) tasks.
    """
    inspector = celery_app.control.inspect()
    active_tasks_raw = inspector.active()
    reserved_tasks_raw = inspector.reserved()
    
    active_jobs = []
    scheduled_jobs = []

    # Helper to safely extract chapter name from task args
    def parse_task_args(task):
        try:
            # Assumes the chapter name is the 3rd argument for download_single_url
            if task.get('type') == 'app.worker.download_single_url' and len(task.get('args', [])) > 2:
                return task['args'][2]
        except (IndexError, TypeError):
            pass
        return "Unknown Task"

    # Process active tasks
    if active_tasks_raw:
        for worker, tasks in active_tasks_raw.items():
            for task in tasks:
                active_jobs.append(f"Downloading: {parse_task_args(task)}")

    # Process scheduled/reserved tasks
    if reserved_tasks_raw:
        for worker, tasks in reserved_tasks_raw.items():
            for task in tasks:
                scheduled_jobs.append(f"Queued: {parse_task_args(task)}")

    return {"active_jobs": active_jobs, "scheduled_jobs": scheduled_jobs}
    
@app.get("/api/sites")
async def get_sites():
    """Reads the sites_config.json and returns a list of site names."""
    try:
        with open('/app/app/sites_config.json', 'r') as f:
            sites_config = json.load(f)
        site_names = [site['name'] for site in sites_config]
        return {"sites": site_names}
    except (FileNotFoundError, json.JSONDecodeError):
        return {"sites": []}
        

@app.post("/api/bulk_add")
async def bulk_add(request: BulkAddRequest):
    """
    Receives a list of URLs and dispatches a background task to process them.
    """
    if not request.urls:
        raise HTTPException(status_code=400, detail="URL list cannot be empty.")
    if request.library not in LIBRARIES:
        raise HTTPException(status_code=400, detail="Invalid library selected.")

    # Start the background task
    task = bulk_add_task.delay(request.urls, request.library, request.frequency)

    return {"job_id": task.id, "status": "Bulk import process initiated."}