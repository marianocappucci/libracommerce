"""La carrera «editar un producto a servicio» contra «marcarlo como perecedero» (ADR-057), contra los dos motores.

Un servicio no tiene inventario, así que no puede quedar con `tracks_expiry = 1`. El router lo rechazaba leyendo la marca ANTES de guardar la edición, y `marcar_vence` leía el
tipo antes de escribir la marca: una edición a servicio concurrente con una marca dejaba un servicio marcado (que `registrar_entrada_con_lote` y `asignar` después rechazan
con 409, pero seguía apareciendo como perecedero). Ahora `update_producto` suelta la marca al pasar a servicio, dentro de su transacción, y `marcar_vence` lleva la condición
«no es un servicio» DENTRO de su `UPDATE`, con la fila ya tomada.
"""

from __future__ import annotations

import threading
import time

import pytest
import test_vencimientos as _vto
from libracore.db import core
from test_vencimientos import _con_conexion, catalogo, migrar, vencimientos

from libracommerce.db.repository import repositorio_de

abrir_vto = _vto.abrir_vto
destino = _vto.destino


def _editar_a_servicio(conn, pid, nombre="Flete"):
    catalogo.update_producto(conn, pid, nombre, "", "", 500.0, 0.0, "u", "", 1, tipo="servicio")


def _fila(abrir, pid):
    with abrir() as conn:
        f = conn.execute("SELECT item_type, tracks_expiry FROM catalog_items WHERE id = ?", (pid,)).fetchone()
        return f[0], f[1]


def _producto_marcado(abrir):
    with abrir() as conn:
        pid = catalogo.create_producto(conn, "Yogur", precio_venta=100.0, precio_costo=60.0)
        vencimientos.marcar_vence(conn, pid, True)
        conn.commit()
    assert _fila(abrir, pid) == ("product", 1)
    return pid


# ── update_producto suelta la marca ──────────────────────────────────────


def test_editar_un_producto_marcado_a_servicio_le_saca_la_marca(abrir_vto):
    pid = _producto_marcado(abrir_vto)
    with abrir_vto() as conn:
        _editar_a_servicio(conn, pid)
        conn.commit()
    assert _fila(abrir_vto, pid) == ("service", 0)


def test_editar_a_servicio_un_producto_sin_marcar_o_seguir_siendo_producto_no_toca_la_marca(abrir_vto):
    marcado = _producto_marcado(abrir_vto)
    with abrir_vto() as conn:
        sin_marcar = catalogo.create_producto(conn, "Leche", precio_venta=80.0, precio_costo=50.0)
        catalogo.update_producto(conn, marcado, "Yogur 2", "", "", 120.0, 60.0, "u", "", 1)         # sigue siendo producto
        _editar_a_servicio(conn, sin_marcar, "Leche servicio")
        conn.commit()
    assert _fila(abrir_vto, marcado) == ("product", 1)                                               # la edición de un producto no desmarca
    assert _fila(abrir_vto, sin_marcar) == ("service", 0)


def test_la_edicion_a_servicio_y_la_marca_van_juntas_o_no_van(abrir_vto):
    """Todo en la transacción de la edición: si el guardado falla (código repetido), el producto sigue siendo producto Y marcado."""
    pid = _producto_marcado(abrir_vto)
    with abrir_vto() as conn:
        otro = catalogo.create_producto(conn, "Otro", precio_venta=1.0, precio_costo=1.0, codigo="REPE")
        assert otro
        conn.commit()
    with abrir_vto() as conn, pytest.raises(ValueError, match="REPE"):
        catalogo.update_producto(conn, pid, "Flete", "REPE", "", 500.0, 0.0, "u", "", 1, tipo="servicio")
    assert _fila(abrir_vto, pid) == ("product", 1)


def test_sin_la_revision_0002_editar_a_servicio_anda_igual(destino):
    """Una base que sólo corrió el arranque no tiene la columna `tracks_expiry`: no hay nada que soltar."""
    _vto._crear_base_al_dia_de_0001(destino)

    def verificar(conn):
        assert not vencimientos.tiene_revision(conn)
        pid = catalogo.create_producto(conn, "Yogur", precio_venta=100.0, precio_costo=60.0)
        _editar_a_servicio(conn, pid)
        conn.commit()
        assert conn.execute("SELECT item_type FROM catalog_items WHERE id = ?", (pid,)).fetchone()[0] == "service"

    _con_conexion(destino, verificar)
    _ = migrar


# ── marcar_vence rechaza lo que dejó de ser producto ─────────────────────


def test_marcar_vence_lee_el_tipo_dentro_de_la_escritura(abrir_vto, monkeypatch):
    """La lectura de arriba puede estar vieja (otra transacción pasó el producto a servicio después): se simula devolviendo `product` de esa lectura. El `UPDATE` igual
    lo rechaza, porque lleva la condición en el `WHERE`."""
    pid = _producto_marcado(abrir_vto)
    with abrir_vto() as conn:
        conn.execute("UPDATE catalog_items SET item_type = 'service', tracks_expiry = 0 WHERE id = ?", (pid,))
        conn.commit()
    original = vencimientos._fila_de_producto

    def lectura_vieja(conn, item_id):
        return {**dict(original(conn, item_id)), "item_type": "product"}

    monkeypatch.setattr(vencimientos, "_fila_de_producto", lectura_vieja)
    with abrir_vto() as conn, pytest.raises(vencimientos.ReglaDeNegocio, match="servicio"):
        vencimientos.marcar_vence(conn, pid, True)
    assert _fila(abrir_vto, pid) == ("service", 0)


def test_marcar_vence_sigue_marcando_y_desmarcando_un_producto(abrir_vto):
    with abrir_vto() as conn:
        pid = catalogo.create_producto(conn, "Yogur", precio_venta=100.0, precio_costo=60.0)
        assert vencimientos.marcar_vence(conn, pid, True) == {"producto_id": pid, "vence": True}
        assert vencimientos.marcar_vence(conn, pid, True) == {"producto_id": pid, "vence": True}      # idempotente
        assert vencimientos.marcar_vence(conn, pid, False) == {"producto_id": pid, "vence": False}
        conn.commit()
    assert _fila(abrir_vto, pid) == ("product", 0)


def test_desmarcar_un_servicio_sigue_pudiendo_hacerse_y_marcarlo_es_409(abrir_vto):
    with abrir_vto() as conn:
        pid = catalogo.create_producto(conn, "Flete", precio_venta=500.0, tipo="servicio")
        conn.execute("UPDATE catalog_items SET tracks_expiry = 1 WHERE id = ?", (pid,))   # una marca vieja, de antes de este arreglo
        conn.commit()
    with abrir_vto() as conn:
        assert vencimientos.marcar_vence(conn, pid, False) == {"producto_id": pid, "vence": False}
        with pytest.raises(vencimientos.ReglaDeNegocio):
            vencimientos.marcar_vence(conn, pid, True)
        conn.commit()
    assert _fila(abrir_vto, pid) == ("service", 0)


def test_marcar_un_producto_que_no_existe_sigue_siendo_producto_no_encontrado(abrir_vto):
    with abrir_vto() as conn, pytest.raises(vencimientos.ProductoNoEncontrado):
        vencimientos.marcar_vence(conn, 999, True)


def test_en_postgres_marcar_mientras_se_edita_a_servicio_no_deja_un_servicio_marcado(abrir_vto):
    """La carrera de verdad, con dos conexiones: A pasa el producto a servicio y todavía no confirma; B lo marca y espera el candado de la fila; A confirma. B tiene que
    rechazar (el producto ya es un servicio) y el servicio queda sin marca."""
    if not core.is_postgres():
        pytest.skip("sólo PostgreSQL espera el candado de la fila")
    with abrir_vto() as conn:
        pid = catalogo.create_producto(conn, "Yogur", precio_venta=100.0, precio_costo=60.0)
        conn.commit()
    a = core.get_connection()
    resultado: dict = {}

    def marcar():
        b = core.get_connection()
        try:
            vencimientos.marcar_vence(b, pid, True)             # B: lee `product` (A no confirmó) y escribe: queda esperando a A
            b.commit()
            resultado["b"] = "marcó"
        except vencimientos.ReglaDeNegocio:
            b.rollback()
            resultado["b"] = "rechazó"
        finally:
            b.close()

    hilo = threading.Thread(target=marcar)
    try:
        with repositorio_de(a).transaction():                        # la transacción exterior de A: confirma al salir del `with`, no antes
            _editar_a_servicio(a, pid)                              # A: editó, sin confirmar (tiene la fila)
            hilo.start()
            time.sleep(0.5)
            assert hilo.is_alive(), "B no esperó el candado de la fila: el UPDATE no bloqueó"
        hilo.join(30)                                               # A confirmó: ahora es un servicio
        assert not hilo.is_alive()
    finally:
        a.close()
    assert resultado == {"b": "rechazó"}
    assert _fila(abrir_vto, pid) == ("service", 0)
