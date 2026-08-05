"""Log de actividad para los productos que corren sobre este motor en sqlite3
crudo (VentaLibra hoy; Contalibra y Restolibra despues de P7/P8).

## Por que existe, si `libraauth.auditoria` ya hace esto

Porque aquel cuelga del `flush` de SQLAlchemy, y aca no hay SQLAlchemy. Los
productos de la familia se partieron en dos mundos:

- **SQLAlchemy** (LibraDesk, Gestiolibra, MedLibra): el registro cuelga del
  flush, con una lista blanca de modelos. Nadie llama a nada.
- **sqlite3 crudo sobre este motor** (VentaLibra): no hay flush del que
  colgarse. Lo que si hay —y es mejor de lo que parece— es que **las 25
  sentencias de escritura del motor viven en un solo archivo**,
  `db/repository.py`, detras de 12 metodos publicos.

Asi que aca el punto de enganche es el repositorio, envuelto entero. El
producto no llama a `registrar(...)` en ningun lado: envuelve una vez, en
`create_app()`, y listo.

## La parte que se degrada sola, y como se evita

Sembrar llamadas a mano funciona el primer dia y se pudre despues: alguien
agrega un metodo de escritura, se olvida de auditarlo, y **un log incompleto se
ve exactamente igual que uno completo**. No hay como darse cuenta mirando la
pantalla.

La defensa no es la disciplina: es `test_toda_escritura_esta_auditada`, que
enumera los metodos de escritura del repositorio y falla si alguno no esta en
`AUDITABLES`. Un metodo nuevo sin registrar no pasa el CI.

## El formato es el mismo que el del otro mundo

`actividad_log` tiene las mismas columnas que la tabla de `libraauth.auditoria`
a proposito: asi la pantalla compartida (`libra-ui/Logs`) y el router del motor
de auth (`build_logs_router`) sirven sin cambios para los dos mundos. Un
producto que migre de uno al otro no cambia ni el frontend ni el endpoint.
"""
import json
import sqlite3
from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable

CREAR = "crear"
EDITAR = "editar"
BORRAR = "borrar"

SISTEMA = "Sistema"

# Lo que se escribe en lugar del valor de un campo oculto que si cambio. Mismo
# criterio y mismo texto que `libraauth.auditoria.OCULTO`: la fila se registra
# igual, tapando el valor, porque un log que descarta la edicion entera no
# oculta el secreto — oculta el hecho.
OCULTO = "(oculto)"

CAMPOS_OCULTOS = frozenset({
    "password", "password_hash", "token", "secret", "api_key", "access_token",
})

# Campos que no aportan nada al diff y lo ensucian: los que el propio motor
# recalcula en cada guardado.
CAMPOS_IGNORADOS = frozenset({"created_at"})


class _Auditable:
    """Como auditar un metodo de escritura del repositorio.

    - `entidad`: el nombre logico que se ve en la pantalla.
    - `getter`: el metodo que trae el estado ANTERIOR, para poder diffear una
      edicion. `None` cuando el repositorio no expone uno; ahi la fila se
      registra igual pero sin `cambios` — se pierde el detalle, no el hecho.
    - `etiqueta`: el campo que identifica la fila para un humano.
    - `solo_alta`: para los ledgers append-only, donde una fila nunca se edita.
    """

    __slots__ = ("entidad", "getter", "etiqueta", "solo_alta")

    def __init__(self, entidad, getter=None, etiqueta=None, solo_alta=False):
        self.entidad = entidad
        self.getter = getter
        self.etiqueta = etiqueta
        self.solo_alta = solo_alta


# {metodo del repositorio: como auditarlo}
#
# 🔴 Agregar un metodo de escritura al repositorio **obliga** a sumarlo aca:
# `test_toda_escritura_esta_auditada` falla si no. No es un recordatorio, es el
# CI.
AUDITABLES: dict[str, _Auditable] = {
    "save_party": _Auditable("cliente/proveedor", "get_party", "display_name"),
    "save_catalog_item": _Auditable("producto", "get_catalog_item", "name"),
    "save_item_code": _Auditable("codigo", None, "code"),
    "save_item_variant": _Auditable("variante", "get_item_variant", "name"),
    "save_price_list": _Auditable("lista de precios", "get_price_list", "name"),
    "save_item_price": _Auditable("precio", None, None),
    "save_location": _Auditable("deposito", "get_location", "name"),
    "save_sale": _Auditable("venta", "get_sale", "number"),
    "save_purchase_order": _Auditable("orden de compra", "get_purchase_order", "number"),
    "save_purchase_receipt": _Auditable("recepcion de compra", "get_purchase_receipt", None),
    # `stock_movements` es un ledger: una fila nunca se edita ni se borra, solo
    # se agrega otra que la compensa. Registrar "editar" aca seria mentir.
    "append_stock_movement": _Auditable("movimiento de stock", None, None, solo_alta=True),
    # Firma distinta a todas las demas —(key, value) en vez de una entidad—, y
    # por eso se maneja aparte en el wrapper.
    "set_setting": _Auditable("configuracion", None, None),
}

# Metodos del repositorio que escriben pero NO se auditan, con el motivo. Estar
# en esta lista es una decision explicita; no estar en ninguna de las dos es lo
# que el test no perdona.
NO_AUDITABLES: dict[str, str] = {
    # Tabla de referencia (unidades de medida: kg, litro, unidad). La escribe
    # `save_catalog_item` de paso, al guardar el producto que la usa: auditarla
    # dejaria dos filas por cada alta de producto, una de ellas sin sentido
    # para quien lee.
    "_upsert_unit": "tabla de referencia, se escribe al guardar un producto",
}


def init_auditoria_schema(conn: sqlite3.Connection) -> None:
    """Mismas columnas que la tabla de `libraauth.auditoria`, para que la
    pantalla compartida y el router del motor de auth sirvan sin cambios.

    `entidad_id` es INTEGER y aca si corresponde: todas las PK de este motor
    son `INTEGER PRIMARY KEY AUTOINCREMENT`.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS actividad_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            usuario TEXT NOT NULL DEFAULT 'Sistema',
            accion TEXT NOT NULL,
            entidad TEXT NOT NULL,
            entidad_id INTEGER,
            descripcion TEXT NOT NULL DEFAULT '',
            cambios TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_actividad_log_ts ON actividad_log(ts);
        CREATE INDEX IF NOT EXISTS ix_actividad_log_accion ON actividad_log(accion);
        CREATE INDEX IF NOT EXISTS ix_actividad_log_entidad ON actividad_log(entidad);
        """
    )
    conn.commit()


def _legible(valor):
    if isinstance(valor, Decimal):
        return str(valor)
    if isinstance(valor, datetime):
        return valor.strftime("%Y-%m-%d %H:%M:%S")
    if valor is None or isinstance(valor, (str, int, float, bool)):
        return valor
    return str(valor)


def _diff(antes, despues) -> dict:
    """`{campo: [antes, despues]}` entre dos entidades del dominio.

    Las colecciones anidadas (los items de una venta, de una orden) quedan
    afuera del detalle: un diff de tuplas de dataclasses no se lee. Lo que si
    entra es que **cambiaron**, para que el log no diga que una venta se edito
    sin decir en que.
    """
    if antes is None or not is_dataclass(antes) or not is_dataclass(despues):
        return {}
    cambios = {}
    for campo in fields(despues):
        nombre = campo.name
        if nombre in CAMPOS_IGNORADOS:
            continue
        viejo = getattr(antes, nombre, None)
        nuevo = getattr(despues, nombre, None)
        if viejo == nuevo:
            continue
        if nombre in CAMPOS_OCULTOS:
            cambios[nombre] = [OCULTO, OCULTO]
        elif isinstance(nuevo, (tuple, list)) or isinstance(viejo, (tuple, list)):
            cambios[nombre] = [f"{len(viejo or ())} items", f"{len(nuevo or ())} items"]
        else:
            cambios[nombre] = [_legible(viejo), _legible(nuevo)]
    return cambios


def _etiqueta(entidad, campo: str | None) -> str:
    if not campo:
        return ""
    valor = getattr(entidad, campo, None)
    return str(valor)[:200] if valor else ""


class RepositorioAuditado:
    """Envuelve `SqliteCommerceRepository` y registra cada escritura.

    Se delega por `__getattr__`, no por herencia, a proposito: asi un metodo de
    LECTURA nuevo en el repositorio funciona sin tocar nada aca, mientras que
    uno de ESCRITURA nuevo queda sin auditar y el test lo caza. Heredando, los
    dos casos pasarian igual de desapercibidos.

    `usuario` es un callable y no un string porque el usuario cambia por
    request. En VentaLibra se le pasa el `ContextVar` que ya llena el middleware
    de `libraauth` — asi este motor no depende del de auth.
    """

    def __init__(self, repo, conn: sqlite3.Connection, usuario: Callable[[], str] | None = None):
        self._repo = repo
        self._conn = conn
        self._usuario = usuario or (lambda: SISTEMA)

    def __getattr__(self, nombre):
        # Solo llega aca lo que no esta definido en esta clase: las lecturas.
        atributo = getattr(self._repo, nombre)
        if nombre in AUDITABLES:
            return self._envolver(nombre, atributo)
        return atributo

    def _envolver(self, nombre: str, metodo):
        meta = AUDITABLES[nombre]

        def auditado(*args, **kwargs):
            if nombre == "set_setting":
                return self._set_setting(metodo, meta, *args, **kwargs)
            entidad = args[0] if args else next(iter(kwargs.values()))
            previo = None
            es_alta = getattr(entidad, "id", None) is None or meta.solo_alta
            if not es_alta and meta.getter:
                previo = getattr(self._repo, meta.getter)(entidad.id)
            resultado = metodo(*args, **kwargs)
            self._registrar(
                accion=CREAR if es_alta else EDITAR,
                entidad=meta.entidad,
                entidad_id=getattr(resultado, "id", None),
                etiqueta=_etiqueta(resultado, meta.etiqueta),
                cambios=None if es_alta else _diff(previo, resultado),
            )
            return resultado

        return auditado

    def _set_setting(self, metodo, meta, key, value):
        previo = self._repo.get_setting(key)
        resultado = metodo(key, value)
        if previo == value:
            # Guardar el mismo valor no es un cambio. Mismo criterio que el
            # otro mundo, donde un `dirty` sin columnas cambiadas se descarta.
            return resultado
        oculto = key.lower() in CAMPOS_OCULTOS
        self._registrar(
            accion=CREAR if previo is None else EDITAR,
            entidad=meta.entidad,
            entidad_id=None,
            etiqueta=key,
            cambios={key: [OCULTO, OCULTO]} if oculto else {key: [previo, value]},
        )
        return resultado

    def _registrar(self, *, accion, entidad, entidad_id, etiqueta, cambios):
        titulo = entidad.capitalize()
        self._conn.execute(
            "INSERT INTO actividad_log "
            "(ts, usuario, accion, entidad, entidad_id, descripcion, cambios) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                # Hora local, igual que `auth_log`: las dos se muestran juntas
                # en la misma pantalla y una en UTC quedaria tres horas corrida.
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                str(self._usuario() or SISTEMA)[:100],
                accion,
                entidad,
                entidad_id,
                (f"{titulo} — {etiqueta}" if etiqueta else titulo)[:500],
                json.dumps(cambios, ensure_ascii=False) if cambios else None,
            ),
        )
        self._conn.commit()


class ActividadRepository:
    """Lectura del log, con la misma interfaz que
    `libraauth.auditoria.AuditoriaRepository` para que `build_logs_router` la
    tome sin saber cual de las dos es.

    Sin metodos de escritura, tambien a proposito: lo que se escribe lo decide
    el wrapper del repositorio, no un llamador.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def _where(self, entidad, accion, usuario, desde, hasta):
        clausulas, params = [], []
        if entidad:
            clausulas.append("entidad = ?")
            params.append(entidad)
        if accion:
            clausulas.append("accion = ?")
            params.append(accion)
        if usuario:
            clausulas.append("usuario = ?")
            params.append(usuario)
        if desde:
            clausulas.append("ts >= ?")
            params.append(desde)
        if hasta:
            # El filtro de fecha llega como dia (`2026-08-05`) y `ts` tiene
            # hora: sin esto, "hasta el 5" dejaria afuera todo el dia 5.
            clausulas.append("ts <= ?")
            params.append(f"{hasta} 23:59:59" if len(hasta) == 10 else hasta)
        return (" WHERE " + " AND ".join(clausulas) if clausulas else ""), params

    def listar(self, *, entidad="", accion="", usuario="", desde="", hasta="",
               limit=100, offset=0) -> list[dict]:
        where, params = self._where(entidad, accion, usuario, desde, hasta)
        filas = self._conn.execute(
            "SELECT id, ts, usuario, accion, entidad, entidad_id, descripcion, cambios "
            f"FROM actividad_log{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [{
            "id": f[0], "ts": f[1], "usuario": f[2], "accion": f[3],
            "entidad": f[4], "entidad_id": f[5], "descripcion": f[6],
            "cambios": json.loads(f[7]) if f[7] else None,
        } for f in filas]

    def contar(self, *, entidad="", accion="", usuario="", desde="", hasta="") -> int:
        where, params = self._where(entidad, accion, usuario, desde, hasta)
        return self._conn.execute(
            f"SELECT COUNT(*) FROM actividad_log{where}", params
        ).fetchone()[0]

    def usuarios(self) -> list[str]:
        return [
            f[0] for f in self._conn.execute(
                "SELECT DISTINCT usuario FROM actividad_log ORDER BY usuario"
            ).fetchall()
        ]


def entidades() -> dict[str, str]:
    """Lo que el producto le pasa a `build_logs_router` para armar el filtro.

    Sale de `AUDITABLES` y no de un `SELECT DISTINCT` sobre el log: asi el
    filtro ofrece todas las entidades auditables aunque todavia no haya
    actividad de alguna.
    """
    return {nombre: meta.entidad for nombre, meta in AUDITABLES.items()}
