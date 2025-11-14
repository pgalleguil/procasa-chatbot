# rag.py
# RAG y búsqueda semántica híbrida para propiedades

import numpy as np
import re  # Agregado para split de prefs múltiples
import torch  # Para no_grad
import threading  # Para lock en modelo
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity  # Mantengo por fallback, pero uso dot
from config import Config
from db import get_properties_filtered
import gc  # Para cleanup explícito

config = Config()

# Lazy loading robusto para ahorrar memoria en startup
_embedding_model = None
_model_lock = threading.Lock()

def get_embedding_model():
    global _embedding_model
    with _model_lock:
        if _embedding_model is None:
            print("[MODEL] Cargando SentenceTransformer (solo una vez)...")
            _embedding_model = SentenceTransformer(config.EMBEDDING_MODEL, device='cpu')
        return _embedding_model

EMBEDDING_DIM = config.EMBEDDING_DIM

def embed_text(text: str) -> np.ndarray:
    model = get_embedding_model()  # Carga solo si se usa
    with torch.no_grad():  # Evita gradientes (ahorra ~10-20% RAM)
        emb = model.encode([text], batch_size=1, show_progress_bar=False, convert_to_tensor=False)
    return np.array(emb[0])  # A np directamente

# Definiciones globales para expansiones y triggers (evita redefiniciones en cada llamada)
LOCATIVE_EXPANSIONS = {
    # Positivas: Lo que la gente quiere cerca o con (comodidad, seguridad, lifestyle)
    'metro': "propiedades con ubicación privilegiada cerca de estaciones de metro, excelente conectividad con transporte público, a pasos de paradas urbanas, accesible por metro en Santiago",
    'colegios': "propiedades cerca de colegios y escuelas de calidad, en sectores educativos, a pasos de instituciones como Verbo Divino o San Ignacio para familias con niños",
    'malls': "propiedades cerca de centros comerciales, malls accesibles, próximos a Costanera Center o Alto Las Condes para compras y entretenimiento diario",
    'parques': "propiedades cerca de parques y áreas verdes, en entornos naturales, próximos a Parque Bicentenario o plazas residenciales para recreación y aire fresco",
    'supermercado': "propiedades cerca de supermercados y servicios básicos, accesibles a Jumbo, Líder o Unimarc, con conveniencia diaria para compras y vida familiar",
    'hospital': "propiedades cerca de hospitales y clínicas, en sectores médicos, a pasos de Clínica Alemana o centros de salud para tranquilidad en emergencias",
    'universidad': "propiedades cerca de universidades y centros educativos superiores, en zonas académicas, a pasos de PUC o Universidad de Chile para estudiantes o profesores",
    'playa': "propiedades cerca de playas y costa, con acceso directo al mar, en Reñaca, Concón o Algarrobo, ideales para veraneos, caminatas o vida costera relajada",
    'vista mar': "con vista panorámica al mar, orientada al océano, en primera línea costera, vistas despejadas a la playa o bahía en Viña del Mar o Valparaíso para amaneceres inspiradores",
    'vista cordillera': "con vista impresionante a la cordillera de los Andes, orientada a montañas, en alturas con panoramas nevados o verdes, para conexión con la naturaleza y serenidad diaria",
    'jardin': "propiedades con jardín amplio y bien mantenido, espacios verdes privados, ideales para familias, mascotas o cultivos, con áreas para relajarse al aire libre",
    'seguro': "propiedades en barrios seguros y vigilados, con portería 24/7, cercas perimetrales y comunidades cerradas, para paz mental y protección familiar",
    # Negativas: Lo que NO quieren (ruido, contaminación, bullicio) - Flip a opuestos para semántica
    'neg_trafico': "propiedades en zonas residenciales tranquilas y alejadas de avenidas ruidosas, sin exposición a tráfico intenso, en barrios peatonales o pasajes internos para paz total",
    'neg_colegios': "propiedades en sectores residenciales distantes de colegios y zonas escolares bulliciosas, priorizando tranquilidad y privacidad sobre proximidad educativa",
    'neg_ruido': "propiedades con aislamiento acústico superior, en entornos silenciosos sin fuentes de ruido urbano, ideales para descanso profundo, trabajo remoto o meditación",
    'neg_aeropuerto': "propiedades alejadas de rutas aéreas y ruido de aviones, en áreas no afectadas por vuelos frecuentes, con silencio aéreo garantizado para noches apacibles",
    'neg_industrial': "propiedades lejos de zonas industriales o fábricas, en entornos residenciales puros sin olores, maquinaria o contaminación, para aire limpio y calidad de vida",
    'neg_contaminacion': "propiedades en áreas con bajo nivel de contaminación, con ventilación natural y proximidad a zonas verdes, evitando smog urbano para salud respiratoria y bienestar",
    'neg_vecinos ruidosos': "propiedades en condominios con normas estrictas de convivencia, unidades independientes o en pasajes privados, minimizando interacciones ruidosas para privacidad absoluta",
    'neg_mascotas': "propiedades en edificios o sectores sin restricciones de mascotas ruidosas, con áreas comunes limpias y sin perros ladrando, para alérgicos o prefieren silencio"
}

LOCATIVE_TRIGGERS = {
    # Positivos
    'metro': ['metro', 'estacion', 'a pasos', 'transporte publico'],
    'colegios': ['colegios', 'escuelas', 'educacion', 'niños cerca'],
    'malls': ['mall', 'centro comercial', 'shopping', 'compras'],
    'parques': ['parque', 'areas verdes', 'plaza', 'recreacion'],
    'supermercado': ['supermercado', 'jumbo', 'lider', 'tiendas', 'compras diarias'],
    'hospital': ['hospital', 'clinica', 'salud', 'medico', 'emergencias'],
    'universidad': ['universidad', 'u. de chile', 'puc', 'estudiantes'],
    'playa': ['playa', 'costa', 'renaca', 'concon', 'veraneo'],
    'vista mar': ['vista mar', 'vista oceano', 'costa', 'playa vista'],
    'vista cordillera': ['vista cordillera', 'vista montaña', 'andes', 'altura'],
    'jardin': ['jardin', 'espacio verde privado', 'patio', 'cultivos'],
    'seguro': ['seguro', 'vigilado', 'porteria', 'comunidad cerrada'],
    # Negativos
    'neg_trafico': ['trafico', 'ruido trafico', 'avenidas ruidosas', 'lejos trafico'],
    'neg_colegios': ['colegios ruidosos', 'lejos colegios', 'sin cerca escuelas', 'evitar niños'],
    'neg_ruido': ['ruido', 'lejos ruido', 'silencioso', 'sin bullicio'],
    'neg_aeropuerto': ['aeropuerto', 'ruido aviones', 'lejos aeropuerto', 'sin vuelos'],
    'neg_industrial': ['industrial', 'fabrica', 'lejos fabrica', 'sin olores'],
    'neg_contaminacion': ['contaminacion', 'smog', 'lejos contaminacion', 'aire limpio'],
    'neg_vecinos ruidosos': ['vecinos ruidosos', 'sin fiestas', 'privacidad', 'lejos vecinos'],
    'neg_mascotas': ['mascotas ruidosas', 'sin perros', 'lejos animales', 'alergia mascotas']
}

def hybrid_semantic_search(properties: list, semantic_query: str, top_k: int = config.TOP_K) -> list:
    if not semantic_query:
        return sorted(properties, key=lambda p: p.get('boost_score', 0), reverse=True)[:top_k]
    
    # MEJORA: Split para prefs múltiples (e.g., "cerca metro, lejos colegios, lejos ruido" → procesa cada uno)
    prefs_list = re.split(r',|y|pero|que no', semantic_query.lower())
    prefs_list = [p.strip() for p in prefs_list if p.strip()]
    print(f"[LOG] Prefs split: {prefs_list}")
    
    # Construye combined_query completo ANTES de embed
    combined_query = semantic_query.lower()  # Base en lower
    for pref in prefs_list:
        # Lógica por pref (llama detección para cada uno)
        sub_key = None
        sub_is_neg = any(phrase in pref for phrase in ['lejos', 'sin cerca', 'evitar', 'alejado'])
        for key, words in LOCATIVE_TRIGGERS.items():
            if any(word in pref for word in words):
                sub_key = key
                break
        if sub_key:
            sub_exp = LOCATIVE_EXPANSIONS.get(sub_key, "propiedades con ubicación ideal")
            if sub_is_neg and not sub_key.startswith('neg_'):
                sub_exp = sub_exp.replace('cerca de', 'alejada de').replace('proximidad a', 'distancia de').replace('a pasos de', 'lejos de')
            combined_query += " " + sub_exp
            print(f"[LOG] Sub-expansión para '{pref}': '{sub_exp}' (neg: {sub_is_neg})")
    
    # Lógica de detección EN combined_query (ya expandida)
    is_locative = False
    expansion_key = None
    is_negative = any(phrase in combined_query for phrase in ['lejos de', 'sin cerca', 'evitar', 'alejado'])
    for key, words in LOCATIVE_TRIGGERS.items():
        if any(word in combined_query for word in words):
            is_locative = True
            base_key = key.replace('neg_', '')
            expansion_key = key if is_negative and key.startswith('neg_') else (f'neg_{base_key}' if is_negative else base_key)
            break
    
    semantic_query_final = combined_query  # Ya es la expandida/base
    threshold = config.SEMANTIC_THRESHOLD_BASE
    weights = config.WEIGHTS
    
    if is_locative:
        expanded_query = LOCATIVE_EXPANSIONS.get(expansion_key, "propiedades con ubicación ideal en entornos tranquilos y convenientes")  # Fallback genérico
        if is_negative and not expansion_key.startswith('neg_'):
            expanded_query = expanded_query.replace('cerca de', 'alejada de').replace('proximidad a', 'distancia de').replace('a pasos de', 'lejos de')  # Flip simple
            print(f"[LOG] Flip NEGATIVO aplicado: '{expanded_query}' para '{combined_query}'")
        
        semantic_query_final = expanded_query
        threshold = 0.35 if is_negative else (0.25 if len(properties) > 10 else 0.2)
        weights = [0.7 if is_negative else 0.6, 0.15, 0.15, 0.1]  # Más desc para neg (tranquilidad)
        print(f"[LOG] Expansión {'NEGATIVA' if is_negative else 'positiva'} para '{expansion_key}': '{expanded_query}' (threshold={threshold})")
    else:
        if semantic_query:
            threshold = 0.15 if 'piscina' in semantic_query.lower() else 0.05  # Original
            initial_pass = len([p for p in properties if p.get('boost_score', 0) > 0])
            if len(properties) < 5 or initial_pass < 3:
                threshold *= 0.7
            if len(properties) < 5:
                threshold *= 0.5
            print(f"[LOG] Threshold ajustado para '{semantic_query}': {threshold} (set size: {len(properties)}, initial_pass: {initial_pass})")
    
    # UN SOLO EMBED: Para la query final
    query_emb = embed_text(semantic_query_final)
    query_emb = query_emb / np.linalg.norm(query_emb)  # Normaliza una vez para dot products
    
    semantic_query = semantic_query_final  # Para logs
    
    sucre_props = [p for p in properties if p.get('oficina', '') == config.PRIORITY_OFICINA]
    other_props = [p for p in properties if p.get('oficina', '') != config.PRIORITY_OFICINA]
    
    print(f"[LOG] Props SUCRE: {len(sucre_props)}, Otras: {len(other_props)}")
    
    # Scores SUCRE con dot manual (eficiente)
    weights = weights[:4] if len(weights) > 4 else weights + [0.0] * (4 - len(weights))  # Asegura 4 pesos
    sucre_scores = []
    for prop in sucre_props:
        desc_emb = np.array(prop.get('vector_descripción', [0]*EMBEDDING_DIM))
        if np.linalg.norm(desc_emb) > 0:
            desc_emb = desc_emb / np.linalg.norm(desc_emb)
        sim_desc = np.dot(query_emb, desc_emb)
        
        imgs_emb = np.array(prop.get('vector_images', [0]*EMBEDDING_DIM))
        if np.linalg.norm(imgs_emb) > 0:
            imgs_emb = imgs_emb / np.linalg.norm(imgs_emb)
        sim_imgs = np.dot(query_emb, imgs_emb)
        
        amens_emb = np.array(prop.get('vector_amenities', [0]*EMBEDDING_DIM))
        if np.linalg.norm(amens_emb) > 0:
            amens_emb = amens_emb / np.linalg.norm(amens_emb)
        sim_amens = np.dot(query_emb, amens_emb)
        
        desc_clean_emb = np.array(prop.get('vector_descripcion_clean', [0]*EMBEDDING_DIM))
        if np.linalg.norm(desc_clean_emb) > 0:
            desc_clean_emb = desc_clean_emb / np.linalg.norm(desc_clean_emb)
        sim_desc_clean = np.dot(query_emb, desc_clean_emb)
        
        semantic_score = (weights[0] * sim_desc + weights[1] * sim_imgs + weights[2] * sim_amens + weights[3] * sim_desc_clean)
        hybrid_score = config.HYBRID_WEIGHT * semantic_score + (1 - config.HYBRID_WEIGHT) * prop.get('boost_score', 0)
        
        sucre_scores.append((prop, hybrid_score, sim_amens))
        if len(sucre_scores) <= 2:
            print(f"[LOG] SUCRE Prop {prop.get('codigo', 'N/A')} scores: desc={sim_desc:.3f}, imgs={sim_imgs:.3f}, amens={sim_amens:.3f}, desc_clean={sim_desc_clean:.3f}, hybrid={hybrid_score:.3f}")
        
        # Cleanup explícito
        del desc_emb, imgs_emb, amens_emb, desc_clean_emb
    
    # Scores otras (mismo para other_props)
    other_scores = []
    for prop in other_props:
        desc_emb = np.array(prop.get('vector_descripción', [0]*EMBEDDING_DIM))
        if np.linalg.norm(desc_emb) > 0:
            desc_emb = desc_emb / np.linalg.norm(desc_emb)
        sim_desc = np.dot(query_emb, desc_emb)
        
        imgs_emb = np.array(prop.get('vector_images', [0]*EMBEDDING_DIM))
        if np.linalg.norm(imgs_emb) > 0:
            imgs_emb = imgs_emb / np.linalg.norm(imgs_emb)
        sim_imgs = np.dot(query_emb, imgs_emb)
        
        amens_emb = np.array(prop.get('vector_amenities', [0]*EMBEDDING_DIM))
        if np.linalg.norm(amens_emb) > 0:
            amens_emb = amens_emb / np.linalg.norm(amens_emb)
        sim_amens = np.dot(query_emb, amens_emb)
        
        desc_clean_emb = np.array(prop.get('vector_descripcion_clean', [0]*EMBEDDING_DIM))
        if np.linalg.norm(desc_clean_emb) > 0:
            desc_clean_emb = desc_clean_emb / np.linalg.norm(desc_clean_emb)
        sim_desc_clean = np.dot(query_emb, desc_clean_emb)
        
        semantic_score = (weights[0] * sim_desc + weights[1] * sim_imgs + weights[2] * sim_amens + weights[3] * sim_desc_clean)
        hybrid_score = config.HYBRID_WEIGHT * semantic_score + (1 - config.HYBRID_WEIGHT) * prop.get('boost_score', 0)
        
        other_scores.append((prop, hybrid_score, sim_amens))
        if len(other_scores) <= 3:
            print(f"[LOG] OTRA Prop {prop.get('codigo', 'N/A')} scores: desc={sim_desc:.3f}, imgs={sim_imgs:.3f}, amens={sim_amens:.3f}, desc_clean={sim_desc_clean:.3f}, hybrid={hybrid_score:.3f}")
        
        # Cleanup explícito
        del desc_emb, imgs_emb, amens_emb, desc_clean_emb
    
    ranked_sucre = sorted(sucre_scores, key=lambda x: x[1], reverse=True)
    ranked_other = sorted(other_scores, key=lambda x: x[1], reverse=True)
    
    top_properties = []
    top_sucre = []
    if ranked_sucre:
        top_sucre = [p for p, score, _ in ranked_sucre if score >= threshold][:1]
        top_properties.extend(top_sucre)
        print(f"[LOG] Top SUCRE seleccionado: {len(top_sucre)}")
    
    top_other_k = top_k - len(top_properties)
    top_other = [p for p, score, _ in ranked_other if score >= threshold][:top_other_k]
    top_properties.extend(top_other)
    print(f"[LOG] Top OTRAS seleccionado: {len(top_other)} (de {top_other_k} solicitados, threshold {threshold})")
    
    current_len = len(top_properties)
    if current_len < top_k:
        remaining_k = top_k - current_len
        remaining_sucre = [p for p, score, _ in ranked_sucre[len(top_sucre):] if p not in top_properties][:remaining_k]
        top_properties.extend(remaining_sucre)
        remaining_k -= len(remaining_sucre)
        if remaining_k > 0:
            remaining_other = [p for p, score, _ in ranked_other[len(top_other):] if p not in top_properties][:remaining_k]
            top_properties.extend(remaining_other)
    
    # MEJORA: Fallback si < top_k (e.g., solo 2 pasan)
    if len(top_properties) < top_k and properties:
        seen_codes = {p['codigo'] for p in top_properties}  # Set para O(1)
        remaining_k = top_k - len(top_properties)
        fallback_props = [p for p in properties if p['codigo'] not in seen_codes][:remaining_k]
        top_properties.extend(fallback_props)
        print(f"[LOG] Fallback agregado: {len(fallback_props)} props para llegar a {top_k}.")
    
    # MEJORA: Fallback total si 0 results
    if len(top_properties) == 0 and properties:
        print(f"[LOG] Fallback total: Relajando threshold a 0 para '{semantic_query}' y tomando top {top_k}.")
        top_properties = sorted(properties, key=lambda p: p.get('boost_score', 0), reverse=True)[:top_k]
    
    print(f"[LOG] Búsqueda semántica: {len(top_properties)} propiedades top para query '{semantic_query}' (threshold {threshold}). Prioridad: SUCRE primero.")
    
    # Cleanup final
    gc.collect()
    
    return top_properties

def search_properties(criteria: dict, semantic_prefs: str = "") -> list:
    # FIX PRINCIPAL: Pasa semantic_prefs al filtrado DB para aplicar keyword PRE-semántica
    filtered_props = get_properties_filtered(criteria, semantic_prefs=semantic_prefs)
    top_props = hybrid_semantic_search(filtered_props, semantic_prefs)
    gc.collect()  # Cleanup post-search
    return top_props