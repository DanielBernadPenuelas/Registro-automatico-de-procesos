#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
logger_file.py
==============
Registro centralizado de ejecuciones de procesos batch en Azure SQL Database
(tabla ops.TASK_LOG).

Tiene dos modos de uso:

1) Como ENVOLTORIO desde el cmd de Windows (lo que hace Order.bat).
   Ejecuta cualquier comando, mide el tiempo, captura la salida y registra
   el resultado. Devuelve el mismo codigo de salida que el proceso hijo.

       python logger_file.py --task "CARGA_VENTAS" -- python procesos\\carga.py --fecha 2026-07-29
       python logger_file.py --task "BACKUP_FTP"   -- powershell -File backup.ps1

2) Como LIBRERIA dentro de tus scripts Python.

       from logger_file import TaskLogger

       with TaskLogger("CARGA_VENTAS") as log:
           procesar()                       # si lanza excepcion -> KO + traza
           log.warning("3 filas descartadas")   # marca la ejecucion como WN

Utilidades adicionales:

       python logger_file.py --test-connection
       python logger_file.py --flush-pending     # reenvia el fichero de contingencia

Configuracion (variables de entorno, normalmente fijadas en Order.bat):

    AZSQL_SERVER      miservidor.database.windows.net
    AZSQL_DATABASE    mibasededatos
    AZSQL_AUTH        sql | aad          (por defecto: aad)
    AZSQL_USER        usuario            (solo si AZSQL_AUTH=sql)
    AZSQL_PASSWORD    contrasena         (solo si AZSQL_AUTH=sql)
    AZSQL_DRIVER      ODBC Driver 18 for SQL Server
    AZSQL_TIMEOUT     30                 (segundos de login timeout)
    AZSQL_PENDING_DIR carpeta del fichero de contingencia (por defecto .\\logs)

Requisitos: pip install pyodbc  +  Microsoft ODBC Driver 18 for SQL Server.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

try:
    import pyodbc
except ImportError:  
    sys.stderr.write(
        "ERROR: falta el paquete 'pyodbc'. Instalalo con: pip install pyodbc\n"
    )
    raise

STATE_RUNNING = "RN"
STATE_OK = "OK"
STATE_ERROR = "KO"
STATE_WARNING = "WN"

MAX_ERROR_LEN = 3000       
MAX_TASK_NAME_LEN = 50     
TAIL_LINES = 40             

TRANSIENT_ERROR_CODES = {
    20, 64, 233, 1205, 4060, 4221,
    10053, 10054, 10060, 10928, 10929,
    40197, 40501, 40613, 49918, 49919, 49920,
}

DEFAULT_RETRIES = 4
DEFAULT_BACKOFF = 2.0     

def _now_utc() -> datetime:
    """Fecha y hora actual en UTC, sin tzinfo (compatible con DATETIME)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def truncate(text: Optional[str], limit: int = MAX_ERROR_LEN) -> Optional[str]:
    """
    Recorta un texto respetando el limite de la columna.
    Conserva el principio y el final, que es donde suele estar la informacion
    util de una traza larga.
    """
    if text is None:
        return None
    text = str(text).strip()
    if len(text) <= limit:
        return text
    marca = "\n...[TRUNCADO]...\n"
    hueco = limit - len(marca)
    cabeza = hueco // 2
    cola = hueco - cabeza
    return text[:cabeza] + marca + text[-cola:]


def _context_prefix() -> str:
    """Contexto de ejecucion que se antepone a los mensajes de error."""
    return f"[{socket.gethostname()} | PID {os.getpid()}] "

def build_connection_string() -> str:
    """Construye la cadena ODBC a partir de las variables de entorno."""
    server = os.getenv("AZSQL_SERVER", "").strip()
    database = os.getenv("AZSQL_DATABASE", "").strip()
    if not server or not database:
        raise RuntimeError(
            "Faltan AZSQL_SERVER y/o AZSQL_DATABASE en las variables de entorno."
        )

    driver = os.getenv("AZSQL_DRIVER", "ODBC Driver 18 for SQL Server").strip()
    timeout = os.getenv("AZSQL_TIMEOUT", "30").strip()
    auth = os.getenv("AZSQL_AUTH", "aad").strip().lower()

    partes = [
        f"DRIVER={{{driver}}}",
        f"SERVER=tcp:{server},1433",
        f"DATABASE={database}",
        "Encrypt=yes",
        "TrustServerCertificate=no",
        f"Connection Timeout={timeout}",
    ]

    if auth == "sql":
        user = os.getenv("AZSQL_USER", "")
        password = os.getenv("AZSQL_PASSWORD", "")
        if not user or not password:
            raise RuntimeError(
                "AZSQL_AUTH=sql requiere AZSQL_USER y AZSQL_PASSWORD."
            )
        partes += [f"UID={user}", f"PWD={password}"]
    else:
        partes.append("Authentication=ActiveDirectoryDefault")

    return ";".join(partes) + ";"


def _is_transient(exc: Exception) -> bool:
    """Determina si el error de SQL merece un reintento."""
    if not isinstance(exc, pyodbc.Error):
        return False
    for arg in exc.args:
        texto = str(arg)
        for codigo in TRANSIENT_ERROR_CODES:
            if f"({codigo})" in texto or f"[{codigo}]" in texto:
                return True
    sqlstate = exc.args[0] if exc.args else ""
    return str(sqlstate) in {"08S01", "08001", "HYT00", "HYT01", "40001"}


def connect(retries: int = DEFAULT_RETRIES) -> "pyodbc.Connection":
    """Abre conexion con reintentos y espera exponencial ante fallos transitorios."""
    espera = DEFAULT_BACKOFF
    ultimo: Optional[Exception] = None

    for intento in range(1, retries + 1):
        try:
            cn = pyodbc.connect(build_connection_string(), autocommit=True)
            cn.timeout = int(os.getenv("AZSQL_TIMEOUT", "30"))
            return cn
        except pyodbc.Error as exc:
            ultimo = exc
            if intento == retries or not _is_transient(exc):
                break
            sys.stderr.write(
                f"[logger] Error transitorio de conexion "
                f"(intento {intento}/{retries}), reintentando en {espera:.0f}s...\n"
            )
            time.sleep(espera)
            espera *= 2

    raise RuntimeError(f"No se pudo conectar a Azure SQL: {ultimo}") from ultimo

def _pending_path() -> Path:
    carpeta = Path(os.getenv("AZSQL_PENDING_DIR", Path(__file__).parent / "logs"))
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta / "task_log_pendiente.jsonl"


def _write_pending(registro: dict) -> None:
    """
    Si la base de datos no responde, el registro no se pierde: se guarda en un
    JSONL local que se reenvia despues con --flush-pending.
    """
    try:
        with _pending_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(registro, ensure_ascii=False, default=str) + "\n")
        sys.stderr.write(f"[logger] Registro guardado en {_pending_path()}\n")
    except OSError as exc:  # pragma: no cover
        sys.stderr.write(f"[logger] No se pudo guardar el registro local: {exc}\n")


def flush_pending() -> int:
    """Reenvia a Azure SQL los registros del fichero de contingencia."""
    ruta = _pending_path()
    if not ruta.exists() or ruta.stat().st_size == 0:
        print("[logger] No hay registros pendientes.")
        return 0

    lineas = [l for l in ruta.read_text(encoding="utf-8").splitlines() if l.strip()]
    enviados, fallidos = 0, []

    with connect() as cn:
        cur = cn.cursor()
        for linea in lineas:
            try:
                reg = json.loads(linea)
                cur.execute(
                    "{CALL ops.usp_TASK_LOG_INSERT_FULL (?, ?, ?, ?, ?)}",
                    reg["TASK_NAME"],
                    reg["START_DATE"],
                    reg.get("END_DATE"),
                    reg["PROGRAM_STATE"],
                    truncate(reg.get("MENSAGE_ERROR")),
                )
                cur.fetchall()
                enviados += 1
            except Exception as exc:
                sys.stderr.write(f"[logger] Registro no reenviado: {exc}\n")
                fallidos.append(linea)

    ruta.write_text("\n".join(fallidos) + ("\n" if fallidos else ""), encoding="utf-8")
    print(f"[logger] Reenviados {enviados} registro(s). Pendientes: {len(fallidos)}.")
    return len(fallidos)

class TaskLogger:
    """
    Gestor de contexto que abre una fila en ops.TASK_LOG al entrar y la cierra
    al salir con el estado que corresponda.

        with TaskLogger("MI_TAREA") as log:
            hacer_cosas()

    Si el bloque termina bien -> OK.
    Si lanza una excepcion    -> KO con la traza (la excepcion se propaga).
    Si se llama a warning()   -> WN.

    strict=False (por defecto) significa que un fallo del propio sistema de log
    nunca tumba el proceso de negocio: se avisa por stderr y se guarda en el
    fichero de contingencia.
    """

    def __init__(self, task_name: str, strict: bool = False) -> None:
        if not task_name or not task_name.strip():
            raise ValueError("task_name es obligatorio.")
        self.task_name = task_name.strip()[:MAX_TASK_NAME_LEN]
        self.strict = strict
        self.id: Optional[int] = None
        self.start_date: Optional[datetime] = None
        self.end_date: Optional[datetime] = None
        self.state: str = STATE_RUNNING
        self._warnings: list[str] = []
        self._cn: Optional["pyodbc.Connection"] = None
        self._degradado = False  

    def start(self) -> "TaskLogger":
        self.start_date = _now_utc()
        try:
            self._cn = connect()
            cur = self._cn.cursor()
            cur.execute("{CALL ops.usp_TASK_LOG_START (?)}", self.task_name)
            self.id = int(cur.fetchone()[0])
            cur.close()
            print(f"[logger] {self.task_name}: ejecucion {self.id} iniciada.")
        except Exception as exc:
            self._degradado = True
            self._on_log_failure("no se pudo abrir la ejecucion", exc)
        return self

    def warning(self, mensaje: str) -> None:
        """Anota una incidencia no bloqueante; la ejecucion terminara como WN."""
        self._warnings.append(str(mensaje))
        print(f"[logger] AVISO: {mensaje}")

    def finish(self, state: str = STATE_OK, error: Optional[str] = None) -> None:
        """Cierra la ejecucion con el estado indicado."""
        if self.state != STATE_RUNNING:
            return 

        if state == STATE_OK and self._warnings:
            state = STATE_WARNING

        detalle = error
        if self._warnings:
            avisos = "AVISOS:\n- " + "\n- ".join(self._warnings)
            detalle = f"{detalle}\n\n{avisos}" if detalle else avisos

        self.state = state
        self.end_date = _now_utc()
        detalle = truncate(detalle)

        try:
            if self._degradado or self.id is None:
                raise RuntimeError("Ejecucion sin ID: no se abrio en base de datos.")
            cur = self._cn.cursor()
            cur.execute(
                "{CALL ops.usp_TASK_LOG_END (?, ?, ?)}", self.id, state, detalle
            )
            cur.close()
            print(
                f"[logger] {self.task_name}: ejecucion {self.id} cerrada "
                f"con estado {state} ({self.duration_seconds:.1f}s)."
            )
        except Exception as exc:
            self._on_log_failure("no se pudo cerrar la ejecucion", exc)
            _write_pending(
                {
                    "TASK_NAME": self.task_name,
                    "START_DATE": self.start_date,
                    "END_DATE": self.end_date,
                    "PROGRAM_STATE": state,
                    "MENSAGE_ERROR": detalle,
                }
            )
        finally:
            self._close_connection()

    def fail(self, exc: BaseException) -> None:
        """Cierra la ejecucion como KO incluyendo la traza completa."""
        traza = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        self.finish(STATE_ERROR, _context_prefix() + traza)


    def __enter__(self) -> "TaskLogger":
        return self.start()

    def __exit__(self, exc_type, exc_value, tb) -> bool:
        if exc_type is None:
            self.finish(STATE_OK)
        else:
            self.fail(exc_value)
        return False  

    @property
    def duration_seconds(self) -> float:
        if not self.start_date:
            return 0.0
        fin = self.end_date or _now_utc()
        return (fin - self.start_date).total_seconds()

    def _on_log_failure(self, contexto: str, exc: Exception) -> None:
        mensaje = f"[logger] FALLO DE REGISTRO ({contexto}): {exc}"
        if self.strict:
            raise RuntimeError(mensaje) from exc
        sys.stderr.write(mensaje + "\n")

    def _close_connection(self) -> None:
        if self._cn is not None:
            try:
                self._cn.close()
            except Exception:
                pass
            self._cn = None


def run_command(comando: Sequence[str], task_name: str, strict: bool = False) -> int:
    """
    Ejecuta un comando externo registrando su resultado en ops.TASK_LOG.
    La salida del hijo se muestra en tiempo real y ademas se guardan las
    ultimas lineas para el mensaje de error.

    Devuelve el codigo de salida del proceso hijo.
    """
    ultimas = deque(maxlen=TAIL_LINES)

    with TaskLogger(task_name, strict=strict) as log:
        print(f"[logger] Ejecutando: {' '.join(comando)}")
        try:
            proceso = subprocess.Popen(
                list(comando),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError as exc:
            log.finish(STATE_ERROR, _context_prefix() + f"Comando no encontrado: {exc}")
            return 9009 

        assert proceso.stdout is not None
        for linea in proceso.stdout:
            sys.stdout.write(linea)
            ultimas.append(linea.rstrip("\n"))
        proceso.stdout.close()
        codigo = proceso.wait()

        if codigo == 0:
            log.finish(STATE_OK)
        else:
            detalle = (
                f"{_context_prefix()}El comando termino con codigo {codigo}.\n"
                f"Comando: {' '.join(comando)}\n"
                f"Ultimas {len(ultimas)} lineas de salida:\n"
                + "\n".join(ultimas)
            )
            log.finish(STATE_ERROR, detalle)

        return codigo

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Registro de ejecuciones batch en Azure SQL (ops.TASK_LOG).",
        epilog=(
            "Ejemplo: python logger_file.py --task CARGA_VENTAS -- "
            "python procesos\\carga.py --fecha 2026-07-29"
        ),
    )
    p.add_argument("--task", "-t", help="Nombre de la tarea (max. 50 caracteres).")
    p.add_argument(
        "--strict",
        action="store_true",
        help="Falla el proceso si el propio registro en base de datos falla.",
    )
    p.add_argument(
        "--test-connection",
        action="store_true",
        help="Comprueba la conectividad con Azure SQL y termina.",
    )
    p.add_argument(
        "--flush-pending",
        action="store_true",
        help="Reenvia los registros del fichero de contingencia.",
    )
    p.add_argument(
        "comando",
        nargs=argparse.REMAINDER,
        help="Comando a ejecutar, precedido de -- ",
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.test_connection:
        try:
            with connect() as cn:
                fila = cn.cursor().execute(
                    "SELECT DB_NAME(), SUSER_SNAME(), SYSUTCDATETIME();"
                ).fetchone()
            print(f"[logger] Conexion correcta. BD={fila[0]} Usuario={fila[1]} UTC={fila[2]}")
            return 0
        except Exception as exc:
            sys.stderr.write(f"[logger] Conexion fallida: {exc}\n")
            return 1

    if args.flush_pending:
        return 0 if flush_pending() == 0 else 1

    if not args.task:
        sys.stderr.write("[logger] Falta el parametro --task.\n")
        return 2

    comando = list(args.comando)
    if comando and comando[0] == "--":
        comando = comando[1:]
    if not comando:
        sys.stderr.write(
            "[logger] No se indico ningun comando. Usa: --task NOMBRE -- <comando>\n"
        )
        return 2

    return run_command(comando, args.task, strict=args.strict)

if __name__ == "__main__":
    sys.exit(main())