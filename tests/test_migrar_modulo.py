"""Que las migraciones de LibraCommerce VIAJEN en el wheel y apunten a la base correcta.

Espejo de `tests/db/test_migrar_modulo.py` de LibraCore, que nació de un
defecto medido: una cadena que vive fuera del paquete no le llega al consumidor,
y 7 de 14 bases quedaron sin versión. Acá la cadena nace adentro del paquete
desde el primer día, y estos tests son los que lo sostienen.

🔑 Los dos que importan: **el del wheel construido** (lo único que contesta
«¿el consumidor las recibe?») y **el del destino** (la diferencia entre migrar
la base donde viven las tablas comerciales y migrar otra y devolver éxito).
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from libracommerce import migrar

RAIZ = Path(__file__).resolve().parents[1]


# ── El paquete instalado sabe dónde están ────────────────────────────────


def test_el_directorio_de_migraciones_esta_dentro_del_paquete():
    assert migrar.DIRECTORIO.is_dir(), migrar.DIRECTORIO
    assert migrar.DIRECTORIO.parent.name == "libracommerce"


def test_estan_la_baseline_y_el_env():
    revisiones = sorted(p.name for p in (migrar.DIRECTORIO / "versions").glob("*.py"))
    # Si `versions/` viajara vacío, el `upgrade` no haría nada y NO fallaría.
    assert "0001_baseline_schema_commerce.py" in revisiones
    assert (migrar.DIRECTORIO / "env.py").is_file()


def test_la_configuracion_apunta_al_paquete_y_no_al_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # lejos de la raíz del repo
    cfg = migrar.configuracion("postgresql://u:p@h/db")
    ubicacion = Path(cfg.get_main_option("script_location"))
    assert ubicacion.is_absolute()
    assert (ubicacion / "versions" / "0001_baseline_schema_commerce.py").is_file()
    # El destino explícito viaja en la opción propia, que es la que env.py
    # prefiere sobre DATABASE_URL del entorno.
    assert cfg.get_main_option("libracommerce.url") == "postgresql+psycopg://u:p@h/db"


def test_la_tabla_de_version_es_propia_y_coincide_con_el_env():
    """🔴 En Contalibra y Restolibra `alembic_version` a secas ya es de
    LibraCore, en la misma base. Se lee del env.py para que el nombre tenga un
    solo dueño."""
    assert migrar.TABLA_DE_VERSION == "alembic_version_libracommerce"
    env = (migrar.DIRECTORIO / "env.py").read_text(encoding="utf-8")
    assert f'TABLA_DE_VERSION = "{migrar.TABLA_DE_VERSION}"' in env
    assert "version_table=TABLA_DE_VERSION" in env


def test_las_migraciones_viajan_en_el_wheel(tmp_path):
    """Se construye el wheel y se abre. `build` está en el extra `dev` para que
    el CI no saltee esto: un skip acá se lee igual que un verde."""
    pytest.importorskip("build", reason="falta `build` en el extra dev")
    salida = tmp_path / "dist"
    proceso = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(salida), str(RAIZ)],
        capture_output=True,
        text=True,
    )
    assert proceso.returncode == 0, proceso.stderr[-2000:]
    wheels = list(salida.glob("*.whl"))
    assert len(wheels) == 1, wheels
    with zipfile.ZipFile(wheels[0]) as z:
        nombres = z.namelist()
    revisiones = [n for n in nombres if n.startswith("libracommerce/migrations/versions/")
                  and n.endswith(".py")]
    assert "libracommerce/migrations/env.py" in nombres
    assert revisiones, "el wheel no lleva ninguna revisión: el upgrade saldría en verde sin aplicar nada"
    assert "libracommerce/erp/hooks.py" in nombres
    assert "libracommerce/web/__init__.py" in nombres
    assert not [n for n in nombres if n.endswith(".pyc")], "viajaron `.pyc`"
    # El console script queda declarado en la metadata del wheel.
    entry_points = [n for n in nombres if n.endswith("entry_points.txt")]
    assert entry_points, nombres
    with zipfile.ZipFile(wheels[0]) as z:
        texto = z.read(entry_points[0]).decode()
    assert "libracommerce-migrar = libracommerce.migrar:main" in texto


# ── Contra qué base migra ────────────────────────────────────────────────

#: Las formas reales del entorno de los cuatro consumidores. En todos, las
#: tablas de LibraCommerce viven en la base del DOMINIO; la de LibraCore puede
#: ir aparte (VentaLibra, LibraDesk) o ser la misma (Contalibra, Restolibra).
ENTORNOS = [
    ("contalibra",
     {"CONTALIBRA_DATABASE_URL": "postgresql://u:p@h/contalibra"},
     "postgresql://u:p@h/contalibra",
     "base única, nombre normalizado"),
    ("restolibra",
     {"RESTOLIBRA_DATABASE_URL": "postgresql://u:p@h/restolibra"},
     "postgresql://u:p@h/restolibra",
     "base única, nombre normalizado"),
    ("ventalibra",
     {"VENTALIBRA_DB_PATH": "postgresql://u:p@h/ventalibra",
      "VENTALIBRA_LIBRACORE_DB_PATH": "postgresql://u:p@h/ventalibra_core"},
     "postgresql://u:p@h/ventalibra",
     "el dominio con su nombre histórico, y NO la del core que va aparte"),
    ("libradesk",
     {"DATABASE_URL": "postgresql://u:p@h/libradesk"},
     "postgresql://u:p@h/libradesk",
     "DATABASE_URL a secas es el nombre histórico de LibraDesk"),
]


@pytest.mark.parametrize("prefijo,entorno,esperado,motivo", ENTORNOS)
def test_url_de_commerce_elige_la_base_del_dominio(prefijo, entorno, esperado, motivo):
    assert migrar.url_de_commerce(prefijo, entorno=entorno) == esperado, motivo


def test_url_de_commerce_no_toma_la_base_del_core_aunque_este_declarada():
    """Control: con las dos variables presentes, la elegida es la del dominio.
    Una implementación que resolviera `core=True` pasaría los casos de base
    única de la tabla de arriba y fallaría en producción de VentaLibra."""
    entorno = {"VENTALIBRA_DATABASE_URL": "postgresql://u:p@h/dominio",
               "VENTALIBRA_LIBRACORE_DATABASE_URL": "postgresql://u:p@h/core"}
    assert migrar.url_de_commerce("ventalibra", entorno=entorno).endswith("/dominio")


def test_un_prefijo_que_no_resuelve_falla_en_vez_de_caer_a_database_url():
    """🔴 Caer a `DATABASE_URL` cuando el prefijo no resuelve es el defecto que
    este módulo existe para evitar: migraría otra base y devolvería éxito."""
    entorno = {"DATABASE_URL": "postgresql://u:p@h/otra"}
    with pytest.raises(migrar.SinURL, match="LIBRACARGO_DATABASE_URL"):
        migrar.url_de_commerce("libracargo", entorno=entorno)
    entorno["LIBRACOMMERCE_MIGRAR_URL"] = "postgresql://u:p@h/libracargo"
    assert migrar.url_de_commerce("libracargo", entorno=entorno).endswith("/libracargo")


def test_sin_prefijo_toma_database_url_y_sin_nada_falla():
    assert migrar.url_de_commerce(entorno={"DATABASE_URL": "postgresql://u:p@h/x"}).endswith("/x")
    with pytest.raises(migrar.SinURL, match="LIBRACOMMERCE_MIGRAR_URL"):
        migrar.url_de_commerce(entorno={})


def test_la_salida_de_emergencia_gana_sobre_todo():
    entorno = {"LIBRACOMMERCE_MIGRAR_URL": "postgresql://u:p@h/elegida",
               "CONTALIBRA_DATABASE_URL": "postgresql://u:p@h/contalibra"}
    assert migrar.url_de_commerce("contalibra", entorno=entorno).endswith("/elegida")


# ── El CLI ───────────────────────────────────────────────────────────────


def test_help_devuelve_cero_y_un_comando_inexistente_dos(capsys):
    assert migrar.main(["--help"]) == 0
    assert migrar.TABLA_DE_VERSION in capsys.readouterr().out
    assert migrar.main(["upgrad"]) == 2


def test_sin_destino_devuelve_uno_y_lo_dice(monkeypatch, capsys):
    for variable in ("LIBRACOMMERCE_MIGRAR_URL", "DATABASE_URL"):
        monkeypatch.delenv(variable, raising=False)
    assert migrar.main(["upgrade"]) == 1
    assert "[ERROR]" in capsys.readouterr().err


def test_parsear_acepta_las_dos_formas_del_prefijo():
    assert migrar._parsear(["upgrade", "--prefijo", "contalibra"]) == ("upgrade", "contalibra", None)
    assert migrar._parsear(["upgrade", "--prefijo=restolibra", "0001"]) == ("upgrade", "restolibra", "0001")
    assert migrar._parsear([]) == ("upgrade", None, None)
