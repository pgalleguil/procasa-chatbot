# rag.py
# RAG y búsqueda semántica híbrida para propiedades

import numpy as np
import re  # Agregado para split de prefs múltiples
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
from config import Config
from db import get_properties_filtered

config = Config()

embedding_model = SentenceTransformer(config.EMBEDDING_MODEL)
EMBEDDING_DIM = config.EMBEDDING_DIM

def embed_text(text: str) -> np.ndarray:
    return embedding_model.encode(text)

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
    
    # Combina expansiones para query final (positivas + negativas)
    combined_query = semantic_query  # Base
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
    
    query_emb = embed_text(combined_query.lower())
    semantic_query = combined_query  # Usa combinada para final
    
    # Lógica de detección (simple)
    is_locative = False
    expansion_key = None
    is_negative = any(phrase in semantic_query.lower() for phrase in ['lejos de', 'sin cerca', 'evitar', 'alejado'])
    for key, words in LOCATIVE_TRIGGERS.items():
        if any(word in semantic_query.lower() for word in words):
            is_locative = True
            base_key = key.replace('neg_', '')
            expansion_key = key if is_negative and key.startswith('neg_') else (f'neg_{base_key}' if is_negative else base_key)
            break
    
    if is_locative:
        expanded_query = LOCATIVE_EXPANSIONS.get(expansion_key, "propiedades con ubicación ideal en entornos tranquilos y convenientes")  # Fallback genérico
        if is_negative and not expansion_key.startswith('neg_'):
            expanded_query = expanded_query.replace('cerca de', 'alejada de').replace('proximidad a', 'distancia de').replace('a pasos de', 'lejos de')  # Flip simple
            print(f"[LOG] Flip NEGATIVO aplicado: '{expanded_query}' para '{semantic_query}'")
        
        semantic_query = expanded_query
        query_emb = embed_text(semantic_query.lower())
        threshold = 0.35 if is_negative else (0.25 if len(properties) > 10 else 0.2)
        weights = [0.7 if is_negative else 0.6, 0.15, 0.15, 0.1]  # Más desc para neg (tranquilidad)
        print(f"[LOG] Expansión {'NEGATIVA' if is_negative else 'positiva'} para '{expansion_key}': '{expanded_query}' (threshold={threshold})")
    else:
        weights = config.WEIGHTS
        threshold = config.SEMANTIC_THRESHOLD_BASE
        if semantic_query:
            threshold = 0.15 if 'piscina' in semantic_query.lower() else 0.05  # Original
            initial_pass = len([p for p in properties if p.get('boost_score', 0) > 0])
            if len(properties) < 5 or initial_pass < 3:
                threshold *= 0.7
            if len(properties) < 5:
                threshold *= 0.5
            print(f"[LOG] Threshold ajustado para '{semantic_query}': {threshold} (set size: {len(properties)}, initial_pass: {initial_pass})")
    
    sucre_props = [p for p in properties if p.get('oficina', '') == config.PRIORITY_OFICINA]
    other_props = [p for p in properties if p.get('oficina', '') != config.PRIORITY_OFICINA]
    
    print(f"[LOG] Props SUCRE: {len(sucre_props)}, Otras: {len(other_props)}")
    
    # Scores SUCRE
    weights = weights[:4] if len(weights) > 4 else weights + [0.0] * (4 - len(weights))  # Asegura 4 pesos
    sucre_scores = []
    for prop in sucre_props:
        desc_emb = np.array(prop.get('vector_descripción', [0]*EMBEDDING_DIM)) if prop.get('vector_descripción') else np.zeros(EMBEDDING_DIM)
        imgs_emb = np.array(prop.get('vector_images', [0]*EMBEDDING_DIM)) if prop.get('vector_images') else np.zeros(EMBEDDING_DIM)
        amens_emb = np.array(prop.get('vector_amenities', [0]*EMBEDDING_DIM)) if prop.get('vector_amenities') else np.zeros(EMBEDDING_DIM)
        desc_clean_emb = np.array(prop.get('vector_descripcion_clean', [0]*EMBEDDING_DIM)) if prop.get('vector_descripcion_clean') else np.zeros(EMBEDDING_DIM)
        
        sim_desc = cosine_similarity([query_emb], [desc_emb])[0][0]
        sim_imgs = cosine_similarity([query_emb], [imgs_emb])[0][0]
        sim_amens = cosine_similarity([query_emb], [amens_emb])[0][0]
        sim_desc_clean = cosine_similarity([query_emb], [desc_clean_emb])[0][0]
        
        semantic_score = (weights[0] * sim_desc + weights[1] * sim_imgs + weights[2] * sim_amens + weights[3] * sim_desc_clean)
        hybrid_score = config.HYBRID_WEIGHT * semantic_score + (1 - config.HYBRID_WEIGHT) * prop.get('boost_score', 0)
        
        sucre_scores.append((prop, hybrid_score, sim_amens))
        if len(sucre_scores) <= 2:
            print(f"[LOG] SUCRE Prop {prop.get('codigo', 'N/A')} scores: desc={sim_desc:.3f}, imgs={sim_imgs:.3f}, amens={sim_amens:.3f}, desc_clean={sim_desc_clean:.3f}, hybrid={hybrid_score:.3f}")
    
    # Scores otras (mismo para other_props)
    other_scores = []
    for prop in other_props:
        desc_emb = np.array(prop.get('vector_descripción', [0]*EMBEDDING_DIM)) if prop.get('vector_descripción') else np.zeros(EMBEDDING_DIM)
        imgs_emb = np.array(prop.get('vector_images', [0]*EMBEDDING_DIM)) if prop.get('vector_images') else np.zeros(EMBEDDING_DIM)
        amens_emb = np.array(prop.get('vector_amenities', [0]*EMBEDDING_DIM)) if prop.get('vector_amenities') else np.zeros(EMBEDDING_DIM)
        desc_clean_emb = np.array(prop.get('vector_descripcion_clean', [0]*EMBEDDING_DIM)) if prop.get('vector_descripcion_clean') else np.zeros(EMBEDDING_DIM)
        
        sim_desc = cosine_similarity([query_emb], [desc_emb])[0][0]
        sim_imgs = cosine_similarity([query_emb], [imgs_emb])[0][0]
        sim_amens = cosine_similarity([query_emb], [amens_emb])[0][0]
        sim_desc_clean = cosine_similarity([query_emb], [desc_clean_emb])[0][0]
        
        semantic_score = (weights[0] * sim_desc + weights[1] * sim_imgs + weights[2] * sim_amens + weights[3] * sim_desc_clean)
        hybrid_score = config.HYBRID_WEIGHT * semantic_score + (1 - config.HYBRID_WEIGHT) * prop.get('boost_score', 0)
        
        other_scores.append((prop, hybrid_score, sim_amens))
        if len(other_scores) <= 3:
            print(f"[LOG] OTRA Prop {prop.get('codigo', 'N/A')} scores: desc={sim_desc:.3f}, imgs={sim_imgs:.3f}, amens={sim_amens:.3f}, desc_clean={sim_desc_clean:.3f}, hybrid={hybrid_score:.3f}")
    
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
        remaining_k = top_k - len(top_properties)
        fallback_props = sorted([p for p in properties if p not in top_properties], key=lambda p: p.get('boost_score', 0), reverse=True)[:remaining_k]
        top_properties.extend(fallback_props)
        print(f"[LOG] Fallback agregado: {len(fallback_props)} props para llegar a {top_k}.")
    
    # MEJORA: Fallback total si 0 results
    if len(top_properties) == 0 and properties:
        print(f"[LOG] Fallback total: Relajando threshold a 0 para '{semantic_query}' y tomando top {top_k}.")
        top_properties = sorted(properties, key=lambda p: p.get('boost_score', 0), reverse=True)[:top_k]
    
    print(f"[LOG] Búsqueda semántica: {len(top_properties)} propiedades top para query '{semantic_query}' (threshold {threshold}). Prioridad: SUCRE primero.")
    return top_properties

def search_properties(criteria: dict, semantic_prefs: str = "") -> list:
    # FIX PRINCIPAL: Pasa semantic_prefs al filtrado DB para aplicar keyword PRE-semántica
    filtered_props = get_properties_filtered(criteria, semantic_prefs=semantic_prefs)
    top_props = hybrid_semantic_search(filtered_props, semantic_prefs)
    return top_props