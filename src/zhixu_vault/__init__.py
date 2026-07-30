"""Isolated high-sensitivity vault implementation.

This package intentionally does not export ``SecretValue``. Trusted vault code
imports it from the private ``types`` module; ordinary Zhixu code must use the
capability-limited client boundary.
"""

from .backup import VaultBackupManager
from .crypto import Argon2Parameters, VaultKeyring
from .database import VaultDatabase
from .policy import SecretKind, VaultAction, VaultClassification
from .service import VaultService
from .unix_api import UnixVaultServer, VaultRPCDispatcher
from .webauthn_auth import PasskeyManager, StepUpProof

__all__ = [
    "Argon2Parameters",
    "PasskeyManager",
    "SecretKind",
    "StepUpProof",
    "VaultAction",
    "VaultBackupManager",
    "VaultClassification",
    "VaultDatabase",
    "VaultKeyring",
    "VaultService",
    "VaultRPCDispatcher",
    "UnixVaultServer",
]
