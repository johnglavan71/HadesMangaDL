import os
import json
import subprocess
import requests
import tempfile
import shutil
import xml.etree.ElementTree as ET
from celery import Celery, group
from urllib.parse import urlparse
from datetime import datetime, timezone

# Imports from our application structure
from app.services import redis_client, WATCHED_URLS_REDIS_KEY, LIBRARIES
from app.scraping import (
    _create_flaresolverr_session,
    extract_chapters_from_json,
    _scrape_series_metadata_from_html,
    _scrape_cover_url_from_html,
    _normalize_status,
    get_flaresolverr_session_args
)
from app.utils import sanitize_filename

# --- Celery Configuration ---
CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
celery_app = Celery('worker', broker=CELERY_BROKER_URL, backend=CELERY_RESULT_BACKEND)

celery_app.conf.beat_schedule = {
    'check-hourly': {
        'task': 'app.worker.check_for_updates_by_frequency',
        'schedule': 3600.0,
        'args': ('hourly',),
    },
    'check-half-daily': {
        'task': 'app.worker.check_for_updates_by_frequency',
        'schedule': 43200.0,
        'args': ('half_daily',),
    },
    'check-daily': {
        'task': 'app.worker.check_for_updates_by_frequency',
        'schedule': 86400.0,
        'args': ('daily',),
    },
    'check-weekly': {
        'task': 'app.worker.check_for_updates_by_frequency',
        'schedule': 604800.0,
        'args': ('weekly',),
    },
}
celery_app.conf.timezone = 'America/Chicago'

GALLERY_DL_CONFIG_PATH = os.environ.get('GALLERY_DL_CONFIG', '/config/gallery-dl.conf')


# --- Webhook Notification Helper ---
def send_webhook_notification(chapter_url: str):
    """
    Sends a simple notification with the chapter URL to a configured webhook.
    """
    webhook_url = os.environ.get('WEBHOOK_URL')

    # If no webhook URL is configured, do nothing.
    if not webhook_url:
        return

    # Construct a simple payload for Discord/Slack
    payload = {
        "content": f"New chapter downloaded: {chapter_url}"
    }

    try:
        response = requests.post(webhook_url, json=payload, timeout=10)
        response.raise_for_status()
        print(f"Successfully sent webhook notification for '{chapter_url}'.")
    except requests.exceptions.RequestException as e:
        print(f"Error sending webhook notification: {e}")


# --- Celery Tasks ---
@celery_app.task(bind=True, max_retries=3, default_retry_delay=300)
def download_single_url(self, url: str, series_path: str, chapter_name: str, use_flaresolverr: bool):
    """
    Downloads all images from a single URL (a chapter), creates a ComicInfo.xml,
    compresses the result into a .cbz archive, and sends a notification.
    """
    cookie_file_path = None
    download_succeeded = False
    safe_chapter_name = sanitize_filename(chapter_name)
    chapter_dir_path = os.path.join(series_path, safe_chapter_name)
    os.makedirs(chapter_dir_path, exist_ok=True)

    try:
        print(f"Attempting direct download for: {url}")
        direct_command = ['gallery-dl', '--config', GALLERY_DL_CONFIG_PATH, '--directory', chapter_dir_path, '--verbose', url]
        direct_result = subprocess.run(direct_command, check=False, text=True, capture_output=True)

        download_succeeded = direct_result.returncode == 0 and os.path.exists(chapter_dir_path) and os.listdir(chapter_dir_path)

        if not download_succeeded and use_flaresolverr:
            print(f"Direct download failed for {url}. Retrying with FlareSolverr...")
            shutil.rmtree(chapter_dir_path)
            os.makedirs(chapter_dir_path, exist_ok=True)

            flaresolverr_args, cookie_file_path = get_flaresolverr_session_args(url)
            if not flaresolverr_args:
                raise Exception(f"Could not get FlareSolverr session arguments for {url}.")
            
            flaresolverr_command = direct_command + flaresolverr_args
            flaresolverr_result = subprocess.run(flaresolverr_command, check=False, text=True, capture_output=True)

            if flaresolverr_result.returncode != 0:
                raise Exception(f"gallery-dl with FlareSolverr also failed for {url}. Stderr: {flaresolverr_result.stderr}")
            
            download_succeeded = True

        elif not download_succeeded:
            raise Exception(f"Direct download failed and FlareSolverr is disabled. Stderr: {direct_result.stderr}")

        if not os.listdir(chapter_dir_path):
            download_succeeded = False
            print(f"Warning: Chapter directory {chapter_dir_path} is empty. Skipping CBZ creation.")
            return {"status": "Failed", "url": url, "error": "Chapter directory empty after download."}

        comicinfo_xml_path = os.path.join(chapter_dir_path, 'ComicInfo.xml')
        root = ET.Element('ComicInfo', {'xmlns:xsi': 'http://www.w3.org/2001/XMLSchema-instance', 'xmlns:xsd': 'http://www.w3.org/2001/XMLSchema'})
        ET.SubElement(root, 'Title').text = chapter_name
        tree = ET.ElementTree(root)
        ET.indent(tree, space="  ", level=0)
        tree.write(comicinfo_xml_path, encoding='utf-8', xml_declaration=True)

        cbz_base_name = os.path.join(series_path, safe_chapter_name)
        shutil.make_archive(cbz_base_name, 'zip', chapter_dir_path)
        os.rename(f"{cbz_base_name}.zip", f"{cbz_base_name}.cbz")
        print(f"Successfully created {cbz_base_name}.cbz")

    except Exception as exc:
        print(f"Download task for {url} failed, will retry if possible. Error: {exc}")
        self.retry(exc=exc)
        return {"status": "Retrying", "url": url}

    finally:
        if cookie_file_path and os.path.exists(cookie_file_path):
            os.remove(cookie_file_path)
        if os.path.exists(chapter_dir_path):
            shutil.rmtree(chapter_dir_path, ignore_errors=True)

    if download_succeeded:
        # Call the notification function with the chapter's direct URL
        send_webhook_notification(url)

    return {"status": "Completed", "url": url, "chapter_name": chapter_name}


@celery_app.task
def process_series(series_folder_name: str, series_urls: list, library_path: str, use_flaresolverr: bool = True):
    series_path = os.path.join(library_path, series_folder_name)
    os.makedirs(series_path, exist_ok=True)
    
    all_discovered_chapters = []
    first_chapter_meta = None
    scraped_html_metadata = {}
    flaresolverr_user_agent = None
    flaresolverr_session_cookies = None

    try:
        with open('/app/app/sites_config.json', 'r') as f:
            sites_config_list = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        return {"status": "Failed", "error": f"Could not load sites_config.json: {e}"}

    for series_url in series_urls:
        print(f"Discovering chapters and metadata for {series_url}...")
        cookie_file_path = None
        try:
            flaresolverr_args = []
            if use_flaresolverr:
                args, path, agent, cookies = _create_flaresolverr_session(series_url)
                flaresolverr_args, cookie_file_path = args, path
                if not flaresolverr_user_agent: flaresolverr_user_agent = agent
                if not flaresolverr_session_cookies: flaresolverr_session_cookies = cookies

            discover_command = ['gallery-dl', '--config', GALLERY_DL_CONFIG_PATH, '--dump-json', series_url]
            discover_command.extend(flaresolverr_args)

            proc = subprocess.run(discover_command, capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                print(f"gallery-dl discovery failed for {series_url}: {proc.stderr or proc.stdout}.")
                continue

            chapters_from_source, current_first_meta = extract_chapters_from_json(proc.stdout)
            all_discovered_chapters.extend(chapters_from_source)

            if not first_chapter_meta and current_first_meta:
                first_chapter_meta = current_first_meta
            
            site_config = next((s for s in sites_config_list if urlparse(s['search_url_template']).hostname in series_url), None)
            if site_config:
                scraped_html_metadata.update(_scrape_series_metadata_from_html(series_url, site_config))

        finally:
            if cookie_file_path and os.path.exists(cookie_file_path):
                os.remove(cookie_file_path)

    unique_by_url = {chap['url']: chap for chap in all_discovered_chapters}
    
    final_chapters = []
    seen_names = {}
    for chap in unique_by_url.values():
        original_name = chap['name']
        count = seen_names.get(original_name, 0)
        if count > 0:
            chap['name'] = f"{original_name} (Part {count + 1})"
        seen_names[original_name] = count + 1
        final_chapters.append(chap)

    unique_chapters = sorted(final_chapters, key=lambda x: x['name'])

    if not unique_chapters:
        return {"status": "Failed", "error": "No chapters found from any of the series pages."}

    chapters_redis_key = f"chapters:{series_folder_name}"
    chapter_names = [chap['name'] for chap in unique_chapters]
    redis_client.set(chapters_redis_key, json.dumps(chapter_names))

    series_json_path = os.path.join(series_path, 'series.json')

    if not os.path.exists(series_json_path):
        cover_url = None
        if first_chapter_meta:
            cover_url = first_chapter_meta.get('cover') or first_chapter_meta.get('thumbnail')
        
        site_config = next((s for s in sites_config_list if urlparse(s['search_url_template']).hostname in series_urls[0]), None)
        if not cover_url and series_urls and use_flaresolverr and site_config:
            cover_url = _scrape_cover_url_from_html(series_urls[0], site_config, flaresolverr_session_cookies, flaresolverr_user_agent)
        
        if cover_url:
            try:
                cover_path = os.path.join(series_path, 'cover.jpg')
                headers = {'User-Agent': flaresolverr_user_agent} if flaresolverr_user_agent else {}
                with requests.get(cover_url, stream=True, headers=headers, cookies=flaresolverr_session_cookies) as r:
                    r.raise_for_status()
                    with open(cover_path, 'wb') as f:
                        shutil.copyfileobj(r.raw, f)
                print(f"Downloaded cover image to {cover_path}")
            except Exception as e:
                print(f"Could not download cover image from {cover_url}: {e}")
        
        metadata = {}
        if first_chapter_meta: metadata.update(first_chapter_meta)
        metadata.update(scraped_html_metadata)
        
        filtered_metadata = {
            "version": "1.0.2",
            "metadata": {
                "type": "comicSeries",
                "publisher": metadata.get("publisher", metadata.get("author", "Unknown")),
                "name": metadata.get("manga", metadata.get("name", series_folder_name)),
                "year": int(str(metadata.get("release", metadata.get("year", "0"))).split('-')[0]),
                "description_text": metadata.get("description", metadata.get("description_text", "")).replace('\r', '').replace('\n', ''),
                "description_formatted": metadata.get("description", metadata.get("description_text", "")),
                "booktype": "Webtoon",
                "comic_image": "cover.jpg" if cover_url else "",
                "total_issues": len(unique_chapters),
                "status": _normalize_status(metadata.get("status", "Continuing"))
            }
        }
        with open(series_json_path, 'w') as f:
            json.dump(filtered_metadata, f, indent=4)

    try:
        downloaded_cbz_names = {
            os.path.splitext(f)[0] for f in os.listdir(series_path) if f.endswith('.cbz')
        }
    except FileNotFoundError:
        downloaded_cbz_names = set()

    new_chapters = [
        chap for chap in unique_chapters
        if sanitize_filename(chap['name']) not in downloaded_cbz_names
    ]

    if not new_chapters:
        return {"status": "Success", "message": "No new chapters found."}

    chapter_download_tasks = [download_single_url.s(chap['url'], series_path, chap['name'], use_flaresolverr) for chap in new_chapters]
    
    if chapter_download_tasks:
        group(chapter_download_tasks).apply_async()
        print(f"Dispatched {len(new_chapters)} new chapters for download.")

    return {"status": "Running", "new_chapters_queued": len(new_chapters)}


@celery_app.task
def refresh_cover_image(series_folder_name: str, source_url: str, library_path: str, use_flaresolverr: bool):
    series_path = os.path.join(library_path, series_folder_name)
    os.makedirs(series_path, exist_ok=True)
    
    try:
        with open('/app/app/sites_config.json', 'r') as f:
            sites_config_list = json.load(f)
            site_config = next((s for s in sites_config_list if urlparse(s['search_url_template']).hostname in source_url), None)
            if not site_config:
                return {"status": "Failed", "message": "No site configuration found."}
    except (FileNotFoundError, json.JSONDecodeError) as e:
        return {"status": "Failed", "message": f"sites_config.json error: {e}"}

    cover_url = None
    if use_flaresolverr:
        try:
            _, _, user_agent, cookies = _create_flaresolverr_session(source_url)
            cover_url = _scrape_cover_url_from_html(source_url, site_config, cookies, user_agent)
            
            if cover_url:
                cover_path = os.path.join(series_path, 'cover.jpg')
                headers = {'User-Agent': user_agent} if user_agent else {}
                with requests.get(cover_url, stream=True, headers=headers, cookies=cookies) as r:
                    r.raise_for_status()
                    with open(cover_path, 'wb') as f:
                        shutil.copyfileobj(r.raw, f)
                return {"status": "Success", "message": "Cover image refreshed."}
            else:
                return {"status": "Failed", "message": "No new cover image URL found."}
        except Exception as e:
            return {"status": "Failed", "message": f"Could not download new cover: {e}"}
    return {"status": "Failed", "message": "FlareSolverr was not enabled for this task."}


@celery_app.task
def refresh_series_metadata(series_folder_name: str, series_urls: list, library_path: str, use_flaresolverr: bool):
    print(f"Refreshing metadata for: {series_folder_name}")
    series_path = os.path.join(library_path, series_folder_name)
    series_json_path = os.path.join(series_path, 'series.json')
    chapters_redis_key = f"chapters:{series_folder_name}"

    if os.path.exists(series_json_path):
        os.remove(series_json_path)
    if redis_client.exists(chapters_redis_key):
        redis_client.delete(chapters_redis_key)
    
    process_series.delay(series_folder_name, series_urls, library_path, use_flaresolverr)
    return {"status": "Metadata refresh initiated via process_series task."}


@celery_app.task
def check_for_updates_by_frequency(frequency: str):
    """
    Scheduled task that checks a specific pool of watched series for new chapters.
    """
    redis_client.set(f"last_run:{frequency}", datetime.utcnow().isoformat())
    print(f"Running scheduled check for '{frequency}' frequency updates...")
    try:
        watched_urls_raw = redis_client.smembers(WATCHED_URLS_REDIS_KEY)
        if not watched_urls_raw:
            return

        update_tasks = []
        for entry_json in watched_urls_raw:
            try:
                entry = json.loads(entry_json)
                if entry.get("frequency", "daily") == frequency:
                    destination_path = LIBRARIES.get(entry["library"])
                    if all([entry.get("series_urls"), destination_path, entry.get("series_folder_name")]):
                        update_tasks.append(process_series.s(
                            entry["series_folder_name"],
                            entry["series_urls"],
                            destination_path,
                            entry.get("use_flaresolverr", True)
                        ))
            except (json.JSONDecodeError, KeyError) as e:
                print(f"Skipping malformed watched entry: {entry_json}, Error: {e}")
                continue
        
        if update_tasks:
            group(update_tasks).apply_async()
            print(f"Dispatched {len(update_tasks)} series for '{frequency}' update check.")
    except Exception as e:
        print(f"An error occurred during the scheduled '{frequency}' update check: {e}")


@celery_app.task
def record_check_timestamp(frequency: str):
    """Sets the 'last_run' timestamp for a given frequency pool to now."""
    if frequency:
        redis_client.set(f"last_run:{frequency}", datetime.utcnow().isoformat())


@celery_app.task
def bulk_add_task(urls: list[str], library: str, frequency: str):
    """
    Processes a list of URLs in the background to add them to the watchlist.
    """
    print(f"Starting bulk import of {len(urls)} URLs.")
    
    try:
        with open('/app/app/sites_config.json', 'r') as f:
            sites_config_list = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Bulk import failed: Could not load sites_config.json: {e}")
        return

    watched_urls_raw = redis_client.smembers(WATCHED_URLS_REDIS_KEY)
    watched_entries = [json.loads(entry) for entry in watched_urls_raw]

    for url in urls:
        if not url or not url.strip():
            continue

        print(f"Processing URL: {url}")
        try:
            site_config = next((s for s in sites_config_list if urlparse(s['search_url_template']).hostname in url), None)
            if not site_config:
                print(f"  -> Skipping, no site config found for {url}")
                continue
            
            scraped_metadata = _scrape_series_metadata_from_html(url, site_config)
            title = scraped_metadata.get("title")

            if not title:
                print(f"  -> Skipping, could not scrape a title from {url}")
                continue

            sanitized_title = sanitize_filename(title)
            found_entry = next((e for e in watched_entries if e.get("series_folder_name") == sanitized_title), None)

            if found_entry:
                if url not in found_entry['series_urls']:
                    print(f"  -> Found existing series '{sanitized_title}'. Adding new source URL.")
                    original_entry_json = next((raw for raw in watched_urls_raw if json.loads(raw) == found_entry), None)
                    if original_entry_json:
                        redis_client.srem(WATCHED_URLS_REDIS_KEY, original_entry_json)
                        found_entry['series_urls'].append(url)
                        found_entry['series_urls'].sort()
                        redis_client.sadd(WATCHED_URLS_REDIS_KEY, json.dumps(found_entry))
            else:
                print(f"  -> Found new series '{title}'. Adding to watchlist.")
                new_entry = {
                    "series_folder_name": sanitized_title,
                    "series_urls": sorted([url]),
                    "library": library,
                    "use_flaresolverr": True,
                    "frequency": frequency
                }
                redis_client.sadd(WATCHED_URLS_REDIS_KEY, json.dumps(new_entry))
                
                destination_path = LIBRARIES[library]
                process_series.delay(sanitized_title, [url], destination_path, True)
                
        except Exception as e:
            print(f"  -> Failed to process {url}. Error: {e}")
            continue

    print("Bulk import finished.")