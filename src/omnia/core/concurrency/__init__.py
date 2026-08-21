"""How a batch of independent work units is RUN — sequentially, or on a bounded pool.

Two modules, split by what they are allowed to import rather than by what they do:

* :mod:`omnia.core.concurrency.dispatch` — the :class:`~omnia.core.concurrency.dispatch.Dispatch`
  protocol and the sequential implementation. Pure stdlib, no threads, no ``aqt``/``anki``, so a
  plugin's pure-logic layer can depend on the seam without depending on concurrency at all.
* :mod:`omnia.core.concurrency.pool` — the threaded implementation and the
  :func:`~omnia.core.concurrency.pool.pooled_dispatch` context manager that pairs a pool with
  the provider limiter. Imports ``concurrent.futures``.

Nothing is re-exported here on purpose: importing this package must not drag
``concurrent.futures`` into a module that only wanted the protocol.
"""

from __future__ import annotations
