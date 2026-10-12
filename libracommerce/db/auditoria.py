"""Log de actividad para los productos que corren sobre este motor en SQL crudo
(VentaLibra, LibraClub; Contalibra y Restolibra tienen la tabla pero no lo usan).

## Por que existe, si `libraauth.auditoria` ya hace esto

Porque aquel cuelga del `flush` de SQLAlchemy, y aca no hay SQLAlchemy. Los
productos de la familia se partieron en dos mundos:

- **SQLAlchemy** (LibraDesk, Gestiolibra, MedLibra): el registro cuelga del
  flush, con una lista blanca de modelos. Nadie llama a nada.
- **SQL crudo sobre este motor** (VentaLibra): no hay flush del que colgarse.
  Lo que si hay —y es mejor de lo que parece— es que **las 25 sentencias de
  escritura del motor viven en un solo archivo**, `db/repository.py`, detras de
  12 metodos publicos.

Asi que aca el punto de enganche es el repositorio, envuelto entero. El
producto no llama a `registrar(...)` en ningun lado: envuelve una vez y listo.

## La parte que se degrada sola, y como se evita

Sembrar llamadas a mano funciona el primer dia y se pudre despues: alguien
agrega un metodo de escritura, se olvida de auditarlo, y **un log incompleto se
ve exactamente igual que uno completo**. No hay como darse cuenta mirando la
pantalla.

La defensa no es la disciplina: es `test_toda_escritura_esta_auditada`, que
enumera los metodos de escritura del repositorio y falla si alguno no esta en
`AUDITABLES`. Un metodo nuevo sin registrar no pasa el CI.

## Este modulo NO define el formato del log (v0.57.0, ADR-050)

Hasta v0.56 la fila de `actividad_log` se armaba aca, con una copia del formato
de `libraauth.auditoria` que ya habia divergido: tapaba menos columnas (sin
`password_cifrada`, `token_hash` ni `secret_key`), ignoraba `created_at` y declaraba
`entidad_id INTEGER`. Dos copias de un formato es exactamente como se desalinean.

Ahora el wrapper solo dice **que paso**: le pasa a un *registrador* un `Cambio`
(`tabla`, `accion`, `id`, `antes`, `despues`, las dos fotos como dict de campos) y
el registrador —el `registrador_dbapi` de `libraauth`, el mismo que usa
`libracore.auditoria` para los clientes— lo vuelve fila. Este motor no importa
`libraauth` ni `libracore` por eso: el contrato es un callable y un duck typing, y
el producto, que ya tiene los dos, los une:

    from libraauth.auditoria import registrador_dbapi
    from libracommerce.db import auditoria as aud

    registrador = registrador_dbapi(
        {**aud.entidades_por_tabla(), "clients": "cliente"},
        **aud.opciones_del_registrador(),
    )
    RepositorioAuditado(SqliteCommerceRepository(conn), conn, registrador)

**El registrador es obligatorio, sin default, a proposito.** Un default seria una
tercera copia del formato (o un wrapper que no audita sin avisar, que es el modo
de falla que este archivo existe para evitar). Pasar `usuario=` como antes es un
`TypeError`: el usuario lo resuelve el registrador, del `ContextVar` de `libraauth`.

El formato de la fila (`actividad_log` con las mismas columnas que la de
`libraauth`, de modo que la pantalla compartida y `build_logs_router` sirven para
los dos mundos) vive en `libraauth.auditoria.fila_de_auditoria`.
"""
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from typing import Any

CREAR = "crear"
EDITAR = "editar"
BORRAR = "borrar"

# Campos que no aportan nada al diff y lo ensucian: los que el propio motor
# recalcula en cada guardado. Se los pasa al registrador (`opciones_del_registrador`).
CAMPOS_IGNORADOS = frozenset({"created_at"})


@dataclass(frozen=True)
class Cambio:
    """Una escritura del repositorio, como la ve el registrador.

    Mismo contrato (duck typing) que `libracore.auditoria.Cambio`, para que un solo
    registrador sirva a los dos. `antes`/`despues` son `{campo: valor}` de la entidad
    (un dict plano: las colecciones anidadas quedan como tuplas); `antes` es `None`
    en un alta y tambien cuando el repositorio no sabe leer el estado previo.
    """

    tabla: str
    accion: str
    id: Any = None
    antes: Mapping[str, Any] | None = None
    despues: Mapping[str, Any] | None = None


#: `(conn, cambio) -> None`. Escribe con la `conn` que recibe y **no confirma**: de eso
#: se ocupa el wrapper (ADR-028).
Registrador = Callable[[Any, Cambio], None]


class _Auditable:
    """Como auditar un metodo de escritura del repositorio.

    - `entidad`: el nombre logico que se ve en la pantalla.
    - `tabla`: la tabla de la base. Es la clave con la que el registrador sabe de que
      se trata (`entidades_por_tabla`).
    - `getter`: el metodo que trae el estado ANTERIOR, para poder diffear una
      edicion. `None` cuando el repositorio no expone uno; ahi el cambio llega sin
      `antes` y el registrador deja la fila sin detalle: se pierde el detalle, no el hecho.
    - `etiqueta`: el campo que identifica la fila para un humano.
    - `solo_alta`: para los ledgers append-only, donde una fila nunca se edita.
    """

    __slots__ = ("entidad", "tabla", "getter", "etiqueta", "solo_alta")

    def __init__(self, entidad, tabla, getter=None, etiqueta=None, solo_alta=False):
        self.entidad = entidad
        self.tabla = tabla
        self.getter = getter
        self.etiqueta = etiqueta
        self.solo_alta = solo_alta


# {metodo del repositorio: como auditarlo}
#
# 🔴 Agregar un metodo de escritura al repositorio **obliga** a sumarlo aca:
# `test_toda_escritura_esta_auditada` falla si no. No es un recordatorio, es el
# CI.
AUDITABLES: dict[str, _Auditable] = {
    "save_party": _Auditable("cliente/proveedor", "parties", "get_party", "display_name"),
    "save_catalog_item": _Auditable("producto", "catalog_items", "get_catalog_item", "name"),
    "save_item_code": _Auditable("codigo", "item_codes", None, "code"),
    "save_item_variant": _Auditable("variante", "item_variants", "get_item_variant", "name"),
    "save_price_list": _Auditable("lista de precios", "price_lists", "get_price_list", "name"),
    "save_item_price": _Auditable("precio", "item_prices", None, None),
    "save_location": _Auditable("deposito", "locations", "get_location", "name"),
    "save_branch": _Auditable("sucursal", "branches", "get_branch", "name"),
    "save_sale": _Auditable("venta", "sales", "get_sale", "number"),
    "save_purchase_order": _Auditable("orden de compra", "purchase_orders", "get_purchase_order", "number"),
    "save_purchase_receipt": _Auditable("recepcion de compra", "purchase_receipts", "get_purchase_receipt", None),
    # `stock_movements` es un ledger: una fila nunca se edita ni se borra, solo
    # se agrega otra que la compensa. Registrar "editar" aca seria mentir.
    "append_stock_movement": _Auditable("movimiento de stock", "stock_movements", None, None, solo_alta=True),
    # Firma distinta a todas las demas —(key, value) en vez de una entidad—, y
    # por eso se maneja aparte en el wrapper: la "fila" es `{clave: valor}`.
    "set_setting": _Auditable("configuracion", "commerce_settings", None, None),
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
    son `INTEGER PRIMARY KEY AUTOINCREMENT`. El registrador de `libraauth` escribe el id
    como texto y la base lo asigna al entero (SQLite por afinidad, PostgreSQL por el
    parametro sin tipo): medido, sin migracion (ADR-050).
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


def _valores(entidad) -> dict | None:
    """Los campos de una entidad del dominio como dict plano (sin recursion).

    Las colecciones anidadas (los items de una venta, de una orden) quedan como tuplas:
    resumirlas es cosa del registrador (`resumir_colecciones`). Lo que no es una
    dataclass no tiene campos que comparar.
    """
    if entidad is None or not is_dataclass(entidad):
        return None
    return {campo.name: getattr(entidad, campo.name) for campo in fields(entidad)}


def resumir_colecciones(valor):
    """Un diff de tuplas de dataclasses no lo lee nadie: lo que importa es que la
    coleccion **cambio**, y de cuanto a cuanto. Se pasa como `resumir=` al registrador;
    la comparacion sigue siendo sobre el valor crudo, asi que dos colecciones del mismo
    largo con otro contenido siguen siendo una edicion."""
    if isinstance(valor, (tuple, list)):
        return f"{len(valor)} items"
    return valor


#: Campo que identifica la fila para un humano, por tabla.
_ETIQUETAS = {meta.tabla: meta.etiqueta for meta in AUDITABLES.values() if meta.tabla != "commerce_settings"}


def entidades_por_tabla() -> dict[str, str]:
    """`{tabla: nombre logico}` de lo que audita este motor: la lista blanca que se le
    pasa a `libraauth.registrador_dbapi`. Sale de `AUDITABLES`, asi que una entidad nueva
    entra sola."""
    return {meta.tabla: meta.entidad for meta in AUDITABLES.values()}


def etiqueta_de_tabla(tabla: str, valores: Mapping[str, Any]) -> str | None:
    """El texto que identifica la fila (`display_name` de un tercero, `number` de una venta).

    Devuelve `None` para una tabla que no es de este motor, asi el registrador la resuelve
    por su cuenta (los clientes del motor de core); `""` para las que no tienen etiqueta. Una
    configuracion se identifica por su clave, que es la unica del dict.
    """
    if tabla == "commerce_settings":
        return str(next(iter(valores), ""))[:200]
    if tabla not in _ETIQUETAS:
        return None
    campo = _ETIQUETAS[tabla]
    valor = valores.get(campo) if campo else None
    return str(valor)[:200] if valor else ""


def opciones_del_registrador() -> dict:
    """Los argumentos de este motor para `libraauth.auditoria.registrador_dbapi`:
    `columnas_ignoradas`, `etiqueta` y `resumir`. Se usan con `**`; las `entidades` se
    arman aparte (`entidades_por_tabla`) para poder sumarles las del producto."""
    return {
        "columnas_ignoradas": CAMPOS_IGNORADOS,
        "etiqueta": etiqueta_de_tabla,
        "resumir": resumir_colecciones,
    }


class RepositorioAuditado:
    """Envuelve `SqliteCommerceRepository` y le avisa a un registrador de cada escritura.

    Se delega por `__getattr__`, no por herencia, a proposito: asi un metodo de
    LECTURA nuevo en el repositorio funciona sin tocar nada aca, mientras que
    uno de ESCRITURA nuevo queda sin auditar y el test lo caza. Heredando, los
    dos casos pasarian igual de desapercibidos.

    `registrador` es un callable `(conn, cambio) -> None` (ver el docstring del modulo) y
    **no tiene default**: el formato de la fila no se define aca. El usuario ya no es un
    parametro: lo resuelve el registrador, del `ContextVar` que llena el middleware de
    `libraauth`.
    """

    def __init__(self, repo, conn: sqlite3.Connection, registrador: Registrador):
        if not callable(registrador):
            raise TypeError(
                "RepositorioAuditado necesita un registrador `(conn, cambio) -> None`: "
                "libraauth.auditoria.registrador_dbapi(...). Ver libracommerce/db/auditoria.py."
            )
        self._repo = repo
        self._conn = conn
        self._registrador = registrador

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
            self._registrar(Cambio(
                tabla=meta.tabla,
                accion=CREAR if es_alta else EDITAR,
                id=getattr(resultado, "id", None),
                antes=None if es_alta else _valores(previo),
                despues=_valores(resultado),
            ))
            return resultado

        return auditado

    def _set_setting(self, metodo, meta, key, value):
        previo = self._repo.get_setting(key)
        resultado = metodo(key, value)
        if previo == value:
            # Guardar el mismo valor no es un cambio.
            return resultado
        self._registrar(Cambio(
            tabla=meta.tabla,
            accion=CREAR if previo is None else EDITAR,
            antes=None if previo is None else {key: previo},
            despues={key: value},
        ))
        return resultado

    def _registrar(self, cambio: Cambio) -> None:
        self._registrador(self._conn, cambio)
        # La confirmación la decide el repositorio de adentro (ADR-028): fuera de `transaction()` confirma, adentro no. Con un `self._conn.commit()` a secas, cada fila del log
        # confirmaba TODO lo escrito hasta ahí en medio de la transacción (soltaba el candado, dejaba grabado lo que después se revertía) y la auditoría de una escritura
        # que falla en el segundo paso quedaba registrada. Un repositorio sin `_commit` (no es el de este motor) conserva el commit de siempre.
        getattr(self._repo, "_commit", self._conn.commit)()


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
