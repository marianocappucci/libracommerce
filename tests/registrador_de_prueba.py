"""Un registrador MINIMO para los tests de este motor.

`RepositorioAuditado` no define el formato de la fila (ADR-050): le pasa un `Cambio` a un
registrador, y el de verdad es `libraauth.auditoria.registrador_dbapi`, que este motor no
importa. Los tests que necesitan que **haya filas en `actividad_log`** (la transaccionalidad,
el lector `ActividadRepository`) usan este: inserta lo justo, con la forma
`«<Entidad> — <etiqueta>»`, y opcionalmente anota los `Cambio` que recibe.

**No es una copia del formato y no hay que tratarla como tal**: no tapa secretos, no arma
diffs ni descarta ediciones vacias. Eso lo prueba `tests/test_auditoria_formato_libraauth.py`
contra el registrador real, donde `libraauth` esta instalado.
"""
from datetime import datetime

from libracommerce.db.auditoria import entidades_por_tabla, etiqueta_de_tabla


def registrador_de_prueba(anotados: list | None = None, usuario: str = "ana"):
    entidades = entidades_por_tabla()

    def registrar(conn, cambio) -> None:
        if anotados is not None:
            anotados.append(cambio)
        entidad = entidades[cambio.tabla]
        etiqueta = etiqueta_de_tabla(cambio.tabla, cambio.despues or cambio.antes or {})
        titulo = entidad.capitalize()
        conn.execute(
            "INSERT INTO actividad_log (ts, usuario, accion, entidad, entidad_id, descripcion) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), usuario, cambio.accion, entidad, cambio.id,
             f"{titulo} — {etiqueta}" if etiqueta else titulo),
        )

    return registrar
