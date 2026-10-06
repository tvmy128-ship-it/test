"""The job engine: steps, scheduler, leases, content store, cache, events, budget, gates (APP_SPEC §8, §9).

Imports here are deliberately empty: ``import duoskin.engine`` must stay cheap. Start with ``duoskin.engine.registry``
(how a pipeline lane registers step handlers) and ``duoskin.engine.context`` (``StepContext`` and ``Runtime``).
"""
