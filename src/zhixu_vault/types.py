"""Secret-bearing types available only inside the vault package."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class SecretValue:
    _buffer: bytearray = field(repr=False)

    @classmethod
    def from_text(cls, value: str) -> SecretValue:
        return cls(bytearray(value.encode("utf-8")))

    def text(self) -> str:
        return bytes(self._buffer).decode("utf-8")

    def bytes(self) -> bytes:
        return bytes(self._buffer)

    def clear(self) -> None:
        for index in range(len(self._buffer)):
            self._buffer[index] = 0

    def __enter__(self) -> SecretValue:
        return self

    def __exit__(self, *_args: object) -> None:
        self.clear()
