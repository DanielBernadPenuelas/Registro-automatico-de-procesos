# Registro automático de procesos batch en Azure SQL

Tres piezas que encajan entre sí:

| Fichero | Papel |
|---|---|
| `SQL_table.sql` | Tabla `ops.TASK_LOG`, índices, procedimientos y vistas de monitorización |
| `logger_file.py` | Cliente Python: abre y cierra el registro, captura errores. Vale como librería y como envoltorio de cualquier comando |
| `Order.bat` | Orquestador para el Programador de tareas de Windows: configuración, bloqueo anti-solapamiento, log local y códigos de salida |

## Flujo de una ejecución

```
Programador de tareas
   └─ Order.bat CARGA_VENTAS
        ├─ comprueba Python, configuración y que no haya otra ejecución activa
        └─ python logger_file.py --task CARGA_VENTAS -- python procesos\carga.py
             ├─ EXEC ops.usp_TASK_LOG_START  ->  fila con PROGRAM_STATE = 'RN'
             ├─ lanza el proceso y captura su salida
             └─ EXEC ops.usp_TASK_LOG_END    ->  'OK' | 'WN' | 'KO' + MENSAGE_ERROR
```

Si Azure SQL no responde, el registro se guarda en `logs\task_log_pendiente.jsonl`
y se reenvía en la siguiente ejecución. El proceso de negocio nunca se cae por un
fallo del sistema de log (salvo que uses `--strict`).

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
   (servidor, base de datos, ruta del proceso). Con `AZSQL_AUTH=aad` no hay
   contraseñas: se usa la identidad administrada de la VM o la cuenta de Windows.
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

## Estados

| Código | Significado |
|---|---|
| `RN` | En ejecución (fila abierta, aún sin `END_DATE`) |
| `OK` | Correcto |
| `KO` | Error: `MENSAGE_ERROR` lleva la traza o las últimas 40 líneas de salida |
| `WN` | Terminó, pero con avisos registrados con `log.warning` |

Las fechas se guardan en **UTC**. La vista `ops.VW_TASK_LOG_DETALLE` las convierte
a hora peninsular y añade la duración en segundos.

## Consultas de vigilancia

```sql
SELECT * FROM ops.VW_TASK_LOG_ULTIMA ORDER BY PROGRAM_STATE, TASK_NAME;
SELECT * FROM ops.VW_TASK_LOG_RESUMEN_30D ORDER BY ERRORES DESC;
EXEC ops.usp_TASK_LOG_CLOSE_ZOMBIES @MinutosMaximos = 240;  
EXEC ops.usp_TASK_LOG_PURGE @DiasRetencion = 180;           
```

    filas = cargar_fichero()
    if filas == 0:
        log.warning("El fichero de entrada estaba vacío")   # -> WN
    procesar(filas)                                          # excepción -> KO + traza
```
