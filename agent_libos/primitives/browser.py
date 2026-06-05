from __future__ import annotations


class BrowserAdapter:
    def open(self, *_args, **_kwargs):
        raise NotImplementedError("browser integration is host-runtime specific")

