"""
Extractor HTTP de leads desde Prop360.

Uso (dry-run):
    python scraping_convecta/extractor_prop360.py --dry-run

Uso (productivo):
    python scraping_convecta/extractor_prop360.py

Uso (fecha específica):
    python scraping_convecta/extractor_prop360.py --from-date 2026-07-01

Uso (todos los leads):
    python scraping_convecta/extractor_prop360.py --all

Uso (prueba con un solo lead, sin escribir):
    python scraping_convecta/extractor_prop360.py --dry-run --max-leads 1

Deduplicación (antes de insertar en la colección `leads`):
    - Busca por teléfono normalizado (o email si no hay teléfono).
    - Omite el lead si ese cliente YA tiene registrada la MISMA propiedad.
    - El mismo cliente con OTRA propiedad NO se omite (se permite).

Variables de entorno requeridas:
    PROP360_EMAIL
    PROP360_PASSWORD
    MONGO_URI
    DB_NAME

Variables de entorno opcionales:
    PROP360_LOGIN_URL  (default: https://procasa.prop360.cl/index)
    PROP360_LOGIN_ASHX (default: https://procasa.prop360.cl/recursos/login.ashx)
    PROP360_LEADS_ASHX (default: https://procasa.prop360.cl/backOffice/Recursos/leadHandler.ashx)
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from chatbot.storage import get_db, COLLECTION_CONVERSATIONS
from chatbot.phone_utils import normalize_phone_strict
from chatbot.ingest_service import ingest_lead_event, LeadEvent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("extractor_prop360")

PROP360_LOGIN_URL = os.getenv("PROP360_LOGIN_URL", "https://procasa.prop360.cl/index")
PROP360_LOGIN_ASHX = os.getenv("PROP360_LOGIN_ASHX", "https://procasa.prop360.cl/recursos/login.ashx")
PROP360_LEADS_ASHX = os.getenv("PROP360_LEADS_ASHX", "https://procasa.prop360.cl/backOffice/Recursos/leadHandler.ashx")

# Aliases del nuevo esquema (leadHandler.ashx -> antiguo listadoContactos).
# El nuevo listado renombra los campos con prefijos c*/p* (cId, pId, cName, ...).
NEW_SCHEMA_ALIASES = {
    "idContacto": "cId",
    "codigo": "pId",
    "contNombre": "cName",
    "contFono": "cPhone1",
    "contMail": "cMail",
    "contMsg": "cMessage",
    "medio": "cSource",
    "idm": "cSourceId",
    "fechaContacto": "cDate",
    "tipoContacto": "cType",
    "contIdEstado": "cSchedStatus",
    "tipo": "pType",
    "comuna": "pBorough",
    "direccionRef": "pRefDirection",
    "sucursal": "aOffice",
    "venta": "pSell",
    "arriendo": "pRent",
    "pv": "pSellPrice",
    "pa": "pRentPrice",
    "pat": "pTRentPrice",
    "estadov": "pSellStatus",
    "estadoa": "pRentStatus",
    "imgPropiedad": "pImage",
}


class Prop360AuthError(Exception):
    pass


class Prop360Extractor:
    def __init__(self, email: str, password: str, dry_run: bool = False):
        self.email = email
        self.password = password
        self.dry_run = dry_run
        self.client = httpx.Client(verify=False, follow_redirects=True, timeout=30.0)
        self.session_active = False
        self.metrics = {
            "started_at": None,
            "finished_at": None,
            "total_received": 0,
            "events_new": 0,
            "leads_created": 0,
            "leads_updated": 0,
            "events_duplicate": 0,
            "properties_found": 0,
            "properties_not_found": 0,
            "identity_conflicts": 0,
            "duplicates_skipped": 0,
            "notifications_enqueued": 0,
            "errors": 0,
            "duration_seconds": 0,
            "last_id_contacto": None,
        }

    def login(self) -> bool:
        logger.info("Iniciando sesión en Prop360...")
        resp = self.client.get(
            f"{PROP360_LOGIN_URL}?ReturnUrl=%2FbackOffice%2Fpropiedades%2FpropLeads"
        )
        logger.info(f"GET login page: status={resp.status_code}")

        login_data = {
            "accion": "login",
            "rfield": "",
            "mail": self.email,
            "password": self.password,
            "usr": 0,
            "_": time.time() % 10,
        }
        resp2 = self.client.post(
            PROP360_LOGIN_ASHX,
            data=login_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        logger.info(f"POST login.ashx: status={resp2.status_code}")

        try:
            result = resp2.json()
        except json.JSONDecodeError:
            raise Prop360AuthError(f"Respuesta no JSON en login: {resp2.text[:200]}")

        if result.get("acceso") != "sí":
            raise Prop360AuthError(f"Login fallido: {result.get('mensajeError', 'desconocido')}")

        redirect = result.get("redireccion", "/backoffice/inicio/index.aspx")
        resp3 = self.client.get(f"https://procasa.prop360.cl{redirect}")
        logger.info(f"Redirect a backoffice: status={resp3.status_code}")

        cookies = {}
        for cookie in list(self.client.cookies.jar):
            cookies[cookie.name] = cookie.value[:20] + "..."
        logger.info(f"Cookies de sesión: {cookies}")

        self.session_active = True
        logger.info("Sesión Prop360 establecida correctamente.")
        return True

    @staticmethod
    def _to_dm(value: Optional[str]) -> str:
        """Convierte YYYY-MM-DD[Thh:mm:ss] a dd/mm/yyyy. Deja intacto dd/mm/yyyy."""
        s = str(value or "").strip()
        if not s:
            return s
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
        if m:
            return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
        return s

    def normalize_lead(self, prop360_lead: Dict[str, Any]) -> Dict[str, Any]:
        """Traduce el listado nuevo (leadHandler.ashx) al esquema antiguo y viceversa.

        Soporta ambos esquemas para no romper si el backend revierte a listadoContactos.
        """
        def get(old_key: str, new_key: Optional[str], default: Any = None) -> Any:
            if old_key in prop360_lead:
                return prop360_lead.get(old_key, default)
            if new_key and new_key in prop360_lead:
                return prop360_lead.get(new_key, default)
            return default

        captador = get("captador", None)
        if not captador:
            a_name = str(get(None, "aName", "") or "").strip()
            a_sname = str(get(None, "aSName", "") or "").strip()
            captador = f"{a_name} {a_sname}".strip() or None

        return {
            "idContacto": get("idContacto", "cId"),
            "codigo": get("codigo", "pId"),
            "contNombre": get("contNombre", "cName"),
            "contFono": get("contFono", "cPhone1"),
            "contMail": get("contMail", "cMail"),
            "contMsg": get("contMsg", "cMessage"),
            "medio": get("medio", "cSource"),
            "idm": get("idm", "cSourceId"),
            "fechaContacto": get("fechaContacto", "cDate"),
            "tipoContacto": get("tipoContacto", "cType"),
            "contIdEstado": get("contIdEstado", "cSchedStatus"),
            "tipo": get("tipo", "pType"),
            "comuna": get("comuna", "pBorough"),
            "region": get("region", None),
            "direccionRef": get("direccionRef", "pRefDirection"),
            "captador": captador,
            "sucursal": get("sucursal", "aOffice"),
            "venta": get("venta", "pSell"),
            "arriendo": get("arriendo", "pRent"),
            "pv": get("pv", "pSellPrice"),
            "pa": get("pa", "pRentPrice"),
            "pat": get("pat", "pTRentPrice"),
            "estadov": get("estadov", "pSellStatus"),
            "estadoa": get("estadoa", "pRentStatus"),
            "imgPropiedad": get("imgPropiedad", "pImage"),
        }

    def fetch_leads(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        page_size: int = 200,
    ) -> List[Dict[str, Any]]:
        if not self.session_active:
            raise Prop360AuthError("Sesión no activa. Ejecutar login() primero.")

        today = datetime.now()
        from_date = from_date or (today - timedelta(days=7)).strftime("%d/%m/%Y")
        to_date = to_date or today.strftime("%d/%m/%Y")
        from_dm = self._to_dm(from_date)
        to_dm = self._to_dm(to_date)

        list_options = {
            "page": 1,
            "pagesize": page_size,
            "order": "1",
            "orderdirection": "DESC",
            "totalrecords": 0,
            "listingtype": 1,
        }

        def _post(page: int) -> Dict[str, Any]:
            list_options["page"] = page
            payload = {
                "Filters": {"date": {"from": from_dm, "to": to_dm}},
                "Action": "Leads_GetListing",
                "ListOptions": list_options,
            }
            resp = self.client.post(
                PROP360_LEADS_ASHX,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code != 200:
                raise Prop360AuthError(f"Error consultando leads: HTTP {resp.status_code}")
            try:
                return resp.json()
            except json.JSONDecodeError:
                raise Prop360AuthError(f"Respuesta no JSON en leads: {resp.text[:200]}")

        logger.info(f"Consultando leads desde {from_dm} hasta {to_dm}...")
        data = _post(1)
        all_leads = data.get("listing", data.get("listado", []))
        total = data.get("totalRecords", data.get("registrosTotales", 0))
        logger.info(f"Recibidos {len(all_leads)}/{total} leads en página 1")

        page = 2
        while len(all_leads) < total:
            data = _post(page)
            page_leads = data.get("listing", data.get("listado", []))
            if not page_leads:
                break
            all_leads.extend(page_leads)
            logger.info(f"Recibidos {len(page_leads)} leads en página {page}")
            page += 1

        logger.info(f"Total leads recuperados: {len(all_leads)}")
        return all_leads

    def _map_contact_type(self, tipo_contacto: str) -> str:
        mapping = {
            "whatsapp": "whatsapp",
            "call": "phone",
            "question": "form",
        }
        return mapping.get(tipo_contacto, "form")

    def _is_duplicate(self, prop360_lead: Dict[str, Any]) -> bool:
        """Verifica si ya existe un lead con el MISMO cliente Y la MISMA propiedad.

        Regla de deduplicación:
          - Busca por teléfono normalizado; si no hay teléfono, por email.
          - Se considera duplicado SOLO si ese cliente ya tiene registrada ESA
            propiedad (en prospecto.codigo o prospecto.propiedades_vistas).
          - El mismo cliente con OTRA propiedad NO es duplicado: se permite,
            porque un cliente puede interesarse en varias propiedades.

        El lead entrante debe estar normalizado (pasar por normalize_lead()).
        """
        db = get_db()
        phone_raw = str(prop360_lead.get("contFono") or "").strip()
        email = str(prop360_lead.get("contMail") or "").strip().lower()
        property_code = str(prop360_lead.get("codigo") or "").strip()

        phone = normalize_phone_strict(phone_raw) if phone_raw else None
        existing = None
        match_by = None

        if phone:
            existing = db[COLLECTION_CONVERSATIONS].find_one({"phone": phone})
            match_by = "phone"
        if not existing and email and "@" in email:
            existing = db[COLLECTION_CONVERSATIONS].find_one({"prospecto.email": email})
            match_by = "email"

        if not existing:
            return False

        if not property_code:
            logger.info(
                f"[DUP] Mismo cliente ya existe (por {match_by}), sin código de "
                f"propiedad: idContacto={prop360_lead.get('idContacto')}"
            )
            return True

        props = set(existing.get("prospecto", {}).get("propiedades_vistas") or [])
        current_codigo = str(existing.get("prospecto", {}).get("codigo") or "").strip()
        if property_code == current_codigo or property_code in props:
            logger.info(
                f"[DUP] Cliente+propiedad ya existe (por {match_by}): "
                f"idContacto={prop360_lead.get('idContacto')} propiedad={property_code}"
            )
            return True

        logger.info(
            f"[OK] Cliente ya existe pero con OTRA propiedad (por {match_by}): "
            f"idContacto={prop360_lead.get('idContacto')} propiedad={property_code}"
        )
        return False

    def _enqueue_notification(
        self,
        lead_id: str,
        exec_name: str,
        event: LeadEvent,
        prop360_lead: Dict[str, Any],
    ) -> None:
        """Replica el flujo canónico de manual/WhatsApp para un lead recién ingestado.

        `ingest_lead_event()` crea/actualiza el lead y asigna ejecutivo, pero NO
        crea el ciclo de asignación ni encola la notificación al ejecutivo.  Ese
        paso lo hacen `manual_entry.create_manual_lead()` (reason
        `manual_lead_created`) y `commercial_intake.process_inbound()` (reason
        `inbound_message`).

        Aquí se crea el ciclo (reason `lead_created`), se marca la fuente como
        verificada, y se encola:
          - LEAD HOT  -> assign_and_enqueue_hot()
          - LEAD (no-HOT) -> accumulate_non_hot_lead() (digest)
        """
        from bson.objectid import ObjectId
        from chatbot.crm_metrics import create_assignment_cycle, coerce_utc_datetime

        db = get_db()
        try:
            fresh_lead = db[COLLECTION_CONVERSATIONS].find_one({"_id": ObjectId(lead_id)})
        except Exception:
            fresh_lead = None
        if not fresh_lead:
            logger.warning(
                "[NOTIF] Lead no encontrado tras ingest: lead_id=%s", lead_id
            )
            return

        exec_user = None
        if exec_name:
            exec_user = db["usuarios"].find_one({"nombre": exec_name}, {"_id": 1})
        assigned_to_user_id = str(exec_user["_id"]) if exec_user else exec_name or ""
        if not assigned_to_user_id:
            logger.warning(
                "[NOTIF] Ejecutivo no resuelto, sin notificación: lead_id=%s exec=%s",
                lead_id, exec_name,
            )
            return

        assigned_at = coerce_utc_datetime(
            (fresh_lead.get("lifecycle") or {}).get("assigned_at")
        ) or coerce_utc_datetime(fresh_lead.get("created_at"))

        cycle = create_assignment_cycle(
            db, lead=fresh_lead, assigned_to_user_id=assigned_to_user_id,
            assigned_by="prop360_extractor", reason="lead_created",
            assigned_at=assigned_at, assigned_to_display_name=exec_name,
        )
        cycle_id = cycle.get("assignment_cycle_id")
        source_event_id = str(prop360_lead.get("idContacto") or event.source_event_id)
        db["crm_assignment_cycles"].update_one(
            {"assignment_cycle_id": cycle_id},
            {"$set": {
                "source_event_id": source_event_id,
                "source_event_verified": True,
                "source_event_type": "PROP360_LEAD",
            }},
        )
        db[COLLECTION_CONVERSATIONS].update_one(
            {"_id": fresh_lead["_id"]},
            {"$set": {"lifecycle.current_assignment_cycle_id": cycle_id}},
        )

        temperature = str(fresh_lead.get("lead_temperature_effective") or "").upper()
        if temperature == "HOT":
            from chatbot.crm_hot_delivery import assign_and_enqueue_hot
            from chatbot.lead_router import get_executive_phone
            prospect = fresh_lead.get("prospecto", {}) or {}
            exec_phone = get_executive_phone(exec_name) or ""
            payload = {
                "phone": fresh_lead.get("phone"),
                "lead_phone": fresh_lead.get("phone"),
                "property_code": prospect.get("codigo") or event.property_code,
                "nombre": prospect.get("nombre") or event.name or "Cliente",
                "comuna": prospect.get("comuna"),
                "operacion": prospect.get("operacion"),
                "last_message": event.message or "",
                "lead_type": "LeadHotWhatsapp",
                "hot_reason": "Lead clasificado HOT desde Prop360",
            }
            result = assign_and_enqueue_hot(
                db, lead=fresh_lead,
                recipient_user_id=assigned_to_user_id,
                recipient_phone=exec_phone,
                payload=payload,
                assigned_by="prop360_extractor",
                reason="lead_created",
                assigned_at=assigned_at,
                recipient_name=exec_name,
                source_event_id=source_event_id,
            )
            self.metrics["notifications_enqueued"] += 1
            logger.info(
                "[NOTIF] Hot encolada lead_id=%s exec=%s cycle=%s notif=%s",
                lead_id, exec_name, cycle_id,
                result.get("notification", {}).get("_id") if result else None,
            )
        else:
            from chatbot.crm_non_hot_digest import accumulate_non_hot_lead
            notification = accumulate_non_hot_lead(db, lead=fresh_lead, cycle=cycle)
            if notification:
                self.metrics["notifications_enqueued"] += 1
                logger.info(
                    "[NOTIF] Digest encolada lead_id=%s exec=%s cycle=%s notif=%s send_after=%s",
                    lead_id, exec_name, cycle_id, notification.get("_id"),
                    notification.get("send_after"),
                )
            else:
                logger.info(
                    "[NOTIF] Lead no acumulado en digest (filtro) lead_id=%s exec=%s",
                    lead_id, exec_name,
                )

    def process_lead(self, prop360_lead: Dict[str, Any]) -> None:
        prop360_lead = self.normalize_lead(prop360_lead)
        self.metrics["total_received"] += 1
        id_contacto = prop360_lead.get("idContacto")
        self.metrics["last_id_contacto"] = id_contacto

        message = str(prop360_lead.get("contMsg") or "").strip()
        contact_type = self._map_contact_type(prop360_lead.get("tipoContacto", ""))
        if not message:
            message = f"Contacto vía {prop360_lead.get('medio', 'Portal')} ({contact_type})"

        event = LeadEvent(
            source_system="prop360",
            source_event_id=str(id_contacto),
            phone=str(prop360_lead.get("contFono") or "").strip() or None,
            email=str(prop360_lead.get("contMail") or "").strip() or None,
            name=str(prop360_lead.get("contNombre") or "").strip() or None,
            message=message,
            property_code=str(prop360_lead.get("codigo") or ""),
            portal_source=str(prop360_lead.get("medio") or "Portal Inmobiliario"),
            contact_date=str(prop360_lead.get("fechaContacto") or ""),
            metadata={
                "idm": prop360_lead.get("idm"),
                "tipoContacto": prop360_lead.get("tipoContacto"),
                "contIdEstado": prop360_lead.get("contIdEstado"),
                "tipo": prop360_lead.get("tipo"),
                "comuna": prop360_lead.get("comuna"),
                "region": prop360_lead.get("region"),
                "direccionRef": prop360_lead.get("direccionRef"),
                "captador": prop360_lead.get("captador"),
                "sucursal": prop360_lead.get("sucursal"),
                "venta": prop360_lead.get("venta"),
                "arriendo": prop360_lead.get("arriendo"),
                "pv": prop360_lead.get("pv"),
                "pa": prop360_lead.get("pa"),
                "vdiv": prop360_lead.get("vdiv"),
                "adiv": prop360_lead.get("adiv"),
            },
        )

        if self._is_duplicate(prop360_lead):
            self.metrics["duplicates_skipped"] += 1
            logger.info(
                f"[DUP] Lead omitido (mismo cliente + misma propiedad): idContacto={id_contacto} "
                f"phone={'[REDACTED]' if event.phone else 'N/A'} "
                f"property_code={event.property_code}"
            )
            return

        if self.dry_run:
            logger.info(
                f"[DRY-RUN] Procesaría: idContacto={id_contacto} "
                f"phone={'[REDACTED]' if event.phone else 'N/A'} "
                f"email={'[REDACTED]' if event.email else 'N/A'} "
                f"property_code={event.property_code} "
                f"portal={event.portal_source} "
                f"action={'crear' if '<existe?>' else 'actualizar'}"
            )
            return

        result = ingest_lead_event(event)

        if result.status == "created":
            self.metrics["leads_created"] += 1
            self.metrics["properties_found"] += 1 if result.property_found else 0
            self.metrics["properties_not_found"] += 0 if result.property_found else 1
            self._enqueue_notification(result.lead_id, result.executive, event, prop360_lead)
            logger.info(f"[OK] Lead creado: idContacto={id_contacto} lead_id={result.lead_id} exec={result.executive}")

        elif result.status == "updated":
            self.metrics["leads_updated"] += 1
            self.metrics["properties_found"] += 1 if result.property_found else 0
            self.metrics["properties_not_found"] += 0 if result.property_found else 1
            if result.assignment_changed:
                self._enqueue_notification(result.lead_id, result.executive, event, prop360_lead)
            logger.info(f"[OK] Lead actualizado: idContacto={id_contacto} lead_id={result.lead_id} action={result.action}")

        elif result.status == "duplicate":
            self.metrics["events_duplicate"] += 1
            logger.info(f"[DUP] Evento duplicado: idContacto={id_contacto} lead_id={result.lead_id}")

        elif result.status == "conflict":
            self.metrics["identity_conflicts"] += 1
            logger.warning(f"[CONFLICT] Conflicto identidad: idContacto={id_contacto} details={result.conflict_details}")

        elif result.status == "error":
            self.metrics["errors"] += 1
            logger.error(f"[ERROR] idContacto={id_contacto}: {result.error}")

    def run(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        max_leads: Optional[int] = None,
    ) -> Dict[str, Any]:
        self.metrics["started_at"] = datetime.now().isoformat()
        logger.info("=" * 60)
        logger.info("EXTRACTOR PROP360 INICIADO")
        logger.info(f"Dry-run: {self.dry_run}")
        logger.info("=" * 60)

        try:
            self.login()
            leads = self.fetch_leads(from_date=from_date, to_date=to_date)
            if max_leads is not None and max_leads > 0:
                leads = leads[:max_leads]
                logger.info(f"Límite aplicado: procesando solo {len(leads)} leads")
            for lead in leads:
                self.process_lead(lead)
        except Prop360AuthError as e:
            logger.error(f"Error de autenticación: {e}")
            self.metrics["errors"] += 1
        except Exception as e:
            logger.error(f"Error general: {e}", exc_info=True)
            self.metrics["errors"] += 1

        self.metrics["finished_at"] = datetime.now().isoformat()
        self.metrics["duration_seconds"] = (
            datetime.fromisoformat(self.metrics["finished_at"]) -
            datetime.fromisoformat(self.metrics["started_at"])
        ).total_seconds()

        logger.info("=" * 60)
        logger.info("RESUMEN DE EXTRACCIÓN")
        logger.info(f"  Total recibidos:     {self.metrics['total_received']}")
        logger.info(f"  Eventos nuevos:      {self.metrics['events_new']}")
        logger.info(f"  Leads creados:       {self.metrics['leads_created']}")
        logger.info(f"  Leads actualizados:  {self.metrics['leads_updated']}")
        logger.info(f"  Eventos duplicados:  {self.metrics['events_duplicate']}")
        logger.info(f"  Props encontradas:   {self.metrics['properties_found']}")
        logger.info(f"  Props no encontradas:{self.metrics['properties_not_found']}")
        logger.info(f"  Conflictos identidad:{self.metrics['identity_conflicts']}")
        logger.info(f"  Duplicados omitidos: {self.metrics['duplicates_skipped']}")
        logger.info(f"  Notificaciones:      {self.metrics['notifications_enqueued']}")
        logger.info(f"  Errores:             {self.metrics['errors']}")
        logger.info(f"  Duración:            {self.metrics['duration_seconds']:.1f}s")
        logger.info(f"  Último idContacto:   {self.metrics['last_id_contacto']}")
        logger.info("=" * 60)

        return self.metrics


def save_extraction_state(metrics: Dict[str, Any]):
    try:
        db = get_db()
        state = {
            "extractor": "prop360",
            "last_run_at": metrics.get("started_at"),
            "last_id_contacto": metrics.get("last_id_contacto"),
            "last_success": metrics.get("errors", 0) == 0,
            "metrics": metrics,
        }
        db["extraction_state"].update_one(
            {"extractor": "prop360"},
            {"$set": state},
            upsert=True,
        )
        logger.info(f"Estado de extracción guardado. Último idContacto: {metrics.get('last_id_contacto')}")
    except Exception as e:
        logger.warning(f"No se pudo guardar estado de extracción: {e}")


def get_last_extraction_state() -> Dict[str, Any]:
    try:
        db = get_db()
        state = db["extraction_state"].find_one({"extractor": "prop360"})
        if state:
            return {
                "last_id_contacto": state.get("last_id_contacto"),
                "last_run_at": state.get("last_run_at"),
                "last_success": state.get("last_success", False),
            }
    except Exception:
        pass
    return {"last_id_contacto": None, "last_run_at": None, "last_success": False}


def main():
    parser = argparse.ArgumentParser(description="Extractor de leads Prop360")
    parser.add_argument("--dry-run", action="store_true", help="No escribir en MongoDB")
    parser.add_argument("--from-date", help="Fecha inicio (YYYY-MM-DD). Default: últimos 7 días")
    parser.add_argument("--to-date", help="Fecha fin (YYYY-MM-DD). Default: hoy")
    parser.add_argument("--days-back", type=int, default=7, help="Días hacia atrás (default: 7)")
    parser.add_argument("--all", action="store_true", help="Extraer TODOS los leads (desde 2000-01-01)")
    parser.add_argument("--max-leads", type=int, default=None, help="Limitar el número de leads a procesar")
    args = parser.parse_args()

    email = os.getenv("PROP360_EMAIL")
    password = os.getenv("PROP360_PASSWORD")

    if not email or not password:
        logger.error("PROP360_EMAIL y PROP360_PASSWORD deben estar definidos")
        sys.exit(1)

    last_state = get_last_extraction_state()

    if args.all:
        from_date = "2000-01-01T00:00:00"
    elif args.from_date:
        from_date = f"{args.from_date}T00:00:00"
    else:
        from_date = (datetime.now() - timedelta(days=args.days_back)).strftime("%Y-%m-%dT00:00:00")

    to_date = f"{args.to_date}T23:59:59" if args.to_date else datetime.now().strftime("%Y-%m-%dT23:59:59")

    logger.info(f"Rango: {from_date} → {to_date}")
    logger.info(f"Última extracción: {last_state}")

    extractor = Prop360Extractor(email=email, password=password, dry_run=args.dry_run)
    metrics = extractor.run(from_date=from_date, to_date=to_date, max_leads=args.max_leads)

    if not args.dry_run:
        save_extraction_state(metrics)

    if metrics["errors"] > 0:
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
