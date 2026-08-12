"""Motor comercial reutilizable de la familia Libra."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

# La version sale de la metadata del paquete instalado, que hatch-vcs deriva
# del tag (ver pyproject.toml). Un literal aca vuelve a abrir el agujero que ya
# nos costo una release: `v0.5.0` publico un paquete que decia `0.4.0` porque
# nadie movio la linea al taguear. Identico a libracore y libraauth.
try:
    __version__ = _version("libracommerce")
except PackageNotFoundError:
    # Checkout de desarrollo sin instalar: no hay metadata de donde leer.
    __version__ = "0.0.0.dev0"
