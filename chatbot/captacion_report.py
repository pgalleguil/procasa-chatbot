"""Compatibilidad del scheduler de Captaciones.

Las cifras semanales se construyen exclusivamente desde el backend compartido
de /captacion. El antiguo recálculo sobre crm_events y notas fue retirado.
"""

from .captacion_weekly_report import check_and_prepare_weekly_report


async def check_and_run_meta_diaria_report(force: bool = False):
    """Nombre heredado; ejecuta el semanal oficial dentro de su ventana."""
    return await check_and_prepare_weekly_report(force=force)


async def send_meta_diaria_report(*args, **kwargs):
    raise RuntimeError(
        "El envío oficial solo puede iniciarse mediante el scheduler semanal idempotente."
    )
