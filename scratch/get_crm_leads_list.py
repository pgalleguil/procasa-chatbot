# --- 1. LISTA DE LEADS (OPTIMIZADA / BULK QUERY) ---
def get_crm_leads_list(filtro_estado=None, busqueda=None, ordenar_por="prioridad", user_role="agente", user_name="", ejecutivo_filter=None, page=1, limit=10):
    db = get_db()
    query_parts = []
    
    # --- FILTRO DE SEGURIDAD (ROL) ---
    # Si NO es admin/supervisor, solo ver sus propios leads
    if user_role not in ["admin", "supervisor"] and user_name:
        regex_name = re.compile(re.escape(user_name), re.IGNORECASE)
        query_parts.append({
            "$or": [
                {"prospecto.ejecutivo": regex_name},
                {"ejecutivo_asignado": regex_name}
            ]
        })
    # Si es admin/supervisor y eligiÃ³ un ejecutivo especÃ­fico
    elif ejecutivo_filter and ejecutivo_filter != "Todos":
        regex_exec = re.compile(re.escape(ejecutivo_filter), re.IGNORECASE)
        query_parts.append({
            "$or": [
                {"prospecto.ejecutivo": regex_exec},
                {"ejecutivo_asignado": regex_exec}
            ]
        })

    if busqueda and busqueda.strip():
        term = busqueda.strip()
        # Limpiar caracteres no numÃ©ricos para bÃºsqueda exacta por telÃ©fono
        clean_phone = re.sub(r'\D', '', term)
        if clean_phone:
            regex_phone = re.compile(re.escape(clean_phone))
            query_parts.append({"phone": regex_phone})
        else:
            # BÃºsqueda por nombre si no es telÃ©fono
            regex_term = re.compile(re.escape(term), re.IGNORECASE)
            query_parts.append({"prospecto.nombre": regex_term})
    
    query = {"$and": query_parts} if query_parts else {}
    
    # 2. SEPARATE KPI COUNTS (Globales para la bÃºsqueda actual pero sin filtro de estado)
    base_kpi_query = query.copy() # Query que incluye ejecutivo y tÃ©rmino de bÃºsqueda
    
    # --- FILTRO DE ESTADO ---
    query_with_state = query.copy()
    UNASSIGNED_VALUES = [None, "", "Sin Asignar", "No asignado", "No Asignado", "Sin asignar"]
    
    if filtro_estado and filtro_estado != "Todos":
        if filtro_estado == "UNASSIGNED":
            # Caso especial: Sin Asignar (Nuevos sin ejecutivo)
            query_with_state["pipeline_stage"] = {"$in": [PipelineStage.NEW, None, "nuevo", "new"]}
            query_with_state["$or"] = [{"ejecutivo_asignado": {"$in": UNASSIGNED_VALUES}}, {"ejecutivo_asignado": {"$exists": False}}]
        else:
            # Mapeo invertido para buscar por el valor del Enum o string legacy en la DB
            state_db_value = filtro_estado
            if filtro_estado in ["nuevo", "NEW"]: 
                state_db_value = PipelineStage.NEW
                # IMPORTANTE: Para el listado "Sin Atender", tambiÃ©n excluimos los no asignados
                query_with_state["ejecutivo_asignado"] = {"$nin": UNASSIGNED_VALUES, "$exists": True}
            elif filtro_estado == "visita": state_db_value = PipelineStage.VISIT_SCHEDULED
            elif filtro_estado == "gestion": state_db_value = PipelineStage.CONTACTED
            elif filtro_estado == "cerrado": state_db_value = PipelineStage.CLOSED_WON
            query_with_state["pipeline_stage"] = state_db_value

    # 1. CONTAR TOTAL PARA PAGINACIÃ“N (Basado en el filtro de estado)
    total_count = db["leads"].count_documents(query_with_state)
    
    kpi_counts = {"total": total_count, "nuevo": 0, "gestion": 0, "visita": 0, "cerrado": 0, "sin_asignar": 0}
    
    assigned_filter = {"ejecutivo_asignado": {"$nin": UNASSIGNED_VALUES, "$exists": True}}
    unassigned_filter = {"$or": [{"ejecutivo_asignado": {"$in": UNASSIGNED_VALUES}}, {"ejecutivo_asignado": {"$exists": False}}]}
    
    # 1. SIN ASIGNAR (Nuevo y sin ejecutivo)
    kpi_counts["sin_asignar"] = db["leads"].count_documents({"$and": [base_kpi_query, {"pipeline_stage": {"$in": [PipelineStage.NEW, None, "nuevo", "new"]}}, unassigned_filter]})

    # 2. SIN ATENDER (Etapa NEW y ASIGNADO)
    kpi_counts["nuevo"] = db["leads"].count_documents({"$and": [base_kpi_query, {"pipeline_stage": {"$in": [PipelineStage.NEW, None, "nuevo", "new"]}}, assigned_filter]})

    # 3. EN GESTIÃ“N (CONTACTED, INTERESTED, OFFER, NEGOTIATION)
    kpi_counts["gestion"] = db["leads"].count_documents({"$and": [base_kpi_query, {"pipeline_stage": {"$in": [
        PipelineStage.CONTACTED, PipelineStage.INTERESTED, PipelineStage.OFFER, PipelineStage.NEGOTIATION,
        "gestion", "contacted"
    ]}}]})

    # 4. VISITAS (Agendadas o Realizadas)
    kpi_counts["visita"] = db["leads"].count_documents({"$and": [base_kpi_query, {"pipeline_stage": {"$in": [
        PipelineStage.VISIT_SCHEDULED, PipelineStage.VISIT_DONE, "visita"
    ]}}]})

    # 5. CERRADOS (Ganados o Perdidos)
    kpi_counts["cerrado"] = db["leads"].count_documents({"$and": [base_kpi_query, {"pipeline_stage": {"$in": [
        PipelineStage.CLOSED_WON, PipelineStage.CLOSED_LOST, "cerrado"
    ]}}]})
    
    # Coherencia del total global para la bÃºsqueda
    global_search_total = db["leads"].count_documents(base_kpi_query)
    kpi_counts["total"] = global_search_total

    # 3. TRAER LEADS PAGINADOS DESDE MONGO
    skip = (page - 1) * limit
    
    # Define sorting
    if ordenar_por == "prioridad":
        sort_criteria = [("priority_score", -1), ("last_event_at", -1)]
    else:
        sort_criteria = [("last_event_at", -1)]
        
    leads_cursor = db["leads"].find(query_with_state, {"messages": 0, "stage_history": 0})\
                              .sort(sort_criteria)\
                              .skip(skip)\
                              .limit(limit)
    
    leads_list = list(leads_cursor)

    leads_procesados = []
    # (KPI counts are already calculated via optimized MongoDB queries above)

    # 4b. BULK QUERY DE EVENTOS para los leads de ESTA PÃGINA solamente (mÃ¡x 10-20 telÃ©fonos)
    # Esto es O(page_size), no O(total_leads). Correcto y eficiente.
    page_phones = [l.get("phone", "").replace("+", "").strip() for l in leads_list]
    management_types = [
        "GESTION_LOG", "HUMAN_NOTE", "SEND_WA_LEAD", "SEND_EMAIL_LEAD",
        "CLICK_PHONE_LEAD", "CLICK_WHATSAPP_LEAD", "SEND_WA_OWNER", "SEND_EMAIL_OWNER",
        "CLICK_PHONE_OWNER", "CLICK_WHATSAPP_OWNER", "ALERT_SENT", "alert_sent"
    ]
    events_cursor = db["crm_events"].find(
        {"phone": {"$in": page_phones}, "type": {"$in": management_types}},
        sort=[("timestamp", -1)]
    )
    events_map = {}
    for ev in events_cursor:
        phone_ev = ev.get("phone", "")
        if phone_ev not in events_map:
            events_map[phone_ev] = ev

    type_labels = {
        "CLICK_WHATSAPP_LEAD": "Click WhatsApp (Lead)",
        "CLICK_PHONE_LEAD": "Llamada Iniciada",
        "CLICK_EMAIL_LEAD": "Click Email (Lead)",
        "SEND_WA_LEAD": "WhatsApp Enviado",
        "SEND_EMAIL_LEAD": "Email Enviado",
        "CLICK_WHATSAPP_OWNER": "Click WhatsApp (Prop)",
        "CLICK_PHONE_OWNER": "Llamada Prop. Iniciada",
        "CLICK_EMAIL_OWNER": "Click Email (Prop)",
        "SEND_WA_OWNER": "WhatsApp Enviado (Prop)",
        "SEND_EMAIL_OWNER": "Email Enviado (Prop)",
        "STATUS_CHANGE": "Cambio de Estado",
        "HUMAN_NOTE": "GestiÃ³n Manual",
        "ASSIGNMENT": "Lead Asignado",
        "GESTION_LOG": "GestiÃ³n Registrada",
        "ALERT_SENT": "Alerta Enviada",
        "MANUAL_ENTRY": "Ingreso Manual",
    }

    # 5. PROCESAR LEADS EN MEMORIA
    state_map = {
        # Enums
        PipelineStage.NEW:   {"label": "Sin Atender", "led": "led-red",    "priority": 1},
        PipelineStage.CONTACTED: {"label": "En GestiÃ³n",  "led": "led-yellow", "priority": 3},
        PipelineStage.INTERESTED: {"label": "Interesado",  "led": "led-yellow", "priority": 3},
        PipelineStage.VISIT_SCHEDULED:  {"label": "Visita Agendada", "led": "led-green",  "priority": 2},
        PipelineStage.VISIT_DONE:  {"label": "Visita Realizada", "led": "led-green",  "priority": 2},
        PipelineStage.OFFER:  {"label": "Oferta", "led": "led-green",  "priority": 2},
        PipelineStage.NEGOTIATION:  {"label": "NegociaciÃ³n", "led": "led-green",  "priority": 2},
        PipelineStage.CLOSED_WON: {"label": "Cerrado Ganado",     "led": "led-gray",   "priority": 4},
        PipelineStage.CLOSED_LOST: {"label": "Cerrado Perdido",     "led": "led-gray",   "priority": 4},
        # Legacy Support
        "nuevo":   {"label": "Sin Atender", "led": "led-red",    "priority": 1},
        "visita":  {"label": "Visita Agendada", "led": "led-green",  "priority": 2},
        "gestion": {"label": "En GestiÃ³n",  "led": "led-yellow", "priority": 3},
        "cerrado": {"label": "Cerrado",     "led": "led-gray",   "priority": 4}
    }
    # Tipos de eventos considerados como gestiÃ³n humana vÃ¡lida
    management_types = [
        "GESTION_LOG", "HUMAN_NOTE", "SEND_WA_LEAD", "SEND_EMAIL_LEAD", 
        "CLICK_PHONE_LEAD", "CLICK_WHATSAPP_LEAD", "SEND_WA_OWNER", "SEND_EMAIL_OWNER", 
        "CLICK_PHONE_OWNER", "CLICK_WHATSAPP_OWNER", "ALERT_SENT", "alert_sent"
    ]

    # 4. PROCESAR LEADS EN MEMORIA
    for lead in leads_list:
        raw_phone = lead.get("phone", "").replace("+", "").strip()
        estado_db = lead.get("pipeline_stage") or lead.get("stage") or lead.get("crm_estado") or PipelineStage.NEW
        
        # Normalizar strings legacy a Enums
        if isinstance(estado_db, str):
            estado_map_legacy = {
                "nuevo": PipelineStage.NEW,
                "new": PipelineStage.NEW,
                "contacted": PipelineStage.CONTACTED,
                "gestion": PipelineStage.CONTACTED,
                "visita": PipelineStage.VISIT_SCHEDULED,
                "cerrado": PipelineStage.CLOSED_WON
            }
            estado_db = estado_map_legacy.get(estado_db.lower(), PipelineStage.NEW)
        
        last_ev = events_map.get(raw_phone)
        if last_ev:
            last_action_text = type_labels.get(last_ev.get("type"), "AcciÃ³n registrada")
            last_action_note = last_ev.get("metadata", {}).get("note", "")
        else:
            last_action_text = lead.get("last_action_label") or "Sin gestiÃ³n aÃºn"
            last_action_note = ""
        
        ultimo_msg_ts = lead.get("prospecto", {}).get("ultimo_mensaje")
        lifecycle_ts = lead.get("lifecycle", {}).get("assigned_at")
        created_ts = lead.get("created_at")
        
        # Determine original fallback (Prioritize Assignment over Message for SLA consistency)
        # We now use the precomputed last_event_at if available
        last_ts = lead.get("last_event_at") or (last_ev.get("timestamp") if last_ev else None) or lifecycle_ts or ultimo_msg_ts or created_ts
        
        estado_final = estado_db
        
        # PromociÃ³n visual de estado: si tiene gestiÃ³n pero DB dice NEW, mostrar como CONTACTADO
        # Esto es visual solamente â€” no modifica la DB
        MANAGEMENT_LABELS = {
            "Click WhatsApp (Lead)", "Llamada Iniciada", "WhatsApp Enviado",
            "Email Enviado", "Click WhatsApp (Prop)", "Llamada Prop. Iniciada",
            "WhatsApp Enviado (Prop)", "Email Enviado (Prop)", "Cambio de Estado",
            "GestiÃ³n Manual", "GestiÃ³n Registrada"
        }
        has_management = last_action_text in MANAGEMENT_LABELS
        
        # Eliminada promocion visual para mantener consistencia estricta con los contadores de las tarjetas (KPIs)
        # if estado_final == PipelineStage.NEW and has_management:
        #     estado_final = PipelineStage.CONTACTED

        
        # Identificar ejecutivo y timestamp real para visualizaciÃ³n
        ejecutivo = lead.get("ejecutivo_asignado") or lead.get("prospecto", {}).get("ejecutivo")
        
        if last_ts:
            try: 
                if isinstance(last_ts, str):
                    last_ts_obj = datetime.fromisoformat(last_ts.replace('Z', '+00:00'))
                else:
                    last_ts_obj = last_ts
            except: last_ts_obj = datetime.now(CHILE_TZ)
        else:
            last_ts_obj = datetime.now(CHILE_TZ)

        # (SaaS Performance: Metrics are precomputed in lead doc)
        config_estado = state_map.get(estado_final, state_map[PipelineStage.CONTACTED])

        # 5. SLA / TIEMPO DE RESPUESTA (DinÃ¡mico para tiempo real)
        sla_status = lead.get("sla_status", "good")
        
        # Un lead tambiÃ©n se considera gestionado (fulfilled) si tiene eventos de gestiÃ³n recientes
        if estado_final == PipelineStage.NEW and not has_management:
            # Calcular SLA en tiempo real midiendo desde la ASIGNACIÃ“N (Sincronizado con Reporte SLA)
            start_time = lead.get("lifecycle", {}).get("assigned_at") or lead.get("created_at")
            if start_time:
                now_cl = datetime.now(CHILE_TZ)
                try:
                    if isinstance(start_time, str):
                        dt_start = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                    else:
                        dt_start = start_time
                    if dt_start.tzinfo is None:
                        dt_start = CHILE_TZ.localize(dt_start)
                    else:
                        dt_start = dt_start.astimezone(CHILE_TZ)
                        
                    # Contamos minutos hÃ¡biles para ser justos con los fines de semana/noches
                    mins = calculate_business_minutes(dt_start, now_cl)
                    sla_hours = mins / 60.0

                    if sla_hours <= 1.5:
                        sla_status = "good"
                    elif sla_hours < 3.0: # 3.0 horas hÃ¡biles es el threshold del reporte crÃ­tico
                        sla_status = "near_critical"
                    else:
                        sla_status = "critical"
                except Exception as eval_e:
                    logger.error(f"Error evaluando metricas de SLA en tarjeta: {eval_e}")
                    pass
        else:
            # Override visual para leads que ya estÃ¡n en gestiÃ³n
            sla_status = "fulfilled"
            
        sla_labels_map = {
            "critical": "CrÃ­tico",
            "near_critical": "PrÃ³ximo a CrÃ­tico",
            "warning": "Advertencia",
            "good": "En tiempo",
            "pending": "Pendiente AsignaciÃ³n",
            "fulfilled": "Gestionado"
        }
        sla_label = sla_labels_map.get(sla_status, "En tiempo")
        
        # Re-check pending if no executive
        if not ejecutivo or ejecutivo in [UNASSIGNED_LABEL, "No asignado", "Sin Asignar", "Sin asignar"]:
             sla_status = "pending"
             sla_label = "Pendiente AsignaciÃ³n"

        leads_procesados.append({
            "phone": raw_phone,
            "sla_status": sla_status,
            "sla_label": sla_label,
            "whatsapp_display": f"+{raw_phone}",
            "nombre": lead.get("prospecto", {}).get("nombre") or "Desconocido",
            "estado": estado_final,
            "estado_badge": config_estado["label"],
            "led_class": config_estado["led"],
            "tiempo_relativo": format_relative_time(last_ts_obj),
            "real_timestamp": last_ts_obj,
            "priority_score": config_estado["priority"],
            "codigo_propiedad": detect_property_code(lead) or "S/N",
            "url_propiedad": f"https://www.procasa.cl/{detect_property_code(lead)}" if detect_property_code(lead) else "#",
            "ultima_accion_titulo": last_action_text,
            "ultima_accion_note": last_action_note,
            "ejecutivo_nombre": ejecutivo or UNASSIGNED_LABEL,
            "fecha_asignacion_relativa": format_relative_time(lead.get("lifecycle", {}).get("assigned_at") or lead.get("fecha_asignacion")),
            "stage": lead.get("stage") or "new"
        })
    
    # 5. RETORNAR RESULTADOS
    return leads_procesados, kpi_counts, total_count


