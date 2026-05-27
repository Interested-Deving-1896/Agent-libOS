from __future__ import annotations


class DatabaseAdapter:
    def query(self, *_args, **_kwargs):
        raise NotImplementedError("database integration is host-runtime specific")

