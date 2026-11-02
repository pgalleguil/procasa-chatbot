<#
  Lanzador local Windows para el barrido Yapo Maip�.
  No cambia .env, Git global, producci�n, Mongo ni configuraci�n persistente.
  Solo usa los proxies ya cargados en el .env/entorno local.
#>
[CmdletBinding()]
param(
    [string]$ExistingRepo = '',
    [string]$EnvFile = '',
    [string]$OutputDir = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepoUrl = 'https://github.com/pgalleguil/procasa-chatbot.git'
$Branch = 'codex/yapo-scraper-context-20261102'
$ExpectedCommit = 'ce3b54bf3db1120a6ef9b87f437c700c389759a2'
$StartUrl = 'https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/region-metropolitana-maipu'
$RunRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('procasa-yapo-maipu-' + [guid]::NewGuid().ToString('N').Substring(0, 12))
$Clone = Join-Path $RunRoot 'procasa-chatbot'
$Venv = Join-Path $RunRoot '.venv'
$PythonScript = Join-Path $RunRoot 'run_yapo_maipu.py'
$DiagnosticDir = Join-Path $RunRoot 'proxy-diagnostic'
$CsvPath = ''

$ProcessVariables = @(
    'ENV_FILE', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy',
    'NO_PROXY', 'no_proxy', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_0', 'GIT_CONFIG_VALUE_0',
    'PROXY_URL', 'PROXY_URLS', 'YAPO_PROXY_URLS', 'PROXIES', 'PROXY_USER', 'PROXY_PASS',
    'DEEPSEEK_ENABLED', 'MONGO_URI'
)
$OriginalEnvironment = @{}
foreach ($name in $ProcessVariables) {
    $OriginalEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

function Stop-Run([string]$Message) {
    throw $Message
}

try {
    if (-not $ExistingRepo) {
        $ExistingRepo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
    }
    if (-not (Test-Path -LiteralPath $ExistingRepo -PathType Container)) {
        Stop-Run 'No se encuentra el repositorio local indicado.'
    }
    $ExistingRepo = (Resolve-Path -LiteralPath $ExistingRepo).Path

    if (-not $EnvFile) {
        foreach ($candidate in @(
            (Join-Path $ExistingRepo 'scrapers\scraper_yapo\.env'),
            (Join-Path $ExistingRepo 'scrapers\.env'),
            (Join-Path $ExistingRepo '.env')
        )) {
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                $EnvFile = $candidate
                break
            }
        }
    }
    if (-not $EnvFile) {
        Stop-Run 'No encontr� el .env local. Usa -EnvFile con la ruta existente; no vuelvas a escribir credenciales en pantalla.'
    }
    $EnvFile = (Resolve-Path -LiteralPath $EnvFile -ErrorAction Stop).Path

    $Git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $Git) { Stop-Run 'Git no est� instalado o no est� en PATH.' }
    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        $SystemPython = 'py'
        $SystemPythonFlags = @('-3')
    } else {
        $Python = Get-Command python -ErrorAction SilentlyContinue
        if (-not $Python) { Stop-Run 'Python 3 no est� instalado o no est� en PATH.' }
        $SystemPython = 'python'
        $SystemPythonFlags = @()
    }

    New-Item -ItemType Directory -Path $RunRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $DiagnosticDir -Force | Out-Null
    $env:ENV_FILE = $EnvFile
    $env:PYTHONPATH = $ExistingRepo

    Write-Host '1/5 Revisando el proxy contratado desde este Windows (sin contactar Yapo)...'
    $ProbeFile = Join-Path $RunRoot 'proxy_preflight.py'
    @'
import base64, json, re, sys
from pathlib import Path
from urllib.parse import urlparse

repo = Path(sys.argv[1]).resolve()
report_dir = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(repo))

try:
    from scrapers.scraper_yapo.config import AppConfig
    from scrapers.scraper_yapo.proxy_manager import ProxyManager
    from scrapers.scraper_yapo.diagnose_proxy import diagnose_proxy

    config = AppConfig()
    manager = ProxyManager.from_config(config)
    diagnostic = diagnose_proxy(config, report_dir=report_dir, timeout_seconds=5.0)
    proxies = list(manager._proxies)
    results = diagnostic.get('proxies') or []

    def provider_domain(url):
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower()
        if not host or ':' in host or re.fullmatch(r'[0-9.]+', host):
            return 'no identificable por dominio'
        if host == 'flashproxy.io' or host.endswith('.flashproxy.io'):
            return 'FlashProxy (flashproxy.io)'
        labels = host.split('.')
        return '.'.join(labels[-2:]) if len(labels) >= 2 else 'dominio no identificable'

    tested = []
    selected = None
    for index, result in enumerate(results):
        proxy = proxies[index] if index < len(proxies) else None
        tested.append({
            'index': result.get('proxy_index', index + 1),
            'scheme': result.get('scheme'),
            'syntax_valid': result.get('syntax_valid'),
            'dns': result.get('dns'),
            'tcp': result.get('tcp'),
            'authentication': result.get('authentication'),
            'http_category': result.get('http_category'),
            'error_category': result.get('error_category'),
        })
        if (proxy and result.get('syntax_valid') and result.get('dns') == 'resolved'
                and result.get('tcp') == 'connected'
                and result.get('auth_present')
                and result.get('authentication') == 'accepted'
                and result.get('http_category') == 'OK'):
            selected = proxy
            break

    summary = {
        'general_dns': diagnostic.get('general_dns_control', {}).get('status'),
        'general_dns_error': diagnostic.get('general_dns_control', {}).get('error_category'),
        'proxy_count': diagnostic.get('valid_proxy_count', 0),
        'provider': provider_domain(selected or (proxies[0] if proxies else '')),
        'result_category': diagnostic.get('result_category'),
        'proxies': tested,
        'diagnostic_report': diagnostic.get('report_file'),
        'credentials_logged': False,
    }
    print('PROBE_RESULT=' + json.dumps(summary, ensure_ascii=False))
    if selected:
        print('SELECTED_PROXY_B64=' + base64.b64encode(selected.encode('utf-8')).decode('ascii'))
        sys.exit(0)
    sys.exit(21)
except SystemExit:
    raise
except Exception as exc:
    # Do not emit exception text: HTTP libraries may include authenticated URLs.
    print('PROBE_FAILURE=' + type(exc).__name__)
    sys.exit(22)
'@ | Set-Content -LiteralPath $ProbeFile -Encoding UTF8

    $ProbeOutput = @(& $SystemPython @SystemPythonFlags $ProbeFile $ExistingRepo $DiagnosticDir 2>&1)
    $ProbeExit = $LASTEXITCODE
    $ProbeLine = $ProbeOutput | ForEach-Object { [string]$_ } | Where-Object { $_.StartsWith('PROBE_RESULT=') } | Select-Object -Last 1
    if ($ProbeLine) {
        $Probe = $ProbeLine.Substring('PROBE_RESULT='.Length) | ConvertFrom-Json
        Write-Host ('Proveedor identificado: ' + $Probe.provider)
        Write-Host ('DNS general: ' + $Probe.general_dns + '; resultado: ' + $Probe.result_category)
        foreach ($item in $Probe.proxies) {
            Write-Host ('Proxy ' + $item.index + ': sintaxis=' + $item.syntax_valid +
                ', DNS=' + $item.dns + ', TCP=' + $item.tcp +
                ', autenticaci�n=' + $item.authentication + ', HTTP=' + $item.http_category)
        }
        if ($Probe.diagnostic_report) {
            Write-Host ('Diagn�stico sanitizado: ' + (Join-Path $DiagnosticDir $Probe.diagnostic_report))
        }
    } else {
        Stop-Run 'No fue posible completar el preflight local. El detalle bruto se omiti� para proteger las credenciales.'
    }
    if ($ProbeExit -ne 0) {
        Stop-Run 'Ning�n proxy pas� DNS, TCP y autenticaci�n HTTP. No se intentar� GitHub ni Yapo por conexi�n directa.'
    }
    $SelectedLine = $ProbeOutput | ForEach-Object { [string]$_ } | Where-Object { $_.StartsWith('SELECTED_PROXY_B64=') } | Select-Object -Last 1
    if (-not $SelectedLine) { Stop-Run 'El preflight no entreg� un proxy validado.' }
    $SelectedProxy = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($SelectedLine.Substring('SELECTED_PROXY_B64='.Length)))

    # El proxy autenticado vive solo en el proceso actual. Git y pip quedan
    # obligados a usarlo; no se altera la configuraci�n global de Git.
    $env:HTTP_PROXY = $SelectedProxy
    $env:HTTPS_PROXY = $SelectedProxy
    $env:ALL_PROXY = $SelectedProxy
    $env:http_proxy = $SelectedProxy
    $env:https_proxy = $SelectedProxy
    $env:all_proxy = $SelectedProxy
    $env:NO_PROXY = ''
    $env:no_proxy = ''
    $env:GIT_CONFIG_COUNT = '1'
    $env:GIT_CONFIG_KEY_0 = 'http.proxy'
    $env:GIT_CONFIG_VALUE_0 = $SelectedProxy

    Write-Host '2/5 Descargando en una carpeta temporal independiente el commit autorizado...'
    $CloneOutput = @(& git clone --depth 1 --single-branch --branch $Branch $RepoUrl $Clone 2>&1)
    if ($LASTEXITCODE -ne 0) { Stop-Run 'No se pudo descargar la rama por el proxy validado. No se ha usado conexi�n directa.' }
    $Commit = (& git -C $Clone rev-parse HEAD 2>$null).Trim()
    if ($LASTEXITCODE -ne 0 -or $Commit -ne $ExpectedCommit) {
        Stop-Run ('El checkout no coincide con el commit autorizado. Esperado: ' + $ExpectedCommit + '; obtenido: ' + $Commit)
    }
    Write-Host ('Commit local verificado: ' + $Commit)

    Write-Host '3/5 Preparando Python aislado; dependencias por el proxy validado si hicieran falta...'
    $VenvOutput = @(& $SystemPython @SystemPythonFlags -m venv --system-site-packages $Venv 2>&1)
    if ($LASTEXITCODE -ne 0) { Stop-Run 'No se pudo crear el entorno Python temporal.' }
    $VenvPython = Join-Path $Venv 'Scripts\python.exe'
    $env:PYTHONPATH = $Clone
    $env:DEEPSEEK_ENABLED = 'false'
    $env:PROXY_URL = $SelectedProxy
    $env:PROXY_URLS = ''
    $env:YAPO_PROXY_URLS = ''
    $env:PROXIES = ''
    $env:PROXY_USER = ''
    $env:PROXY_PASS = ''

    $PipOutput = @(& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $Clone 'scrapers\scraper_yapo\requirements.txt') 2>&1)
    if ($LASTEXITCODE -ne 0) { Stop-Run 'No se pudieron verificar/instalar las dependencias por el proxy. No se inici� Yapo.' }

    Write-Host '4/5 Comprobando MongoDB de solo lectura y ejecutando la paginaci�n completa...'
    @'
import csv, json, os, re, sys
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
output_dir = Path(sys.argv[2]).resolve()
start_url = sys.argv[3]
sys.path.insert(0, str(repo))

from scrapers.scraper_yapo.config import AppConfig
from scrapers.scraper_yapo.proxy_manager import ProxyManager
from scrapers.scraper_yapo.mongo_store import MongoStore
from scrapers.scraper_yapo.pilot import run_pilot

config = AppConfig()
config.proxy_url = os.environ.get('PROXY_URL', '')
config.proxy_urls = []
config.deepseek_enabled = False
if config.deepseek_enabled:
    raise RuntimeError('DeepSeek no est� permitido para este lanzador.')
if not ProxyManager.from_config(config).has_proxies():
    raise RuntimeError('No qued� disponible el proxy validado; abortando sin acceso directo.')

readonly_uri = os.getenv('MONGO_READONLY_URI', '').strip() or config.mongo_uri
if not readonly_uri:
    raise RuntimeError('MongoDB no est� configurado; la deduplicaci�n no se puede verificar.')
config.mongo_uri = readonly_uri
os.environ['MONGO_URI'] = readonly_uri

from pymongo import MongoClient
client = MongoClient(readonly_uri, serverSelectionTimeoutMS=10000, connectTimeoutMS=10000)
try:
    status = client.admin.command('connectionStatus', showPrivileges=True)
    auth = status.get('authInfo') or {}
    roles = auth.get('authenticatedUserRoles') or []
    privileges = auth.get('authenticatedUserPrivileges') or []
    if not roles or not privileges:
        raise RuntimeError('MongoDB no permiti� verificar los privilegios de solo lectura.')
    write_role_names = {
        'readwrite', 'readwriteanydatabase', 'dbowner', 'dbadmin',
        'dbadminanydatabase', 'root', 'useradmin', 'useradminanydatabase',
    }
    if any(str(role.get('role', '')).lower() in write_role_names for role in roles):
        raise RuntimeError('La identidad MongoDB tiene un rol con permisos de escritura.')
    write_actions = {
        '*', 'anyaction', 'insert', 'update', 'remove', 'createcollection',
        'dropcollection', 'createindex', 'dropindex', 'collmod', 'dropdatabase',
        'renamecollection', 'createuser', 'dropuser',
        'grantrole', 'revokerole', 'grantrolestouser', 'revokerolesfromuser',
    }
    for privilege in privileges:
        actions = {str(action).lower() for action in privilege.get('actions', [])}
        write_prefixes = ('insert', 'update', 'remove', 'delete', 'create', 'drop',
                          'rename', 'collmod', 'grant', 'revoke', 'shard', 'movechunk')
        if actions & write_actions or any(action.startswith(write_prefixes) for action in actions):
            raise RuntimeError('La identidad MongoDB no es de solo lectura.')
finally:
    client.close()

class ReadOnlyMongoGuard(MongoStore):
    def upsert_listing(self, record):
        raise RuntimeError('Bloqueo local: se proh�ben escrituras MongoDB durante este piloto.')
    def write_many(self, records):
        raise RuntimeError('Bloqueo local: se proh�ben escrituras MongoDB durante este piloto.')

# run_pilot imports MongoStore in its own module; patch those write methods too.
import scrapers.scraper_yapo.pilot as pilot_module
pilot_module.MongoStore.upsert_listing = ReadOnlyMongoGuard.upsert_listing
pilot_module.MongoStore.write_many = ReadOnlyMongoGuard.write_many

summary = run_pilot(start_url, config)
if not summary.get('pagination_complete'):
    print('PILOT_INCOMPLETE=' + json.dumps({
        'pages_visited': summary.get('pages_visited'),
        'pagination_cause': summary.get('pagination_cause'),
        'page_log': summary.get('page_log'),
        'errors_or_blocks': summary.get('errors_or_blocks'),
    }, ensure_ascii=False))
    sys.exit(31)
if not summary.get('mongo_dedup_verified') and summary.get('ads_discovered') != 0:
    print('PILOT_INCOMPLETE=' + json.dumps({
        'pages_visited': summary.get('pages_visited'),
        'pagination_complete': True,
        'mongo_dedup_status': summary.get('mongo_dedup_status'),
    }, ensure_ascii=False))
    sys.exit(32)
if not summary.get('search_filters_verified'):
    print('PILOT_INCOMPLETE=' + json.dumps({
        'pages_visited': summary.get('pages_visited'),
        'pagination_complete': True,
        'search_filters_verified': False,
    }, ensure_ascii=False))
    sys.exit(33)

audit_path = Path(summary['audit_report'])
audit = json.loads(audit_path.read_text(encoding='utf-8-sig'))
rows = []
for item in audit:
    commune = str(item.get('validacion_comuna') or 'pending')
    if commune == 'confirmed_other_commune':
        continue
    if item.get('operacion') != 'venta' or item.get('tipo_propiedad') != 'casa':
        continue
    price = item.get('precio_uf')
    price_status = 'pendiente' if price is None else ('cumple' if 3000 <= float(price) <= 6000 else 'fuera_de_rango')
    if price_status == 'fuera_de_rango':
        continue
    classification = item.get('clasificacion_preliminar') or {}
    context = classification.get('contextual_analysis') or {}
    owner_signals = (classification.get('owner_signal_candidates')
                     or classification.get('owner_signal_evidence')
                     or context.get('owner_signal_candidates') or [])
    state = classification.get('state') or 'INCIERTO'
    if state == 'CORREDOR_SEGURO':
        segment = 'corredor_seguro'
    elif state == 'CORREDOR_PROBABLE':
        segment = 'corredor_probable'
    elif owner_signals:
        segment = 'incierto_con_senales_de_propietario'
    elif context.get('trato_directo_with_commercial_contradictions'):
        segment = 'incierto_trato_directo_con_contradicciones'
    else:
        segment = 'incierto_revision_humana'
    rows.append({
        'id_yapo': item.get('listing_id', ''),
        'url': item.get('url', ''),
        'pagina': item.get('discovered_page', ''),
        'titulo': item.get('title', ''),
        'comuna_validacion': commune,
        'operacion': item.get('operacion', ''),
        'tipo_propiedad': item.get('tipo_propiedad', ''),
        'precio_original': item.get('precio_raw', ''),
        'precio_uf': '' if price is None else price,
        'validacion_precio': price_status,
        'clasificacion_preliminar': state,
        'segmento_revision': segment,
        'senales_propietario_candidatas': ' | '.join(map(str, owner_signals)),
        'trato_directo_candidato': bool(context.get('trato_directo_candidate')),
        'contradiccion_comercial': bool(
            context.get('direct_deal_with_commercial_contradictions')
            or context.get('trato_directo_with_commercial_contradictions')
            or classification.get('direct_deal_with_commercial_contradictions')
        ),
        'evidencia_clasificador': ' | '.join(map(str, classification.get('evidence') or [])),
    })

output_dir.mkdir(parents=True, exist_ok=True)
csv_path = output_dir / ('oportunidades_yapo_maipu_3000_6000_uf_' +
                         __import__('datetime').datetime.now().strftime('%Y%m%d_%H%M%S') + '.csv')
fields = [
    'id_yapo', 'url', 'pagina', 'titulo', 'comuna_validacion', 'operacion',
    'tipo_propiedad', 'precio_original', 'precio_uf', 'validacion_precio',
    'clasificacion_preliminar', 'segmento_revision', 'senales_propietario_candidatas',
    'trato_directo_candidato', 'contradiccion_comercial', 'evidencia_clasificador',
]
with csv_path.open('w', encoding='utf-8-sig', newline='') as handle:
    writer = csv.DictWriter(handle, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)

print('PILOT_COMPLETE=' + json.dumps({
    'pages_visited': summary.get('pages_visited'),
    'pages_complete': summary.get('pages_complete'),
    'pagination_complete': summary.get('pagination_complete'),
    'ads_discovered': summary.get('ads_discovered'),
    'batch_duplicates': summary.get('batch_duplicates'),
    'mongo_existing': summary.get('mongo_existing'),
    'new_ads': summary.get('new_ads'),
    'filter_matches': summary.get('filter_matches'),
    'preliminary_classification_counts': summary.get('preliminary_classification_counts'),
    'mongo_dedup_status': summary.get('mongo_dedup_status'),
    'deepseek_calls': 0,
    'mongo_writes': 0,
    'csv_path': str(csv_path),
    'summary_report': summary.get('summary_report'),
    'audit_report': summary.get('audit_report'),
}, ensure_ascii=False))
'@ | Set-Content -LiteralPath $PythonScript -Encoding UTF8

    $env:PYTHONPATH = $Clone
    $env:PROXY_URL = $SelectedProxy
    $env:PROXY_URLS = ''
    $env:YAPO_PROXY_URLS = ''
    $env:PROXIES = ''
    $env:PROXY_USER = ''
    $env:PROXY_PASS = ''
    $env:DEEPSEEK_ENABLED = 'false'
    if (-not $OutputDir) {
        $OutputDir = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'PROCASA\YapoMaipu'
    }
    $OutputDir = [System.IO.Path]::GetFullPath($OutputDir)

    $PilotOutput = @(& $VenvPython $PythonScript $Clone $OutputDir $StartUrl 2>&1)
    $PilotExit = $LASTEXITCODE
    $ResultLine = $PilotOutput | ForEach-Object { [string]$_ } | Where-Object {
        $_.StartsWith('PILOT_COMPLETE=') -or $_.StartsWith('PILOT_INCOMPLETE=')
    } | Select-Object -Last 1
    if (-not $ResultLine) {
        Stop-Run 'El proceso local termin� sin un resultado verificable. Se omiti� la salida bruta para proteger datos y credenciales.'
    }
    $ResultJson = $ResultLine.Substring($ResultLine.IndexOf('=') + 1) | ConvertFrom-Json
    if ($PilotExit -ne 0 -or $ResultLine.StartsWith('PILOT_INCOMPLETE=')) {
        $IncompleteCause = ''
        if ($ResultJson.PSObject.Properties.Name -contains 'pagination_cause') {
            $IncompleteCause = $ResultJson.pagination_cause
        }
        if (-not $IncompleteCause -and $ResultJson.PSObject.Properties.Name -contains 'mongo_dedup_status') {
            $IncompleteCause = $ResultJson.mongo_dedup_status
        }
        if (-not $IncompleteCause) { $IncompleteCause = 'sin verificar' }
        Write-Host ('Barrido incompleto. P�ginas visitadas: ' + $ResultJson.pages_visited +
            '; causa: ' + $IncompleteCause) -ForegroundColor Yellow
        Write-Host 'No se gener� un CSV de oportunidades porque la cobertura o la deduplicaci�n no qued� verificada.'
        Stop-Run 'Revisa el reporte de resumen/auditor�a antes de reanudar; no se declar� el fin de paginaci�n.'
    }

    Write-Host '5/5 Barrido completo y deduplicaci�n MongoDB verificada.' -ForegroundColor Green
    Write-Host ('P�ginas recorridas: ' + $ResultJson.pages_visited + '; avisos descubiertos: ' + $ResultJson.ads_discovered)
    Write-Host ('Duplicados del barrido: ' + $ResultJson.batch_duplicates + '; ya existentes en MongoDB: ' + $ResultJson.mongo_existing)
    Write-Host ('Anuncios nuevos: ' + $ResultJson.new_ads + '; cumplen rango/localidad/precio: ' + $ResultJson.filter_matches)
    Write-Host ('DeepSeek: 0 llamadas; MongoDB: 0 escrituras.')
    Write-Host ('CSV local: ' + $ResultJson.csv_path)
    Write-Host ('Resumen: ' + $ResultJson.summary_report)
    Write-Host ('Auditor�a privada local: ' + $ResultJson.audit_report)
    Write-Host ('Carpeta temporal de ejecuci�n: ' + $RunRoot)
}
catch {
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host ('Los archivos temporales y reportes privados, si se generaron, est�n en: ' + $RunRoot)
    exit 1
}
finally {
    foreach ($name in $ProcessVariables) {
        [Environment]::SetEnvironmentVariable($name, $OriginalEnvironment[$name], 'Process')
    }
}

