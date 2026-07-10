from __future__ import annotations

from functools import lru_cache

from app.tools.risk import risk_labels_for_contract_run
from .builtins import generated_builtin_contracts
from .loader import load_contracts
from .models import ToolContract


class ToolContractRegistry:
    def __init__(self, contracts: list[ToolContract] | None = None):
        self._contracts: dict[str, ToolContract] = {}
        for contract in contracts or []:
            self.register(contract)

    def register(self, contract: ToolContract) -> None:
        if not contract.risk_labels:
            contract.risk_labels = risk_labels_for_contract_run(contract.name)
        self._contracts[contract.name] = contract

    def list_contracts(self) -> list[ToolContract]:
        return sorted(self._contracts.values(), key=lambda contract: contract.name)

    def get_contract(self, name: str) -> ToolContract | None:
        return self._contracts.get(name)

    def has_contract(self, name: str) -> bool:
        return name in self._contracts


@lru_cache(maxsize=1)
def get_default_contract_registry() -> ToolContractRegistry:
    contracts = load_contracts()
    existing_names = {contract.name for contract in contracts}
    contracts.extend(generated_builtin_contracts(existing_names=existing_names))
    return ToolContractRegistry(contracts)
