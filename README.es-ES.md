<h1 align="center">caw: Embalaje de Agente de Codificación</h1>
<p align="center">
  <a href="https://pypi.org/project/coding-agent-wrapper/"><img src="https://img.shields.io/pypi/v/coding-agent-wrapper.svg" alt="Versión de PyPI"></a>
  <a href="https://pypi.org/project/coding-agent-wrapper/"><img src="https://img.shields.io/pypi/pyversions/coding-agent-wrapper.svg" alt="Versiones de Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="Licencia: Apache-2.0"></a>
  <a href="https://github.com/zzjas/caw/actions/workflows/lint.yml"><img src="https://img.shields.io/github/actions/workflow/status/zzjas/caw/lint.yml?label=lint" alt="Estado de lint"></a>
  <a href="https://zzjas.github.io/caw/"><img src="https://img.shields.io/badge/docs-mkdocs--material-blue.svg" alt="Documentación"></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff"></a>
</p>

<p align="center">
  <a href="https://zzjas.github.io/caw/">Documentación</a> ·
  <a href="https://zzjas.github.io/caw/getting-started/quickstart/">Inicio rápido</a> ·
  <a href="examples/">Ejemplos</a>
</p>

---

**caw** (Embalaje de Agente de Codificación) envuelve múltiples interfaces de línea de comandos de agentes de codificación — [Claude Code](https://docs.claude.com/en/docs/claude-code), [Codex](https://github.com/openai/codex) y [opencode](https://github.com/sst/opencode) — detrás de una única API de `Agent` / `Session`. Cambia de proveedor sin modificar tu código, adjunta servidores de herramientas MCP, captura trayectorias estructuradas y gestiona credenciales para contenedores Docker. caw está diseñado para casos comunes con una API pequeña y fácil de usar. Si necesitas un control más detallado sobre el comportamiento del agente, utiliza los SDK subyacentes; caw no pretende reemplazarlos.

## Instalación

caw está disponible en PyPI como `coding-agent-wrapper`. La forma recomendada de instalarlo es mediante [uv](https://docs.astral.sh/uv/):

```bash
uv add coding-agent-wrapper            # Usar como biblioteca en un proyecto gestionado por uv
uv tool install coding-agent-wrapper   # Instalar solo la CLI (caw, caw-traj) globalmente
```

También puedes usarlo con `pip` directamente:

```bash
pip install coding-agent-wrapper
```

Requiere Python 3.10 o superior. Además, necesitas al menos un proveedor de CLI instalado y autenticado (`claude`, `codex` o `opencode`). Ejecuta `caw doctor` para ver qué puede encontrar caw.

Para desarrollo local:

```bash
uv sync --extra dev
```

## Inicio rápido

```python
from caw import Agent

agent = Agent()  # Usa Claude Code por defecto
traj = agent.completion("Explica qué hace este repositorio")

print(traj.result)
print(f"{traj.usage.total_tokens} tokens, ${traj.usage.cost_usd:.4f}")
```

### Sesión de múltiples turnos

```python
agent = Agent(model="claude-opus-4-7", reasoning="high")
agent.set_system_prompt("Eres un revisor de seguridad.")

with agent.start_session() as session:
    print(session.send("Revisa src/auth.py en busca de vulnerabilidades").result)
    print(session.send("Ahora revisa src/api.py").result)
# session.end() se ejecuta al salir y devuelve la Trayectoria completa
```

### Cambiar de proveedor sin modificar el código

La forma más común es usar la variable de entorno `CAW_PROVIDER`. Configúrala una vez y cada `Agent()` la utilizará:

```bash
export CAW_PROVIDER=codex
```

```python
from caw import Agent
agent = Agent()                        # Usa lo que CAW_PROVIDER indique
```

O proporciona a caw un orden de prioridad y déjalo elegir cuál está instalado y saludable en tiempo de ejecución:

```python
agent = Agent(provider=["claude", "codex", "opencode"])
traj = agent.completion("Responde con un hola de una línea.")
print(f"[{traj.agent}] {traj.result}")  # El proveedor que haya manejado la solicitud
```

### Proporcionar herramientas al agente

Decora una función Python con `@tool` y pásala. caw levantará un servidor de herramientas para ti:

```python
from caw import Agent, tool

@tool(description="Sumar dos números")
def add(a: int, b: int) -> int:
    return a + b

agent = Agent(stateless_tools=[add])
print(agent.completion("¿Cuál es 17 más 25? Usa la herramienta.").result)
```

Para herramientas con estado (estado compartido entre llamadas en una sesión), subclass `ToolKit`. Consulta la [guía](https://zzjas.github.io/caw/guides/toolkit/).

### Inspeccionar una trayectoria

Cada llamada devuelve una `Trajectory` estructurada. Guárdala y repásala después:

```python
with Agent(data_dir="caw_data").start_session(traj_path="run.json") as session:
    session.send("Lista los archivos Python aquí y cuéntalos.")
```

caw incluye dos visores:

```bash
caw-traj run.json          # Vista compacta e indexada por pasos en terminal
caw viewer                 # Interfaz web, abre run.json en el navegador por ruta
```

## Qué puedes hacer

- **[Proveedores](https://zzjas.github.io/caw/guides/providers/)** — Una API única para Claude Code, Codex y opencode.
- **[Fallback automático de proveedor](https://zzjas.github.io/caw/guides/auto-provider/)** — Elige el primer proveedor instalado/saludable y cambia transparentemente.
- **[Modelos y niveles](https://zzjas.github.io/caw/guides/models-and-tiers/)** — Selección portátil de `ModelTier` en lugar de cadenas de modelo hardcodeadas.
- **[Sesiones y reanudación](https://zzjas.github.io/caw/guides/resuming/)** — Conversaciones de múltiples turnos que se reanudan entre procesos mediante un `resume_handle`.
- **[Herramientas MCP](https://zzjas.github.io/caw/guides/mcp-servers/)**, **[ToolKit](https://zzjas.github.io/caw/guides/toolkit/)** y **[subagentes](https://zzjas.github.io/caw/guides/subagents/)** — Proporciona herramientas al agente, declarativamente o como agentes hijos.
- **[Salud del proveedor](https://zzjas.github.io/caw/guides/health/)** — Señales crudas de disponibilidad/credenciales, con una sonda opcional en vivo (`caw doctor`).
- **[Visor de trayectorias](https://zzjas.github.io/caw/guides/trajectory-viewer/)** — Explora trayectorias guardadas en el navegador o en terminal.
- **[Credenciales de Docker](https://zzjas.github.io/caw/guides/docker-credentials/)** — Monta en bind los agentes OAuth en contenedores sin tocar los archivos del host (`caw auth`).

Cada interacción produce una `Trajectory` estructurada con turnos, bloques de contenido, uso de tokens y costo, persistida en JSONL cuando pasas `data_dir=`.

## Documentación

Documentación completa, guías y referencia de API: **<https://zzjas.github.io/caw/>**. También se publican archivos legibles por máquina [`llms.txt`](https://zzjas.github.io/caw/llms.txt) / [`llms-full.txt`](https://zzjas.github.io/caw/llms-full.txt), ya que los usuarios de caw suelen ser agentes.

Hay ejemplos ejecutables en [`examples/`](examples/).

## Licencia

[Apache-2.0](LICENSE)
