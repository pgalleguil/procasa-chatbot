import unicodedata

def remove_accents(input_str):
    if not input_str:
        return ""
    nfkd_form = unicodedata.normalize('NFKD', input_str)
    return "".join([c for c in nfkd_form if not unicodedata.combining(c)])

def normalize_commune(name: str) -> str:
    if not name:
        return ""
    return remove_accents(name).strip().lower()

NEIGHBOR_MAP_RAW = {

    # ======================
    # REGIÓN METROPOLITANA
    # ======================

    # Oriente
    "Las Condes": ["Vitacura", "Lo Barnechea", "La Reina", "Providencia"],
    "Vitacura": ["Las Condes", "Lo Barnechea", "Providencia", "Huechuraba"],
    "Lo Barnechea": ["Las Condes", "Vitacura", "Colina"],
    "La Reina": ["Las Condes", "Peñalolén", "Ñuñoa", "Providencia"],
    "Providencia": ["Las Condes", "Vitacura", "Santiago", "Ñuñoa", "La Reina", "Recoleta"],
    "Ñuñoa": ["Providencia", "La Reina", "Macul", "Santiago", "San Joaquín"],
    "Peñalolén": ["La Reina", "Ñuñoa", "Macul", "La Florida"],

    # Centro
    "Santiago": ["Providencia", "Ñuñoa", "Estación Central", "Recoleta",
                  "Independencia", "San Miguel", "San Joaquín",
                  "Pedro Aguirre Cerda", "Quinta Normal"],
    "Estación Central": ["Santiago", "Maipú", "Cerrillos", "Lo Prado"],
    "Independencia": ["Santiago", "Recoleta", "Conchalí", "Renca"],
    "Recoleta": ["Santiago", "Providencia", "Independencia", "Huechuraba"],

    # Norte
    "Huechuraba": ["Vitacura", "Recoleta", "Conchalí", "Quilicura"],
    "Conchalí": ["Recoleta", "Huechuraba", "Quilicura", "Independencia"],
    "Quilicura": ["Huechuraba", "Conchalí", "Colina", "Lampa", "Renca"],
    "Renca": ["Independencia", "Conchalí", "Quilicura", "Cerro Navia"],
    "Colina": ["Lo Barnechea", "Quilicura", "Lampa"],
    "Lampa": ["Quilicura", "Colina", "Pudahuel"],

    # Poniente
    "Maipú": ["Estación Central", "Cerrillos", "Pudahuel"],
    "Pudahuel": ["Maipú", "Cerro Navia", "Lo Prado", "Lampa"],
    "Cerrillos": ["Maipú", "Estación Central", "Pedro Aguirre Cerda"],
    "Lo Prado": ["Estación Central", "Quinta Normal", "Cerro Navia"],
    "Quinta Normal": ["Santiago", "Lo Prado", "Cerro Navia"],
    "Cerro Navia": ["Lo Prado", "Renca", "Pudahuel"],

    # Sur
    "San Miguel": ["Santiago", "San Joaquín", "La Cisterna", "Pedro Aguirre Cerda"],
    "San Joaquín": ["Ñuñoa", "Macul", "Santiago", "San Miguel"],
    "Macul": ["Ñuñoa", "Peñalolén", "La Florida", "San Joaquín"],
    "La Florida": ["Peñalolén", "Macul", "Puente Alto", "La Granja"],
    "Puente Alto": ["La Florida", "La Pintana", "San Bernardo", "Pirque"],
    "La Pintana": ["Puente Alto", "San Bernardo"],
    "San Bernardo": ["El Bosque", "La Pintana", "Puente Alto", "Buin"],
    "El Bosque": ["San Bernardo", "La Cisterna"],
    "La Cisterna": ["San Miguel", "El Bosque", "San Ramón"],
    "San Ramón": ["La Cisterna", "La Granja"],
    "La Granja": ["La Florida", "San Ramón", "San Joaquín"],
    "Pedro Aguirre Cerda": ["Santiago", "San Miguel", "Cerrillos"],
    "Buin": ["San Bernardo", "Paine"],
    "Paine": ["Buin"],
    "Pirque": ["Puente Alto"],

    # ======================
    # REGIÓN DE VALPARAÍSO
    # ======================

    # Gran Valparaíso
    "Valparaíso": ["Viña del Mar", "Casablanca"],
    "Viña del Mar": ["Valparaíso", "Concón", "Quilpué"],
    "Concón": ["Viña del Mar", "Quintero"],
    "Quilpué": ["Viña del Mar", "Villa Alemana", "Limache"],
    "Villa Alemana": ["Quilpué", "Limache"],
    "Limache": ["Villa Alemana", "Quilpué", "Olmué"],
    "Olmué": ["Limache"],

    # Costa Norte
    "Quintero": ["Concón", "Puchuncaví"],
    "Puchuncaví": ["Quintero"],

    # Litoral Central (zona inmobiliaria fuerte)
    "Algarrobo": ["El Quisco", "El Tabo", "San Antonio"],
    "El Quisco": ["Algarrobo", "El Tabo"],
    "El Tabo": ["El Quisco", "Cartagena"],
    "Cartagena": ["El Tabo", "San Antonio"],
    "San Antonio": ["Cartagena", "Santo Domingo", "Algarrobo"],
    "Santo Domingo": ["San Antonio"],

    # Interior
    "Casablanca": ["Valparaíso", "Curacaví"],
    "Curacaví": ["Casablanca"]
}

def make_neighbor_map_symmetric(neighbor_map):
    for commune, neighbors in list(neighbor_map.items()):
        for n in neighbors:
            neighbor_map.setdefault(n, [])
            if commune not in neighbor_map[n]:
                neighbor_map[n].append(commune)
    return neighbor_map

# Initialize the symmetric map
NEIGHBOR_MAP = make_neighbor_map_symmetric(NEIGHBOR_MAP_RAW)

# Pre-normalized map for faster lookup
NEIGHBOR_MAP_NORM = {normalize_commune(k): [normalize_commune(n) for n in v] 
                     for k, v in NEIGHBOR_MAP.items()}
# Mapping normalized names back to official names
COMMUNE_OFFICIAL_NAMES = {normalize_commune(k): k for k in NEIGHBOR_MAP.keys()}

def get_neighboring_communes(commune_name: str) -> list[str]:
    """Returns a list of neighboring communes (official names) for the given commune name."""
    norm_input = normalize_commune(commune_name)
    if not norm_input:
        return []
    
    # Direct match in normalized map
    if norm_input in NEIGHBOR_MAP_NORM:
        norm_neighbors = NEIGHBOR_MAP_NORM[norm_input]
        return [COMMUNE_OFFICIAL_NAMES.get(nn, nn.title()) for nn in norm_neighbors]
    
    # Fuzzy/partial match (fallback)
    for norm_key, norm_neighbors in NEIGHBOR_MAP_NORM.items():
        if norm_key in norm_input or norm_input in norm_key:
            return [COMMUNE_OFFICIAL_NAMES.get(nn, nn.title()) for nn in norm_neighbors]
            
    return []
