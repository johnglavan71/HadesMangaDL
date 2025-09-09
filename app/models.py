from pydantic import BaseModel

class RefreshImageRequest(BaseModel):
    series_folder_name: str
    source_url: str
    library: str
    use_flaresolverr: bool

class RefreshMetadataRequest(BaseModel):
    series_folder_name: str
    series_urls: list[str]
    library: str
    use_flaresolverr: bool

class RemoveSeriesRequest(BaseModel):
    series_folder_name: str
    
class RemoveSourceRequest(BaseModel):
    series_folder_name: str
    source_url_to_remove: str

# This model has been moved here
class UrlRequest(BaseModel):
    url: str
    
class BulkAddRequest(BaseModel):
    urls: list[str]
    library: str
    frequency: str