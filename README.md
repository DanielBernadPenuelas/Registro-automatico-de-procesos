# Registro automático de procesos batch en Azure SQL

Saber, sin entrar al servidor, **qué procesos se han ejecutado, cuáles han fallado, por qué y cuánto han tardado**.

## El problema

Los procesos batch que lanza el Programador de tareas de Windows suelen fallar en silencio:

- **Nadie se entera de un fallo** hasta que un usuario detecta que faltan datos. El Programador de tareas solo muestra un código de salida, y únicamente en esa máquina.
- **El error se pierde**: la traza se queda en la consola, en un `.log` local que rota o en ninguna parte.
- **Ejecuciones solapadas**: si una carga tarda más de lo previsto, la siguiente arranca encima y duplica o corrompe datos.
- **Procesos "colgados"**: un proceso muerto a medias no deja rastro. No se sabe si sigue en marcha o se cayó.
- **Sin histórico**: no hay forma de responder "¿cuánto tarda esta carga normalmente?" o "¿cuántas veces falló este mes?".
- **Cada script registra a su manera**, o no registra nada, y añadir trazas obliga a tocar el código de negocio.
- **El propio log puede tumbar el proceso**: si la base de datos de log no responde, el proceso que la usa no debería caerse por eso.

## Qué aporta

| Problema | Cómo se resuelve |
|---|---|
| Fallos que nadie ve | Cada ejecución deja una fila en `ops.TASK_LOG` con su estado (`OK`, `WN`, `KO`). Se consulta desde cualquier sitio con acceso a Azure SQL. |
| Error perdido | Ante un fallo, `MENSAGE_ERROR` guarda la traza o las últimas 40 líneas de salida del proceso. |
| Solapamientos | `Order.bat` usa un bloqueo y no arranca si ya hay otra ejecución activa de la misma tarea. |
| Procesos colgados | Las filas que se quedan en `RN` se detectan y se cierran con `ops.usp_TASK_LOG_CLOSE_ZOMBIES`. |
| Sin histórico | Vistas con la última ejecución de cada tarea, el resumen de 30 días y la duración de cada ejecución. |
| Cambiar el código de negocio | `logger_file.py` envuelve **cualquier comando** sin modificarlo. También se puede usar como librería si se quieren registrar avisos. |
| El log tumba el proceso | Si Azure SQL no responde, el registro se guarda en `logs\task_log_pendiente.jsonl` y se reenvía en la siguiente ejecución. El proceso sigue (salvo con `--strict`). |
| Contraseñas en scripts | Con `AZSQL_AUTH=aad` se usa la identidad administrada de la VM o la cuenta de Windows, sin credenciales en ficheros. |

## Cómo funciona

```
Programador de tareas
   └─ Order.bat CARGA_VENTAS
        ├─ comprueba Python, configuración y que no haya otra ejecución activa
        └─ python logger_file.py --task CARGA_VENTAS -- python procesos\carga.py
             ├─ EXEC ops.usp_TASK_LOG_START  ->  fila con PROGRAM_STATE = 'RN'
             ├─ lanza el proceso y captura su salida
             └─ EXEC ops.usp_TASK_LOG_END    ->  'OK' | 'WN' | 'KO' + MENSAGE_ERROR
```

| Fichero | Papel |
|---|---|
| `SQL_table.sql` | Tabla `ops.TASK_LOG`, índices, procedimientos y vistas de monitorización |
| `logger_file.py` | Cliente Python: abre y cierra el registro y captura errores. Funciona como librería o como envoltorio de cualquier comando |
| `Order.bat` | Orquestador para el Programador de tareas: configuración, bloqueo anti-solapamiento, log local y códigos de salida |

### Estados

| Código | Significado |
|---|---|
| `RN` | En ejecución (fila abierta, aún sin `END_DATE`) |
| `OK` | Correcto |
| `WN` | Terminó, pero con avisos registrados con `log.warning` |
| `KO` | Error: `MENSAGE_ERROR` lleva la traza o las últimas 40 líneas de salida |

Las fechas se guardan en **UTC**. La vista `ops.VW_TASK_LOG_DETALLE` las convierte a hora peninsular y añade la duración en segundos.

## Respuestas que da

```sql
-- ¿Cómo terminó cada proceso la última vez? (los errores primero)
SELECT * FROM ops.VW_TASK_LOG_ULTIMA ORDER BY PROGRAM_STATE, TASK_NAME;

-- ¿Qué procesos fallan más en el último mes?
SELECT * FROM ops.VW_TASK_LOG_RESUMEN_30D ORDER BY ERRORES DESC;

-- Cerrar ejecuciones que llevan más de 4 horas en 'RN'
EXEC ops.usp_TASK_LOG_CLOSE_ZOMBIES @MinutosMaximos = 240;

-- Mantener la tabla acotada: borrar registros de más de 180 días
EXEC ops.usp_TASK_LOG_PURGE @DiasRetencion = 180;
```

## Puesta en marcha

1. **Base de datos**: ejecutar `SQL_table.sql` conectado a la base de datos destino.
   Descomenta el apartado 9 para crear el rol con permisos mínimos.

2. **Servidor Windows**:
   ```bat
   :: Microsoft ODBC Driver 18 for SQL Server (necesario para pyodbc)
   winget install Microsoft.msodbcsql.18

   python -m venv .venv
   .venv\Scripts\pip install pyodbc
   ```

3. **Configuración**: ajustar en `Order.bat` el bloque `1. CONFIGURACION`
   (servidor, base de datos, ruta del proceso). Con `AZSQL_AUTH=aad` no hacen falta contraseñas.
   Si necesitas autenticación SQL, crea `config_secreto.bat` con
   `set "AZSQL_AUTH=sql"`, `set "AZSQL_USER=..."` y `set "AZSQL_PASSWORD=..."`,
   y añádelo al `.gitignore`.

4. **Comprobar la conexión**:
   ```bat
   .venv\Scripts\python logger_file.py --test-connection
   ```

5. **Programar** (ver el anexo al final de `Order.bat`):
   ```bat
   schtasks /Create /TN "BATCH\CARGA_VENTAS" /TR "\"C:\procesos\Order.bat\" CARGA_VENTAS" /SC DAILY /ST 06:00 /RU "DOMINIO\svc_batch" /RP * /RL HIGHEST /F
   ```
