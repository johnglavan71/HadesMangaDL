import asyncio
import httpx
import json
import re
import os
import tempfile
from urllib.parse import urljoin, urlparse, quote
from bs4 import BeautifulSoup
import requests

# --- Constants ---
FLARESOLVERR_URL = "http://localhost:8191/v1"
search_cache = {}

# --- FlareSolverr Helpers ---

def _get_flaresolverr_solution(target_url):
    """
    Posts a URL to FlareSolverr and returns the solution part of the response,
    which includes the HTML content, cookies, and user agent.
    """
    payload = {'cmd': 'request.get', 'url': target_url, 'maxTimeout': 60000}
    response = requests.post(FLARESOLVERR_URL, json=payload, timeout=65)
    response.raise_for_status()
    data = response.json()
    if data.get('status') != 'ok':
        raise Exception(f"FlareSolverr failed: {data.get('message')}")
    return data['solution']

def _create_flaresolverr_session(target_url):
    """
    Creates a FlareSolverr session and returns all necessary components for both
    gallery-dl (CLI arguments) and requests (cookies, user-agent).
    """
    solution = _get_flaresolverr_solution(target_url)
    user_agent = solution['userAgent']
    session_cookies = {c['name']: c['value'] for c in solution['cookies']}
    
    # Create a Netscape-formatted cookie file for gallery-dl
    cookie_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt')
    for cookie in solution['cookies']:
        # Format: domain, httpOnly, path, secure, expiry, name, value
        cookie_file.write(
            f"{cookie['domain']}\tTRUE\t{cookie['path']}\t{str(cookie['secure']).upper()}\t"
            f"{int(cookie.get('expiry', 0) or 0)}\t{cookie['name']}\t{cookie['value']}\n"
        )
    cookie_file.close()
    
    # Arguments for the gallery-dl command-line tool
    args = ['--user-agent', user_agent, '--cookies', cookie_file.name]
    return args, cookie_file.name, user_agent, session_cookies

def get_flaresolverr_session_args(target_url):
    """
    A convenience function that creates a cookie file and returns only the
    arguments needed for a gallery-dl subprocess call.
    """
    try:
        args, cookie_file_path, _, _ = _create_flaresolverr_session(target_url)
        return args, cookie_file_path
    except Exception as e:
        print(f"FlareSolverr session failed for {target_url}: {e}")
        return [], None

# --- Parsing Helpers ---

# In scraping.py

def extract_chapters_from_json(stdout):
    """
    Parses the --dump-json output from gallery-dl to extract a list of chapters.
    Handles multiple JSON formats and correctly combines chapter and chapter_minor.
    """
    chapters = []
    first_meta = None

    def process_chapter_data(data):
        nonlocal first_meta
        if isinstance(data, dict):
            if not first_meta:
                first_meta = data
            
            chapter_url = data.get('url')
            
            # --- NEW LOGIC TO COMBINE chapter and chapter_minor ---
            chapter_num_raw = None
            main_chapter = data.get('chapter')
            minor_chapter = data.get('chapter_minor')

            if main_chapter is not None:
                # If chapter_minor exists and is not an empty string, combine them
                if minor_chapter and str(minor_chapter).strip():
                    # This creates a string like "15.1" from chapter: 15 and chapter_minor: ".1"
                    chapter_num_raw = f"{main_chapter}{str(minor_chapter).strip()}"
                else:
                    chapter_num_raw = main_chapter
            else:
                # Fallback to the 'num' field if 'chapter' doesn't exist
                chapter_num_raw = data.get('num')
            # --- END OF NEW LOGIC ---

            if chapter_url and chapter_num_raw is not None:
                chapter_str = str(chapter_num_raw)
                parts = chapter_str.split('.')
                
                if len(parts) == 1:
                    padded_name = parts[0].zfill(4)
                elif len(parts) == 2:
                    int_part = parts[0].zfill(4)
                    dec_part = parts[1]
                    padded_name = f"{int_part}.{dec_part}"
                else:
                    padded_name = chapter_str
                
                name = f"Chapter {padded_name}"
                
                chapters.append({'url': chapter_url, 'name': name})

    try:
        full_json = json.loads(stdout)
        
        if isinstance(full_json, list):
            if not full_json:
                pass
            elif isinstance(full_json[0], list):
                for sublist in full_json:
                    if len(sublist) > 2 and isinstance(sublist[2], dict):
                        item_data = sublist[2]
                        item_data['url'] = sublist[1]
                        process_chapter_data(item_data)
            elif isinstance(full_json[0], dict):
                for entry in full_json:
                    process_chapter_data(entry)

        elif isinstance(full_json, dict):
            entries = full_json.get('entries')
            if isinstance(entries, list):
                for entry in entries:
                    process_chapter_data(entry)
            else:
                process_chapter_data(full_json)

    except json.JSONDecodeError:
        for line in stdout.strip().split('\n'):
            try:
                data = json.loads(line)
                if isinstance(data, list) and len(data) > 2 and isinstance(data[2], dict):
                    item_data = data[2]
                    item_data['url'] = data[1]
                    process_chapter_data(item_data)
                else:
                    process_chapter_data(data)
            except (json.JSONDecodeError, TypeError):
                continue

    return chapters, first_meta

def _scrape_series_metadata_from_html(series_url, site_config):
    """
    Scrapes a series page using BeautifulSoup and site-specific selectors
    to extract metadata like title, publisher, status, etc.
    """
    metadata = {}
    try:
        solution = _get_flaresolverr_solution(series_url)
        html_content = solution.get("response")
        if not html_content:
            return metadata

        soup = BeautifulSoup(html_content, 'html.parser')
        selectors = site_config.get('series_selectors', {})

        for key, selector in selectors.items():
            if not selector: continue
            
            elements = soup.select(selector)
            if not elements: continue

            if key in ['title', 'publisher', 'status', 'year', 'description']:
                element = elements[0]
                if element.name == 'meta':
                    metadata[key] = element.get('content', '').strip()
                else:
                    metadata[key] = element.get_text(strip=True)
            elif key == 'tags':
                metadata['tags'] = [tag.get_text(strip=True) for tag in elements]

    except Exception as e:
        print(f"Error scraping metadata from HTML for {series_url}: {e}")
    
    return metadata

def _scrape_cover_url_from_html(series_url, site_config, cookies, user_agent):
    """
    Scrapes a series page to find a cover image URL using site-specific selectors
    and common fallbacks like Open Graph tags.
    """
    try:
        solution = _get_flaresolverr_solution(series_url)
        html_content = solution.get("response")
        if not html_content:
            return None

        soup = BeautifulSoup(html_content, 'html.parser')
        cover_url = None
        
        # 1. Try the site-specific selector first
        selector = site_config.get('series_selectors', {}).get('cover_url')
        if selector:
            element = soup.select_one(selector)
            if element and element.get('src'):
                cover_url = urljoin(series_url, element['src'])

        # 2. Fallback to Open Graph meta tag
        if not cover_url:
            og_image = soup.find('meta', property='og:image')
            if og_image and og_image.get('content'):
                cover_url = og_image['content']
        
        return cover_url
    except Exception as e:
        print(f"Error scraping HTML for cover image from {series_url}: {e}")
        return None

def _normalize_status(status: str) -> str:
    """Normalizes scraped status strings to 'Continuing' or 'Ended'."""
    if not isinstance(status, str):
        return 'Continuing' # Default
    normalized_status = status.lower().strip()
    if any(s in normalized_status for s in ['continuing', 'ongoing', 'publishing']):
        return 'Continuing'
    elif any(s in normalized_status for s in ['ended', 'completed', 'finished']):
        return 'Ended'
    return 'Continuing'

# --- Main Search Functionality ---

# In scraping.py

async def search_all_sites(term: str, site_filter: str | None = None, limit: int = 10):
    """
    Asynchronously searches configured sites for a given term with a result limit.
    Can be filtered to search only one specific site.
    """
    # The cache is simple and doesn't account for limits, so we bypass it
    # if a specific limit is requested that isn't the default.
    if term in search_cache and not site_filter and limit == 10:
        return search_cache[term]

    try:
        with open('/app/app/sites_config.json', 'r') as f:
            sites_config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print("Error: sites_config.json not found or is invalid.")
        return []

    sites_to_search = sites_config
    if site_filter and site_filter != "All Sites":
        sites_to_search = [s for s in sites_config if s['name'] == site_filter]

    async def scrape_site(site_config: dict, search_term: str, result_limit: int):
        site_name = site_config['name']
        
        if site_name == "Mangadex":
            try:
                async with httpx.AsyncClient() as client:
                    api_url = "https://api.mangadex.org/manga"
                    # Use the result_limit parameter for the API call
                    params = {"title": search_term, "limit": result_limit, "includes[]": ["author", "cover_art"]}
                    response = await client.get(api_url, params=params)
                    response.raise_for_status()
                    data = response.json().get('data', [])
                    
                    results = []
                    for entry in data:
                        attrs = entry.get('attributes', {})
                        rels = {rel['type']: rel for rel in entry.get('relationships', [])}
                        
                        cover_filename = rels.get('cover_art', {}).get('attributes', {}).get('fileName')
                        cover_url = f"https://uploads.mangadex.org/covers/{entry['id']}/{cover_filename}" if cover_filename else None
                        
                        results.append({
                            "title": attrs.get('title', {}).get('en', 'N/A'),
                            "cover_url": cover_url,
                            "source_url": f"https://mangadex.org/title/{entry['id']}",
                            "site": site_name,
                            "author": rels.get('author', {}).get('attributes', {}).get('name', 'N/A'),
                            "status": attrs.get('status', 'N/A').title(),
                            "description": attrs.get('description', {}).get('en', 'N/A')
                        })
                    return results
            except Exception as e:
                print(f"Error using MangaDex API: {e}")
                return []
        
        encoded_term = quote(search_term.strip())
        search_url = site_config["search_url_template"].format(query=encoded_term)
        selectors = site_config["selectors"]

        try:
            async with httpx.AsyncClient() as client:
                payload = {'cmd': 'request.get', 'url': search_url, 'maxTimeout': 60000}
                response = await client.post(FLARESOLVERR_URL, json=payload, timeout=65.0)
                response.raise_for_status()
                
                solution = response.json().get("solution", {})
                html = solution.get("response")
                if not html: return []

                soup = BeautifulSoup(html, 'html.parser')
                results = []
                # Use the result_limit parameter to slice the results
                for element in soup.select(selectors["results_container"])[:result_limit]:
                    title_elem = element.select_one(selectors["result_title"])
                    url_elem = element.select_one(selectors["result_url"])
                    cover_elem = element.select_one(selectors["result_cover"])

                    if not (title_elem and url_elem): continue

                    results.append({
                        "title": title_elem.get_text(strip=True),
                        "cover_url": urljoin(solution.get("url"), cover_elem['src']) if cover_elem and cover_elem.get('src') else None,
                        "source_url": urljoin(solution.get("url"), url_elem['href']),
                        "site": site_name,
                        "author": None, "status": None, "description": None
                    })
                return results
        except Exception as e:
            print(f"Error scraping {site_name}: {e}")
            return []

    tasks = [scrape_site(site, term, limit) for site in sites_to_search]
    results_from_all_sites = await asyncio.gather(*tasks)
    
    all_results = [item for sublist in results_from_all_sites for item in sublist]
    
    if not site_filter or site_filter == "All Sites":
        search_cache[term] = all_results

    return all_results
    return all_results

    async def scrape_site(site_config: dict, search_term: str):
        site_name = site_config['name']
        
        # Special handling for MangaDex API
        if site_name == "Mangadex":
            try:
                async with httpx.AsyncClient() as client:
                    api_url = "https://api.mangadex.org/manga"
                    params = {"title": search_term, "limit": 10, "includes[]": ["author", "cover_art"]}
                    response = await client.get(api_url, params=params)
                    response.raise_for_status()
                    data = response.json().get('data', [])
                    
                    results = []
                    for entry in data:
                        attrs = entry.get('attributes', {})
                        rels = {rel['type']: rel for rel in entry.get('relationships', [])}
                        
                        cover_filename = rels.get('cover_art', {}).get('attributes', {}).get('fileName')
                        cover_url = f"https://uploads.mangadex.org/covers/{entry['id']}/{cover_filename}" if cover_filename else None
                        
                        results.append({
                            "title": attrs.get('title', {}).get('en', 'N/A'),
                            "cover_url": cover_url,
                            "source_url": f"https://mangadex.org/title/{entry['id']}",
                            "site": site_name,
                            "author": rels.get('author', {}).get('attributes', {}).get('name', 'N/A'),
                            "status": attrs.get('status', 'N/A').title(),
                            "description": attrs.get('description', {}).get('en', 'N/A')
                        })
                    return results
            except Exception as e:
                print(f"Error using MangaDex API: {e}")
                return []
        
        # Standard scraping for other sites
        encoded_term = quote(search_term.strip())
        search_url = site_config["search_url_template"].format(query=encoded_term)
        selectors = site_config["selectors"]

        try:
            async with httpx.AsyncClient() as client:
                payload = {'cmd': 'request.get', 'url': search_url, 'maxTimeout': 60000}
                response = await client.post(FLARESOLVERR_URL, json=payload, timeout=65.0)
                response.raise_for_status()
                
                solution = response.json().get("solution", {})
                html = solution.get("response")
                if not html: return []

                soup = BeautifulSoup(html, 'html.parser')
                results = []
                for element in soup.select(selectors["results_container"])[:5]: # Limit to 5 results per site
                    title_elem = element.select_one(selectors["result_title"])
                    url_elem = element.select_one(selectors["result_url"])
                    cover_elem = element.select_one(selectors["result_cover"])

                    if not (title_elem and url_elem): continue

                    results.append({
                        "title": title_elem.get_text(strip=True),
                        "cover_url": urljoin(solution.get("url"), cover_elem['src']) if cover_elem and cover_elem.get('src') else None,
                        "source_url": urljoin(solution.get("url"), url_elem['href']),
                        "site": site_name,
                        "author": None, "status": None, "description": None # These are often not on search pages
                    })
                return results
        except Exception as e:
            print(f"Error scraping {site_name}: {e}")
            return []

    tasks = [scrape_site(site, term) for site in sites_config]
    results_from_all_sites = await asyncio.gather(*tasks)
    
    all_results = [item for sublist in results_from_all_sites for item in sublist]
    search_cache[term] = all_results
    return all_results

def get_display_site_name(url: str) -> str:
    """Extracts a clean, human-readable site name from a URL."""
    try:
        hostname = urlparse(url).hostname
        if not hostname:
            return url
        if hostname.startswith('www.'):
            hostname = hostname[4:]
        
        parts = hostname.split('.')
        domain = parts[-2] if len(parts) > 1 else parts[0]
        return domain.replace('-', ' ').title()
    except Exception:
        return url