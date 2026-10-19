from typing import Annotated

from fastapi import Depends

from app.services.cache import CacheService, get_cache_service

Cache = Annotated[CacheService, Depends(get_cache_service)]
