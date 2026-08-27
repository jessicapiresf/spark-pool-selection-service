"""Contratos das duas fontes externas que a agregadora consulta.

As duas sao opcionais por desenho. Sem a Databricks o servico volta a operar so com o
historico, que era o desenho original; sem o placement score ele volta a ser reativo.
Nenhuma das duas e chamada no caminho de request.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from pool_selection.domain.catalog import InstanceSpec
from pool_selection.domain.scoring import Capacity, PlacementForecast


class CapacityProvider(Protocol):
    def fetch(self) -> Mapping[str, Capacity]:
        """Capacidade atual por `pool_id`. Tambem define quem sao os candidatos."""
        ...


class PlacementScoreProvider(Protocol):
    def fetch(
        self, instance_types_by_profile: Mapping[str, Sequence[str]], targets: Mapping[str, int]
    ) -> Mapping[tuple[str, str], PlacementForecast]:
        """Score por (AZ, perfil).

        A consulta e por perfil porque a API devolve score baixo por definicao para menos
        de tres tipos de instancia diferentes, e um pool tem um tipo so.
        """
        ...


class InstanceCatalogProvider(Protocol):
    def describe(self, instance_types: Sequence[str]) -> Sequence[InstanceSpec]:
        """Atributos reais dos tipos, para classificar perfil sem tabela fixa."""
        ...
