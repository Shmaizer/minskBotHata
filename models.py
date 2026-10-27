from dataclasses import dataclass
from typing import Optional
from datetime import datetime


@dataclass
class Listing:
    source: str
    url: str
    title: str = ""
    price: Optional[int] = None  # Monthly price in BYN only
    rooms: Optional[int] = None
    location: str = ""  # Address/location as displayed by the listing site
    metro: str = ""
    area: str = ""
    description: str = ""
    image_url: str = ""
    published_at: Optional[datetime] = None  # Source-reported publication time in Europe/Minsk
