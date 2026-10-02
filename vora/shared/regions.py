"""Country names with their ISO code and DuckDuckGo search region."""

from __future__ import annotations

REGIONS = {
    "argentina": ("AR", "ar-es"), "australia": ("AU", "au-en"), "austria": ("AT", "at-de"),
    "bangladesh": ("BD", "wt-wt"), "belgium": ("BE", "be-fr"), "brazil": ("BR", "br-pt"),
    "canada": ("CA", "ca-en"), "chile": ("CL", "cl-es"), "china": ("CN", "cn-zh"),
    "colombia": ("CO", "co-es"), "denmark": ("DK", "dk-da"), "egypt": ("EG", "xa-ar"),
    "finland": ("FI", "fi-fi"), "france": ("FR", "fr-fr"), "germany": ("DE", "de-de"),
    "greece": ("GR", "gr-el"), "india": ("IN", "in-en"), "indonesia": ("ID", "id-en"),
    "ireland": ("IE", "ie-en"), "israel": ("IL", "il-he"), "italy": ("IT", "it-it"),
    "japan": ("JP", "jp-jp"), "kenya": ("KE", "wt-wt"), "malaysia": ("MY", "my-en"),
    "mexico": ("MX", "mx-es"), "netherlands": ("NL", "nl-nl"), "new zealand": ("NZ", "nz-en"),
    "nigeria": ("NG", "wt-wt"), "norway": ("NO", "no-no"), "pakistan": ("PK", "pk-en"),
    "philippines": ("PH", "ph-en"), "poland": ("PL", "pl-pl"), "portugal": ("PT", "pt-pt"),
    "russia": ("RU", "ru-ru"), "saudi arabia": ("SA", "xa-ar"), "singapore": ("SG", "sg-en"),
    "south africa": ("ZA", "za-en"), "south korea": ("KR", "kr-kr"), "korea": ("KR", "kr-kr"),
    "spain": ("ES", "es-es"), "sri lanka": ("LK", "wt-wt"), "sweden": ("SE", "se-sv"),
    "switzerland": ("CH", "ch-de"), "taiwan": ("TW", "tw-tzh"), "thailand": ("TH", "th-th"),
    "turkey": ("TR", "tr-tr"), "uae": ("AE", "xa-en"), "united arab emirates": ("AE", "xa-en"),
    "uk": ("GB", "uk-en"), "united kingdom": ("GB", "uk-en"), "ukraine": ("UA", "ua-uk"),
    "usa": ("US", "us-en"), "us": ("US", "us-en"), "united states": ("US", "us-en"),
    "vietnam": ("VN", "vn-vi"),
}

COUNTRY_NAMES = frozenset(REGIONS)
