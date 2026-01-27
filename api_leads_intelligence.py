from pymongo import MongoClient
from config import Config
from datetime import datetime, timedelta
from collections import Counter, defaultdict

# --------------------------------------------------
# Helper para conversión segura de int
# --------------------------------------------------
def safe_int_conversion(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None

# --------------------------------------------------
# 1. FECHA REAL DE CREACIÓN DEL LEAD
# --------------------------------------------------
def get_creation_date(doc):
    try:
        return doc["_id"].generation_time.replace(tzinfo=None)
    except Exception:
        messages = doc.get("messages", [])
        if messages:
            ts = messages[0].get("timestamp")
            if ts:
                try:
                    return datetime.fromisoformat(ts.replace("Z", ""))
                except Exception:
                    pass
        return datetime.now()

# --------------------------------------------------
# 2. SCORE DE CALIDAD (0–10) - LÓGICA HÍBRIDA
# --------------------------------------------------
def calculate_score(prospecto, intencion_legacy=None, bi_data=None):
    """
    Cálculo de Score Robusto: 
    Soporta la llamada antigua (3 args) y la nueva lógica de negocio.
    """
    score = 0
    
    # 1. PRIORIDAD MÁXIMA: ALERTA DE RECLAMO
    if bi_data and bi_data.get("ALERTA_CRITICA") == "RECLAMO_CONTACTO":
        return 10

    # 2. LOGICA DE NEGOCIO (Comportamiento)
    if bi_data:
        # Si es corredor, score mínimo
        if bi_data.get("TIPO_CONTACTO") == "CORREDOR_EXTERNO":
            return 1
            
        # Visita Solicitada o Agendada
        if bi_data.get("RESULTADO_CHAT") in ["VISITA_AGENDADA", "VISITA_SOLICITADA"]:
            score += 5
        
        # Urgencia
        if bi_data.get("URGENCIA") == "ALTA_URGENCIA":
            score += 3
            
        # Recuperabilidad (Tu criterio de diálogo activo)
        if bi_data.get("RECUPERABILIDAD") == "ALTA":
            score += 2
        elif bi_data.get("RECUPERABILIDAD") == "BAJA":
            return 1 # Abandono inicial o ghosting

    # 3. DATOS FÍSICOS (Si no hay BI o para sumar puntos extra)
    if prospecto.get("rut"): score += 1
    if prospecto.get("email"): score += 1

    # Si venía de la lógica antigua sin BI data
    if not bi_data and intencion_legacy == "agendar_visita":
        score += 4

    return min(score, 10)

# --------------------------------------------------
# 3. INTENCIÓN MÁS FUERTE (LEGACY)
# --------------------------------------------------
def determine_strongest_intent(messages):
    prioridades = [
        "escalado_urgente",
        "agendar_visita",
        "contacto_directo",
        "consultar_precio"
    ]
    intenciones = set()
    for m in messages:
        if m.get("intencion"):
            intenciones.add(m.get("intencion"))

    for p in prioridades:
        if p in intenciones:
            return p
    return "consulta_general"

# --------------------------------------------------
# 4. REPORTE EJECUTIVO (API PRINCIPAL)
# --------------------------------------------------
def get_leads_executive_report():
    try:
        client = MongoClient(Config.MONGO_URI)
        db = client[Config.DB_NAME]

        # Traemos leads ordenados por fecha descendente
        documentos = list(
            db["leads"]
            .find({})
            .sort("_id", -1)
            .limit(2000)
        )

        if not documentos:
            return {"kpis": {}, "charts": {}, "aggs": {}, "leads": []}

        # --- Inicializar Contadores ---
        temporal_diario = {}
        fuentes = Counter()
        bi_intenciones = Counter()
        bi_resultados = Counter()
        bi_recuperabilidad = Counter()

        # Contadores para propiedades (operacion: venta/arriendo, tipo: casa/depto, etc.)
        operaciones = Counter()
        tipos = Counter()
        comunas = Counter()

        # Para insights más profundos: precios por categoría, tiempos por operacion, etc.
        precios_por_operacion = defaultdict(list)
        precios_por_tipo = defaultdict(list)
        tiempos_por_operacion = defaultdict(list)
        hot_leads_por_tipo = defaultdict(int)

        total_leads = len(documentos)
        leads_calientes = 0
        leads_calientes_con_datos = 0
        tiempos_conversion = []

        leads_table = []

        hot_intents = ["agendar_visita", "escalado_urgente", "contacto_directo"]

        # Contadores diarios para KPIs específicos
        daily_totals = defaultdict(int)
        daily_hots = defaultdict(int)
        daily_completes = defaultdict(int)
        daily_tiempos = defaultdict(list)

        hoy = datetime.now().date()
        week_ago = hoy - timedelta(days=7)
        same_day_last_week = hoy - timedelta(days=7)

        for doc in documentos:
            p = doc.get("prospecto", {})
            messages = doc.get("messages", [])
            bi_data = doc.get("bi_analytics_global", {}) # Data de IA

            phone = doc.get("phone")
            nombre = p.get("nombre", "")
            email = p.get("email")
            rut = p.get("rut")

            comuna = p.get("comuna") or "Sin Comuna"
            origen = p.get("origen") or "Directo"

            comunas[comuna] += 1
            fuentes[origen] += 1

            # --- Propiedades: operacion (venta/arriendo), tipo (casa/depto) ---
            operacion = p.get("operacion") or "Venta"  # Ej: Venta, Arriendo
            tipo = p.get("tipo") or "Departamento"     # Ej: Casa, Departamento, Oficina

            operaciones[operacion] += 1
            tipos[tipo] += 1

            # Precio
            precio_uf_val = safe_int_conversion(p.get("precio_uf"))
            precio_uf = precio_uf_val if precio_uf_val is not None else 0
            if precio_uf > 0:
                precios_por_operacion[operacion].append(precio_uf)
                precios_por_tipo[tipo].append(precio_uf)

            # --- LÓGICA DE HOT LEADS ---
            intencion_legacy = determine_strongest_intent(messages)
            
            is_hot = False
            
            if bi_data:
                recup = bi_data.get("RECUPERABILIDAD", "")
                resultado = bi_data.get("RESULTADO_CHAT", "")
                
                if recup == "ALTA_PRIORIDAD" or resultado in ["VISITA_AGENDADA", "VISITA_SOLICITADA"]:
                    is_hot = True
            else:
                is_hot = intencion_legacy in hot_intents

            # --- FECHAS ---
            fecha_obj = get_creation_date(doc)
            fecha_date = fecha_obj.date()
            fecha_str = fecha_obj.strftime("%Y-%m-%d")

            if fecha_obj.year >= datetime.now().year - 1:
                temporal_diario[fecha_str] = temporal_diario.get(fecha_str, 0) + 1

            daily_totals[fecha_date] += 1

            if is_hot:
                leads_calientes += 1
                hot_leads_por_tipo[tipo] += 1
                daily_hots[fecha_date] += 1
                if email and rut:
                    leads_calientes_con_datos += 1
                    daily_completes[fecha_date] += 1
                
                # Tiempo de conversión
                try:
                    if messages:
                        t1 = datetime.fromisoformat(messages[0]["timestamp"].replace("Z", ""))
                        t2 = None
                        for m in messages:
                            if m.get("role") == "assistant" and m.get("intencion") in hot_intents:
                                t2 = datetime.fromisoformat(m["timestamp"].replace("Z", ""))
                                break
                        if t2 is None and len(messages) > 1:
                            t2 = datetime.fromisoformat(messages[-1]["timestamp"].replace("Z", ""))
                        if t2:
                            delta = (t2 - t1).total_seconds() / 60
                            if 0 < delta < 43200:
                                tiempos_conversion.append(delta)
                                tiempos_por_operacion[operacion].append(delta)
                                daily_tiempos[fecha_date].append(delta)
                except Exception:
                    pass

            # --- DATOS PARA GRÁFICOS BI ---
            if bi_data:
                intent_bi = bi_data.get("INTENCION_CLIENTE", "SIN_CLASIFICAR")
                res_bi = bi_data.get("RESULTADO_CHAT", "SIN_CLASIFICAR")
                recup_bi = bi_data.get("RECUPERABILIDAD", "N/A")

                bi_intenciones[intent_bi] += 1
                bi_resultados[res_bi] += 1
                bi_recuperabilidad[recup_bi] += 1
            
            # Calcular Score
            score = calculate_score(p, intencion_legacy, bi_data)

            leads_table.append({
                "nombre": nombre,
                "phone": phone,
                "email": email,
                "rut": rut,
                "intencion_legacy": intencion_legacy,
                "bi_data": bi_data,
                "score": score,
                "origen": origen,
                "fecha": fecha_str,
                "hot_lead": is_hot,
                "operacion": operacion,  # Nuevo para table
                "tipo": tipo,            # Nuevo para table
                "comuna": comuna,
                "precio_uf": precio_uf
            })

        # --- CÁLCULO DE PROMEDIOS KPI ---
        avg_speed = (
            sum(tiempos_conversion) / len(tiempos_conversion)
            if tiempos_conversion else 0
        )

        pct_hot_leads = (
            leads_calientes / total_leads * 100
            if total_leads > 0 else 0
        )

        tasa_captura_datos_hot = (
            leads_calientes_con_datos / leads_calientes * 100
            if leads_calientes > 0 else 0
        )

        penetracion_datos_total = (
            leads_calientes_con_datos / total_leads * 100
            if total_leads > 0 else 0
        )

        # --- KPIs diarios y comparaciones ---
        leads_hoy = daily_totals[hoy]
        hot_hoy = daily_hots[hoy]
        completes_hoy = daily_completes[hoy]
        avg_speed_hoy = sum(daily_tiempos[hoy]) / len(daily_tiempos[hoy]) if daily_tiempos[hoy] else 0
        hot_rate_hoy = (hot_hoy / leads_hoy * 100) if leads_hoy > 0 else 0
        tasa_datos_hoy = (completes_hoy / hot_hoy * 100) if hot_hoy > 0 else 0

        # Promedio 7d (últimos 7 días excluyendo hoy)
        last_7_days = [hoy - timedelta(days=i) for i in range(1, 8)]
        avg_total_7d = sum(daily_totals[d] for d in last_7_days) / 7 if last_7_days else 0
        avg_hot_7d = sum(daily_hots[d] for d in last_7_days) / 7 if last_7_days else 0
        avg_completes_7d = sum(daily_completes[d] for d in last_7_days) / 7 if last_7_days else 0
        avg_speed_7d = sum(sum(daily_tiempos[d]) / len(daily_tiempos[d]) if daily_tiempos[d] else 0 for d in last_7_days) / 7 if last_7_days else 0
        hot_rate_7d = (avg_hot_7d / avg_total_7d * 100) if avg_total_7d > 0 else 0
        tasa_datos_7d = (avg_completes_7d / avg_hot_7d * 100) if avg_hot_7d > 0 else 0

        pct_delta_total_7d = ((leads_hoy - avg_total_7d) / avg_total_7d * 100) if avg_total_7d > 0 else 0
        pct_delta_hot_7d = ((hot_hoy - avg_hot_7d) / avg_hot_7d * 100) if avg_hot_7d > 0 else 0
        pct_delta_datos_7d = ((tasa_datos_hoy - tasa_datos_7d) / tasa_datos_7d * 100) if tasa_datos_7d > 0 else 0

        # Vs mismo día semana pasada
        total_week = daily_totals[same_day_last_week]
        hot_week = daily_hots[same_day_last_week]
        completes_week = daily_completes[same_day_last_week]
        avg_speed_week = sum(daily_tiempos[same_day_last_week]) / len(daily_tiempos[same_day_last_week]) if daily_tiempos[same_day_last_week] else 0
        hot_rate_week = (hot_week / total_week * 100) if total_week > 0 else 0
        tasa_datos_week = (completes_week / hot_week * 100) if hot_week > 0 else 0

        pct_delta_total_week = ((leads_hoy - total_week) / total_week * 100) if total_week > 0 else 0
        pct_delta_hot_week = ((hot_hoy - hot_week) / hot_week * 100) if hot_week > 0 else 0
        pct_delta_datos_week = ((tasa_datos_hoy - tasa_datos_week) / tasa_datos_week * 100) if tasa_datos_week > 0 else 0

        # --- Agregados mejorados para insights (tops, avgs por categoría) ---
        top_operacion = operaciones.most_common(1)[0][0] if operaciones else "N/A"
        pct_top_operacion = round((operaciones[top_operacion] / total_leads * 100) if total_leads > 0 else 0, 1)
        
        top_tipo = tipos.most_common(1)[0][0] if tipos else "N/A"
        pct_top_tipo = round((tipos[top_tipo] / total_leads * 100) if total_leads > 0 else 0, 1)
        
        top_comuna = comunas.most_common(1)[0][0] if comunas else "N/A"
        pct_top_comuna = round((comunas[top_comuna] / total_leads * 100) if total_leads > 0 else 0, 1)
        
        avg_precio_uf = round(sum(precios_por_operacion[top_operacion] + precios_por_tipo[top_tipo]) / (len(precios_por_operacion[top_operacion]) + len(precios_por_tipo[top_tipo])) if (precios_por_operacion[top_operacion] or precios_por_tipo[top_tipo]) else 0, 1)

        # Avgs por categoría
        avgs_por_operacion = {op: round(sum(precios) / len(precios) if precios else 0, 1) for op, precios in precios_por_operacion.items()}
        avgs_por_tipo = {tp: round(sum(precios) / len(precios) if precios else 0, 1) for tp, precios in precios_por_tipo.items()}
        
        avg_tiempos_por_operacion = {op: round(sum(tiempos) / len(tiempos) if tiempos else 0, 1) for op, tiempos in tiempos_por_operacion.items()}
        
        pct_hot_por_tipo = {tp: round(hot / tipos[tp] * 100 if tipos[tp] > 0 else 0, 1) for tp, hot in hot_leads_por_tipo.items()}

        return {
            "kpis": {
                "total_leads": total_leads,
                "leads_calientes": leads_calientes,
                "pct_leads_calientes": round(pct_hot_leads, 1),
                "tasa_captura_datos_hot": round(tasa_captura_datos_hot, 1),
                "penetracion_datos_total": round(penetracion_datos_total, 1),
                "avg_speed_minutes": round(avg_speed, 1),
                # Nuevos KPIs diarios
                "leads_hoy": leads_hoy,
                "hot_hoy": hot_hoy,
                "hot_rate_hoy": round(hot_rate_hoy, 1),
                "tasa_datos_hoy": round(tasa_datos_hoy, 1),
                "avg_speed_hoy": round(avg_speed_hoy, 1),
                "pct_delta_total_7d": round(pct_delta_total_7d, 1),
                "pct_delta_hot_7d": round(pct_delta_hot_7d, 1),
                "pct_delta_datos_7d": round(pct_delta_datos_7d, 1),
                "pct_delta_total_week": round(pct_delta_total_week, 1),
                "pct_delta_hot_week": round(pct_delta_hot_week, 1),
                "pct_delta_datos_week": round(pct_delta_datos_week, 1),
                "hot_rate_7d": round(hot_rate_7d, 1),
                "hot_rate_week": round(hot_rate_week, 1),
                "tasa_datos_7d": round(tasa_datos_7d, 1),
                "avg_speed_7d": round(avg_speed_7d, 1)
            },
            "charts": {
                "temporal": {
                    "labels": sorted(temporal_diario.keys()),
                    "values": [temporal_diario[d] for d in sorted(temporal_diario)]
                },
                "fuentes": {
                    "labels": list(fuentes.keys()),
                    "values": list(fuentes.values())
                },
                "operaciones": {  # Nuevo chart para operaciones (venta/arriendo)
                    "labels": list(operaciones.keys()),
                    "values": list(operaciones.values())
                },
                "tipos": {  # Nuevo chart para tipos (casa/depto)
                    "labels": list(tipos.keys()),
                    "values": list(tipos.values())
                },
                "comunas": {  # Nuevo chart para comunas
                    "labels": list(comunas.keys()),
                    "values": list(comunas.values())
                },
                "bi_intencion": {
                    "labels": list(bi_intenciones.keys()),
                    "values": list(bi_intenciones.values())
                },
                "bi_resultado": {
                    "labels": list(bi_resultados.keys()),
                    "values": list(bi_resultados.values())
                },
                "bi_recuperabilidad": {
                    "labels": list(bi_recuperabilidad.keys()),
                    "values": list(bi_recuperabilidad.values())
                }
            },
            "aggs": {
                "top_operacion": top_operacion,
                "pct_top_operacion": pct_top_operacion,
                "top_tipo": top_tipo,
                "pct_top_tipo": pct_top_tipo,
                "top_comuna": top_comuna,
                "pct_top_comuna": pct_top_comuna,
                "avg_precio_uf": avg_precio_uf,
                # Nuevos aggs para insights ricos
                "avgs_precio_por_operacion": avgs_por_operacion,
                "avgs_precio_por_tipo": avgs_por_tipo,
                "avg_tiempos_por_operacion": avg_tiempos_por_operacion,
                "pct_hot_por_tipo": pct_hot_por_tipo
            },
            "leads": leads_table
        }

    except Exception as e:
        print("❌ Error en reporte intelligence:", e)
        return {"kpis": {}, "charts": {}, "aggs": {}, "leads": []}

def get_specific_lead_chat(phone):
    try:
        client = MongoClient(Config.MONGO_URI)
        db = client[Config.DB_NAME]
        doc = db["leads"].find_one({"phone": phone})
        if not doc: return None
        return {
            "phone": doc.get("phone"),
            "prospecto": doc.get("prospecto", {}),
            "messages": doc.get("messages", []),
            "bi_analytics_global": doc.get("bi_analytics_global", {})
        }
    except Exception as e:
        print(f"❌ Error obteniendo chat {phone}:", e)
        return None